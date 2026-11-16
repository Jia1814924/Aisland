from ctypes import wintypes
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets
from shiboken6 import delete

from aisland.services import media, system
from aisland.ui.compact import CompactView
from aisland.ui.displays import DisplayIslands

MAIN = (0, 0)
SIDE = (-2560, 0)


class AppStub(QtCore.QObject):
    screenAdded = QtCore.Signal(object)
    screenRemoved = QtCore.Signal(object)
    primaryScreenChanged = QtCore.Signal(object)
    activeWindow = activeModalWidget = activePopupWidget = lambda self: None

    def __init__(self):
        super().__init__()
        self.main, self.side = Mock(), Mock()
        self.main.geometry.return_value = QtCore.QRect(*MAIN, 1920, 1280)
        self.side.geometry.return_value = QtCore.QRect(*SIDE, 2048, 1280)

    def screens(self):
        return [self.main, self.side]

    def primaryScreen(self):
        return self.main

    def screenAt(self, _position):
        return self.main


class DisplayChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication([])

    def test_close_retires_every_window_if_screen_signal_is_already_destroying(self):
        from shiboken6 import isValid

        displays = DisplayIslands(self.app, SimpleNamespace(), lambda _screen: None)
        screens = [Mock(), Mock()]
        windows = [QtWidgets.QWidget(), QtWidgets.QWidget()]
        for screen, window in zip(screens, windows):
            window.dispose = Mock(side_effect=window.deleteLater)
            displays.views[screen] = SimpleNamespace(island=window, screen_destroyed=lambda: None)
        screens[0].destroyed.disconnect.side_effect = RuntimeError("Signal source has been deleted")
        displays.primary = displays.views[screens[0]]
        displays.caption = displays.views[screens[1]]
        displays.timer.start()
        with patch("aisland.ui.displays.isValid", return_value=True):
            displays.close()
        self.assertFalse(displays.timer.isActive())
        self.assertFalse(displays.views)
        self.assertIsNone(displays.primary)
        self.assertIsNone(displays.caption)
        for window in windows:
            window.dispose.assert_called_once()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertTrue(all(not isValid(window) for window in windows))
        self.assertFalse(displays._retiring)

    def test_foreground_executable_ignores_own_process_and_caches_external_lookup(self):
        win = system.WindowService()
        foreground = [1]
        pids = {1: 101, 2: win._own_pid, 3: 0}
        native = SimpleNamespace(GetForegroundWindow=lambda: foreground[0])
        with (patch.object(system, "_u32", native),
              patch.object(system, "_pid", side_effect=pids.__getitem__),
              patch.object(system, "_exe_path", return_value=r"C:\Apps\WorkBuddy.EXE") as lookup):
            self.assertEqual(win.foreground_executable(), "workbuddy.exe")
            self.assertEqual(win.foreground_executable(), "workbuddy.exe")
            lookup.assert_called_once_with(101)
            for foreground[0] in (0, 2, 3):
                self.assertEqual(win.foreground_executable(), "")

    def test_media_snapshots_keep_browser_and_explicit_video(self):
        def session(app_id, status, kind):
            return SimpleNamespace(source_app_user_model_id=app_id,
                                   get_playback_info=lambda: SimpleNamespace(playback_status=status, playback_type=kind))

        chrome = session("Chrome", 4, media.MediaPlaybackType.MUSIC)
        player = session("VideoPlayer", 4, media.MediaPlaybackType.VIDEO)
        paused = session("Paused", 5, media.MediaPlaybackType.VIDEO)
        _, playing, videos = media.MediaService()._inspect_sessions([chrome, player, paused])
        self.assertEqual(playing, {"chrome", "videoplayer"})
        self.assertEqual(videos, {"videoplayer"})

    def test_native_window_match_tracks_title_and_display(self):
        win = system.WindowService()
        foreground = [1]
        # Two Chrome windows, plus maximized Office on the primary screen.
        monitors = {1: wintypes.RECT(0, 0, 2880, 1920), 2: wintypes.RECT(-2560, 0, 0, 1600)}
        window_monitor = {1: 1, 2: 2, 3: 1}
        rects = {1: monitors[1], 2: wintypes.RECT(-2300, 150, -500, 1300), 3: wintypes.RECT(40, 60, 1100, 850)}
        styles = {1: system.WS_MAXIMIZE, 2: 0, 3: 0}
        paths = {101: r"C:\Office\WINWORD.EXE", 102: r"C:\Chrome\chrome.exe"}

        def enum_windows(visit, _param):
            for hwnd in (1, 3, 2):
                visit(hwnd, 0)

        def get_rect(hwnd, output):
            rect = rects[hwnd]
            output._obj.left, output._obj.top = rect.left, rect.top
            output._obj.right, output._obj.bottom = rect.right, rect.bottom
            return True

        def get_monitor(handle, output):
            output._obj.rcMonitor = monitors[handle]
            return True

        native = SimpleNamespace(
            GetForegroundWindow=lambda: foreground[0], IsWindowVisible=lambda hwnd: True,
            IsIconic=lambda hwnd: False, GetWindow=lambda hwnd, flag: 0,
            GetWindowLongPtrW=lambda hwnd, flag: styles[hwnd] if flag == system.GWL_STYLE else 0,
            MonitorFromWindow=lambda hwnd, flag: window_monitor[hwnd],
            GetWindowRect=get_rect, GetMonitorInfoW=get_monitor, EnumWindows=enum_windows)
        chrome = frozenset({"chrome"})
        with (patch.object(system, "_u32", native),
              patch.object(system, "_pid", side_effect=lambda hwnd: 101 if hwnd == 1 else 102),
              patch.object(system, "_class", return_value="App"),
              patch.object(system, "_cloaked", return_value=False),
              patch.object(system, "_exe_path", side_effect=paths.__getitem__),
              patch.object(system, "_process_app_id", return_value=""),
              patch.object(system, "_title", side_effect={1: "Word", 2: "Clip_bilibili - Chrome", 3: "New tab - Chrome"}.__getitem__)):
            self.assertIsNone(win.foreground_fullscreen_video_monitor(chrome))
            self.assertEqual(win.playing_media_monitor(chrome, ("chrome", "Clip"), MAIN), SIDE)
            foreground[0] = 3
            self.assertEqual(win.playing_media_monitor(chrome, ("chrome", "Clip"), MAIN), SIDE)
            self.assertEqual(win.playing_media_monitor(chrome, ("", ""), SIDE), SIDE)
            window_monitor[2] = 1
            rects[2] = monitors[1]
            foreground[0] = 2
            self.assertEqual(win.foreground_fullscreen_video_monitor(chrome), MAIN)
            self.assertEqual(win.playing_media_monitor(chrome, ("chrome", "Clip"), SIDE), MAIN)
            styles[2] = system.WS_MAXIMIZE
            self.assertIsNone(win.foreground_fullscreen_video_monitor(chrome))

    def test_only_playback_island_gets_captions_and_survives_unplug(self):
        screens = AppStub()
        ctx = SimpleNamespace(media=SimpleNamespace(playback_apps=(frozenset({"chrome"}), frozenset()),
                                                   state={"app_id": "Chrome", "title": "Clip"}),
                              windows=Mock())
        ctx.windows.foreground_fullscreen_video_monitor.return_value = SIDE

        def view_for(screen):
            island = Mock(scr=screen, compact=CompactView(), expanded=False, locks=set())
            island.winId.return_value = 1 if screen is screens.main else 2
            return SimpleNamespace(island=island, pages={"trans": Mock()})

        displays = DisplayIslands(screens, ctx, view_for)
        self.addCleanup(displays.close)
        displays.open()
        main, side = displays.views.values()
        main.island.set_native_listener.assert_called_once_with(True)
        side.island.set_native_listener.assert_not_called()
        displays.update_layer()
        main.island.set_fullscreen_hidden.assert_called_with(False)
        side.island.set_fullscreen_hidden.assert_called_with(True)

        with patch("aisland.ui.displays.bg.run_bg", return_value=object()) as scan:
            displays.set_listening(True)
            displays.set_subtitle(jp="字幕", zh="实时字幕")
            displays.update_layer()
            displays.update_layer()
            scan.assert_called_once()
            scan.call_args.kwargs["on_done"](SIDE)
            side.island.set_fullscreen_hidden.assert_called_with(False)
            self.assertFalse(main.island.compact.sub_on)
            self.assertEqual(side.island.compact.sub_zh, "实时字幕")
            main.pages["trans"].set_live_display.assert_called_with(False)
            side.pages["trans"].set_live_display.assert_called_with(True)
            displays.remove_screen(screens.side)
            self.assertIs(displays.caption, main)
            self.assertEqual(main.island.compact.sub_jp, "字幕")
            side.island.dispose.assert_called_once()
            displays.set_listening(False)
            self.assertFalse(main.island.compact.sub_on)
            self.assertIsNone(displays.caption)

    def test_dual_screen_idle_views_follow_pointer_and_single_screen_cycles(self):
        screens = AppStub()
        ctx = SimpleNamespace()

        def view_for(screen):
            island = Mock(scr=screen, compact=CompactView(), expanded=False)
            island.passthrough.return_value = False
            return SimpleNamespace(island=island, pages={"trans": Mock()})

        displays = DisplayIslands(screens, ctx, view_for)
        self.addCleanup(displays.close)
        displays.open()
        main, side = displays.views.values()
        self.assertEqual((main.island.compact.view, side.island.compact.view), ("pet", "clock"))
        screens.screenAt = lambda _point: screens.side
        displays.update_idle_views()
        self.assertEqual((main.island.compact.view, side.island.compact.view), ("clock", "pet"))
        displays.cycle_idle_views()
        self.assertEqual((main.island.compact.view, side.island.compact.view), ("clock", "pet"))
        side.island.compact.set_view("music")
        displays.update_idle_views()
        self.assertEqual(side.island.compact.view, "music")
        displays.remove_screen(screens.side)
        displays.cycle_idle_views()
        self.assertEqual(main.island.compact.view, "pet")
        displays.cycle_idle_views()
        self.assertEqual(main.island.compact.view, "clock")

    def test_destroyed_screen_is_removed_without_screen_removed_signal(self):
        screens = AppStub()
        screens.side = QtCore.QObject()
        screens.side.geometry = lambda: QtCore.QRect(*SIDE, 2048, 1280)
        ctx = SimpleNamespace(media=SimpleNamespace(playback_apps=(frozenset(), frozenset())),
                              windows=Mock())

        def view_for(screen):
            island = Mock(scr=screen, compact=CompactView(), expanded=False, locks=set())
            island.winId.return_value = 1
            return SimpleNamespace(island=island, pages={"trans": Mock()})

        displays = DisplayIslands(screens, ctx, view_for)
        self.addCleanup(displays.close)
        displays.open()
        old_side = displays.views[screens.side]
        displays.set_listening(True)
        displays._set_caption(old_side)
        displays.set_subtitle(jp="字幕", zh="译文")
        delete(screens.side)
        self.assertEqual(len(displays.views), 1)
        self.assertIs(displays.caption, displays.views[screens.main])
        self.assertEqual(displays.caption.island.compact.sub_zh, "译文")
        displays.update_layer()
        old_side.island.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
