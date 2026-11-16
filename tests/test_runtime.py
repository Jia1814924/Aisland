"""Storage/upgrade regression checks; all writes stay in temporary directories."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import shutil
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from aisland.core.runtime import (adopt_legacy_state, autostart_command,
                                  import_legacy_data, resolve_paths, rotate_log)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.test_area = PROJECT / "build" / "runtime-tests"
        # 被中断的测试进程会留下上一轮的目录：每次开跑先清场。
        shutil.rmtree(self.test_area, ignore_errors=True)
        self.test_area.mkdir(parents=True, exist_ok=True)
        self.root = self.test_area / uuid.uuid4().hex
        self.root.mkdir()
        self.addCleanup(self.remove_test_root)
        self.source = self.root / "old version"
        self.source.mkdir()
        self.state = self.root / "user state"
        self.paths = resolve_paths(source_root=self.source, frozen=True,
                                   executable=self.root / "Programs" / "Aisland.exe",
                                   bundle_root=self.root / "bundle",
                                   environ={"AISLAND_USER_HOME": str(self.state)})

    def remove_test_root(self):
        if self.root.resolve().parent != self.test_area.resolve():
            raise RuntimeError("Refusing to remove a directory outside test area")
        shutil.rmtree(self.root)

    def config(self, value=None):
        content = value or {"island": {"theme": "sky"}, "live": {"models_dir": "models"},
                            "translate": {"api_key": "LOCAL_TEST_SENTINEL"}}
        path = self.source / "config.json"
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        return path

    def test_source_layout_is_unchanged_without_override(self):
        p = resolve_paths(source_root=self.source, frozen=False, environ={})
        self.assertEqual(p.state_root, self.source)
        self.assertEqual(p.config_path, self.source / "config.json")
        self.assertEqual(p.data_dir, self.source / "data")
        self.assertFalse((self.source / "data").exists())

    def test_frozen_resources_and_user_state_are_separate(self):
        p = resolve_paths(source_root=self.source, frozen=True,
                          executable=self.root / "Programs" / "Aisland.exe",
                          bundle_root=self.root / "Programs" / "_internal",
                          environ={"LOCALAPPDATA": str(self.root / "Local")})
        self.assertEqual(p.state_root, self.root / "Local" / "Aisland")
        self.assertEqual(p.resource_root, self.root / "Programs" / "_internal")
        self.assertNotEqual(p.state_root, p.install_root)
        self.assertFalse(p.state_root.exists())

    def test_test_override_also_isolates_source_mode(self):
        p = resolve_paths(source_root=self.source, frozen=False,
                          environ={"AISLAND_USER_HOME": str(self.state)})
        self.assertEqual(p.state_root, self.state)
        self.assertTrue(p.user_home_overridden)

    def test_legacy_home_env_still_used_as_fallback(self):
        p = resolve_paths(source_root=self.source, frozen=True,
                          executable=self.root / "Programs" / "Aisland.exe",
                          bundle_root=self.root / "Programs",
                          environ={"WISLAND_USER_HOME": str(self.state)})
        self.assertEqual(p.state_root, self.state)
        self.assertTrue(p.user_home_overridden)

    def test_legacy_state_directory_is_adopted_once(self):
        local = self.root / "Local"
        legacy = local / "Wisland"
        (legacy / "data").mkdir(parents=True)
        (legacy / "config.json").write_text('{"island": {"theme": "sky"}}', encoding="utf-8")
        (legacy / "data" / "clipboard.json").write_text(
            json.dumps({"items": [{"image_path": str(legacy / "data" / "a.png")}]}), encoding="utf-8")
        (legacy / "data" / "aisland.lock").write_text("", encoding="utf-8")
        p = resolve_paths(source_root=self.source, frozen=True,
                          executable=self.root / "Programs" / "Aisland.exe",
                          bundle_root=self.root / "Programs",
                          environ={"LOCALAPPDATA": str(local)})
        self.assertEqual(adopt_legacy_state(p, environ={"LOCALAPPDATA": str(local)}), "renamed")
        self.assertTrue((p.state_root / "config.json").is_file())
        self.assertFalse(legacy.exists())
        clipboard = json.loads((p.state_root / "data" / "clipboard.json").read_text(encoding="utf-8"))
        self.assertEqual(clipboard["items"][0]["image_path"],
                         str(p.state_root / "data" / "a.png"))
        self.assertEqual(adopt_legacy_state(p, environ={"LOCALAPPDATA": str(local)}), "exists")

    def test_adoption_skips_when_user_home_is_overridden(self):
        local = self.root / "Local"
        (local / "Wisland").mkdir(parents=True)
        p = resolve_paths(source_root=self.source, frozen=True,
                          executable=self.root / "Programs" / "Aisland.exe",
                          bundle_root=self.root / "Programs",
                          environ={"LOCALAPPDATA": str(local),
                                   "AISLAND_USER_HOME": str(self.state)})
        self.assertEqual(adopt_legacy_state(p, environ={"LOCALAPPDATA": str(local)}), "skipped")
        self.assertTrue((local / "Wisland").exists())

    def test_frozen_autostart_does_not_depend_on_python(self):
        exe = self.root / "Program Files" / "Aisland.exe"
        command = autostart_command(self.paths, executable=exe)
        self.assertEqual(command, subprocess.list2cmdline([str(exe)]))
        self.assertNotIn("run.pyw", command)
        self.assertNotIn("python", command.lower())

    def test_import_preserves_config_and_remaps_clipboard(self):
        original_config = self.config().read_bytes()
        data = self.source / "data"
        clips = data / "clips"
        clips.mkdir(parents=True)
        image = clips / "test.png"
        image.write_bytes(b"TEST_IMAGE_DATA")
        (data / "clipboard.json").write_text(json.dumps({"items": [
            {"kind": "image", "image_path": str(image)}]}), encoding="utf-8")
        (data / "aisland.lock").write_text("old process", encoding="utf-8")
        (data / "aisland.log").write_text("old log", encoding="utf-8")
        result = import_legacy_data(self.source, self.paths)
        self.assertEqual(result["status"], "imported")
        self.assertEqual(self.paths.config_path.read_bytes(), original_config)
        migrated = json.loads((self.paths.data_dir / "clipboard.json").read_text(encoding="utf-8"))
        self.assertEqual(migrated["items"][0]["image_path"], str(self.paths.data_dir / "clips" / "test.png"))
        self.assertEqual((self.paths.data_dir / "clips" / "test.png").read_bytes(), b"TEST_IMAGE_DATA")
        self.assertFalse((self.paths.data_dir / "aisland.lock").exists())
        self.assertFalse((self.paths.data_dir / "aisland.log").exists())
        self.assertEqual((self.source / "config.json").read_bytes(), original_config)

    def test_existing_upgrade_state_is_never_overwritten(self):
        self.config()
        self.state.mkdir()
        self.paths.config_path.write_text('{"custom":"keep"}', encoding="utf-8")
        self.paths.data_dir.mkdir()
        history = self.paths.data_dir / "translate_history.json"
        history.write_text('[{"src":"newer data"}]', encoding="utf-8")
        before = (self.paths.config_path.read_bytes(), history.read_bytes())
        result = import_legacy_data(self.source, self.paths)
        self.assertEqual(result["status"], "kept_existing")
        self.assertEqual((self.paths.config_path.read_bytes(), history.read_bytes()), before)

    def test_database_backup_includes_uncheckpointed_wal(self):
        self.config()
        old_db = self.source / "data" / "wordbook" / "wordbook.db"
        old_db.parent.mkdir(parents=True)
        connection = sqlite3.connect(old_db)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE cards(word TEXT)")
        connection.execute("INSERT INTO cards VALUES('WAL_SENTINEL')")
        connection.commit()
        self.assertTrue(Path(str(old_db) + "-wal").exists())
        import_legacy_data(self.source, self.paths)
        with closing(sqlite3.connect(self.paths.data_dir / "wordbook" / "wordbook.db")) as migrated:
            self.assertEqual(migrated.execute("SELECT word FROM cards").fetchone()[0], "WAL_SENTINEL")
        self.assertFalse((self.paths.data_dir / "wordbook" / "wordbook.db-wal").exists())

    def test_dictionary_is_user_data_and_survives_source_removal(self):
        self.config()
        old_dictionary = self.source / "models" / "dict" / "dictionary.db"
        old_dictionary.parent.mkdir(parents=True)
        with closing(sqlite3.connect(old_dictionary)) as db:
            db.execute("CREATE TABLE dicts(title TEXT)")
            db.execute("INSERT INTO dicts VALUES('TEST_DICTIONARY')")
            db.commit()
        import_legacy_data(self.source, self.paths)
        with closing(sqlite3.connect(self.state / "dictionary" / "dictionary.db")) as migrated:
            self.assertEqual(migrated.execute("SELECT title FROM dicts").fetchone()[0], "TEST_DICTIONARY")

    def test_bad_source_does_not_create_config(self):
        (self.source / "config.json").write_text("invalid JSON", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            import_legacy_data(self.source, self.paths)
        self.assertFalse(self.paths.config_path.exists())

    def test_import_only_cli_needs_no_qt_or_services(self):
        self.config()
        env = dict(os.environ, AISLAND_USER_HOME=str(self.state))
        result = subprocess.run([sys.executable, "-I", "-S", str(PROJECT / "run.pyw"),
                                 "--import-from", str(self.source), "--import-only"],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "imported")
        self.assertFalse((self.paths.data_dir / "aisland.log").exists())

    def test_verify_ui_refuses_without_isolated_state_before_qt_import(self):
        env = dict(os.environ)
        env.pop("AISLAND_USER_HOME", None)
        report = self.root / "ui-verification.json"
        result = subprocess.run([sys.executable, "-I", "-S", str(PROJECT / "run.pyw"),
                                 "--verify-ui", str(report)],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(report.exists())


class LogRotationTests(unittest.TestCase):
    def setUp(self):
        area = PROJECT / "build" / "runtime-tests"
        area.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "aisland.log"

    def test_oversized_log_is_rotated_keeping_one_generation(self):
        self.path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        self.assertTrue(rotate_log(self.path))
        rotated = self.path.with_name("aisland.log.1")
        self.assertFalse(self.path.exists())
        self.assertEqual(rotated.stat().st_size, 2 * 1024 * 1024 + 1)
        # 下一轮轮转直接覆盖旧一代，不无限累积
        self.path.write_bytes(b"y" * (2 * 1024 * 1024 + 1))
        self.assertTrue(rotate_log(self.path))
        self.assertEqual(rotated.stat().st_size, 2 * 1024 * 1024 + 1)

    def test_small_or_missing_log_is_left_alone(self):
        self.path.write_bytes(b"x" * 10)
        self.assertFalse(rotate_log(self.path))
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_name("aisland.log.1").exists())
        self.assertFalse(rotate_log(self.path.with_name("missing.log")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
