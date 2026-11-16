# -*- coding: utf-8 -*-
"""无线电控制、窗口唤回、置顶维护和全屏视频识别。"""
from __future__ import annotations

import asyncio
import ctypes
import os
import re
import threading
import time
import traceback
from ctypes import wintypes as W
from dataclasses import dataclass

from PySide6 import QtCore

from aisland.core.bg import launch, run_hidden, ui
from aisland.services.vpn import connections as vpn_connections


def _decode(data: bytes) -> str:
    """netsh 等命令输出：先试 UTF-8，再按 OEM 代码页（中文系统为 cp936）。"""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in (f"cp{ctypes.windll.kernel32.GetOEMCP()}", "mbcs", "gbk"):
        try:
            return data.decode(enc)
        except Exception:  # noqa: BLE001
            continue
    return data.decode("utf-8", "replace")


# ======================================================================
# 无线电
# ======================================================================
try:
    from winrt.windows.devices.radios import Radio as _Radio, RadioAccessStatus as _RAS, \
        RadioKind as _RK, RadioState as _RS
except Exception:  # noqa: BLE001
    traceback.print_exc()
    _Radio = None
try:
    from winrt.windows.devices.bluetooth import BluetoothAdapter as _BtAdapter
except Exception:  # noqa: BLE001
    _BtAdapter = None


class RadioService(QtCore.QObject):
    """changed(dict): {wifi_on, wifi_ssid, wifi_signal(int % 或 None), bt_on,
    wifi_available, bt_available, vpn_connections}"""

    changed = QtCore.Signal(dict)
    POLL_S = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self.state: dict = {"wifi_on": False, "wifi_ssid": "", "wifi_signal": None,
                            "bt_on": False, "wifi_available": False, "bt_available": False,
                            "vpn_connections": ()}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake: asyncio.Event | None = None
        self._access = None
        self._last_emit = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aisland-radio", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.refresh()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def refresh(self):
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self._loop = None
            loop.close()

    async def _main(self):
        self._wake = asyncio.Event()
        while not self._stop.is_set():
            try:
                await self._poll()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            try:
                await asyncio.wait_for(self._wake.wait(), self.POLL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _radios(self):
        if _Radio is None:
            return []
        if self._access is None:
            try:
                self._access = await _Radio.request_access_async()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._access = _RAS.UNSPECIFIED
        radios = list(await _Radio.get_radios_async() or [])
        # 部分电脑上 Radio.GetRadiosAsync 不返回蓝牙（驱动 / 权限原因），改从默认蓝牙适配器取
        if _BtAdapter is not None and not any(int(r.kind) == int(_RK.BLUETOOTH) for r in radios):
            try:
                ad = await _BtAdapter.get_default_async()
                r = await ad.get_radio_async() if ad is not None else None
                if r is not None:
                    radios.append(r)
            except Exception:  # noqa: BLE001
                pass
        return radios

    async def _poll(self):
        st = dict(self.state)
        st.update(wifi_on=False, bt_on=False, wifi_available=False, bt_available=False)
        try:
            for r in await self._radios():
                kind = int(r.kind)
                on = int(r.state) == int(_RS.ON)
                if kind == int(_RK.WI_FI):
                    st["wifi_available"] = True
                    st["wifi_on"] = st["wifi_on"] or on
                elif kind == int(_RK.BLUETOOTH):
                    st["bt_available"] = True
                    st["bt_on"] = st["bt_on"] or on
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        ssid, sig = "", None
        if st["wifi_on"] or not st["wifi_available"]:
            ssid, sig, iface = await asyncio.get_running_loop().run_in_executor(None, self._netsh)
            if iface and not st["wifi_available"]:
                st["wifi_available"] = True
                st["wifi_on"] = True
        st["wifi_ssid"], st["wifi_signal"] = ssid, sig
        st["vpn_connections"] = vpn_connections()
        self.state = st
        if st != self._last_emit:
            self._last_emit = dict(st)
            self.changed.emit(dict(st))

    @staticmethod
    def _netsh():
        """-> (ssid, signal%, has_interface)"""
        try:
            r = run_hidden(["netsh", "wlan", "show", "interfaces"], timeout=5)
            text = _decode(r.stdout)
        except Exception:  # noqa: BLE001
            return "", None, False
        ssid, sig, iface = "", None, False
        for line in text.splitlines():
            parts = re.split(r"\s*[:：]\s*", line.strip(), maxsplit=1)
            if len(parts) != 2:
                continue
            key, val = parts
            if key.upper() == "GUID":
                iface = True
            if "SSID" in key.upper() and "BSSID" not in key.upper() and not ssid:
                ssid = val.strip()
            elif sig is None and re.fullmatch(r"\d{1,3}\s*%", val.strip()):
                sig = int(val.strip().rstrip("%").strip())
        return ssid, sig, iface

    def _set(self, kind: int, on: bool):
        loop = self._loop
        if loop is None:
            return

        async def go():
            try:
                for r in await self._radios():
                    if int(r.kind) == kind:
                        await r.set_state_async(_RS.ON if on else _RS.OFF)
                await asyncio.sleep(0.5)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            if self._wake:
                self._wake.set()
        try:
            asyncio.run_coroutine_threadsafe(go(), loop)
        except RuntimeError:
            pass

    def set_wifi(self, on: bool):
        if _Radio is not None:
            self._set(int(_RK.WI_FI), bool(on))

    def set_bluetooth(self, on: bool):
        if _Radio is not None:
            self._set(int(_RK.BLUETOOTH), bool(on))


# ======================================================================
# 窗口
# ======================================================================
_u32 = ctypes.WinDLL("user32")
_k32 = ctypes.windll.kernel32
_dwm = ctypes.windll.dwmapi

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_MAXIMIZE = 0x01000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x08000000
GW_HWNDPREV, GW_OWNER = 3, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
DWMWA_CLOAKED = 14
SMTO_ABORTIFHUNG = 0x0002
SW_RESTORE, SW_SHOW = 9, 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
KEYEVENTF_KEYUP = 0x0002
_VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}


def send_hotkey(spec: str) -> bool:
    """模拟按下组合键，如 "Ctrl+Alt+W"。"""
    keys = []
    for part in (spec or "").split("+"):
        k = part.strip().lower()
        if k in _VK:
            keys.append(_VK[k])
        elif len(k) == 1 and k.isalnum():
            keys.append(ord(k.upper()))
        elif k.startswith("f") and k[1:].isdigit():
            keys.append(0x70 + int(k[1:]) - 1)
    if not keys:
        return False
    try:
        for vk in keys:
            _u32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(keys):
            _u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:  # noqa: BLE001
        return False
MONITOR_DEFAULTTONULL = 0

_WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
_u32.EnumWindows.argtypes = [_WNDENUMPROC, W.LPARAM]
_u32.EnumChildWindows.argtypes = [W.HWND, _WNDENUMPROC, W.LPARAM]
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [W.HWND, ctypes.c_int]
_u32.GetWindow.restype = W.HWND
_u32.GetWindow.argtypes = [W.HWND, W.UINT]
_u32.GetWindowTextLengthW.argtypes = [W.HWND]
_u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_u32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
_u32.GetWindowThreadProcessId.restype = W.DWORD
_u32.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT,
                                     ctypes.POINTER(ctypes.c_size_t)]
_u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_u32.IsWindowVisible.argtypes = [W.HWND]
_u32.IsWindow.argtypes = [W.HWND]
_u32.IsIconic.argtypes = [W.HWND]
_u32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [W.HWND]
_u32.BringWindowToTop.argtypes = [W.HWND]
_u32.GetForegroundWindow.restype = W.HWND
_u32.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
_u32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
_u32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, W.UINT]
_u32.SetWindowPos.restype = W.BOOL
_u32.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
_u32.MonitorFromWindow.restype = W.HMONITOR
_u32.GetShellWindow.restype = W.HWND
_u32.keybd_event.argtypes = [W.BYTE, W.BYTE, W.DWORD, ctypes.c_size_t]
_dwm.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]
_k32.OpenProcess.restype = W.HANDLE
_k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
_k32.GetApplicationUserModelId.argtypes = [W.HANDLE, ctypes.POINTER(W.UINT), W.LPWSTR]
_k32.GetApplicationUserModelId.restype = W.LONG
_k32.CloseHandle.argtypes = [W.HANDLE]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


_u32.GetMonitorInfoW.argtypes = [W.HMONITOR, ctypes.POINTER(_MONITORINFO)]

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


@dataclass(frozen=True)
class _VideoWindow:
    hwnd: int
    monitor: tuple[int, int]
    apps: frozenset[str]
    fullscreen: bool
    title_matches: bool


def _title(hwnd) -> str:
    n = _u32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd) -> int:
    pid = W.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_path(pid: int) -> str:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = W.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return ""
    finally:
        _k32.CloseHandle(h)


def _process_app_id(pid: int) -> str:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        size = W.UINT(len(buf))
        if _k32.GetApplicationUserModelId(h, ctypes.byref(size), buf) == 0:
            return buf.value
        return ""
    finally:
        _k32.CloseHandle(h)


def _cloaked(hwnd) -> bool:
    v = W.DWORD()
    if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0:
        return v.value != 0
    return False


def visible_app_windows(executable: str, include_minimized: bool = False) -> list[int]:
    """Include owned settings windows, which are often absent from Alt+Tab."""
    matches = []
    owners = {}

    def visit(hwnd, _param):
        if (_u32.IsWindowVisible(hwnd) and not _cloaked(hwnd)
                and (include_minimized or not _u32.IsIconic(hwnd))):
            pid = _pid(hwnd)
            if pid not in owners:
                owners[pid] = os.path.basename(_exe_path(pid)).casefold()
            if owners[pid] == executable:
                matches.append(hwnd)
        return True

    _u32.EnumWindows(_WNDENUMPROC(visit), 0)
    return matches


def chromium_renderer_windows(hwnd: int) -> list[int]:
    """Find the renderer handles used to expose embedded Chromium documents."""
    children = []

    def visit(child, _param):
        if _class(child) == "Chrome_RenderWidgetHostHWND":
            children.append(child)
        return len(children) < 8

    _u32.EnumChildWindows(hwnd, _WNDENUMPROC(visit), 0)
    for child in children:
        # WM_GETOBJECT for Chromium's screen-reader probe enables lazy accessibility.
        # The renderer handle itself can be hidden even while its document is visible.
        result = ctypes.c_size_t()
        _u32.SendMessageTimeoutW(child, 0x003D, 0, 1, SMTO_ABORTIFHUNG, 150, ctypes.byref(result))
    return children


class WindowService(QtCore.QObject):
    """窗口唤回、原生置顶和全屏视频检测。"""

    notice = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._own_pid = os.getpid()
        self._exe_cache: dict[int, str] = {}

    def start(self):
        pass

    def stop(self):
        pass

    # ---------------- 列表 ----------------
    def _is_alt_tab(self, hwnd) -> bool:
        if not _u32.IsWindowVisible(hwnd):
            return False
        ex = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
            return False
        if ex & WS_EX_NOACTIVATE and not ex & WS_EX_APPWINDOW:
            return False
        if _u32.GetWindow(hwnd, GW_OWNER) and not ex & WS_EX_APPWINDOW:
            return False
        if _cloaked(hwnd):
            return False
        return True

    def _real_pid(self, hwnd, pid: int, exe: str) -> int:
        """UWP 窗口属于 ApplicationFrameHost.exe，找其子 CoreWindow 的真实进程。"""
        if exe.lower() != "applicationframehost.exe":
            return pid
        found = []

        def cb(h, lp):
            p = _pid(h)
            if p != pid:
                found.append(p)
                return False
            return True
        _u32.EnumChildWindows(hwnd, _WNDENUMPROC(cb), 0)
        return found[0] if found else pid

    def _exe_for(self, pid: int) -> str:
        p = self._exe_cache.get(pid)
        if p is None:
            p = _exe_path(pid)
            if not p:
                return ""          # 一次 OpenProcess 竞态不该把这个 pid 永久记成「无 exe」
            if len(self._exe_cache) > 512:
                self._exe_cache.clear()
            self._exe_cache[pid] = p
        return p

    def foreground_application_path(self) -> str:
        """Keep the full path so shared process names can be distinguished."""
        hwnd = _u32.GetForegroundWindow()
        pid = _pid(hwnd) if hwnd else 0
        if not pid or pid == self._own_pid:
            return ""
        path = self._exe_for(pid)
        real_pid = self._real_pid(hwnd, pid, os.path.basename(path))
        if real_pid == self._own_pid:
            return ""
        return self._exe_for(real_pid) if real_pid != pid else path

    def foreground_executable(self) -> str:
        return os.path.basename(self.foreground_application_path()).casefold()

    # ---------------- 操作 ----------------
    def activate(self, hwnd: int) -> bool:
        try:
            hwnd = int(hwnd)
            if not _u32.IsWindow(hwnd):
                return False
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, SW_RESTORE)
            # 不再模拟按 Alt（会让 QQ / 微信 进入菜单模式）。用户刚点击过灵动岛，本进程通常
            # 有权设置前台窗口；不行时再临时挂接前台线程的输入队列。
            ok = bool(_u32.SetForegroundWindow(hwnd))
            if not ok or _u32.GetForegroundWindow() != hwnd:
                fg = _u32.GetForegroundWindow()
                cur_tid = _k32.GetCurrentThreadId()
                fg_tid = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
                tgt_tid = _u32.GetWindowThreadProcessId(hwnd, None)
                attached = []
                for t in {fg_tid, tgt_tid}:
                    if t and t != cur_tid and _u32.AttachThreadInput(cur_tid, t, True):
                        attached.append(t)
                try:
                    _u32.BringWindowToTop(hwnd)
                    ok = bool(_u32.SetForegroundWindow(hwnd))
                finally:
                    for t in attached:
                        _u32.AttachThreadInput(cur_tid, t, False)
            _u32.BringWindowToTop(hwnd)
            return ok or _u32.GetForegroundWindow() == hwnd
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False

    def find_app_window(self, exe: str) -> int | None:
        """找到某个进程（按 exe 文件名）的顶层窗口，按 Z 序返回最上面的那个。"""
        exe = os.path.basename(exe or "").lower()
        if not exe:
            return None
        hwnds: list[int] = []

        def cb(h, lp):
            hwnds.append(h)
            return True
        try:
            _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        except Exception:  # noqa: BLE001
            return None
        for h in hwnds:
            try:
                if not self._is_alt_tab(h) or not _title(h).strip():
                    continue
                pid = _pid(h)
                path = self._exe_for(pid)
                name = os.path.basename(path)
                rpid = self._real_pid(h, pid, name)
                if rpid != pid:
                    name = os.path.basename(self._exe_for(rpid) or path)
                if name.lower() == exe:
                    return int(h)
            except Exception:  # noqa: BLE001
                continue
        return None

    def find_hidden_main_window(self, exe: str, titles: list[str]) -> int | None:
        """托盘里的应用：主窗口只是被隐藏了。按标题在该进程的顶层窗口里找（包括不可见的）。"""
        exe = os.path.basename(exe or "").lower()
        want = [t.lower() for t in titles or []]
        if not exe or not want:
            return None
        hwnds: list[int] = []

        def cb(h, lp):
            hwnds.append(h)
            return True
        try:
            _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        except Exception:  # noqa: BLE001
            return None
        best = None
        for h in hwnds:
            try:
                if _u32.GetWindow(h, 4):              # 有 owner 的不是主窗口
                    continue
                title = _title(h).strip().lower()
                if title not in want:
                    continue
                name = os.path.basename(self._exe_for(_pid(h))).lower()
                if name != exe:
                    continue
                r = W.RECT()
                _u32.GetWindowRect(h, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                if area < 200 * 200:
                    continue
                if best is None or area > best[1]:
                    best = (int(h), area)
            except Exception:  # noqa: BLE001
                continue
        return best[0] if best else None

    def _process_running(self, exe: str) -> bool:
        import psutil
        exe = exe.lower()
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == exe:
                return True
        return False

    def _focus_later(self, exe: str, hk: str = ""):
        h = self.find_app_window(exe)
        if h:
            self.activate(h)
        else:
            self.notice.emit(f"没能唤出窗口：请检查该应用的「显示窗口」快捷键是否为 {hk}")

    def show_hidden(self, hwnd: int) -> bool:
        try:
            _u32.ShowWindow(hwnd, SW_SHOW)
            if _u32.IsIconic(hwnd):
                _u32.ShowWindow(hwnd, SW_RESTORE)
            return self.activate(hwnd)
        except Exception:  # noqa: BLE001
            return False

    def open_or_focus(self, path: str, exe: str | None = None, args=None) -> str:
        """应用已打开 -> 切到它的窗口；否则启动。返回 "focused" / "launched" / "failed"。"""
        exe = exe or (os.path.basename(path) if str(path).lower().endswith(".exe") else "")
        if exe:
            h = self.find_app_window(exe)
            if h and self.activate(h):
                return "focused"
            # 最小化到托盘（窗口被隐藏）：用应用自己的"显示窗口"热键让它自己恢复，
            # 不强行 ShowWindow（会和应用内部状态不一致），也不重新启动 exe（微信会开新实例）
            from aisland.core.config import cfg
            hk = (cfg.get("apps.restore_hotkey") or {}).get(exe.lower())
            if hk and self._process_running(exe):
                if send_hotkey(hk):
                    QtCore.QTimer.singleShot(600, lambda e=exe, k=hk: self._focus_later(e, k))
                    return "focused"
            # 只拿到 exe 名、没找到窗口时不要试着启动：微信会开出新实例，
            # 办公应用也多半不在 PATH。要自动启动请在 notify.open.<kind> 配完整路径。
            if not os.path.dirname(str(path)):
                return "failed"
        if args and isinstance(args, str):
            args = args.split()
        return "launched" if launch(path, list(args or [])) else "failed"

    def ensure_topmost(self, hwnd: int):
        """原生置顶被撤销或被别的置顶窗口挡住时，恢复层级但保留键盘焦点。"""
        if not _u32.IsWindowVisible(hwnd):
            return
        if _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
            own_rect, other_rect = W.RECT(), W.RECT()
            if not _u32.GetWindowRect(hwnd, ctypes.byref(own_rect)):
                return
            above = hwnd
            for _ in range(128):
                above = _u32.GetWindow(above, GW_HWNDPREV)
                if not above:
                    return
                if not _u32.IsWindowVisible(above) or not _u32.GetWindowRect(above, ctypes.byref(other_rect)):
                    continue
                if (other_rect.left < own_rect.right and other_rect.right > own_rect.left
                        and other_rect.top < own_rect.bottom and other_rect.bottom > own_rect.top
                        and _pid(above) != self._own_pid and _class(above) not in _SHELL_CLASSES
                        and not _cloaked(above)):
                    break
            else:
                return
        _u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _video_window(self, hwnd, video_apps, title, paths, identities, *, fullscreen_only=False):
        if not hwnd or not _u32.IsWindowVisible(hwnd) or _u32.IsIconic(hwnd):
            return None
        pid = _pid(hwnd)
        if not pid or pid == self._own_pid or _cloaked(hwnd) or _class(hwnd) in _SHELL_CLASSES:
            return None
        hm = _u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL)
        if not hm:
            return None
        r, mi = W.RECT(), _MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if not _u32.GetWindowRect(hwnd, ctypes.byref(r)) or not _u32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            return None
        m = mi.rcMonitor
        # 自动隐藏任务栏时，普通最大化窗口也可能覆盖显示器。
        fullscreen = (not _u32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_MAXIMIZE
                      and r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom)
        if not fullscreen and (fullscreen_only or not self._is_alt_tab(hwnd)):
            return None
        if pid not in paths:
            paths[pid] = _exe_path(pid).casefold()
        real_pid = self._real_pid(hwnd, pid, os.path.basename(paths[pid]))
        if real_pid not in identities:
            if real_pid not in paths:
                paths[real_pid] = _exe_path(real_pid).casefold()
            path = paths[real_pid]
            exe = os.path.basename(path)
            # Chromium 的桌面会话可用 Chrome / MSEdge 作为 ID。
            identities[real_pid] = {_process_app_id(real_pid).casefold(), path, exe, exe.removesuffix(".exe")}
        matched = video_apps.intersection(identities[real_pid] - {""})
        if not matched:
            return None
        title_matches = False
        app_id, media_title = title
        if app_id in matched and media_title:
            window_title = " ".join(_title(hwnd).casefold().split())
            title_matches = window_title == media_title or window_title.startswith(
                tuple(media_title + suffix for suffix in (" - ", " — ", " – ", "_", " | ", "｜")))
        return _VideoWindow(hwnd, (m.left, m.top), matched, fullscreen, title_matches)

    def foreground_fullscreen_video_monitor(self, video_apps: frozenset[str]) -> tuple[int, int] | None:
        if not video_apps:
            return None
        window = self._video_window(_u32.GetForegroundWindow(), video_apps, ("", ""), {}, {},
                                    fullscreen_only=True)
        return window.monitor if window else None

    def playing_media_monitor(self, apps: frozenset[str], title: tuple[str, str],
                              current: tuple[int, int] | None) -> tuple[int, int] | None:
        """用正在播放的会话匹配可见窗口。无法区分时保留当前屏。"""
        foreground = _u32.GetForegroundWindow()
        paths, identities, windows = {}, {}, []
        media_title = title[0].casefold(), " ".join(title[1].casefold().split())

        def visit(hwnd, _param):
            window = self._video_window(hwnd, apps, media_title, paths, identities)
            if window:
                windows.append(window)
            return True

        _u32.EnumWindows(_WNDENUMPROC(visit), 0)
        front = next((w for w in windows if w.hwnd == foreground), None)
        if front and (front.title_matches or front.fullscreen or not any(
                w.monitor != front.monitor and w.apps & front.apps for w in windows)):
            return front.monitor
        candidates = ([w for w in windows if w.title_matches]
                      or [w for w in windows if w.fullscreen] or windows)
        monitors = {w.monitor for w in candidates}
        if len(monitors) == 1:
            return monitors.pop()
        return current if current in monitors else None
