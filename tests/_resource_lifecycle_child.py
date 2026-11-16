from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtTest

from aisland.core.config import cfg
from aisland.learn.service import LearnService
from aisland.live import service
from aisland.live.segment import ClipHistory, MAX_RECENT_SEGMENTS
from aisland.services.media import COVER_EDGE, _decode_cover
from aisland.services.weather import WeatherService
from aisland.ui.glass import Backdrop


class ControlledPipeline(QtCore.QThread):
    sig_status = QtCore.Signal(str)
    sig_error = QtCore.Signal(str)
    sig_partial = QtCore.Signal(str)
    sig_segment = QtCore.Signal(object)
    sig_live_zh = QtCore.Signal(str, str)
    sig_final_zh = QtCore.Signal(str, str)
    sig_level = QtCore.Signal(float)

    def __init__(self, _cfg, *, clips):
        super().__init__()
        self.clips = clips
        self.stop_requested = Event()
        self.release_allowed = Event()

    def request_stop(self):
        self.stop_requested.set()

    def run(self):
        self.stop_requested.wait(3)
        self.release_allowed.wait(3)


class LifecycleChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtCore.QCoreApplication([])

    def make_live(self):
        live = service.LiveService()
        live.available = lambda: (True, "")
        loads = []

        def import_later(*_args, on_done, on_error):
            loads.append(on_done)
            return object()

        worker = patch.object(service, "run_bg", side_effect=import_later)
        worker.start()
        self.addCleanup(worker.stop)
        self.addCleanup(live.stop, wait=True)
        return live, loads

    def test_cancelled_import_does_not_start_a_thread(self):
        live, loads = self.make_live()
        live.start_listen()
        live.stop()
        loads.pop()(ControlledPipeline)
        self.assertFalse(live.running)
        self.assertIsNone(live.pipe)

    def test_restart_waits_for_previous_thread_and_keeps_audio(self):
        live, loads = self.make_live()
        received = []
        live.partial.connect(received.append)
        live.start_listen()
        loads.pop()(ControlledPipeline)
        old = live.pipe
        self.addCleanup(old.release_allowed.set)
        example = object()
        old.clips.add("last", example)
        live.stop()
        live.start_listen()
        self.assertIs(live.pipe, old)
        self.assertEqual(loads, [])
        old.sig_partial.emit("stale")
        old.release_allowed.set()
        for _ in range(80):
            if loads:
                break
            QtTest.QTest.qWait(15)
        self.assertEqual(received, [])
        self.assertTrue(old.isFinished())
        self.assertIsNone(live.pipe)
        self.assertIs(live.clip("last"), example)
        loads.pop()(ControlledPipeline)
        new = live.pipe
        self.addCleanup(new.release_allowed.set)
        self.assertIsNot(new, old)
        new.sig_partial.emit("fresh")
        QtTest.QTest.qWait(20)
        self.assertEqual(received, ["fresh"])
        live.clear_clips()
        self.assertIsNone(live.clip("last"))

    def test_recent_clip_limit_applies_across_sessions(self):
        clips = ClipHistory()
        for i in range(MAX_RECENT_SEGMENTS + 1):
            clips.add(str(i), i)
        self.assertIsNone(clips.get("0"))
        self.assertEqual(clips.get("1"), 1)

    def test_learning_starts_without_opening_dictionary_or_loading_audio(self):
        learn = LearnService()
        learn.start()
        self.addCleanup(learn.stop)
        for name in ("numpy", "fugashi", "aisland.learn.dictionary", "aisland.learn.wordbook", "aisland.live.pipeline"):
            self.assertNotIn(name, sys.modules)

    def test_cover_is_decoded_at_display_size(self):
        source = QtGui.QImage(1200, 600, QtGui.QImage.Format.Format_RGB32)
        source.fill(QtGui.QColor("#3366aa"))
        encoded = QtCore.QBuffer()
        encoded.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(source.save(encoded, "PNG"))
        cover = _decode_cover(bytes(encoded.data()))
        self.assertEqual((cover.width(), cover.height()), (COVER_EDGE, COVER_EDGE // 2))
        self.assertIsNone(_decode_cover(b"not an image"))

    def test_backdrop_waits_for_first_capture_request(self):
        backdrop = Backdrop()
        with patch.object(backdrop._wake, "wait", side_effect=lambda _: backdrop.stop()) as wait:
            backdrop._run()
        wait.assert_called_once_with(None)

    def test_weather_restarts_timer_with_valid_interval(self):
        weather = WeatherService()
        self.addCleanup(weather._client.close)
        self.addCleanup(weather.stop)
        with patch.object(weather, "refresh") as refresh, patch.object(cfg, "get") as get:
            for minutes, expected_ms in ((15, 900_000), (5, 300_000), (float("inf"), 2_147_483_647)):
                get.return_value = minutes
                weather.start()
                self.assertTrue(weather._timer.isActive())
                self.assertEqual(weather._timer.interval(), expected_ms)
        self.assertEqual(refresh.call_count, 3)

    def test_wordbook_can_save_example_audio(self):
        import numpy as np
        from aisland.learn import wordbook

        book = wordbook.get()
        card_id = book.add("試験", "しけん", "测试", audio=np.full(16000, 0.1, dtype=np.float32))
        card = book.get(card_id)
        self.assertTrue(card.audio)
        with wave.open(str(wordbook.MEDIA_DIR / card.audio), "rb") as clip:
            self.assertEqual((clip.getframerate(), clip.getnchannels(), clip.getnframes()), (16000, 1, 16000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
