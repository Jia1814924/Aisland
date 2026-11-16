"""设置类对话框：默认弹在屏幕正中并置顶，不被灵动岛挡住；厂商表覆盖国内外主流。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CHILD = PROJECT / "tests" / "_dialog_child.py"


class DialogTests(unittest.TestCase):
    def _run(self, name: str):
        with tempfile.TemporaryDirectory(prefix="aisland-dialog-state-") as state:
            env = os.environ.copy()
            env["AISLAND_USER_HOME"] = state
            env["QT_QPA_PLATFORM"] = "offscreen"
            return subprocess.run(
                [sys.executable, "-B", str(CHILD), name],
                env=env, capture_output=True, text=True, timeout=180)

    def test_dialog_is_top_level_and_centered(self):
        result = self._run("ConnectionDialogTests.test_dialog_is_top_level_and_centered")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_vendor_list_and_known_provider_layout(self):
        result = self._run("ConnectionDialogTests.test_vendor_list_and_known_provider_layout")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_custom_provider_reveals_endpoint_field(self):
        result = self._run("ConnectionDialogTests.test_custom_provider_reveals_endpoint_field")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_speed_probe_button_is_available(self):
        result = self._run("ConnectionDialogTests.test_speed_probe_button_is_available")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
