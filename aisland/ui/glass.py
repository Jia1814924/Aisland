# -*- coding: utf-8 -*-
"""毛玻璃背景。

后台线程用 GDI StretchBlt(HALFTONE) 把灵动岛身后的屏幕区域直接缩成很小的图（灵动岛自身已设为
不进入截图），再做一次平均模糊；主线程绘制时用平滑放大的纹理画刷填充形状 —— 边缘抗锯齿、任意形状。
截屏要等 DWM 回读（10~30ms），所以绝不能放在 UI 线程里。
"""
from __future__ import annotations

import ctypes
import colorsys
import threading
import time
from ctypes import wintypes as W

from PySide6 import QtCore, QtGui

WDA_EXCLUDEFROMCAPTURE = 0x11
DOWN = 12  # 缩小倍数（越大越糊）

_u32 = ctypes.WinDLL("user32")
_gdi = ctypes.WinDLL("gdi32")
_u32.GetDC.restype = W.HDC
_u32.GetDC.argtypes = [W.HWND]
_u32.ReleaseDC.argtypes = [W.HWND, W.HDC]
_gdi.CreateCompatibleDC.restype = W.HDC
_gdi.CreateCompatibleDC.argtypes = [W.HDC]
_gdi.CreateDIBSection.restype = W.HBITMAP
_gdi.CreateDIBSection.argtypes = [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD]
_gdi.SelectObject.restype = W.HGDIOBJ
_gdi.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]
_gdi.DeleteObject.argtypes = [W.HGDIOBJ]
_gdi.DeleteDC.argtypes = [W.HDC]
_gdi.SetStretchBltMode.argtypes = [W.HDC, ctypes.c_int]
_gdi.SetBrushOrgEx.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_gdi.StretchBlt.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD]
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HALFTONE = 4


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


def exclude_from_capture(hwnd: int, on: bool = True) -> bool:
    try:
        return bool(_u32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if on else 0))
    except Exception:  # noqa: BLE001
        return False


def _blur(img: QtGui.QImage) -> QtGui.QImage:
    """3x3 平均：第 k 张以 1/k 不透明度叠加（SourceOver），结果正好是平均值，不会变亮。"""
    out = img.copy()
    p = QtGui.QPainter(out)
    k = 1
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        k += 1
        p.setOpacity(1 / k)
        p.drawImage(dx, dy, img)
    p.end()
    return out


def grab_tiny(px: int, py: int, pw: int, ph: int, tw: int, th: int) -> QtGui.QImage | None:
    """物理像素区域 -> tw x th 的缩略图（HALFTONE 面积平均）。线程安全。"""
    sdc = _u32.GetDC(None)
    if not sdc:
        return None
    mdc = _gdi.CreateCompatibleDC(sdc)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), tw, -th, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = _gdi.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    img = None
    try:
        if hbmp and bits.value:
            old = _gdi.SelectObject(mdc, hbmp)
            _gdi.SetStretchBltMode(mdc, HALFTONE)
            _gdi.SetBrushOrgEx(mdc, 0, 0, None)
            if _gdi.StretchBlt(mdc, 0, 0, tw, th, sdc, px, py, pw, ph, SRCCOPY):   # 不用 CAPTUREBLT：它会让分层窗口闪烁
                buf = ctypes.string_at(bits.value, tw * th * 4)
                img = QtGui.QImage(buf, tw, th, tw * 4, QtGui.QImage.Format.Format_RGB32).copy()
            _gdi.SelectObject(mdc, old)
    finally:
        if hbmp:
            _gdi.DeleteObject(hbmp)
        _gdi.DeleteDC(mdc)
        _u32.ReleaseDC(None, sdc)
    return img


class Backdrop(QtCore.QObject):
    """后台截取 + 模糊。主线程调用 request() 告知要覆盖的区域，结果通过 ready 信号回到主线程。"""
    ready = QtCore.Signal(bool)          # changed
    finished = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # (模糊小图, 对应的全局逻辑区域, 亮度, 背景主色)；快照一次赋值原子发布：
        # 主线程要么整组读到旧值、要么整组读到新值，不会撞上
        # 「新背景图 + 旧主色」的错色一帧（莫兰迪模式下可见）。
        self._snapshot: tuple = (None, QtCore.QRect(), 0.5, (255, 255, 255))
        self._sig = None
        self._lock = threading.Lock()
        self._want: tuple | None = None     # (逻辑区域, 物理区域)
        self._interval = 0.1
        self._wake = threading.Event()
        self._stop = False
        self._paused = False
        self._thread = threading.Thread(target=self._run, name="aisland-glass", daemon=True)
        # 参与屏幕捕获时：只在自己截取背景的那一瞬间把灵动岛排除，截完立即恢复
        self.hwnd = 0
        self.self_exclude = False
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()

    @property
    def state(self) -> tuple:
        """(模糊小图, 全局逻辑区域, 亮度)：与 rgb 同一快照，不会读到错配的一对。"""
        return self._snapshot[:3]

    @property
    def rgb(self) -> tuple:
        """背景主色（给莫兰迪主题用）：取最饱和的一格与平均值混合，避免互补色被平均成灰。"""
        return self._snapshot[3]

    @property
    def tiny(self):
        return self.state[0]

    @property
    def rect(self):
        return self.state[1]

    @property
    def luminance(self):
        return self.state[2]

    def stop(self):
        with self._lock:
            self._stop = True
            self._want = None
        self._wake.set()

    @property
    def running(self):
        return self._thread.is_alive()

    def pause(self):
        """窗口隐藏时停止截屏：线程阻塞在事件上，不再占用 CPU / GDI。"""
        with self._lock:
            self._paused = True
            self._want = None
        self._wake.set()

    def resume(self):
        with self._lock:
            self._paused = False
        self._wake.set()

    def request(self, screen: QtGui.QScreen, grect: QtCore.QRect, interval: float, urgent: bool = False):
        """grect：需要覆盖的全局逻辑区域；interval：期望的刷新间隔（秒）。"""
        sg = screen.geometry()
        r = grect.intersected(sg)
        if r.width() < 4 or r.height() < 4:
            return
        dpr = screen.devicePixelRatio()
        # Qt6 在 Windows 上保持屏幕原点不缩放：物理 = 原点 + (逻辑 - 原点) * dpr
        px = sg.x() + round((r.x() - sg.x()) * dpr)
        py = sg.y() + round((r.y() - sg.y()) * dpr)
        phys = (px, py, round(r.width() * dpr), round(r.height() * dpr))
        with self._lock:
            if self._paused or self._stop:
                return
            new = (QtCore.QRect(r), phys)
            moved = self._want is None or self._want[0] != new[0]
            self._want = new
            self._interval = interval
        if urgent or moved:
            self._wake.set()

    def _run(self):
        last = 0.0
        try:
            while not self._stop:
                with self._lock:
                    interval = self._interval if self._want is not None else None
                # 启动和暂停期间没有取景区域，等到收到请求再工作。
                delay = None if interval is None else max(0.0, interval - (time.perf_counter() - last))
                self._wake.wait(delay)
                self._wake.clear()
                # 两次截图之间至少隔 1/45s，避免被连续唤醒时占满 CPU
                gap = time.perf_counter() - last
                if gap < 1 / 45:
                    time.sleep(1 / 45 - gap)
                with self._lock:
                    want = self._want
                if self._stop:
                    break
                if want is None:
                    continue
                last = time.perf_counter()
                try:
                    self._capture(*want)
                except Exception:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
        finally:
            self.finished.emit()

    def _capture(self, r: QtCore.QRect, phys):
        tw, th = max(2, r.width() // DOWN), max(2, r.height() // DOWN)
        toggle = self.self_exclude and self.hwnd
        if toggle:
            _u32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
        try:
            img = grab_tiny(phys[0], phys[1], phys[2], phys[3], tw, th)
        finally:
            if toggle:
                _u32.SetWindowDisplayAffinity(self.hwnd, 0)
        if img is None or self._stop:
            return
        img = img.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img = _blur(_blur(img))
        mode = QtCore.Qt.TransformationMode.SmoothTransformation
        sig = bytes(img.scaled(8, 4, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, mode).constBits())
        c = img.scaled(1, 1, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, mode).pixelColor(0, 0)
        lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
        # 主色：4x2 里饱和度×明度最高的一格 60% + 全图平均 40%
        small = img.scaled(4, 2, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, mode)
        best, best_score = None, -1.0
        for yy in range(small.height()):
            for xx in range(small.width()):
                px = small.pixelColor(xx, yy)
                _, s_, v_ = colorsys.rgb_to_hsv(px.redF(), px.greenF(), px.blueF())
                if s_ * v_ > best_score:
                    best_score, best = s_ * v_, px
        if best is None:
            rgb = (c.red(), c.green(), c.blue())
        else:
            rgb = (round(best.red() * 0.6 + c.red() * 0.4),
                   round(best.green() * 0.6 + c.green() * 0.4),
                   round(best.blue() * 0.6 + c.blue() * 0.4))
        changed = sig != self._sig or r != self.rect
        if self._stop:
            return
        self._sig = sig
        self._snapshot = (img, QtCore.QRect(r), lum, rgb)
        self.ready.emit(changed)

    def covers(self, grect: QtCore.QRect) -> bool:
        tiny, rect, _ = self.state
        return tiny is not None and rect.contains(grect)

    @staticmethod
    def brush_for(state, window_origin: QtCore.QPoint) -> QtGui.QBrush | None:
        if not state or state[0] is None:
            return None
        tiny, rect = state[0], state[1]
        b = QtGui.QBrush(tiny)
        tr = QtGui.QTransform()
        tr.translate(rect.x() - window_origin.x(), rect.y() - window_origin.y())
        tr.scale(rect.width() / tiny.width(), rect.height() / tiny.height())
        b.setTransform(tr)
        return b

    def brush(self, window_origin: QtCore.QPoint) -> QtGui.QBrush | None:
        """返回把模糊小图铺到窗口坐标的画刷。window_origin = 窗口左上角的全局坐标。"""
        tiny, rect, _ = self.state
        if tiny is None:
            return None
        b = QtGui.QBrush(tiny)
        tr = QtGui.QTransform()
        tr.translate(rect.x() - window_origin.x(), rect.y() - window_origin.y())
        tr.scale(rect.width() / tiny.width(), rect.height() / tiny.height())
        b.setTransform(tr)
        return b
