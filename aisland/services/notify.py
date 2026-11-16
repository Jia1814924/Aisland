# -*- coding: utf-8 -*-
"""消息提醒：QQ / 微信 / 邮件。

三个来源：
  A. 轮询 Windows 通知数据库 wpndatabase.db（只读），新出现的 toast 按 PrimaryId 前缀映射。
  B. UI 收到 HSHELL_FLASH 时调用 on_flash(hwnd)，按进程名映射（8 秒内同类去重）。
  C. 邮件兜底：每 20 秒检查 Thunderbird 各账户 INBOX mbox 文件是否变大。

微信从 Windows 通知和已显示的托盘预览提取联系人名称与头像，结果保留在内存中。
"""
from __future__ import annotations

import configparser
import ctypes
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

from PySide6 import QtCore, QtGui

from aisland.core.bg import launch, run_bg, ui
from aisland.core.config import cfg, expand

from . import wechat_preview
from .ai_usage import AI_NAMES
from .wechat_tray_hover import mouse_is_idle

WPN_DB = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")
POLL_MS = 1500
MAIL_FILE_MS = 20_000
FLASH_DEDUPE_S = 8.0
WECHAT_TOAST_MERGE_S = 2.5
AI_TOAST_DEDUPE_S = 20.0
WECHAT_PREVIEW_POLL_MS = 400
WECHAT_PREVIEW_TTL_S = 60.0
WECHAT_HOVER_WINDOW_S = 6.0
MAIL_TOAST_GRACE_S = 60.0
# 历史上限与消息页可见条数保持一致：界面只画这么多，多存的永远不可见。
HISTORY_MAX = 30

# 常见办公客户端的开箱即用映射；用户在 notify.toast_apps / flash_processes
# 里写同名键即可覆盖，不必改动这些默认值。
BUILTIN_TOAST_APPS = {
    "DingTalk": {"name": "钉钉", "kind": "dingtalk"},
    "com.alibaba.DingTalk": {"name": "钉钉", "kind": "dingtalk"},
    "Lark": {"name": "飞书", "kind": "feishu"},
    "Feishu": {"name": "飞书", "kind": "feishu"},
    "com.ss.android.lark": {"name": "飞书", "kind": "feishu"},
    "WXWork": {"name": "企业微信", "kind": "wecom"},
    "WeCom": {"name": "企业微信", "kind": "wecom"},
    "Tencent.WXWork": {"name": "企业微信", "kind": "wecom"},
}
BUILTIN_FLASH_PROCESSES = {
    "dingtalk.exe": {"name": "钉钉", "kind": "dingtalk"},
    "lark.exe": {"name": "飞书", "kind": "feishu"},
    "feishu.exe": {"name": "飞书", "kind": "feishu"},
    "wxwork.exe": {"name": "企业微信", "kind": "wecom"},
    "wecom.exe": {"name": "企业微信", "kind": "wecom"},
    "deepseek harness.exe": {"name": "DeepSeek Harness", "kind": "deepseek_harness"},
}


def toast_apps() -> dict:
    return {**BUILTIN_TOAST_APPS, **(cfg.get("notify.toast_apps") or {})}


def flash_processes() -> dict:
    return {**BUILTIN_FLASH_PROCESSES, **(cfg.get("notify.flash_processes") or {})}


_FILETIME_EPOCH_DIFF = 116444736000000000  # 1601 -> 1970，单位 100ns


def filetime_to_epoch(ft) -> float:
    try:
        ft = int(ft)
    except (TypeError, ValueError):
        return time.time()
    if ft <= 0:
        return time.time()
    return (ft - _FILETIME_EPOCH_DIFF) / 1e7


_TEXT_RE = re.compile(r"<text\b[^>]*>(.*?)</text>", re.S | re.I)


def toast_first_text(payload) -> str:
    """toast XML 的第一个 <text>（通常是发送者 / 群名）。解析失败返回空串。"""
    try:
        s = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload or "")
        m = _TEXT_RE.search(s)
        if not m:
            return ""
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = (t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&")).strip()
        return t[:40]
    except Exception:  # noqa: BLE001
        return ""


def toast_texts(payload, limit: int = 3) -> list[str]:
    """toast XML 里按顺序取文本节点（第 1 条通常是标题，第 2 条是正文）。

    与 wechat_toast_details 同样的安全前提：限制大小、拒绝 DTD、跳过 attribution 署名行。
    解析失败返回空列表。
    """
    if not isinstance(payload, (str, bytes, bytearray)) or len(payload) > 32_768:
        return []
    raw = payload.encode("utf-8", "replace") if isinstance(payload, str) else bytes(payload)
    if b"<!DOCTYPE" in raw.replace(b"\0", b"").upper():
        return []
    try:
        root = ET.fromstring(payload if isinstance(payload, str) else raw)
    except (ET.ParseError, ValueError, LookupError):
        return []
    out: list[str] = []
    for node in root.iter():
        if not str(node.tag).endswith("text"):
            continue
        if (node.get("placement") or "").casefold() == "attribution":
            continue
        text = " ".join("".join(node.itertext()).split())
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def ai_subtitle(title, body, limit: int = 80) -> str:
    """Codex / WorkBuddy 的气泡副标题：状态和会话名都要。

    两家的文本顺序不一样：WorkBuddy 是「状态｜会话名」，Codex 是「会话名｜事件」，
    拼在一起两边都读得通。任一侧缺失或重复时退化成单段，不出现「A · A」。
    """
    head = " ".join(str(title or "").split())
    tail = " ".join(str(body or "").split())
    if not head:
        return tail[:limit]
    if not tail or tail == head or tail.startswith(head):
        return (tail or head)[:limit]
    return f"{head} · {tail}"[:limit]


_WECHAT_GENERIC = re.compile(r"(?:微信\s*)?(?:[你您]?(?:收到|有)了?)?\s*"
                             r"(?:\d+|一)?\s*条?\s*(?:微信)?(?:未读|新)?消息[。！!.]?")

def _wechat_generic(text: str) -> bool:
    return (text.casefold() in {"微信", "wechat", "weixin", "new message", "new messages"}
            or bool(_WECHAT_GENERIC.fullmatch(text)))


def _local_avatar(src: str) -> QtGui.QImage | None:
    # 只处理通知附带的本地图片。远程 URL 和网络共享会保留默认消息图标。
    url = QtCore.QUrl(src)
    if url.isLocalFile() and not url.host():
        src = url.toLocalFile()
    if not re.match(r"^[A-Za-z]:[\\/]", src) or _kernel32.GetDriveTypeW(Path(src).anchor) != 3:
        return None
    try:
        path = Path(src).resolve(strict=True)
        if not re.fullmatch(r"[A-Za-z]:", path.drive) or not path.is_file() or path.stat().st_size > 512 * 1024:
            return None
    except (OSError, ValueError):
        return None
    reader = QtGui.QImageReader(str(path))
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > 2048:
        return None
    size.scale(64, 64, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    reader.setScaledSize(size)
    image = reader.read()
    return None if image.isNull() else image


def wechat_toast_details(payload) -> dict:
    """提取 Windows 微信通知的联系人标题和本地头像。"""
    if not isinstance(payload, (str, bytes, bytearray)) or len(payload) > 32_768:
        return {}
    # 通知 XML 不需要 DTD，禁止自定义实体，也兼容 UTF-16 的标记。
    raw = payload.encode("utf-8", "replace") if isinstance(payload, str) else bytes(payload)
    if b"<!DOCTYPE" in raw.replace(b"\0", b"").upper():
        return {}
    try:
        root = ET.fromstring(payload if isinstance(payload, str) else raw)
    except (ET.ParseError, ValueError, LookupError):
        return {}

    binding = root.find(".//{*}binding")
    if binding is None:
        return {}
    heading = next((node for node in binding.iterfind("{*}text")
                    if node.get("placement") != "attribution"), None)
    title = " ".join("".join(heading.itertext()).split()) if heading is not None else ""
    details = {}
    # 合并成「名称：正文」的标题不能作为联系人名称。
    if title and not _wechat_generic(title) and not re.search(r"[:：]", title):
        details["sender"] = title[:40]
    for node in binding.findall(".//{*}image"):
        if node.get("placement", "").casefold() == "applogooverride":
            avatar = _local_avatar(node.get("src", ""))
            if avatar is not None:
                details["avatar"] = avatar
            break
    return details


# ---------------- hwnd -> exe ----------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def exe_of_hwnd(hwnd: int) -> str:
    """窗口句柄 -> 小写 exe 文件名（失败返回空串）。"""
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        if not pid.value:
            return ""
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
            finally:
                _kernel32.CloseHandle(h)
        try:
            import psutil
            return psutil.Process(pid.value).name().lower()
        except Exception:  # noqa: BLE001
            return ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------- Thunderbird ----------------

def thunderbird_profile() -> Path | None:
    root = Path(os.path.expandvars(r"%APPDATA%\Thunderbird"))
    ini = root / "profiles.ini"
    if not ini.exists():
        return None
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(ini, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    cand: list[tuple[str, bool]] = []
    for sec in cp.sections():
        if sec.lower().startswith("install") and cp[sec].get("default"):
            cand.append((cp[sec]["default"], True))
    for sec in cp.sections():
        s = cp[sec]
        if sec.lower().startswith("profile") and s.get("path") and s.get("default") == "1":
            cand.append((s["path"], s.get("isrelative", "1") == "1"))
    for sec in cp.sections():
        s = cp[sec]
        if sec.lower().startswith("profile") and s.get("path"):
            cand.append((s["path"], s.get("isrelative", "1") == "1"))
    for path, rel in cand:
        p = (root / path) if rel else Path(path)
        if (p / "ImapMail").is_dir() or (p / "Mail").is_dir():
            return p
    return None


def inbox_files(profile: Path) -> list[Path]:
    out = []
    for sub in ("ImapMail", "Mail"):
        base = profile / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if f in ("INBOX", "Inbox"):
                    out.append(Path(dirpath) / f)
    return out


# ---------------- service ----------------

class NotifyService(QtCore.QObject):
    notified = QtCore.Signal(dict)        # {kind, name, source, time, [sender, avatar]}
    counts_changed = QtCore.Signal(dict)  # {kind: n}
    wechat_preview_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 让 bg 的 invoker 在主线程创建（否则 on_done 回调发不回来）
        self.counts: dict[str, int] = {}
        self.history: list[dict] = []
        self.wechat_preview: list[dict] = []
        self._wechat_popup = 0
        self._wechat_attempts = 0
        self._wechat_busy = False
        self._wechat_revision = 0
        self._wechat_expires = 0.0
        self._wechat_hover_pending: tuple[int, float, float] | None = None
        self._last_by_kind: dict[str, float] = {}
        self._last_mail_toast = 0.0
        self._max_id: int | None = None
        self._busy = False
        self._mail_busy = False
        self._lock = threading.Lock()
        self._inbox_state: dict[str, tuple[int, float]] | None = None
        self._profile: Path | None = None
        self._running = False

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._mail_timer = QtCore.QTimer(self)
        self._mail_timer.setInterval(MAIL_FILE_MS)
        self._mail_timer.timeout.connect(self._mail_tick)
        self._wechat_timer = QtCore.QTimer(self, interval=WECHAT_PREVIEW_POLL_MS)
        self._wechat_timer.timeout.connect(self._watch_wechat_preview)

    # ---------- lifecycle ----------
    def start(self):
        self._running = True
        self._timer.start()
        self._mail_timer.start()
        self._wechat_timer.start()
        self._tick()
        self._mail_tick()

    def stop(self):
        self._running = False
        self._timer.stop()
        self._mail_timer.stop()
        self._wechat_timer.stop()
        self._wechat_popup = 0
        self._wechat_attempts = 0
        self._clear_wechat_preview()

    # ---------- public ----------
    def clear(self, kind: str):
        if kind == "wechat":
            self._clear_wechat_preview()
        if self.counts.get(kind):
            self.counts[kind] = 0
            self.counts_changed.emit(dict(self.counts))

    def open(self, kind: str) -> bool:
        """跳到通知对应的应用：已运行则聚焦窗口，否则启动配置的完整路径。

        内置的 exe 名只用于给 open_or_focus 找窗口；要支持「没打开时自动启动」，
        在 notify.open.<kind> 里写完整路径即可，裸 exe 名不会被当作启动路径。
        依次尝试同类进程名；仅在成功聚焦或启动后标记已读。
        """
        configured = str(cfg.get(f"notify.open.{kind}") or "").strip()
        candidates = ([configured] if configured else []) + [
            exe for exe, mapping in flash_processes().items() if mapping.get("kind") == kind]
        opener = getattr(self, "opener", None)     # app.py 注入：已打开则切换窗口
        seen = set()
        for candidate in candidates:
            path = expand(candidate).strip()
            key = os.path.normcase(path)
            if not path or key in seen:
                continue
            seen.add(key)
            if opener is not None:
                opened = opener(path) in ("focused", "launched")
            else:
                opened = os.path.isabs(path) and launch(path)
            if opened:
                self.clear(kind)
                return True
        return False

    def on_flash(self, hwnd: int):
        try:
            exe = exe_of_hwnd(int(hwnd))
            if not exe:
                return
            m = flash_processes().get(exe)
            if not m:
                return
            kind = m.get("kind", exe)
            now = time.time()
            if now - self._last_by_kind.get(kind, 0) < FLASH_DEDUPE_S:
                return
            self._emit({"kind": kind, "name": m.get("name", kind), "source": "flash", "time": now})
            if kind == "wechat" and exe == "weixin.exe" and cfg.get("notify.wechat_auto_preview"):
                self._wechat_hover_pending = (hwnd, now, time.monotonic() + WECHAT_HOVER_WINDOW_S)
        except Exception:  # noqa: BLE001
            pass

    # ---------- internal ----------
    def _emit(self, d: dict):
        kind = d["kind"]
        if kind == "wechat":
            self._clear_wechat_preview()
            self._wechat_attempts = 0
        self._last_by_kind[kind] = time.time()
        if kind in AI_NAMES and d.get("source") == "toast":
            # Codex / WorkBuddy 同一个事件会连发两条一模一样的 toast（相隔几秒），
            # 只刷新时间，不重复计数、不再弹一次气泡。
            last = next((row for row in self.history if row.get("kind") == kind), None)
            if (last and last.get("title") == d.get("title") and last.get("body") == d.get("body")
                    and abs(d["time"] - last["time"]) <= AI_TOAST_DEDUPE_S):
                last["time"] = d["time"]
                return
        if (kind == "wechat" or kind in AI_NAMES) and d.get("source") == "toast":
            last = next((row for row in self.history if row["kind"] == kind), None)
            if (last and last.get("source") == "flash"
                    and abs(d["time"] - last["time"]) <= WECHAT_TOAST_MERGE_S):
                last.update(d)
                if any(field in d for field in ("sender", "avatar", "title", "body")):
                    self.notified.emit(last)
                return
        if kind == "mail":
            d.pop("sender", None)
            if d.get("source") == "toast":
                self._last_mail_toast = time.time()
        self.history.insert(0, d)
        del self.history[HISTORY_MAX:]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.notified.emit(d)
        self.counts_changed.emit(dict(self.counts))

    def _clear_wechat_preview(self):
        self._wechat_revision += 1
        self._wechat_hover_pending = None
        if self.wechat_preview:
            self.wechat_preview = []
            self.wechat_preview_changed.emit()

    def _publish_wechat_preview(self, contacts, flash_time: float | None = None):
        observed = time.time()
        self.wechat_preview = [
            dict(contact, kind="wechat", name="微信未读", source="tray", time=observed, flash_time=flash_time)
            for contact in contacts
        ]
        self._wechat_expires = time.monotonic() + WECHAT_PREVIEW_TTL_S
        self.wechat_preview_changed.emit()

    def _try_automatic_preview(self):
        if not self._wechat_hover_pending or self._wechat_busy:
            return
        flash_hwnd, flash_time, deadline = self._wechat_hover_pending
        if time.monotonic() >= deadline or not cfg.get("notify.wechat_auto_preview"):
            self._wechat_hover_pending = None
            return
        if not mouse_is_idle():
            return

        self._wechat_hover_pending = None
        self._wechat_busy = True
        revision = self._wechat_revision

        def cancelled():
            return not self._running or revision != self._wechat_revision

        def finished(contacts):
            self._wechat_busy = False
            if contacts and not cancelled() and cfg.get("notify.wechat_auto_preview"):
                self._wechat_attempts = 2
                self._publish_wechat_preview(contacts, flash_time)

        run_bg(wechat_preview.read_automatic_preview, flash_hwnd, exe_of_hwnd, cancelled,
               on_done=finished, on_error=lambda _exc: finished([]))

    def _watch_wechat_preview(self):
        if self.wechat_preview and time.monotonic() >= self._wechat_expires:
            self._clear_wechat_preview()
        hwnd = wechat_preview.visible_popup(exe_of_hwnd)
        if hwnd != self._wechat_popup:
            self._wechat_popup = hwnd
            self._wechat_attempts = 0
            return  # 等待下一次轮询，让刚出现的小窗完成绘制
        if not hwnd:
            self._try_automatic_preview()
            return
        if self._wechat_busy or self._wechat_attempts >= 2:
            return
        self._wechat_attempts += 1
        self._wechat_busy = True
        revision = self._wechat_revision

        def finished(contacts):
            self._wechat_busy = False
            if (not contacts or not self._running or revision != self._wechat_revision
                    or self._wechat_popup not in (0, hwnd)):
                return
            self._wechat_attempts = 2
            pending = self._wechat_hover_pending
            self._wechat_hover_pending = None
            flash_time = pending[1] if pending and time.monotonic() < pending[2] else None
            self._publish_wechat_preview(contacts, flash_time)

        run_bg(wechat_preview.read_preview, hwnd, on_done=finished, on_error=lambda _exc: finished([]))

    @staticmethod
    def _map_handler(primary: str, apps: dict):
        # 前缀后必须是结尾或非字母数字（'ThunderbirdToast-990…' 命中，'QQExtension_…' 不命中）
        primary = primary.casefold()
        # 具体规则优先，避免内置的短前缀抢先截获用户的应用子类型。
        for prefix, m in sorted(apps.items(), key=lambda item: len(item[0]), reverse=True):
            prefix = prefix.casefold()
            if primary and primary.startswith(prefix):
                rest = primary[len(prefix):]
                if not rest or not rest[0].isalnum():
                    return m
        return None

    def _tick(self):
        if self._busy or not self._running:
            return
        self._busy = True
        run_bg(self._poll_db, on_done=self._on_rows, on_error=lambda e: setattr(self, "_busy", False))

    def _query(self, sql: str, args=()):
        uri = "file:" + WPN_DB.replace("\\", "/") + "?mode=ro"
        last = None
        for _ in range(4):
            try:
                con = sqlite3.connect(uri, uri=True, timeout=1.0)
                try:
                    return con.execute(sql, args).fetchall()
                finally:
                    con.close()
            except sqlite3.OperationalError as e:
                last = e
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(0.15)
                    continue
                raise
        raise last  # type: ignore[misc]

    def _poll_db(self):
        if not os.path.exists(WPN_DB):
            return []
        if self._max_id is None:
            r = self._query("SELECT MAX(Id) FROM Notification")
            self._max_id = int(r[0][0] or 0) if r else 0
            return []
        rows = self._query(
            "SELECT n.Id, h.PrimaryId, n.ArrivalTime, n.Type, n.Payload FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId WHERE n.Id > ? ORDER BY n.Id",
            (self._max_id,))
        if rows:
            self._max_id = max(int(r[0]) for r in rows)
        return self._decode_rows(rows)

    def _decode_rows(self, rows):
        """在后台解析通知，避免头像读取阻塞界面。"""
        apps = toast_apps()
        notices = []
        for _id, primary, arrival, ntype, payload in rows:
            try:
                if str(ntype or "").lower() not in ("toast", ""):
                    continue
                m = self._map_handler(str(primary or ""), apps)
                if not m:
                    continue
                kind = m.get("kind", "other")
                d = {"kind": kind, "name": m.get("name", kind), "source": "toast",
                     "time": filetime_to_epoch(arrival)}
                if kind == "wechat":
                    d.update(wechat_toast_details(payload))
                elif kind == "qq":
                    s = toast_first_text(payload)
                    if s:
                        d["sender"] = s
                elif kind in AI_NAMES:
                    texts = toast_texts(payload)
                    if texts and texts[0].casefold() == AI_NAMES[kind].casefold():
                        texts = texts[1:]          # 首行就是应用名时跳过，下一行才是内容
                    if texts:
                        d["title"] = texts[0][:80]
                    if len(texts) > 1:
                        d["body"] = " ".join(texts[1:])[:200]
                elif kind != "wechat":
                    # 钉钉 / 飞书 / 企业微信等：没有专属解析时保留标题与正文。
                    texts = toast_texts(payload)
                    if texts:
                        d["title"] = texts[0][:80]
                    if len(texts) > 1:
                        d["body"] = " ".join(texts[1:])[:200]
                notices.append(d)
            except Exception:  # noqa: BLE001
                continue
        return notices

    def _on_rows(self, notices):
        self._busy = False
        for notice in notices:
            self._emit(notice)

    # ---------- recent (debug / test) ----------
    def recent_mapped(self, n: int = 3) -> list[dict]:
        """数据库里最近 n 条已映射通知（阻塞，供测试；不含正文）。"""
        apps = toast_apps()
        rows = self._query(
            "SELECT n.Id, h.PrimaryId, n.ArrivalTime, n.Type, n.Payload FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId ORDER BY n.Id DESC LIMIT 500")
        out = []
        for _id, primary, arrival, ntype, payload in rows:
            m = self._map_handler(str(primary or ""), apps)
            if m and str(ntype or "").lower() == "toast":
                d = {"id": _id, "kind": m.get("kind"), "time": filetime_to_epoch(arrival),
                     "sender_len": len(toast_first_text(payload))}
                out.append(d)
                if len(out) >= n:
                    break
        return out

    # ---------- mail file fallback ----------
    def _mail_tick(self):
        if self._mail_busy or not self._running:
            return
        self._mail_busy = True
        run_bg(self._scan_inbox, on_done=self._on_inbox, on_error=lambda e: setattr(self, "_mail_busy", False))

    def _scan_inbox(self):
        if self._profile is None:
            self._profile = thunderbird_profile()
        if self._profile is None:
            return None
        st = {}
        for p in inbox_files(self._profile):
            try:
                s = p.stat()
                st[str(p)] = (s.st_size, s.st_mtime)
            except OSError:
                continue
        return st

    def _on_inbox(self, st):
        self._mail_busy = False
        if st is None:
            return
        prev = self._inbox_state
        self._inbox_state = st
        if prev is None:
            return
        grew = any(path in prev and size > prev[path][0] for path, (size, _m) in st.items())
        if grew and time.time() - self._last_mail_toast > MAIL_TOAST_GRACE_S:
            m = toast_apps().get("ThunderbirdToast") or {}
            self._emit({"kind": "mail", "name": m.get("name", "邮件"), "source": "file", "time": time.time()})
