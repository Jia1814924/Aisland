"""收起态「时间天气」与「表情」必须同宽（切换视图时不改变尺寸）。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CHILD = PROJECT / "tests" / "_compact_width_child.py"


class CompactWidthTests(unittest.TestCase):
    def _run(self, name: str):
        with tempfile.TemporaryDirectory(prefix="aisland-compact-state-") as state:
            env = os.environ.copy()
            env["AISLAND_USER_HOME"] = state
            env["QT_QPA_PLATFORM"] = "offscreen"
            return subprocess.run(
                [sys.executable, "-B", str(CHILD), name],
                env=env, capture_output=True, text=True, timeout=120)

    def test_clock_weather_and_pet_share_width(self):
        result = self._run("CompactWidthTests.test_clock_and_pet_share_one_width")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_weather_stays_clear_of_clock_and_badges(self):
        result = self._run("CompactWidthTests.test_weather_never_overlaps_clock")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_weather_is_drawn_when_there_is_room(self):
        result = self._run("CompactWidthTests.test_weather_is_drawn_when_there_is_room")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_weather_is_dropped_when_too_narrow(self):
        result = self._run("CompactWidthTests.test_weather_is_dropped_when_there_is_no_room")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_activity_with_status_stays_within_cap(self):
        result = self._run("CompactWidthTests.test_activity_with_status_and_task_stays_within_cap")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
