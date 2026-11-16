"""Lyrics behavior with original fixture text; no Qt, network or user media."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]


class _BoundSignal:
    def __init__(self):
        self.events = []
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, value):
        self.events.append(value)
        for callback in self.callbacks:
            callback(value)


class _Signal:
    def __init__(self, *_args):
        self.name = ""

    def __set_name__(self, _owner, name):
        self.name = "_signal_" + name

    def __get__(self, instance, _owner):
        if instance is None:
            return self
        return instance.__dict__.setdefault(self.name, _BoundSignal())


class LyricsServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"media.lyrics": True, "media.lyrics_online": True, "media.music_dirs": []}
        self.jobs = []
        self.client = mock.Mock()
        fake_http = types.ModuleType("httpx")
        fake_http.Client = mock.Mock(return_value=self.client)
        fake_http.Timeout = mock.Mock()
        qt = types.ModuleType("PySide6")
        class QObject:
            def __init__(self, *_args):
                pass
        qt.QtCore = types.SimpleNamespace(QObject=QObject, Signal=_Signal)
        background = types.ModuleType("aisland.core.bg")
        background.ui = lambda fn: fn()
        background.run_bg = lambda fn, *args, **kwargs: self.jobs.append((fn, args, kwargs))
        config = types.ModuleType("aisland.core.config")
        config.cfg = types.SimpleNamespace(get=lambda key, default=None: self.settings.get(key, default))
        embedded = types.ModuleType("aisland.services.embedded_lyrics")
        embedded.find_lyrics = mock.Mock(return_value=None)
        embedded.clear_cache = mock.Mock()
        name = "aisland.services._lyrics_service_test_fixture"
        spec = importlib.util.spec_from_file_location(name, PROJECT / "aisland" / "services" / "lyrics.py")
        self.module = importlib.util.module_from_spec(spec)
        imports = {"httpx": fake_http, "PySide6": qt, "aisland.core.bg": background,
                   "aisland.core.config": config, "aisland.services.embedded_lyrics": embedded,
                   name: self.module}
        with mock.patch.dict(sys.modules, imports):
            spec.loader.exec_module(self.module)
        self.clock = mock.patch.object(self.module.time, "monotonic", return_value=100.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.service = self.module.LyricsService()
        self.media = {"app_id": "QQMusic.exe", "source": "smtc", "title": "Fictional Sunrise",
                      "artist": "Fixture Singer", "duration": 0}

    def response(self, body):
        response = mock.Mock()
        response.json.return_value = body
        return response

    def complete_job(self, index=0):
        fn, args, options = self.jobs[index]
        try:
            result = fn(*args)
        except Exception as error:
            options["on_error"](error)
        else:
            options["on_done"](result)

    def embedded(self, *, plain=False):
        return {"lines": [] if plain else [[1.0, "Original fixture dawn"], [2.0, "Original fixture day"]],
                "plain_text": "Original untimed fixture verse" if plain else "",
                "source_path": "X:/fixture-only/fictional.flac",
                "directory_signature": "fixture-directory", "file_signature": "fixture-file"}

    def test_enhanced_lrc_offset_multiple_timestamps_and_legacy_translation(self):
        lines = self.module.parse_lrc("[offset:+500]\n[00:01.25][00:02.50]<00:01.25>Original <00:01.60>fixture\n")
        self.assertEqual(lines, [(1.75, "Original fixture"), (3.0, "Original fixture")])
        self.assertEqual(self.module.parse_lrc("[offset:-2500]\n[00:01]Original fixture"), [(0.0, "Original fixture")])
        lyrics = self.module.Lyrics(lines, [(1.75, "原创测试译文")])
        self.assertEqual(lyrics.at(1.8), (0, "Original fixture", "原创测试译文"))
        self.assertEqual(lyrics.at(0), (-1, "", ""))

    def test_plain_text_is_available_without_fake_timestamps(self):
        self.module.find_lyrics.return_value = self.embedded(plain=True)
        self.service.get(self.media)
        self.complete_job()
        lyrics = self.service.get(self.media)
        self.assertTrue(lyrics)
        self.assertFalse(lyrics.synced)
        self.assertEqual(lyrics.lines, [])
        self.assertEqual(lyrics.at(9999), (-1, "", ""))
        self.assertEqual(self.service.status(self.media), "plain")
        self.assertEqual(lyrics.source, "embedded")
        self.client.get.assert_not_called()

    def test_embedded_has_priority_and_only_configured_roots_are_passed(self):
        self.settings["media.music_dirs"] = ["X:/fixture-only", None]
        self.module.find_lyrics.return_value = self.embedded()
        self.assertIsNone(self.service.get(self.media))
        self.assertEqual(self.service.status(self.media), "loading")
        self.complete_job()
        self.module.find_lyrics.assert_called_once_with(self.media, ["X:/fixture-only"])
        lyrics = self.service.get(self.media)
        self.assertTrue(lyrics.synced)
        self.assertEqual(self.service.status(self.media), "synced")
        self.assertNotIn("fixture-only", self.service.ready.events[0])
        self.client.get.assert_not_called()

    def test_unknown_duration_requires_exact_title_and_artist(self):
        songs = [{"id": 11, "name": "Fictional Sunrise", "artists": [{"name": "Different Fixture Singer"}]},
                 {"id": 12, "name": "Fictional Sunrise (Live)", "artists": [{"name": "Fixture Singer"}]},
                 {"id": 13, "name": "Fictional Sunrise", "artists": [{"name": "Fixture Singer"}]}]
        self.client.get.return_value = self.response({"result": {"songs": songs}})
        self.assertEqual(self.service._search(self.media), "13")
        self.client.get.return_value = self.response({"result": {"songs": songs[:2]}})
        self.assertEqual(self.service._search(self.media), "")

    def test_known_duration_enforces_tolerance_before_accepting_song(self):
        self.client.get.return_value = self.response({"result": {"songs": [
            {"id": 11, "name": "Fictional Sunrise", "duration": 220000, "artists": [{"name": "Fixture Singer"}]},
            {"id": 12, "name": "Fictional Sunrise", "duration": 205000, "artists": [{"name": "Fixture Singer"}]}]}})
        self.assertEqual(self.service._search({**self.media, "duration": 200}), "12")
        self.assertEqual(self.service._search({**self.media, "duration": 100}), "")

    def test_browser_video_missing_artist_and_invalid_duration_do_not_make_requests(self):
        cases = [{**self.media, "app_id": "chrome.exe", "song_id": "12"},
                 {**self.media, "app_id": "VLC.exe"},
                 {**self.media, "app_id": "PotPlayer.exe"},
                 {**self.media, "artist": ""}, {**self.media, "duration": 4000}]
        for media in cases:
            with self.subTest(app=media["app_id"], duration=media.get("duration")):
                self.assertEqual(self.service._search(media), "")
        for media in cases[:3]:
            self.assertIsNone(self.service._fetch(media).lyrics)
        self.client.get.assert_not_called()

    def test_search_non_object_result_and_malformed_song_fields_are_safe(self):
        cases = [{"code": 200, "result": "upstream-format-changed"}, [], {"result": None},
                 {"result": {"songs": "invalid"}}, {"result": {"songs": [None, "invalid", {"name": "Other"}]}}]
        for body in cases:
            with self.subTest(body=body):
                self.client.get.return_value = self.response(body)
                self.assertEqual(self.service._search(self.media), "")

    def test_direct_netease_id_retains_legacy_timed_lyrics(self):
        self.client.get.return_value = self.response({"lrc": {"lyric": "[00:01]Original fixture dawn"},
                                                     "tlyric": {"lyric": "[00:01]原创测试译文"}})
        result = self.service._fetch({**self.media, "source": "netease", "song_id": "123"})
        self.assertEqual(result.status, "synced")
        self.assertEqual(result.lyrics.source, "netease")
        self.assertEqual(result.lyrics.at(1), (0, "Original fixture dawn", "原创测试译文"))
        self.assertEqual(self.client.get.call_count, 1)

    def test_negative_cache_expires_but_ready_does_not_immediately_reschedule(self):
        self.client.get.return_value = self.response({"result": "upstream-format-changed"})
        self.service.ready.connect(lambda _key: self.service.get(self.media))
        self.service.get(self.media)
        self.service.get(self.media)
        self.assertEqual(len(self.jobs), 1)
        self.complete_job()
        self.assertEqual(self.service.status(self.media), "not_found")
        self.assertEqual(len(self.jobs), 1)
        self.now.return_value = 116.0
        self.service.get(self.media)
        self.assertEqual(len(self.jobs), 2)

    def test_network_error_expires_and_online_toggle_prevents_all_http(self):
        self.client.get.side_effect = OSError("fictional offline fixture")
        self.service.get(self.media)
        self.complete_job()
        self.assertEqual(self.service.status(self.media), "offline")
        self.now.return_value = 109.0
        self.service.get(self.media)
        self.assertEqual(len(self.jobs), 2)
        self.settings["media.lyrics_online"] = False
        self.client.get.reset_mock()
        result = self.service._fetch(self.media)
        self.assertEqual(result.status, "offline")
        self.client.get.assert_not_called()

    def test_stale_success_remains_visible_while_refresh_is_pending(self):
        self.module.find_lyrics.return_value = self.embedded()
        self.service.get(self.media)
        self.complete_job()
        previous = self.service.get(self.media)
        self.now.return_value = 116.0
        self.assertIs(self.service.get(self.media), previous)
        self.assertIs(self.service.get(self.media), previous)
        self.assertEqual(self.service.status(self.media), "synced")
        self.assertEqual(len(self.jobs), 2)

    def test_invalidate_drops_old_pending_result_and_restarts_with_new_roots(self):
        self.module.find_lyrics.return_value = self.embedded()
        self.service.get(self.media)
        self.settings["media.music_dirs"] = ["X:/new-fixture-only"]
        self.service.invalidate()
        self.module.clear_cache.assert_called_once_with()
        self.complete_job()
        self.assertEqual(self.service.ready.events, [])
        self.service.get(self.media)
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(self.jobs[1][1][1], ["X:/new-fixture-only"])

    def test_key_separates_apps_sources_duration_availability_and_local_files(self):
        key = self.service.key(self.media)
        self.assertEqual(key, self.service.key({**self.media, "position": 99}))
        for extra in ({"app_id": "Spotify.exe"}, {"source": "netease"}, {"duration": 200},
                      {"album": "Other Fixture Album"}, {"local_path": "X:/fixture-only/private.flac"}):
            self.assertNotEqual(key, self.service.key({**self.media, **extra}))
        self.assertEqual(self.service.key({**self.media, "duration": 200}),
                         self.service.key({**self.media, "duration": 201}))
        self.assertNotIn("private.flac", self.service.key({**self.media, "local_path": "X:/fixture-only/private.flac"}))

    def test_disabled_lyrics_never_dispatch_background_work(self):
        self.settings["media.lyrics"] = False
        self.assertIsNone(self.service.get(self.media))
        self.assertEqual(self.service.status(self.media), "disabled")
        self.assertEqual(self.jobs, [])
        self.module.find_lyrics.assert_not_called()
        self.client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
