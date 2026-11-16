"""Offline embedded-lyrics tests; all tags and files are fictional fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from aisland.services import embedded_lyrics as embedded


class ID3Tags(dict):
    def __init__(self, frames):
        super().__init__()
        self.frames = frames

    def getall(self, key):
        return self.frames.get(key, [])


class VorbisTags(dict):
    def __iter__(self):
        return iter(self.items())


def audio(tags, duration=180):
    return SimpleNamespace(tags=tags, info=SimpleNamespace(length=duration))


class EmbeddedLyricsTests(unittest.TestCase):
    def setUp(self):
        area = PROJECT / "build" / "embedded-lyrics-tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        embedded.clear_cache()
        self.addCleanup(embedded.clear_cache)

    def file(self, name, folder=None):
        path = (folder or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FICTIONAL_MUSIC_FIXTURE")
        return path

    def test_flac_enhanced_lrc_uses_keys_and_removes_word_timestamps(self):
        tags = VorbisTags({"LYRICS": ["[00:01.25]<00:01.25>First <00:01.75>line\n[00:03.00]Second line"],
                           "lyricist": ["Must never become lyrics"]})
        result = embedded.extract_lyrics(audio(tags))
        self.assertEqual(result["lines"], [[1.25, "First line"], [3.0, "Second line"]])
        self.assertNotIn("<00:", result["plain_text"])

    def test_id3_uslt_with_gbk_bytes_and_multiple_timestamps(self):
        tag = SimpleNamespace(text="[00:01.50][00:02.00]你好世界".encode("gbk"))
        result = embedded.extract_lyrics(audio(ID3Tags({"USLT": [tag]})))
        self.assertEqual(result["lines"], [[1.5, "你好世界"], [2.0, "你好世界"]])

    def test_lrc_offsets_and_same_time_translations(self):
        result = embedded.extract_lyrics(audio({"lyrics": ["[offset:+500]\n[00:01]Original\n[00:01]译文"]}))
        self.assertEqual(result["lines"], [[1.5, "Original / 译文"]])
        result = embedded.extract_lyrics(audio({"lyrics": ["[offset:-2000]\n[00:01]Early"]}))
        self.assertEqual(result["lines"], [[0.0, "Early"]])

    def test_id3_sylt_milliseconds_and_mpeg_frames_are_distinct(self):
        frame = SimpleNamespace(format=2, text=[("First", 1500), ("Second", 3000)])
        self.assertEqual(embedded.extract_lyrics(audio(ID3Tags({"SYLT": [frame]})))["lines"],
                         [[1.5, "First"], [3.0, "Second"]])
        frame.format = 1
        result = embedded.extract_lyrics(audio(ID3Tags({"SYLT": [frame]})))
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["plain_text"], "First\nSecond")

    def test_mp4_and_vorbis_plain_text_are_not_synchronised(self):
        for key in ("©lyr", "UNSYNCEDLYRICS", "SYNCEDLYRICS", "lyrics"):
            with self.subTest(key=key):
                result = embedded.extract_lyrics(audio({key: ["Plain first line\nPlain second line"]}))
                self.assertEqual(result["lines"], [])
                self.assertTrue(result["plain_text"].startswith("Plain"))
        self.assertIsNone(embedded.extract_lyrics(audio({"lyricist": ["Songwriter"]})))

    def test_explicit_file_is_authoritative_and_does_not_scan_roots(self):
        path = self.file("Explicit.mp3")
        with patch.object(embedded, "_snapshot", side_effect=AssertionError("unexpected scan")), \
                patch.object(embedded, "_open_audio", return_value=audio({"lyrics": ["[00:01]Explicit"]})):
            result = embedded.find_lyrics({"file_path": str(path), "title": "Different"}, [str(self.root)])
        self.assertEqual(result["source_path"], str(path))
        self.assertEqual(result["lines"], [[1.0, "Explicit"]])
        json.dumps(result)

    def test_metadata_finds_song_even_when_filename_does_not_contain_title(self):
        path = self.file("opaque-123.flac")
        tags = VorbisTags({"title": ["Fixture Song"], "artist": ["Fixture Artist"], "lyrics": ["[00:01]Matched"]})
        with patch.object(embedded, "_open_audio", return_value=audio(tags, 207.5)):
            result = embedded.find_lyrics({"title": "Fixture Song", "artist": "Fixture Artist", "duration": 208}, [str(self.root)])
        self.assertEqual(result["source_path"], str(path))
        self.assertEqual(len(result["directory_signature"]), 64)

    def test_wrong_artist_or_duration_and_ambiguous_versions_do_not_match(self):
        first = self.file("a.flac")
        second = self.file("b.flac")
        def open_audio(path):
            return audio({"title": ["Fixture"], "artist": ["Artist"],
                          "lyrics": ["[00:01]" + ("Version A" if Path(path) == first else "Version B")]}, 180)
        with patch.object(embedded, "_open_audio", side_effect=open_audio):
            self.assertIsNone(embedded.find_lyrics({"title": "Fixture", "artist": "Wrong"}, [str(self.root)]))
            self.assertIsNone(embedded.find_lyrics({"title": "Fixture", "artist": "Artist", "duration": 220}, [str(self.root)]))
            self.assertIsNone(embedded.find_lyrics({"title": "Fixture", "artist": "Artist", "duration": 180}, [str(self.root)]))
        self.assertTrue(second.exists())

    def test_duet_must_match_all_reported_artists(self):
        self.file("solo.flac")
        with patch.object(embedded, "_open_audio", return_value=audio({"title": ["Fixture"],
                "artist": ["Artist A"], "lyrics": ["[00:01]Solo version"]})):
            self.assertIsNone(embedded.find_lyrics({"title": "Fixture", "artist": "Artist A / Artist B"}, [str(self.root)]))

    def test_salt_recovers_stale_duration_only_for_a_unique_tag_match(self):
        first = self.file("opaque-a.flac")
        tags = {"title": ["Fixture"], "artist": ["Artist"], "lyrics": ["[00:01]Local"]}
        media = {"title": "Fixture", "artist": "Artist", "duration": 243,
                 "app_id": "Sakawish.SaltPlayerforWindows_q65q631pyh094!SaltPlayerforWindows",
                 "source": "smtc"}
        roots = [str(self.root)]

        def open_audio(path):
            if Path(path) == first:
                return audio(tags, 252)
            return audio({"title": ["Fixture"], "artist": ["Artist"]}, 275)

        with patch.object(embedded, "_open_audio", side_effect=open_audio):
            self.assertEqual(embedded.find_lyrics(media, roots)["source_path"], str(first))
            self.assertIsNone(embedded.find_lyrics({**media, "app_id": "other-player.exe"}, roots))
            self.assertIsNone(embedded.find_lyrics({**media, "artist": ""}, roots))
            self.file("opaque-b.flac")
            embedded.clear_cache()
            self.assertIsNone(embedded.find_lyrics(media, roots))

    def test_filename_can_match_when_title_and_artist_tags_are_absent(self):
        path = self.file("Artist - Fixture.flac")
        with patch.object(embedded, "_open_audio", return_value=audio({"lyrics": ["[00:01]Filename"]})):
            result = embedded.find_lyrics({"title": "Fixture", "artist": "Artist", "duration": 180}, [str(self.root)])
        self.assertEqual(result["source_path"], str(path))

    def test_only_selected_directories_are_searched_and_scan_is_cached(self):
        selected = self.root / "selected"
        other = self.root / "other"
        good = self.file("good.flac", selected)
        self.file("do-not-read.flac", other)
        opened = []
        def open_audio(path):
            opened.append(Path(path))
            return audio({"title": ["Fixture"], "artist": ["Artist"], "lyrics": ["[00:01]Local"]})
        real_walk = embedded.os.walk
        with patch.object(embedded.os, "walk", wraps=real_walk) as walk, \
                patch.object(embedded, "_open_audio", side_effect=open_audio):
            result = embedded.find_lyrics({"title": "Fixture", "artist": "Artist"}, [str(selected)])
            first_signature = embedded.directory_signature([str(selected)])
            second_signature = embedded.directory_signature([str(selected)])
            self.assertEqual(walk.call_count, 1)
        self.assertEqual(result["source_path"], str(good))
        self.assertEqual(opened, [good])
        self.assertEqual(first_signature, second_signature)

    def test_cloud_placeholders_are_skipped_but_local_reparse_files_are_read(self):
        local = self.file("local.flac")
        offline = self.file("offline.flac")
        recall = self.file("recall.flac")
        real_stat = embedded._stat
        def stat(path):
            original = real_stat(path)
            flag = 0x1000 if Path(path) == offline else 0x400000 if Path(path) == recall else 0x400
            return SimpleNamespace(st_mtime_ns=original.st_mtime_ns, st_size=original.st_size,
                                   st_file_attributes=flag)
        opened = []
        def open_audio(path):
            opened.append(Path(path))
            return audio({"title": ["Fixture"], "artist": ["Artist"], "lyrics": ["[00:01]Hydrated"]})
        with patch.object(embedded, "_stat", side_effect=stat), patch.object(embedded, "_open_audio", side_effect=open_audio):
            result = embedded.find_lyrics({"title": "Fixture", "artist": "Artist"}, [str(self.root)])
        self.assertEqual(result["source_path"], str(local))
        self.assertEqual(opened, [local])

    def test_changed_file_tags_invalidate_cached_record(self):
        path = self.file("changed.flac")
        with patch.object(embedded, "_open_audio", side_effect=[audio({"lyrics": ["[00:01]Before"]}),
                                                                  audio({"lyrics": ["[00:01]After"]})]) as opened:
            first = embedded.find_lyrics({"file_path": str(path)}, [])
            path.write_bytes(b"FICTIONAL_CHANGED_MUSIC_FIXTURE")
            second = embedded.find_lyrics({"file_path": str(path)}, [])
        self.assertEqual(opened.call_count, 2)
        self.assertNotEqual(first["file_signature"], second["file_signature"])
        self.assertEqual(second["lines"], [[1.0, "After"]])

    def test_transient_tag_read_failure_can_retry_without_file_change(self):
        path = self.file("temporarily-locked.flac")
        with patch.object(embedded, "_open_audio", side_effect=[OSError("fictional sharing violation"),
                                                                  audio({"lyrics": ["[00:01]Recovered"]})]) as opened:
            self.assertIsNone(embedded.find_lyrics({"file_path": str(path)}, []))
            result = embedded.find_lyrics({"file_path": str(path)}, [])
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(result["lines"], [[1.0, "Recovered"]])

    def test_no_lyrics_result_can_retry_without_file_change(self):
        path = self.file("synced.flac")
        with patch.object(embedded, "_open_audio", side_effect=[audio({"title": ["Fixture"]}),
                                                                  audio({"lyrics": ["[00:01]Recovered"]})]):
            self.assertIsNone(embedded.find_lyrics({"file_path": str(path)}, []))
            result = embedded.find_lyrics({"file_path": str(path)}, [])
        self.assertEqual(result["lines"], [[1.0, "Recovered"]])

    def test_nested_file_changes_refresh_directory_signature_after_ttl(self):
        path = self.file("nested/fixture.flac")
        with patch.object(embedded.time, "monotonic", return_value=100.0):
            first = embedded.directory_signature([str(self.root)])
        path.write_bytes(b"FICTIONAL_CHANGED_FIXTURE")
        with patch.object(embedded.time, "monotonic", return_value=100.5):
            cached = embedded.directory_signature([str(self.root)])
        with patch.object(embedded.time, "monotonic", return_value=116.0):
            refreshed = embedded.directory_signature([str(self.root)])
        self.assertEqual(first, cached)
        self.assertNotEqual(first, refreshed)

    def test_subdirectory_junctions_are_not_traversed(self):
        blocked = self.root / "junction"
        self.file("not-selected.flac", blocked)
        real_junction = getattr(Path, "is_junction", lambda _path: False)
        with patch.object(Path, "is_junction", lambda path: Path(path) == blocked or real_junction(path)), \
                patch.object(embedded, "_open_audio", side_effect=AssertionError("junction was traversed")):
            self.assertIsNone(embedded.find_lyrics({"title": "Fixture"}, [str(self.root)]))

    def test_remote_or_relative_media_paths_are_not_opened(self):
        with patch.object(embedded, "_open_audio", side_effect=AssertionError("unexpected file open")):
            self.assertIsNone(embedded.find_lyrics({"file_path": "relative.mp3"}, []))
            self.assertIsNone(embedded.find_lyrics({"file_path": "file://remote/share/song.mp3"}, []))
            self.assertIsNone(embedded.find_lyrics({"title": "Fixture"}, ["relative-folder"]))


if __name__ == "__main__":
    unittest.main()
