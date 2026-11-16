"""Error paths must remain safe when the user-data directory is inaccessible."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from aisland.maintenance import log_import_failure


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.area = PROJECT / "build" / "maintenance-tests"
        self.area.mkdir(parents=True, exist_ok=True)
        self.root = self.area / uuid.uuid4().hex
        self.root.mkdir()
        self.blocked = self.root / "blocked-user-data"
        self.fallback = self.root / "safe-temp"
        self.fallback.mkdir()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.root.resolve().parent != self.area.resolve():
            raise RuntimeError("Unexpected cleanup path")
        shutil.rmtree(self.root)

    def block_user_mkdir(self):
        original = Path.mkdir
        blocked = self.blocked
        def mkdir(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError(5, "denied for fixture only")
            return original(path, *args, **kwargs)
        return mock.patch.object(Path, "mkdir", mkdir)

    def test_native_reader_entry_does_not_start_app_or_load_config(self):
        helper = types.ModuleType("aisland.services.native_ai_worker")
        helper.main = mock.Mock(return_value=0)
        with (mock.patch.dict(sys.modules, {"aisland.services.native_ai_worker": helper}),
              mock.patch.object(sys, "argv", [str(PROJECT / "run.pyw"), "--native-usage-worker"]),
              mock.patch.object(Path, "mkdir", side_effect=AssertionError("Worker entry initialized user state"))):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(PROJECT / "run.pyw"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 0)
        helper.main.assert_called_once_with()
        self.assertNotIn("aisland.app", sys.modules)
        self.assertFalse(any(name.startswith("PySide6") for name in sys.modules))

    def test_log_falls_back_when_user_directory_cannot_be_created(self):
        with self.block_user_mkdir(), mock.patch("aisland.maintenance.tempfile.gettempdir", return_value=str(self.fallback)):
            destination = log_import_failure(self.blocked, "ORIGINAL_IMPORT_FAILURE")
        self.assertEqual(destination, self.fallback / "aisland-import-error.log")
        self.assertEqual(destination.read_text(encoding="utf-8"), "ORIGINAL_IMPORT_FAILURE\n")
        self.assertFalse(self.blocked.exists())

    def test_log_never_raises_if_both_destinations_are_blocked(self):
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("all fixture logs denied")), \
             mock.patch("aisland.maintenance.tempfile.gettempdir", return_value=str(self.fallback)):
            self.assertIsNone(log_import_failure(self.blocked, "original failure"))

    def test_import_cli_returns_one_without_initializing_app_or_qt(self):
        fake_runtime = types.ModuleType("aisland.core.runtime")
        fake_runtime.DATA_DIR = self.blocked
        fake_runtime.INSTALL_ROOT = self.root
        fake_runtime.PATHS = object()
        fake_runtime.LEGACY_HOME_ENV = "WISLAND_USER_HOME"
        fake_runtime.rotate_log = lambda *_args, **_kwargs: False
        fake_runtime.adopt_legacy_state = lambda *_args, **_kwargs: "skipped"
        def denied(*_args):
            raise PermissionError(5, "import destination denied")
        fake_runtime.import_legacy_data = denied
        with mock.patch.dict(sys.modules, {"aisland.core.runtime": fake_runtime}), \
             mock.patch.object(sys, "argv", [str(PROJECT / "run.pyw"), "--import-from", "fixture", "--import-only"]), \
             mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None), \
             self.block_user_mkdir(), \
             mock.patch("aisland.maintenance.tempfile.gettempdir", return_value=str(self.fallback)):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(PROJECT / "run.pyw"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn("import destination denied", (self.fallback / "aisland-import-error.log").read_text(encoding="utf-8"))
        self.assertNotIn("aisland.app", sys.modules)
        self.assertFalse(any(name.startswith("PySide6") for name in sys.modules))

    def test_access_diagnostic_cli_does_not_initialize_runtime_or_qt(self):
        output = self.root / "access.json"
        environment = os.environ.copy()
        environment["AISLAND_USER_HOME"] = str(self.blocked)
        result = subprocess.run([sys.executable, "-I", "-S", str(PROJECT / "run.pyw"),
            "--diagnose-access", str(output)], env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["filesystem_write_probe"])
        self.assertEqual(report["expected_user_state"]["path"], str(self.blocked))
        self.assertFalse(self.blocked.exists())
        self.assertIn("is_app_container", report["token"])

    def test_old_private_key_import_command_is_rejected_without_initializing_state(self):
        environment = os.environ.copy()
        environment["AISLAND_USER_HOME"] = str(self.blocked)
        result = subprocess.run([sys.executable, "-I", "-S", str(PROJECT / "run.pyw"),
            "--adopt-translation-defaults", str(self.root)], env=environment,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn("do not import bundled translation credentials", result.stderr)
        self.assertFalse(self.blocked.exists())


if __name__ == "__main__":
    unittest.main()
