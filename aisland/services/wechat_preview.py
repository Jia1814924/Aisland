"""Read the avatar and nickname from an already visible Weixin tray preview."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes as W
from pathlib import Path
from statistics import median

from PySide6 import QtCore, QtGui

from ..core.bg import run_hidden
from ..ui.glass import grab_tiny

_POPUP_CLASS = "Qt51514QWindowToolSaveBits"
_MAX_ROWS = 8
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.FindWindowExW.argtypes = [W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR]
_user32.FindWindowExW.restype = W.HWND
_user32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
_user32.GetDpiForWindow.argtypes = [W.HWND]
_user32.GetDpiForWindow.restype = W.UINT
_user32.IsWindowVisible.argtypes = [W.HWND]
_user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
_user32.GetWindowThreadProcessId.restype = W.DWORD
_user32.GetAncestor.argtypes = [W.HWND, W.UINT]
_user32.GetAncestor.restype = W.HWND
_user32.WindowFromPoint.argtypes = [W.POINT]
_user32.WindowFromPoint.restype = W.HWND


def visible_popup(exe_of_hwnd, pid: int = 0) -> int:
    """Locate the supported tray popup, leaving the cursor and focus alone."""
    hwnd = 0
    for _ in range(12):
        hwnd = _user32.FindWindowExW(None, hwnd, _POPUP_CLASS, None)
        if not hwnd:
            break
        if not _user32.IsWindowVisible(hwnd):
            continue
        if pid:
            owner = W.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                return hwnd
        elif exe_of_hwnd(hwnd) == "weixin.exe":
            return hwnd
    return 0


def _window_bounds(hwnd: int) -> QtCore.QRect:
    rect = W.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return QtCore.QRect()
    return QtCore.QRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _owns_region(hwnd: int, rect: QtCore.QRect) -> bool:
    if not _user32.IsWindowVisible(hwnd) or not _window_bounds(hwnd).contains(rect):
        return False
    inner = rect.adjusted(1, 1, -1, -1)
    points = (inner.topLeft(), inner.topRight(), inner.bottomLeft(), inner.bottomRight(), inner.center())
    for point in points:
        hit = _user32.WindowFromPoint(W.POINT(point.x(), point.y()))
        if not hit or (hit != hwnd and _user32.GetAncestor(hit, 2) != hwnd):
            return False
    return True


def _capture(hwnd: int, rect: QtCore.QRect) -> QtGui.QImage | None:
    if not _owns_region(hwnd, rect):
        return None
    image = grab_tiny(rect.x(), rect.y(), rect.width(), rect.height(), rect.width(), rect.height())
    return image if _owns_region(hwnd, rect) else None


def _capture_rows(hwnd: int) -> list[tuple[QtGui.QImage, QtGui.QImage]]:
    import comtypes.client

    comtypes.CoInitialize()
    try:
        return _capture_accessible_rows(hwnd, comtypes.client)
    finally:
        comtypes.CoUninitialize()


def _capture_accessible_rows(hwnd: int, client) -> list[tuple[QtGui.QImage, QtGui.QImage]]:
    library = client.GetModule(str(Path(os.environ["SystemRoot"]) / "System32/UIAutomationCore.dll"))
    automation = client.CreateObject(library.CUIAutomation, interface=library.IUIAutomation)
    popup = automation.ElementFromHandle(hwnd)
    footer = automation.CreatePropertyCondition(library.UIA_NamePropertyId, "暂不处理")
    if not popup.FindFirst(library.TreeScope_Descendants, footer):
        return []
    list_item = automation.CreatePropertyCondition(library.UIA_ControlTypePropertyId,
                                                   library.UIA_ListItemControlTypeId)
    items = popup.FindAll(library.TreeScope_Descendants, list_item)
    dpi = _user32.GetDpiForWindow(hwnd) / 96
    if not 0.75 <= dpi <= 4:
        return []
    captures = []
    for index in range(min(items.Length, _MAX_ROWS)):
        item = items.GetElement(index)
        if item.CurrentIsOffscreen:
            continue
        rect = item.CurrentBoundingRectangle
        width, height = rect.right - rect.left, rect.bottom - rect.top
        # The tested Weixin layout has one label, 56 DIP tall rows, and a
        # 36 DIP photo. Stay within its expected shape when Qt changes.
        if not (170 * dpi <= width <= 320 * dpi and 48 * dpi <= height <= 66 * dpi):
            continue
        mid_y = rect.top + round(height * 0.53)
        size = round(30 * dpi)
        photo_rect = QtCore.QRect(rect.left + round(17 * dpi), mid_y - size // 2, size, size)
        left, label_height = round(55 * dpi), round(24 * dpi)
        label_rect = QtCore.QRect(rect.left + left, mid_y - label_height // 2,
                                  width - left - round(18 * dpi), label_height)
        # Capture both fields together so a repaint cannot mix two contacts.
        strip_rect = photo_rect.united(label_rect)
        strip = _capture(hwnd, strip_rect)
        if strip is not None:
            origin = strip_rect.topLeft()
            captures.append((strip.copy(photo_rect.translated(-origin)),
                             strip.copy(label_rect.translated(-origin))))
    return captures


_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ocrType = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$resultType = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation' + [char]96 + '1'
} | Select-Object -First 1
function Await-WinRt($Operation, [type]$Type) {
    $task = $asTask.MakeGenericMethod($Type).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}
$engine = $ocrType::TryCreateFromUserProfileLanguages()
if (!$engine) { throw 'No local OCR language is available.' }
$names = @()
while ($line = [Console]::In.ReadLine()) {
    $memory = [IO.MemoryStream]::new([Convert]::FromBase64String($line))
    try {
        $stream = [IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($memory)
        $decoder = Await-WinRt ($decoderType::CreateAsync($stream)) $decoderType
        $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat,Windows.Graphics.Imaging,ContentType=WindowsRuntime]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode,Windows.Graphics.Imaging,ContentType=WindowsRuntime]::Premultiplied)) $bitmapType
        $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) $resultType
        $names += [string]$result.Text
    } finally {
        $memory.Dispose()
    }
}
[Console]::WriteLine((ConvertTo-Json -InputObject $names -Compress))
"""


def _encode_strip(strip: QtGui.QImage) -> bytes:
    image = strip.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    background = round(median(image.pixelColor(x, y).lightness()
                              for y in (0, image.height() - 1)
                              for x in range(0, image.width(), max(1, image.width() // 16))))
    if background < 128:
        image.invertPixels()
        background = 255 - background
    image = image.scaled(image.width() * 3, image.height() * 3,
                         QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                         QtCore.Qt.TransformationMode.SmoothTransformation)
    canvas = QtGui.QImage(image.width() + 48, image.height() + 48, QtGui.QImage.Format.Format_RGB32)
    canvas.fill(QtGui.QColor(background, background, background))
    painter = QtGui.QPainter(canvas)
    painter.drawImage(24, 24, image)
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not canvas.save(buffer, "PNG"):
        raise ValueError("Could not encode nickname strip")
    return base64.b64encode(bytes(buffer.data()))


def _read_names(strips: list[QtGui.QImage]) -> list[str]:
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = base64.b64encode(_OCR_SCRIPT.encode("utf-16-le")).decode("ascii")
    result = run_hidden(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        timeout=7, input=b"\n".join(_encode_strip(strip) for strip in strips) + b"\n",
    )
    if result.returncode:
        return []
    names = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(names, list) or len(names) != len(strips):
        return []
    return [" ".join(name.split()) if isinstance(name, str) else "" for name in names]


def _decode_rows(captures) -> list[dict]:
    if not captures:
        return []
    try:
        names = _read_names([label for _, label in captures])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [{"sender": name, "avatar": photo} for (photo, _), name in zip(captures, names)
            if 0 < len(name) <= 40 and name not in {"微信", "暂不处理", "新消息"}]


def read_preview(hwnd: int) -> list[dict]:
    """Keep a row only if both the photo and its own nickname were read."""
    return _decode_rows(_capture_rows(hwnd))


def read_automatic_preview(flash_hwnd: int, exe_of_hwnd, cancelled) -> list[dict]:
    """Copy the tray rows during the hover and run OCR after restoring the pointer."""
    from .wechat_tray_hover import hover_tray

    if cancelled() or visible_popup(exe_of_hwnd):
        return []
    captures = []
    with hover_tray(flash_hwnd) as pid:
        if not pid:
            return []
        deadline = time.monotonic() + 0.36
        while not cancelled() and time.monotonic() < deadline:
            hwnd = visible_popup(exe_of_hwnd, pid)
            if hwnd:
                captures = _capture_rows(hwnd)
                if captures:
                    break
            time.sleep(0.025)
    return [] if cancelled() else _decode_rows(captures)
