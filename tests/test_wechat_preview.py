"""Exercise automatic preview scheduling and capture without Qt or desktop input."""
from contextlib import contextmanager
import ctypes
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


class _Signal:
    def __init__(self, *_args):
        pass

    def __set_name__(self, _owner, name):
        self._key = "_fixture_signal_" + name

    def __get__(self, instance, _owner):
        if instance is None:
            return self
        if self._key not in instance.__dict__:
            instance.__dict__[self._key] = mock.Mock()
        return instance.__dict__[self._key]


class _Object:
    def __init__(self, _parent):
        pass


def _load_fixture(module, suffix):
    name = f"aisland.services._{module}_{suffix}_fixture"
    spec = importlib.util.spec_from_file_location(name, PROJECT / "aisland" / "services" / f"{module}.py")
    fixture = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {name: fixture}):
        spec.loader.exec_module(fixture)
    return fixture


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "notify.wechat_auto_preview": True,
            "notify.flash_processes": {"weixin.exe": {"kind": "wechat", "name": "微信"}},
        }
        self.hover = SimpleNamespace(mouse_is_idle=mock.Mock(return_value=False), hover_tray=mock.Mock())
        self.jobs = mock.Mock()
        imports = {
            "PySide6": SimpleNamespace(QtCore=SimpleNamespace(QObject=_Object, Signal=_Signal, QTimer=mock.Mock()),
                                       QtGui=SimpleNamespace()),
            "aisland.core.bg": SimpleNamespace(ui=mock.Mock(), run_bg=self.jobs,
                                               launch=mock.Mock(), run_hidden=mock.Mock()),
            "aisland.core.config": SimpleNamespace(cfg=SimpleNamespace(get=self.settings.get), expand=mock.Mock()),
            "aisland.services.wechat_tray_hover": self.hover,
            "aisland.ui.glass": SimpleNamespace(grab_tiny=mock.Mock()),
        }
        self.native_modules = mock.patch.dict(sys.modules, imports)
        self.native_modules.start()
        self.addCleanup(self.native_modules.stop)
        with mock.patch.object(ctypes, "WinDLL", create=True):
            self.preview = _load_fixture("wechat_preview", "capture")
            with mock.patch.dict(sys.modules, {"aisland.services.wechat_preview": self.preview}):
                self.notify = _load_fixture("notify", "service")
        self.notify.wechat_preview = self.preview
        self.notify.exe_of_hwnd = mock.Mock(return_value="weixin.exe")
        self.notify.time = SimpleNamespace(time=mock.Mock(return_value=100.0),
                                           monotonic=mock.Mock(return_value=10.0))
        self.preview.time = SimpleNamespace(monotonic=mock.Mock(return_value=10.0), sleep=mock.Mock())
        self.preview.visible_popup = mock.Mock(return_value=0)
        self.service = self.notify.NotifyService()
        self.service._running = True
        self.contact = {"sender": "测试联系人", "avatar": object()}

    def _dispatch(self):
        self.service.on_flash(42)
        self.hover.mouse_is_idle.return_value = True
        self.service._watch_wechat_preview()
        self.jobs.assert_called_once()
        return self.jobs.call_args

    def test_waits_for_idle_then_requests_once_without_counting_again(self):
        self.service.on_flash(42)
        self.service.on_flash(42)
        self.service._watch_wechat_preview()
        self.jobs.assert_not_called()
        self.assertEqual(self.service.counts, {"wechat": 1})

        self.hover.mouse_is_idle.return_value = True
        self.service._watch_wechat_preview()
        self.service._watch_wechat_preview()
        self.jobs.assert_called_once()
        args, callbacks = self.jobs.call_args
        self.assertIs(args[0], self.preview.read_automatic_preview)
        self.assertEqual(args[1], 42)
        callbacks["on_done"]([self.contact])

        self.service._watch_wechat_preview()
        self.jobs.assert_called_once()
        self.assertEqual(self.service.counts, {"wechat": 1})
        self.assertEqual(len(self.service.history), 1)
        self.assertEqual(self.service.wechat_preview[0]["flash_time"], self.service.history[0]["time"])
        self.assertEqual(self.service.wechat_preview[0]["sender"], self.contact["sender"])
        self.service.counts_changed.emit.assert_called_once_with({"wechat": 1})
        self.service.wechat_preview_changed.emit.assert_called_once_with()

    def test_clear_rejects_in_flight_capture(self):
        args, callbacks = self._dispatch()
        self.assertFalse(args[3]())
        self.service.clear("wechat")
        self.assertTrue(args[3]())
        callbacks["on_done"]([self.contact])

        self.assertEqual(self.service.wechat_preview, [])
        self.assertEqual(self.service.counts, {"wechat": 0})

    def test_real_toast_supersedes_in_flight_capture(self):
        args, callbacks = self._dispatch()
        self.service._emit({"kind": "wechat", "name": "微信", "source": "toast",
                            "time": 101.0, "sender": "来自通知的联系人"})
        self.assertTrue(args[3]())
        callbacks["on_done"]([self.contact])

        self.assertEqual(self.service.wechat_preview, [])
        self.assertEqual(self.service.counts, {"wechat": 1})
        self.assertEqual(self.service.history[0]["sender"], "来自通知的联系人")

    def test_disabling_or_expiring_request_keeps_plain_alert(self):
        self.service.on_flash(42)
        self.notify.time.monotonic.return_value = 10.0 + self.notify.WECHAT_HOVER_WINDOW_S
        self.hover.mouse_is_idle.return_value = True
        self.service._watch_wechat_preview()
        self.jobs.assert_not_called()

        self.settings["notify.wechat_auto_preview"] = False
        self.notify.time.time.return_value = 200.0
        self.service.on_flash(42)
        self.service._watch_wechat_preview()
        self.jobs.assert_not_called()
        self.assertEqual(self.service.counts, {"wechat": 2})
        self.assertEqual(self.service.wechat_preview, [])

    def test_crops_while_hovered_and_recognizes_after_pointer_is_restored(self):
        events = []
        crop = object()

        @contextmanager
        def hover_scope(hwnd):
            events.append(("hover", hwnd))
            try:
                yield 123
            finally:
                events.append(("restore", hwnd))

        def capture(hwnd):
            events.append(("crop", hwnd))
            return [(self.contact["avatar"], crop)]

        def recognize(strips):
            self.assertEqual(strips, [crop])
            events.append(("OCR",))
            return [self.contact["sender"]]

        self.hover.hover_tray.side_effect = hover_scope
        self.preview.visible_popup.side_effect = [0, 84]
        self.preview._capture_rows = mock.Mock(side_effect=capture)
        self.preview._read_names = mock.Mock(side_effect=recognize)

        rows = self.preview.read_automatic_preview(42, self.notify.exe_of_hwnd, lambda: False)

        self.assertEqual(rows, [self.contact])
        self.preview.visible_popup.assert_called_with(self.notify.exe_of_hwnd, 123)
        self.assertEqual(events, [("hover", 42), ("crop", 84), ("restore", 42), ("OCR",)])

    def test_existing_native_popup_prevents_pointer_move(self):
        self.preview.visible_popup.return_value = 84
        self.preview._capture_rows = mock.Mock()
        self.preview._read_names = mock.Mock()

        self.assertEqual(self.preview.read_automatic_preview(42, self.notify.exe_of_hwnd, lambda: False), [])
        self.hover.hover_tray.assert_not_called()
        self.preview._capture_rows.assert_not_called()
        self.preview._read_names.assert_not_called()


class TrayHoverTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(ctypes, "WinDLL", create=True):
            self.hover = _load_fixture("wechat_tray_hover", "native")
        self.origin = (80, 60)
        self.target = self.hover._TrayTarget(99, 123, (300, 600))
        self.cursor = self.origin
        self.now = 10_000
        self.last_input = self.now - 2_500
        self.hover._find_target = mock.Mock(return_value=self.target)
        self.hover._is_tray_window = mock.Mock(return_value=True)
        self.hover._on_shell_tray = mock.Mock(return_value=True)
        self.hover._clip_contains = mock.Mock(return_value=True)
        self.hover._buttons_down = mock.Mock(return_value=False)
        self.hover._gesture_active = mock.Mock(return_value=False)
        self.hover._set_thread_dpi = mock.Mock(return_value=ctypes.c_void_p(12))
        self.hover._cursor_position = mock.Mock(side_effect=lambda: self.cursor)
        self.hover._last_input_tick = mock.Mock(side_effect=lambda: self.last_input)
        self.hover._kernel32.GetTickCount.side_effect = lambda: self.now
        self.move = mock.Mock(side_effect=self._move_cursor)
        self.hover._user32.SetPhysicalCursorPos = self.move
        self.hover._user32.PostMessageW.return_value = True
        self.timer_factory = mock.Mock()
        self.hover.threading = SimpleNamespace(Lock=threading.Lock, Timer=self.timer_factory)

    def _move_cursor(self, x, y):
        self.cursor = (x, y)
        self.last_input = self.now
        return True

    def test_cursor_move_can_update_input_tick_and_restore_on_exit(self):
        with self.hover.hover_tray(42) as pid:
            self.assertEqual(pid, self.target.pid)
            self.assertEqual(self.cursor, self.target.point)
            self.assertEqual(self.last_input, self.now)
            self.timer_factory.return_value.start.assert_called_once_with()

        self.timer_factory.return_value.cancel.assert_called_once_with()
        self.assertEqual(self.cursor, self.origin)
        self.assertEqual(self.move.call_args_list, [mock.call(*self.target.point), mock.call(*self.origin)])

    def test_watchdog_restores_before_capture_finishes_only_once(self):
        with self.hover.hover_tray(42) as pid:
            self.assertEqual(pid, self.target.pid)
            interval, watchdog = self.timer_factory.call_args.args
            self.assertLessEqual(interval, 0.5)
            watchdog()
            self.assertEqual(self.cursor, self.origin)
            watchdog()

        self.assertEqual(self.move.call_args_list, [mock.call(*self.target.point), mock.call(*self.origin)])

    def test_new_user_input_leaves_cursor_under_user_control(self):
        with self.hover.hover_tray(42) as pid:
            self.assertEqual(pid, self.target.pid)
            self.cursor = (111, 77)
            self.last_input = self.now + 7
            self.timer_factory.call_args.args[1]()

        self.move.assert_called_once_with(*self.target.point)
        self.assertEqual(self.cursor, (111, 77))
