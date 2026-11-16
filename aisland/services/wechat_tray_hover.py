"""Temporarily hover over the tray icon of the verified Weixin build."""
from __future__ import annotations

import ctypes
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes as W
from dataclasses import dataclass
from pathlib import Path


_TRAY_TITLE = "WxTrayIconMessageWindow"
_TRAY_CLASS = "WxTrayIconMessageWindowClass"
_TRAY_CALLBACK = 0x8385
_WM_MOUSEMOVE = 0x0200
_IDLE_MS = 2000
_MAX_HOVER_S = 0.5
_SHELL_TRAY_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
}
_MOUSE_AND_MODIFIER_KEYS = (0x01, 0x02, 0x04, 0x05, 0x06, 0x10, 0x11, 0x12, 0x5B, 0x5C)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("size", W.UINT), ("tick", W.DWORD)]


class _NotifyIconIdentifier(ctypes.Structure):
    _fields_ = [("size", W.DWORD), ("hwnd", W.HWND), ("icon_id", W.UINT), ("guid", W.BYTE * 16)]


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [("size", W.DWORD), ("flags", W.DWORD), ("active", W.HWND), ("focus", W.HWND),
                ("capture", W.HWND), ("menu_owner", W.HWND), ("move_size", W.HWND),
                ("caret", W.HWND), ("caret_rect", W.RECT)]


class _FixedFileInfo(ctypes.Structure):
    # Only the four leading members of VS_FIXEDFILEINFO are needed.
    _fields_ = [("signature", W.DWORD), ("struct_version", W.DWORD),
                ("file_version_ms", W.DWORD), ("file_version_ls", W.DWORD)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_version = ctypes.WinDLL("version", use_last_error=True)

_user32.FindWindowExW.argtypes = [W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR]
_user32.FindWindowExW.restype = W.HWND
_user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
_user32.GetWindowThreadProcessId.restype = W.DWORD
_user32.IsWindowVisible.argtypes = [W.HWND]
_user32.IsWindowVisible.restype = W.BOOL
_user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetAncestor.argtypes = [W.HWND, W.UINT]
_user32.GetAncestor.restype = W.HWND
_user32.WindowFromPoint.argtypes = [W.POINT]
_user32.WindowFromPoint.restype = W.HWND
_user32.GetClipCursor.argtypes = [ctypes.POINTER(W.RECT)]
_user32.GetClipCursor.restype = W.BOOL
_user32.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
_user32.GetPhysicalCursorPos.restype = W.BOOL
_user32.SetPhysicalCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetPhysicalCursorPos.restype = W.BOOL
_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
_user32.GetLastInputInfo.restype = W.BOOL
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = W.SHORT
_user32.GetGUIThreadInfo.argtypes = [W.DWORD, ctypes.POINTER(_GUIThreadInfo)]
_user32.GetGUIThreadInfo.restype = W.BOOL
_user32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
_user32.PostMessageW.restype = W.BOOL
_set_thread_dpi = getattr(_user32, "SetThreadDpiAwarenessContext", None)
if _set_thread_dpi is not None:
    _set_thread_dpi.argtypes = [W.HANDLE]
    _set_thread_dpi.restype = W.HANDLE

_kernel32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
_kernel32.OpenProcess.restype = W.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = W.BOOL
_kernel32.CloseHandle.argtypes = [W.HANDLE]
_kernel32.CloseHandle.restype = W.BOOL
_kernel32.GetTickCount.argtypes = []
_kernel32.GetTickCount.restype = W.DWORD

_shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(_NotifyIconIdentifier), ctypes.POINTER(W.RECT)]
_shell32.Shell_NotifyIconGetRect.restype = W.LONG
_version.GetFileVersionInfoSizeW.argtypes = [W.LPCWSTR, ctypes.POINTER(W.DWORD)]
_version.GetFileVersionInfoSizeW.restype = W.DWORD
_version.GetFileVersionInfoW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID]
_version.GetFileVersionInfoW.restype = W.BOOL
_version.VerQueryValueW.argtypes = [W.LPCVOID, W.LPCWSTR, ctypes.POINTER(W.LPVOID), ctypes.POINTER(W.UINT)]
_version.VerQueryValueW.restype = W.BOOL


def _window_pid(hwnd: int) -> int:
    pid = W.DWORD()
    return pid.value if _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) else 0


def _process_path(pid: int) -> Path | None:
    if not pid:
        return None
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        path = ctypes.create_unicode_buffer(1024)
        size = W.DWORD(len(path))
        if _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return Path(path.value)
        return None
    finally:
        _kernel32.CloseHandle(handle)


def _supported_version(path: Path) -> bool:
    size = _version.GetFileVersionInfoSizeW(str(path), None)
    if not 0 < size <= 1024 * 1024:
        return False
    data = ctypes.create_string_buffer(size)
    info = W.LPVOID()
    info_size = W.UINT()
    if (not _version.GetFileVersionInfoW(str(path), 0, size, data)
            or not _version.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(info_size))
            or not info or info_size.value < ctypes.sizeof(_FixedFileInfo)):
        return False
    fixed = ctypes.cast(info, ctypes.POINTER(_FixedFileInfo)).contents
    return (fixed.signature == 0xFEEF04BD
            and (fixed.file_version_ms, fixed.file_version_ls) == (0x00040002, 0x0000000D))


def _is_tray_window(hwnd: int, pid: int) -> bool:
    title = ctypes.create_unicode_buffer(len(_TRAY_TITLE) + 2)
    window_class = ctypes.create_unicode_buffer(len(_TRAY_CLASS) + 2)
    return (_window_pid(hwnd) == pid and not _user32.IsWindowVisible(hwnd)
            and _user32.GetClassNameW(hwnd, window_class, len(window_class)) == len(_TRAY_CLASS)
            and window_class.value == _TRAY_CLASS
            and _user32.GetWindowTextW(hwnd, title, len(title)) == len(_TRAY_TITLE)
            and title.value == _TRAY_TITLE)


def _tray_window(pid: int) -> int:
    matches = set()
    for parent in (None, W.HWND(-3)):  # top-level and message-only windows
        hwnd = None
        for _ in range(16):
            hwnd = _user32.FindWindowExW(parent, hwnd, _TRAY_CLASS, _TRAY_TITLE)
            if not hwnd:
                break
            if _is_tray_window(hwnd, pid):
                matches.add(hwnd)
        else:
            return 0  # not all matching windows could be checked
    return matches.pop() if len(matches) == 1 else 0


def _on_shell_tray(point: tuple[int, int]) -> bool:
    hit = _user32.WindowFromPoint(W.POINT(*point))
    root = _user32.GetAncestor(hit, 2) if hit else None  # GA_ROOT
    if not root or not _user32.IsWindowVisible(root):
        return False
    name = ctypes.create_unicode_buffer(80)
    if not _user32.GetClassNameW(root, name, len(name)) or name.value not in _SHELL_TRAY_CLASSES:
        return False
    path = _process_path(_window_pid(root))
    return path is not None and path.name.casefold() == "explorer.exe"


def _clip_contains(point: tuple[int, int]) -> bool:
    clip = W.RECT()
    x, y = point
    return (bool(_user32.GetClipCursor(ctypes.byref(clip)))
            and clip.left <= x < clip.right and clip.top <= y < clip.bottom)


@dataclass(frozen=True)
class _TrayTarget:
    hwnd: int
    pid: int
    point: tuple[int, int]


def _find_target(flash_hwnd: int) -> _TrayTarget | None:
    pid = _window_pid(flash_hwnd)
    path = _process_path(pid)
    if path is None or path.name.casefold() != "weixin.exe" or not _supported_version(path):
        return None
    hwnd = _tray_window(pid)
    if not hwnd:
        return None
    icon = _NotifyIconIdentifier(size=ctypes.sizeof(_NotifyIconIdentifier), hwnd=hwnd)
    rect = W.RECT()
    if (_shell32.Shell_NotifyIconGetRect(ctypes.byref(icon), ctypes.byref(rect)) != 0
            or rect.right <= rect.left or rect.bottom <= rect.top):
        return None
    point = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    if not all(-32768 <= coordinate <= 32767 for coordinate in point):
        return None
    return _TrayTarget(hwnd, pid, point)


def _cursor_position() -> tuple[int, int] | None:
    point = W.POINT()
    if _user32.GetPhysicalCursorPos(ctypes.byref(point)):
        return point.x, point.y
    return None


def _last_input_tick() -> int | None:
    info = _LastInputInfo(size=ctypes.sizeof(_LastInputInfo))
    return info.tick if _user32.GetLastInputInfo(ctypes.byref(info)) else None


def _buttons_down() -> bool:
    return any(_user32.GetAsyncKeyState(key) & 0x8000 for key in _MOUSE_AND_MODIFIER_KEYS)


def _gesture_active() -> bool:
    info = _GUIThreadInfo(size=ctypes.sizeof(_GUIThreadInfo))
    # Window move/size, menu, system menu, popup menu, or mouse capture.
    return (not _user32.GetGUIThreadInfo(0, ctypes.byref(info))
            or bool(info.flags & 0x1E or info.capture))


def _idle_tick() -> int | None:
    tick = _last_input_tick()
    if tick is None:
        return None
    elapsed = (_kernel32.GetTickCount() - tick) & 0xFFFFFFFF
    if not _IDLE_MS <= elapsed < 0x80000000 or _buttons_down() or _gesture_active():
        return None
    return tick


def mouse_is_idle() -> bool:
    """Return whether this session has had at least two seconds of idle input."""
    return _idle_tick() is not None


@contextmanager
def _physical_coordinates() -> Iterator[bool]:
    # Keep the tray rectangle, point hit-test and cursor clipping in the same
    # physical coordinate space even when the worker runs on a scaled monitor.
    previous = _set_thread_dpi(W.HANDLE(-4)) if _set_thread_dpi is not None else None
    try:
        yield bool(previous)
    finally:
        if previous:
            _set_thread_dpi(previous)


class _CursorMove:
    def __init__(self, original: tuple[int, int], point: tuple[int, int], input_tick: int):
        self.original = original
        self.point = point
        self.input_tick = input_tick
        self._guard = threading.Lock()
        self._finished = False

    def still_at_target(self) -> bool:
        return (_cursor_position() == self.point and not _buttons_down()
                and _last_input_tick() == self.input_tick)

    def restore(self) -> None:
        with self._guard:
            if self._finished:
                return
            self._finished = True
            with _physical_coordinates() as physical:
                if physical and _clip_contains(self.original) and self.still_at_target():
                    _user32.SetPhysicalCursorPos(*self.original)


_hover_lock = threading.Lock()


@contextmanager
def hover_tray(flash_hwnd: int) -> Iterator[int]:
    """Yield the verified Weixin PID while hovering, or zero if it is unsafe.

    Keep work inside the context brief. The cursor is independently restored
    after at most 500 ms when it still belongs to this operation.
    """
    if not _hover_lock.acquire(blocking=False):
        yield 0
        return

    move = None
    timer = None
    try:
        with _physical_coordinates() as physical:
            target = _find_target(flash_hwnd) if physical and mouse_is_idle() else None
            original = _cursor_position() if target else None
            tick = _idle_tick() if original is not None else None
            if (target is None or original is None or tick is None or original == target.point
                    or not _clip_contains(target.point) or not _is_tray_window(target.hwnd, target.pid)
                    or not _on_shell_tray(target.point) or _cursor_position() != original
                    or _buttons_down() or _gesture_active() or _last_input_tick() != tick):
                yield 0
                return

            move = _CursorMove(original, target.point, tick)
            if not _user32.SetPhysicalCursorPos(*target.point):
                move = None
                yield 0
                return

            # A programmatic cursor movement can itself advance the input
            # clock. Sample immediately before the independent restore timer.
            post_move_tick = _last_input_tick()
            if post_move_tick is not None:
                move.input_tick = post_move_tick
            timer = threading.Timer(_MAX_HOVER_S, move.restore)
            timer.daemon = True
            timer.start()
            if post_move_tick is None or not move.still_at_target():
                yield 0
                return

            x, y = target.point
            coordinates = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
            _user32.PostMessageW(target.hwnd, _TRAY_CALLBACK, coordinates, _WM_MOUSEMOVE)
            yield target.pid
    finally:
        try:
            if timer is not None:
                timer.cancel()
            if move is not None:
                move.restore()
        finally:
            _hover_lock.release()
