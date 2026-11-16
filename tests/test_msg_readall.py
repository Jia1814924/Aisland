"""消息页右上角「一键已读」：只清当前 AI 分组，且不影响其他未读。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CHILD = PROJECT / "tests" / "_msg_readall_child.py"


class MsgReadAllTests(unittest.TestCase):
    def _run(self, name: str):
        with tempfile.TemporaryDirectory(prefix="aisland-readall-state-") as state:
            env = os.environ.copy()
            env["AISLAND_USER_HOME"] = state
            env["QT_QPA_PLATFORM"] = "offscreen"
            return subprocess.run(
                [sys.executable, "-B", str(CHILD), name],
                env=env, capture_output=True, text=True, timeout=120)

    def test_clears_only_current_group(self):
        result = self._run("MsgReadAllTests.test_button_sits_in_ai_header_and_clears_only_that_group")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wechat_header_clears_only_wechat(self):
        result = self._run("MsgReadAllTests.test_wechat_header_clears_only_wechat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_click_without_provider(self):
        result = self._run("MsgReadAllTests.test_click_without_provider_is_harmless")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_header_width_stable(self):
        result = self._run("MsgReadAllTests.test_header_button_does_not_grow_on_refresh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_quota_tile_paints(self):
        result = self._run("MsgReadAllTests.test_quota_tile_does_not_shadow_paintdevice_metric")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_harness_auto_selection_clear_and_badge(self):
        result = self._run("MsgReadAllTests.test_harness_auto_selection_clear_and_shared_blue_badge")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_credits_survive_quitting_the_client(self):
        result = self._run("MsgReadAllTests.test_credits_survive_quitting_the_client")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_auto_switches_both_displays_promptly_and_respects_fixed_selection(self):
        result = self._run("MsgReadAllTests.test_auto_tracks_focus_on_both_displays_without_waiting_for_notifications")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_visible_and_hidden_quota_refresh(self):
        result = self._run("MsgReadAllTests.test_visible_refresh_is_prompt_but_hidden_reads_stay_throttled")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_other_notices_get_their_own_block_and_a_yellow_dot(self):
        result = self._run("MsgReadAllTests.test_other_notices_get_their_own_block_and_a_yellow_dot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_other_notice_hides_the_yellow_dot(self):
        result = self._run("MsgReadAllTests.test_no_other_notice_hides_the_yellow_dot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_clicking_any_notice_opens_its_own_application(self):
        result = self._run("MsgReadAllTests.test_clicking_any_notice_opens_its_own_application")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_notice_open_keeps_unread_and_panel_open(self):
        result = self._run("MsgReadAllTests.test_failed_notice_open_keeps_unread_and_panel_open")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_row_at_maps_clicks_and_ignores_empty_space(self):
        result = self._run("MsgReadAllTests.test_row_at_maps_clicks_and_ignores_empty_space")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_exe_name_is_not_launched_when_no_window_exists(self):
        result = self._run("MsgReadAllTests.test_bare_exe_name_is_not_launched_when_no_window_exists")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_other_notice_badge_in_collapsed_view_is_yellow(self):
        result = self._run("MsgReadAllTests.test_other_notice_badge_in_collapsed_view_is_yellow")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_provider_callbacks_and_shared_reads(self):
        for name in ("test_quota_requests_and_callbacks_stay_with_their_provider",
                     "test_two_hidden_pages_share_one_quota_read",
                     "test_dropped_background_job_does_not_block_future_reads"):
            with self.subTest(name=name):
                result = self._run("MsgReadAllTests." + name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
