# -*- coding: utf-8 -*-
"""音量（pycaw，主线程 COM）与亮度（Twinkle Tray CLI / DDC-CI / WMI，后台单线程）。"""
from __future__ import annotations

import ctypes
import os
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as W
from itertools import count

from PySide6 import QtCore, QtGui

from aisland.core.bg import run_hidden, ui
from aisland.core.config import cfg, expand


# ======================================================================
# 音量
# ======================================================================
class AudioService(QtCore.QObject):
    """默认播放设备的主音量。所有 COM 调用只在主线程。"""

    volume_changed = QtCore.Signal(float, bool)   # (0..1, muted)

    POLL_MS = 250
    DEVICE_CHECK_S = 3.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enum = None
        self._dev_id = None
        self._vol = None      # IAudioEndpointVolume
        self._meter = None    # IAudioMeterInformation
        self._last = None     # (volume, muted)
        self._last_dev_check = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ---------------- 设备 ----------------
    def _acquire(self) -> bool:
        try:
            import comtypes
            from pycaw.pycaw import (IAudioEndpointVolume, IAudioMeterInformation,
                                     IMMDeviceEnumerator)
            try:
                from pycaw.constants import CLSID_MMDeviceEnumerator
            except Exception:  # noqa: BLE001
                from pycaw.pycaw import CLSID_MMDeviceEnumerator  # type: ignore
            if self._enum is None:
                self._enum = comtypes.CoCreateInstance(
                    CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
            dev = self._enum.GetDefaultAudioEndpoint(0, 1)   # eRender, eMultimedia
            self._dev_id = dev.GetId()
            self._vol = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None) \
                .QueryInterface(IAudioEndpointVolume)
            try:
                self._meter = dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None) \
                    .QueryInterface(IAudioMeterInformation)
            except Exception:  # noqa: BLE001
                self._meter = None
            self._last_dev_check = time.monotonic()
            return True
        except Exception:  # noqa: BLE001
            # 没有播放设备时会走到这里
            self._vol = self._meter = None
            self._dev_id = None
            self._last_dev_check = time.monotonic()
            return False

    def _check_device(self):
        """便宜地检查默认设备是否变了（只取 ID 字符串）。"""
        now = time.monotonic()
        if now - self._last_dev_check < self.DEVICE_CHECK_S and self._vol is not None:
            return
        self._last_dev_check = now
        try:
            if self._enum is None:
                self._acquire()
                return
            dev_id = self._enum.GetDefaultAudioEndpoint(0, 1).GetId()
            if dev_id != self._dev_id or self._vol is None:
                self._acquire()
        except Exception:  # noqa: BLE001
            self._vol = self._meter = None
            self._dev_id = None

    def _ev(self):
        if self._vol is None:
            self._acquire()
        return self._vol

    def _call(self, fn, default=None):
        """调用 COM，出错则重新获取设备再试一次。"""
        for attempt in range(2):
            ev = self._ev()
            if ev is None:
                return default
            try:
                return fn(ev)
            except Exception:  # noqa: BLE001
                self._vol = self._meter = None
                if attempt == 1:
                    return default
        return default

    # ---------------- 生命周期 ----------------
    def start(self):
        self._acquire()
        self._poll()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        try:
            self._check_device()
            cur = (round(self.volume(), 4), self.muted())
            if cur != self._last:
                self._last = cur
                self.volume_changed.emit(cur[0], cur[1])
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _notify_soon(self):
        QtCore.QTimer.singleShot(0, self._poll)

    # ---------------- API ----------------
    def available(self) -> bool:
        return self._ev() is not None

    def volume(self) -> float:
        return float(self._call(lambda ev: ev.GetMasterVolumeLevelScalar(), 0.0) or 0.0)

    def set_volume(self, v: float):
        v = max(0.0, min(1.0, float(v)))
        self._call(lambda ev: ev.SetMasterVolumeLevelScalar(v, None))
        self._notify_soon()

    def muted(self) -> bool:
        return bool(self._call(lambda ev: ev.GetMute(), False))

    def set_muted(self, b: bool):
        self._call(lambda ev: ev.SetMute(1 if b else 0, None))
        self._notify_soon()

    def toggle_mute(self):
        self.set_muted(not self.muted())

    def step(self, delta: float):
        """delta 为 0..1 刻度，例如 +0.02。调大音量时自动取消静音。"""
        self.set_volume(self.volume() + delta)
        if delta > 0 and self.muted():
            self.set_muted(False)

    def peak(self) -> float:
        m = self._meter
        if m is None:
            return 0.0
        try:
            return max(0.0, min(1.0, float(m.GetPeakValue())))
        except Exception:  # noqa: BLE001
            self._meter = None
            self._vol = None
            return 0.0


# ======================================================================
# 亮度
# ======================================================================
_user32 = ctypes.WinDLL("user32")
_dxva2 = ctypes.windll.dxva2


class _PHYS(ctypes.Structure):
    _fields_ = [("h", W.HANDLE), ("desc", W.WCHAR * 128)]


class _MI(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT),
                ("dwFlags", W.DWORD), ("szDevice", W.WCHAR * 32)]


class _DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("name", W.WCHAR * 32), ("description", W.WCHAR * 128),
                ("flags", W.DWORD), ("id", W.WCHAR * 128), ("key", W.WCHAR * 128)]


_MONENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
_dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [W.HMONITOR, ctypes.POINTER(W.DWORD)]
_dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [W.HMONITOR, W.DWORD, ctypes.POINTER(_PHYS)]
_dxva2.DestroyPhysicalMonitors.argtypes = [W.DWORD, ctypes.POINTER(_PHYS)]
_dxva2.GetMonitorBrightness.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD),
                                        ctypes.POINTER(W.DWORD)]
_dxva2.SetMonitorBrightness.argtypes = [W.HANDLE, W.DWORD]
_dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [W.HANDLE, W.BYTE, ctypes.POINTER(W.DWORD),
                                                   ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD)]
_dxva2.SetVCPFeature.argtypes = [W.HANDLE, W.BYTE, W.DWORD]
_user32.GetMonitorInfoW.argtypes = [W.HMONITOR, ctypes.POINTER(_MI)]
_user32.EnumDisplayDevicesW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(_DISPLAY_DEVICE), W.DWORD]


def _monitor_interface(device: str) -> str:
    """一个逻辑屏只有一个活动设备接口时，才可用它匹配 WMI 或 Twinkle Tray。"""
    interface = ""
    for i in count():
        info = _DISPLAY_DEVICE(cb=ctypes.sizeof(_DISPLAY_DEVICE))
        # EDD_GET_DEVICE_INTERFACE_NAME
        if not _user32.EnumDisplayDevicesW(device, i, ctypes.byref(info), 1):
            return interface
        if info.flags & 1 and info.id.upper().startswith("\\\\?\\DISPLAY#"):
            if interface:
                return ""
            interface = info.id.split("#{", 1)[0]


def _wmi_interface(instance: str) -> str:
    # WMI 的 DISPLAY\型号\实例_0 与 GDI 的 \\?\DISPLAY#型号#实例#{GUID} 对应。
    device, _, suffix = instance.rpartition("_")
    instance = device if suffix.isdecimal() else instance
    return "\\\\?\\" + instance.replace("\\", "#")


def _enum_physical():
    """[(id, device_name, handle, description, interface)]，调用方负责 _destroy。"""
    hmons = []

    def cb(h, dc, r, lp):
        hmons.append(h)
        return True

    _user32.EnumDisplayMonitors(None, None, _MONENUMPROC(cb), 0)
    out, arrays = [], []
    for hm in hmons:
        mi = _MI()
        mi.cbSize = ctypes.sizeof(_MI)
        if not _user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            continue
        dev = mi.szDevice
        interface = _monitor_interface(dev)
        n = W.DWORD()
        if not _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hm, ctypes.byref(n)) or n.value == 0:
            out.append((interface.casefold() or f"{dev}#0", dev, None, "", interface))
            continue
        arr = (_PHYS * n.value)()
        if not _dxva2.GetPhysicalMonitorsFromHMONITOR(hm, n.value, arr):
            out.append((interface.casefold() or f"{dev}#0", dev, None, "", interface))
            continue
        arrays.append((n.value, arr))
        if n.value != 1:
            interface = ""
        for i, p in enumerate(arr):
            out.append((interface.casefold() or f"{dev}#{i}:{p.desc}", dev, p.h, p.desc, interface))
    return out, arrays


def _destroy(arrays):
    for n, arr in arrays:
        try:
            _dxva2.DestroyPhysicalMonitors(n, arr)
        except Exception:  # noqa: BLE001
            pass


def _ddc_get(h) -> int | None:
    if not h:
        return None
    mn, cur, mx = W.DWORD(), W.DWORD(), W.DWORD()
    if _dxva2.GetMonitorBrightness(h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)) and mx.value > mn.value:
        return _brightness_percent(cur.value, mn.value, mx.value)
    ct = W.DWORD()
    if _dxva2.GetVCPFeatureAndVCPFeatureReply(h, 0x10, ctypes.byref(ct), ctypes.byref(cur), ctypes.byref(mx)) \
            and mx.value > 0:
        return _brightness_percent(cur.value, 0, mx.value)
    return None


def _ddc_set(h, value: int) -> bool:
    if not h:
        return False
    mn, cur, mx = W.DWORD(), W.DWORD(), W.DWORD()
    if _dxva2.GetMonitorBrightness(h, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)) and mx.value > mn.value:
        raw = mn.value + round(value * (mx.value - mn.value) / 100)
        if _dxva2.SetMonitorBrightness(h, raw):
            return True
    ct = W.DWORD()
    if _dxva2.GetVCPFeatureAndVCPFeatureReply(h, 0x10, ctypes.byref(ct), ctypes.byref(cur), ctypes.byref(mx)) \
            and mx.value > 0:
        return bool(_dxva2.SetVCPFeature(h, 0x10, round(value * mx.value / 100)))
    return False


def _brightness_percent(value: int, minimum: int, maximum: int) -> int:
    return max(0, min(100, round((value - minimum) * 100 / (maximum - minimum))))


class _Wmi:
    """root\\wmi 亮度（笔记本内屏）。只在亮度工作线程里使用（MTA）。"""

    def __init__(self):
        self.svc = None
        self.failed = False

    def _conn(self):
        if self.svc is None and not self.failed:
            try:
                import comtypes.client
                self.svc = comtypes.client.CoGetObject(
                    "winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\wmi")
            except Exception:  # noqa: BLE001
                self.failed = True
        return self.svc

    @staticmethod
    def _p(o, name):
        return o.Properties_.Item(name).Value

    def read(self) -> list[tuple[str, int, int, int]]:
        svc = self._conn()
        if svc is None:
            return self._read_ps()
        try:
            out = []
            for m in svc.ExecQuery("SELECT * FROM WmiMonitorBrightness WHERE Active = TRUE"):
                levels = self._p(m, "Level") or (0, 100)
                out.append((str(self._p(m, "InstanceName")), int(self._p(m, "CurrentBrightness")),
                            int(min(levels)), int(max(levels))))
            return out
        except Exception:  # noqa: BLE001
            # 没有内屏时 WMI 会抛“不支持”，属正常
            self.svc = None
            return []

    def set(self, value: int, instance: str) -> bool:
        svc = self._conn()
        if svc is None:
            return self._set_ps(value, instance)
        try:
            for m in svc.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods WHERE Active = TRUE"):
                if str(self._p(m, "InstanceName")).casefold() != instance.casefold():
                    continue
                ip = m.Methods_.Item("WmiSetBrightness").InParameters.SpawnInstance_()
                ip.Properties_.Item("Timeout").Value = 0
                ip.Properties_.Item("Brightness").Value = int(value)
                result = m.ExecMethod_("WmiSetBrightness", ip)
                return self._p(result, "ReturnValue") == 0
            return False
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.svc = None
            return False

    # 最后手段：PowerShell（慢，约 1 秒）
    @staticmethod
    def _read_ps():
        try:
            r = run_hidden(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance -Namespace root/wmi WmiMonitorBrightness | "
                            "Where-Object Active | ForEach-Object { "
                            "$r = $_.Level | Measure-Object -Minimum -Maximum; "
                            "\"$($_.InstanceName)|$($_.CurrentBrightness)|$($r.Minimum)|$($r.Maximum)\" }"],
                           timeout=8)
            out = []
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                fields = line.rsplit("|", 3)
                if len(fields) == 4:
                    instance, value, minimum, maximum = fields
                    out.append((instance.strip(), int(value), int(minimum or 0), int(maximum or 100)))
            return out
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _set_ps(value, instance):
        try:
            target = instance.replace("'", "''")
            r = run_hidden(["powershell", "-NoProfile", "-Command",
                            "$m = Get-CimInstance -Namespace root/wmi WmiMonitorBrightnessMethods | "
                            f"Where-Object {{ $_.Active -and $_.InstanceName -eq '{target}' }}; "
                            "if (-not $m) { exit 1 }; "
                            "$r = Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
                            f"-Arguments @{{Timeout=0;Brightness={int(value)}}} -ErrorAction Stop; "
                            "if ($r.ReturnValue -ne 0) { exit 1 }"],
                           timeout=8)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False


def _init_worker():
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:  # noqa: BLE001
        pass


class BrightnessService(QtCore.QObject):
    """分别读取和调节每块显示器的亮度，写入由单个后台线程串行完成。"""

    changed = QtCore.Signal(list)

    DEBOUNCE_MS = 150
    TWINKLE_CHECK_S = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)   # 确保 bg 的 _Invoker 在主线程创建（它是惰性创建的）
        self.monitors: list[dict] = []
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aisland-bright",
                                        initializer=_init_worker)
        self._wmi = _Wmi()
        self._pending: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._flush)
        self._twinkle_state = (0.0, False)
        self._twinkle_launched = False
        self._gui_app = None

    def start(self):
        self._gui_app = QtGui.QGuiApplication.instance()
        if self._gui_app:
            self._gui_app.screenAdded.connect(self.refresh)
            self._gui_app.screenRemoved.connect(self.refresh)
        self.refresh()

    def stop(self):
        if self._gui_app:
            self._gui_app.screenAdded.disconnect(self.refresh)
            self._gui_app.screenRemoved.disconnect(self.refresh)
            self._gui_app = None
        self._debounce.stop()
        self._pending.clear()
        self._exec.shutdown(wait=False, cancel_futures=True)

    def _submit(self, fn, on_done=None):
        def task():
            try:
                res = fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                res = None
            if on_done:
                ui(lambda: on_done(res))
        try:
            self._exec.submit(task)
        except RuntimeError:
            pass   # 已 stop

    # ---------------- 读取 ----------------
    def refresh(self):
        self._submit(self._read_all, self._on_read)

    def _read_all(self) -> list[dict]:
        wmi = {_wmi_interface(inst).casefold(): (inst, val, minimum, maximum)
               for inst, val, minimum, maximum in self._wmi.read() if maximum > minimum}
        phys, arrays = _enum_physical()
        try:
            mons = {}
            for mid, dev, h, desc, interface in phys:
                b = _ddc_get(h) if mid not in wmi else None
                mons[mid] = {"id": mid, "device": dev, "desc": desc, "interface": interface,
                             "brightness": b, "method": "ddc" if b is not None else None}
        finally:
            _destroy(arrays)
        for mid, (inst, val, minimum, maximum) in wmi.items():
            m = mons.setdefault(mid, {"id": mid, "device": "", "desc": "", "interface": _wmi_interface(inst)})
            m.update(brightness=_brightness_percent(val, minimum, maximum), method="wmi",
                     wmi=inst, minimum=minimum, maximum=maximum)
        if self._twinkle_exe():
            # Twinkle Tray 用实例部分（如 4&…&UID123）作为命令行的显示器键。
            keys = {mid: m["interface"].rsplit("#", 1)[-1].casefold() for mid, m in mons.items()}
            for mid, m in mons.items():
                key = keys[mid]
                if not m["method"] and key and sum(key in other for other in keys.values()) == 1:
                    m["method"] = "twinkle"
                    m["twinkle_key"] = key
        return sorted(mons.values(), key=lambda m: m["method"] != "wmi")

    def _on_read(self, mons: list[dict] | None):
        if mons is None:
            return
        names = {}
        try:
            for s in QtGui.QGuiApplication.screens():
                names[s.name()] = (s.model() or s.manufacturer() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        generic = ("generic pnp monitor", "通用即插即用监视器", "generic monitor", "")
        pending = self._inflight | self._pending
        for i, m in enumerate(mons):
            desc = (m.get("desc") or "").strip()
            nm = desc if desc.lower() not in generic else (names.get(m["device"]) or f"显示器 {i + 1}")
            m["name"] = "内置屏幕" if m["method"] == "wmi" else nm
            if m["method"] and m["id"] in pending:
                m["brightness"] = pending[m["id"]]
        counts = Counter(m["name"] for m in mons)
        for i, m in enumerate(mons, 1):
            if counts[m["name"]] > 1:
                m["name"] = f"{m['name']} · {i}"
        self.monitors = mons
        self._emit()
        if not any(m["method"] in ("wmi", "ddc") for m in mons):
            self._maybe_launch_twinkle()

    def _emit(self):
        self.changed.emit([{"id": m["id"], "name": m["name"], "brightness": m["brightness"],
                            "controllable": bool(m["method"])}
                           for m in self.monitors])

    # ---------------- 设置（防抖） ----------------
    def set(self, monitor_id: str, value: int):
        value = int(max(0, min(100, round(value))))
        monitor = next((m for m in self.monitors if m["id"] == monitor_id), None)
        if not monitor or not monitor["method"] or monitor["brightness"] == value:
            return
        self._pending[monitor_id] = value
        monitor["brightness"] = value
        self._emit()
        if not self._debounce.isActive() and not self._inflight:
            self._debounce.start()

    def _flush(self):
        if self._inflight or not self._pending:
            return
        jobs, self._pending = self._pending, {}
        items = [(dict(m), jobs[m["id"]]) for m in self.monitors if m["method"] and m["id"] in jobs]
        if items:
            self._inflight = jobs
            self._submit(lambda: self._apply(items), self._on_applied)

    def _on_applied(self, ok):
        self._inflight = {}
        if not ok:
            self.refresh()
        if self._pending:
            self._debounce.start()

    def _twinkle_running(self) -> bool:
        t, val = self._twinkle_state
        if time.monotonic() - t < self.TWINKLE_CHECK_S:
            return val
        val = False
        try:
            import psutil
            for p in psutil.process_iter(["name"]):
                n = (p.info.get("name") or "").lower()
                if "twinkle" in n:
                    val = True
                    break
        except Exception:  # noqa: BLE001
            pass
        self._twinkle_state = (time.monotonic(), val)
        return val

    def _twinkle_exe(self) -> str:
        p = expand(cfg.get("brightness.twinkle") or "")
        return p if p and os.path.isfile(p) else ""

    def _twinkle(self, args: list[str]) -> bool:
        exe = self._twinkle_exe()
        if not exe:
            return False
        try:
            result = run_hidden([exe, *args], timeout=5, cwd=os.path.dirname(exe))
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return False

    def _maybe_launch_twinkle(self):
        """直连方式全都不可用时，尝试把 Twinkle Tray 启动到托盘（只尝试一次）。"""
        if self._twinkle_launched or not self._twinkle_exe():
            return
        self._twinkle_launched = True

        def go():
            if not self._twinkle_running():
                from aisland.core.bg import launch
                launch(self._twinkle_exe())
                self._twinkle_state = (0.0, False)
        self._submit(go)

    def _apply(self, items: list[tuple[dict, int]]) -> bool:
        phys, arrays = [], []
        if any(m["method"] != "wmi" for m, _ in items):
            phys, arrays = _enum_physical()
        by_id = {mid: h for mid, _, h, _, _ in phys}
        try:
            ok = True
            for m, v in items:
                if m["method"] == "wmi":
                    raw = m["minimum"] + round(v * (m["maximum"] - m["minimum"]) / 100)
                    success = self._wmi.set(raw, m["wmi"])
                elif m["method"] == "ddc":
                    handle = by_id.get(m["id"])
                    success = bool(handle and _ddc_set(handle, v))
                else:
                    key = m["twinkle_key"]
                    matches = sum(key in interface.rsplit("#", 1)[-1].casefold()
                                  for _, _, _, _, interface in phys)
                    success = m["id"] in by_id and matches == 1 and self._twinkle_running() and self._twinkle(
                        [f"--MonitorID={key}", f"--Set={v}"])
                ok = success and ok
            return ok
        finally:
            _destroy(arrays)
