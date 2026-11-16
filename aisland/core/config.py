# -*- coding: utf-8 -*-
"""用户配置；安装版保存在 LocalAppData/Aisland，源码版保持项目目录行为。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time

from PySide6 import QtCore

from .runtime import CONFIG_PATH, DATA_DIR, IS_FROZEN, RESOURCE_ROOT
from .personal_defaults import apply_personal_defaults, load_personal_defaults
from .translation_settings import (DEFAULT_PROVIDER, PROTECTED_KEY, Connection, bind_installation,
                                   protect_key, read_installation, unprotect_key)

ROOT = RESOURCE_ROOT
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULTS: dict = {
    "island": {
        "edge": "top",              # top / bottom / left / right
        "pos": 0.5,                 # 沿边位置 0..1
        "screen": 0,
        "screen_positions": {},
        "theme": "auto",            # auto / morandi / light / dark / rouge / sky
        "hover_expand_ms": 120,
        "collapse_delay_ms": 320,
        "exclude_from_capture": False,  # False = 灵动岛出现在截图 / 录屏里（取背景时只临时排除自己）
        "blur_fps": 8,
        "scale": 1.0,
        "default_tab": "home",
        "expand_mode": "hover",     # hover = 悬停展开 / always = 常驻展开 / never = 保持收起
        "pinned": False,            # 旧版"保持展开"（兼容）
        "compact_view": "clock",    # 收起态视图：clock / pet / music（滚轮切换）
        "auto_cycle_s": 30,         # 时间天气与小形象自动轮换的间隔（秒），0 = 不轮换
        "passthrough": False,       # 可穿透：鼠标穿过灵动岛，只响应右键（不影响展开 / 收起）
    },
    "hotkeys": {
        "toggle": "Ctrl+Alt+I",
        "translate": "Ctrl+Alt+J",
        "clipboard": "Ctrl+Alt+V",
    },
    "media": {
        "auto_show": True,           # 开始播放时收起态自动切到音乐（暂停 8 秒后切回）
        "lyrics": True,              # 优先读歌曲内嵌歌词，缺失时可联网补充
        "music_dirs": [],            # 本机音乐目录；右键菜单可添加，不随安装包预置个人路径
        "lyrics_online": True,       # 本地没有歌词时允许按音乐元数据联网补充
        "netease_log": True,         # 读取网易云本地日志获得播放状态与进度（网易云默认不接入系统媒体控件）
        "priority": ["cloudmusic", "bilibili", "chrome", "zen", "msedge", "firefox"],
    },
    "notify": {
        "ai_provider": "auto",  # auto / codex / workbuddy / deepseek_harness
        # WorkBuddy 积分来源：本地 JSON 文件或回环 HTTP 接口；留空时只显示上次缓存
        "workbuddy_quota_source": "",
        "wechat_auto_preview": True,  # 鼠标空闲时短暂悬停可见的微信托盘图标
        # wpndatabase 中的 NotificationHandler.PrimaryId 前缀 -> 显示名 / 类型
        "toast_apps": {
            "QQ": {"name": "QQ", "kind": "qq"},
            "ThunderbirdToast": {"name": "邮件", "kind": "mail"},
            "Weixin": {"name": "微信", "kind": "wechat"},
            "OpenAI.Codex_2p2nqsd0c76g0!App": {"name": "Codex", "kind": "codex"},
            "WorkBuddy": {"name": "WorkBuddy", "kind": "workbuddy"},
            "com.tencent.WorkBuddy": {"name": "WorkBuddy", "kind": "workbuddy"},
            "com.deepseek.dsh": {"name": "DeepSeek Harness", "kind": "deepseek_harness"},
        },
        # 任务栏闪烁（FlashWindow）的进程 -> 显示名 / 类型
        "flash_processes": {
            "weixin.exe": {"name": "微信", "kind": "wechat"},
            "wechat.exe": {"name": "微信", "kind": "wechat"},
            "qq.exe": {"name": "QQ", "kind": "qq"},
            "thunderbird.exe": {"name": "邮件", "kind": "mail"},
            "codex.exe": {"name": "Codex", "kind": "codex"},
            "workbuddy.exe": {"name": "WorkBuddy", "kind": "workbuddy"},
            "deepseek harness.exe": {"name": "DeepSeek Harness", "kind": "deepseek_harness"},
        },
        "open": {
            "qq": "",          # 例：C:/Program Files/Tencent/QQNT/QQ.exe
            "wechat": "",      # 例：C:/Program Files/Tencent/Weixin/Weixin.exe
            "mail": "",        # 例：C:/Program Files/Mozilla Thunderbird/thunderbird.exe
        },
    },
    "weather": {
        "provider": "open-meteo",  # 免费、无需 Key；可选 openweathermap
        "api_key": "",         # 仅选择 openweathermap 时使用；由用户自行填写
        "city": "上海",
        "lat": None,
        "lon": None,
        "units": "metric",
        "lang": "zh_cn",
        "interval_min": 15,
        "cities": [],          # [{"name": "上海", "lat": 31.23, "lon": 121.47}]，第一个显示在收起态
    },
    "brightness": {"twinkle": ""},   # 留空 = 直接用 DDC/CI / WMI（便携版 Twinkle 每次调用都会弹"安装中"）
    "clash": {
        "config_dir": "%APPDATA%/io.github.clash-verge-rev.clash-verge-rev",
        "proxy_port": 7897,
        "app": "",                   # clash-verge.exe 路径（「打开 Clash Verge」按钮用）
    },
    "translate": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "import_from": "",           # 可选：从另一个 JSON 配置的 translate.api_key 读取 Key
        "use_bundled_defaults": False, # 公开版本不提供任何翻译凭据
    },
    "apps": {
        # 托盘里的应用（窗口被隐藏）：发送应用自己的"显示窗口"热键让它恢复（微信默认 Ctrl+Alt+W）
        "restore_hotkey": {
            "weixin.exe": "Ctrl+Alt+W",
            "wechat.exe": "Ctrl+Alt+W",
        },
    },
    "mail": {
        "interval_s": 120,
        # IMAP 账户（首次运行自动从 Thunderbird 读取服务器和用户名）；填入授权码 / 应用专用密码后才会检查
        "imap": [],
    },
    "live": {
        "models_dir": "models",     # 识别模型（parakeet-ja-0.6b-int8/、sense-voice-int8/）与 silero_vad.onnx 所在目录
        # block_ms 直接加在「声音出来 → 进流水线」这一段：40ms 一块，平均只拖 20ms
        "audio": {"source": "loopback", "device": "", "block_ms": 40},
        # min_silence 决定「说完多久才收句」：260ms 仍能跨过句中的自然停顿，但比 350ms 早 90ms 出整句
        "vad": {"threshold": 0.4, "min_silence_ms": 260, "min_speech_ms": 200, "max_segment_s": 10},
        "asr": {"language": "ja",   # 识别语种：auto（自动判断）/ ja（Parakeet）/ en、ko（SenseVoice）
                "partial": True, "draft_mode": "rolling", "draft_interval_ms": 250, "draft_threads": 4,
                # draft_min_s：攒够这么多语音就开始第一次草稿识别（0.3s 足够出几个字，再多等就只是延迟）
                "draft_min_s": 0.3,
                "final_engine": "parakeet", "final_threads": 6, "max_segment_s": 10, "context_prompt": False},
        "translate": {"enabled": True, "live": True, "live_min_interval_ms": 500, "live_min_new_chars": 2,
                      "context_lines": 3},
    },
    "learn": {
        "deck": "日语::生词",          # Anki 牌组
        "ankiconnect_url": "",        # 留空 = 本机 127.0.0.1:8765；AnkiConnect Android 填 手机IP:8765
        "auto_sync": False,           # 加词后 / 每 20 秒自动推送到 Anki（Anki 开着时）
        "screenshot": False,          # 加词时截取播放器画面作为卡片配图
        "player_title": "",           # 播放器窗口标题关键字（截图用），如 "Kazumi"
        "source": "",
    },
    "clipboard": {"max": 10},      # 只保留最近 10 条（置顶的不计）
    "launcher": [
        {"name": "文件资源管理器", "path": "C:/Windows/explorer.exe"},
    ],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("alerts",):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(path or "")))


class Config(QtCore.QObject):
    changed = QtCore.Signal()
    save_failed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._load_failed = False
        self._save_failed_notified = False
        self._translation_installation = None
        self._personal_defaults = load_personal_defaults(RESOURCE_ROOT, frozen=IS_FROZEN)
        self._stored_data: dict = copy.deepcopy(DEFAULTS)
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        user = {}
        failed = False
        if CONFIG_PATH.exists():
            try:
                user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if not isinstance(user, dict):
                    raise ValueError("配置必须是 JSON 对象")
            except Exception as e:  # noqa: BLE001
                print("config.json 解析失败，使用默认值：", e)
                failed = True
                self._backup_corrupt_config()
                user = {}
        with self._lock:
            self._load_failed = failed
            if not failed:
                # 修复后的重新加载恢复提示资格：之后再损坏仍要让用户看见。
                self._save_failed_notified = False
            self._stored_data = _merge(DEFAULTS, user)
            changed = False
            if self._translation_installation is not None:
                settings = self._stored_data.get("translate")
                if not isinstance(settings, dict):
                    settings = self._stored_data["translate"] = copy.deepcopy(DEFAULTS["translate"])
                changed = bind_installation(settings, self._translation_installation)
                key = str(settings.get("api_key") or "").strip()
                if key:
                    try:
                        base = str(settings.get("base_url") or DEFAULT_PROVIDER.base_url)
                        settings[PROTECTED_KEY] = protect_key(key, base)
                    except OSError:
                        print("暂时无法保护现有翻译 Key")
                    else:
                        settings.update(api_key="", use_bundled_defaults=False)
                        changed = True
            self._refresh_effective(None if changed else user)
            if not failed and (changed or not CONFIG_PATH.exists()):
                try:
                    self.save()
                except OSError:
                    if not changed:
                        raise
                    # Apply the reset in memory and retry the same binding next startup.
                    print("翻译连接的安装记录暂未保存")
        self.changed.emit()
        if failed:
            # 读取失败本身也要让用户知道：不能只在写失败时才提示，否则用户
            # 不改任何设置就永远看不到配置已经损坏。启动时的这一次没有听
            # 众，由 app.py 按 load_failed 补一条提示。
            self.save_failed.emit()

    @property
    def load_failed(self) -> bool:
        with self._lock:
            return self._load_failed

    def bind_translation_installation(self, install_root):
        self._translation_installation = read_installation(install_root)
        if self._translation_installation is not None:
            self.load()

    def save_translation_connection(self, connection: Connection):
        key = connection.api_key.strip()
        if not key or any(not ("!" <= char <= "~") for char in key):
            raise ValueError("请填写有效的 API Key")
        with self._lock:
            if self._load_failed:
                raise OSError("请先修复配置文件并重新加载")
            previous = self._stored_data.get("translate")
            settings = dict(previous) if isinstance(previous, dict) else {}
            protected = protect_key(key, connection.base_url) if self._translation_installation is not None else ""
            settings.update(api_key="" if protected else key, base_url=connection.base_url, model=connection.model,
                            import_from="", use_bundled_defaults=False)
            if protected:
                settings[PROTECTED_KEY] = protected
            else:
                settings.pop(PROTECTED_KEY, None)
            self._stored_data["translate"] = settings
            try:
                self.save()
            except OSError:
                self._stored_data["translate"] = previous
                raise
            self._refresh_effective()
        self.changed.emit()

    def adopt_translation_defaults(self, resource_root):
        """Compatibility no-op: public builds never import private package keys."""
        return

    def _backup_corrupt_config(self):
        """Keep the unreadable file before falling back to in-memory defaults."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            backup = DATA_DIR / f"config.corrupt-{time.time_ns()}.json"
            shutil.copy2(CONFIG_PATH, backup)
            print(f"损坏的配置已备份到 {backup}，修复后选择「重新加载配置」即可恢复保存")
        except OSError as e:
            print("损坏的配置备份失败（不影响启动）：", e)

    def _refresh_effective(self, user: dict | None = None):
        """Keep bundled credentials in memory; persist only their API/model defaults.

        Recompute after every set so changing the endpoint cannot retain a
        credential that was borrowed from the private release.
        """
        self.data = apply_personal_defaults(
            self._stored_data, self._stored_data if user is None else user,
            self._personal_defaults)
        stored = self._stored_data.get("translate")
        effective = self.data.get("translate")
        if isinstance(stored, dict) and isinstance(effective, dict):
            if stored.get(PROTECTED_KEY) and not stored.get("api_key"):
                try:
                    base = str(effective.get("base_url") or DEFAULT_PROVIDER.base_url)
                    effective["api_key"] = unprotect_key(stored[PROTECTED_KEY], base)
                except (OSError, ValueError, TypeError):
                    effective["api_key"] = ""
            for field in ("base_url", "model"):
                if field in effective:
                    stored[field] = effective[field]

    def save(self):
        notify = False
        with self._lock:
            if self._load_failed:
                # An unreadable user config must never be replaced by defaults,
                # not even by the unconditional save during shutdown.
                # Tell the user once per session instead of failing silently:
                # the on-screen changes really are not being persisted.
                notify = not self._save_failed_notified
                self._save_failed_notified = True
                print("配置读取失败，本次修改未写入 config.json；修复后选择「重新加载配置」")
            else:
                text = json.dumps(self._stored_data, ensure_ascii=False, indent=2)
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = CONFIG_PATH.with_name(
                    f"{CONFIG_PATH.name}.{time.time_ns()}.{threading.get_ident()}.tmp")
                try:
                    tmp.write_text(text, encoding="utf-8")
                    os.replace(tmp, CONFIG_PATH)
                finally:
                    tmp.unlink(missing_ok=True)
        # Emitted outside the lock: a direct slot may read config right back.
        if notify:
            self.save_failed.emit()

    def get(self, key: str, default=None):
        with self._lock:
            cur = self.data
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur

    def set(self, key: str, value, save: bool = True):
        with self._lock:
            parts = key.split(".")
            cur = self._stored_data
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
            self._refresh_effective()
        if save:
            self.save()
        self.changed.emit()


cfg = Config()
