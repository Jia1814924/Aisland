"""Pending music-folder setup checks using fictional paths and temporary files."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from aisland.core.music_setup import apply_pending_music_dirs


class ConfigFixture:
    def __init__(self, directories=None):
        self.data = {"media.music_dirs": directories if directories is not None else [],
                     "translate.api_key": "FICTIONAL_UNRELATED_KEY", "island.theme": "sky"}
        self.saved = copy.deepcopy(self.data)
        self.set_calls = []
        self.fail_next_save = False

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, save=True):
        self.set_calls.append((key, copy.deepcopy(value), save))
        self.data[key] = copy.deepcopy(value)
        if save and self.fail_next_save:
            self.fail_next_save = False
            raise OSError("fictional save failure")
        if save:
            self.saved = copy.deepcopy(self.data)


class MusicSetupTests(unittest.TestCase):
    def setUp(self):
        area = PROJECT / "build" / "music-setup-tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "pending_music_dirs.json"

    def request(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return self.path

    def test_missing_request_does_not_change_configuration(self):
        config = ConfigFixture([r"C:\Existing Music"])
        before = copy.deepcopy(config.data)
        self.assertFalse(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.data, before)
        self.assertEqual(config.set_calls, [])

    def test_merge_and_case_insensitive_dedup_preserve_existing_directories(self):
        config = ConfigFixture([r"C:\Existing Music", "D:/Library/"])
        self.request({"music_dirs": ["c:\\existing music\\", r"D:\Library", r"E:\中文歌曲", r"E:\中文歌曲"]})
        self.assertTrue(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.get("media.music_dirs"), [r"C:\Existing Music", "D:/Library/", r"E:\中文歌曲"])
        self.assertEqual(config.saved, config.data)
        self.assertEqual(config.get("translate.api_key"), "FICTIONAL_UNRELATED_KEY")
        self.assertFalse(self.path.exists())

    def test_invalid_requests_are_not_partially_applied_or_deleted(self):
        invalid = [None, [], {}, {"music_dirs": "C:\\Music"}, {"music_dirs": [1]},
                   {"music_dirs": [""]}, {"music_dirs": ["relative-folder"]},
                   {"music_dirs": [r"C:relative"]}, {"music_dirs": [r"\\server\share\Music"]},
                   {"music_dirs": [r"\\?\C:\Music"]}, {"music_dirs": [r"\\.\C:\Music"]},
                   {"music_dirs": [r"C:\Music\..\Other"]}, {"music_dirs": ["C:\\Music\ncommand"]},
                   {"music_dirs": [r"C:\Music:stream"]}, {"music_dirs": [r'C:\Music\"command"']},
                   {"music_dirs": [r"C:\Music"], "translate": {"api_key": "DO_NOT_COPY"}}]
        for request in invalid:
            with self.subTest(request_type=type(request).__name__):
                config = ConfigFixture([r"D:\Existing"])
                before = copy.deepcopy(config.data)
                self.request(request)
                raw = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "^Invalid local music directory setup request$"):
                    apply_pending_music_dirs(config, self.path)
                self.assertEqual(config.data, before)
                self.assertEqual(config.set_calls, [])
                self.assertEqual(self.path.read_bytes(), raw)

    def test_valid_prefix_followed_by_invalid_path_does_not_partially_apply(self):
        config = ConfigFixture()
        self.request({"music_dirs": [r"C:\Valid", "relative"]})
        with self.assertRaises(ValueError):
            apply_pending_music_dirs(config, self.path)
        self.assertEqual(config.get("media.music_dirs"), [])
        self.assertTrue(self.path.exists())

    def test_save_failure_restores_memory_and_retains_request_for_retry(self):
        config = ConfigFixture([r"C:\Original"])
        before = copy.deepcopy(config.data)
        config.fail_next_save = True
        self.request({"music_dirs": [r"D:\New"]})
        self.assertFalse(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.data, before)
        self.assertEqual(config.saved, before)
        self.assertTrue(self.path.exists())
        self.assertFalse(config.set_calls[-1][2])
        self.assertTrue(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.get("media.music_dirs"), [r"C:\Original", r"D:\New"])

    def test_cleanup_failure_leaves_idempotent_request_after_successful_save(self):
        config = ConfigFixture()
        self.request({"music_dirs": [r"C:\Music"]})
        with patch.object(Path, "unlink", side_effect=OSError("fictional cleanup failure")):
            self.assertTrue(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.get("media.music_dirs"), [r"C:\Music"])
        self.assertTrue(self.path.exists())
        self.assertTrue(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.get("media.music_dirs"), [r"C:\Music"])
        self.assertFalse(self.path.exists())

    def test_only_exact_pending_file_is_removed(self):
        config = ConfigFixture()
        neighbor = self.root / "other-pending.json"
        neighbor.write_text("PRESERVE", encoding="utf-8")
        self.request({"music_dirs": []})
        self.assertTrue(apply_pending_music_dirs(config, self.path))
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "PRESERVE")

    def test_malformed_json_and_invalid_existing_setting_are_retained(self):
        config = ConfigFixture([r"C:\Existing"])
        self.path.write_text("invalid JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            apply_pending_music_dirs(config, self.path)
        self.assertTrue(self.path.exists())
        config.data["media.music_dirs"] = "invalid setting"
        self.request({"music_dirs": [r"D:\New"]})
        with self.assertRaises(ValueError):
            apply_pending_music_dirs(config, self.path)
        self.assertEqual(config.get("media.music_dirs"), "invalid setting")
        self.assertEqual(config.set_calls, [])

    def test_shell_punctuation_in_a_valid_folder_is_never_executed(self):
        config = ConfigFixture()
        path = r"C:\Music & this-is-only-a-folder-name"
        self.request({"music_dirs": [path]})
        self.assertTrue(apply_pending_music_dirs(config, self.path))
        self.assertEqual(config.get("media.music_dirs"), [path])


if __name__ == "__main__":
    unittest.main()
