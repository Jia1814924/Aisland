"""额度面板的及时刷新：在隔离子进程里跑 Qt 相关断言。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CHILD = PROJECT / "tests" / "_quota_refresh_child.py"


class QuotaRefreshTests(unittest.TestCase):
    def _run(self, name: str):
        with tempfile.TemporaryDirectory(prefix="aisland-quota-refresh-") as state:
            env = os.environ.copy()
            env["AISLAND_USER_HOME"] = state
            env["QT_QPA_PLATFORM"] = "offscreen"
            return subprocess.run(
                [sys.executable, "-B", str(CHILD), "QuotaRefreshTests." + name],
                env=env, capture_output=True, text=True, timeout=120)

    def test_throttled_tick_is_skipped_but_a_forced_read_runs(self):
        result = self._run("test_throttled_tick_is_skipped_but_a_forced_read_runs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_visible_panel_short_retries_after_a_miss(self):
        result = self._run("test_a_visible_panel_short_retries_after_a_miss")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repeated_visible_failures_use_the_full_backoff_schedule(self):
        result = self._run("test_repeated_visible_failures_use_the_full_backoff_schedule")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missed_panel_counts_failures_and_success_resets_them(self):
        result = self._run("test_missed_panel_counts_failures_and_success_resets_them")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_local_source_change_forces_an_immediate_read(self):
        result = self._run("test_local_source_change_forces_an_immediate_read")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_two_displays_share_one_in_flight_read(self):
        result = self._run("test_two_displays_share_one_in_flight_read")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_lost_callback_does_not_freeze_the_provider(self):
        result = self._run("test_a_lost_callback_does_not_freeze_the_provider")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_old_callback_cannot_clear_a_new_reads_busy_mark(self):
        result = self._run("test_old_callback_cannot_clear_a_new_reads_busy_mark")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_old_success_cannot_overwrite_newer_metrics(self):
        result = self._run("test_old_success_cannot_overwrite_newer_metrics")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_notification_and_foreground_switch_request_a_refresh(self):
        result = self._run("test_notification_and_foreground_switch_request_a_refresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
