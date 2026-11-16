"""Codex / WorkBuddy 通知要带上正文（详情），而不只是应用名。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CHILD = PROJECT / "tests" / "_notify_detail_child.py"


class NotifyDetailTests(unittest.TestCase):
    def _run(self, name: str):
        with tempfile.TemporaryDirectory(prefix="aisland-notify-state-") as state:
            env = os.environ.copy()
            env["AISLAND_USER_HOME"] = state
            env["QT_QPA_PLATFORM"] = "offscreen"
            return subprocess.run(
                [sys.executable, "-B", str(CHILD), name],
                env=env, capture_output=True, text=True, timeout=120)

    def test_toast_texts(self):
        result = self._run("NotifyDetailChildTests.test_texts_keep_order_and_skip_attribution")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_doctype_rejected(self):
        result = self._run("NotifyDetailChildTests.test_doctype_payload_is_rejected")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ai_toast_keeps_body(self):
        result = self._run("NotifyDetailChildTests.test_ai_toast_keeps_body")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_single_line_toast(self):
        result = self._run("NotifyDetailChildTests.test_single_line_ai_toast_still_shows_text")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_toast_collapses(self):
        result = self._run("NotifyDetailChildTests.test_duplicate_ai_toast_collapses")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_different_content_kept(self):
        result = self._run("NotifyDetailChildTests.test_different_ai_content_is_kept")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stale_repeat_kept(self):
        result = self._run("NotifyDetailChildTests.test_stale_ai_repeat_is_kept")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bubble_subtitle_has_status(self):
        result = self._run("NotifyDetailChildTests.test_ai_subtitle_combines_status_and_conversation")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bubble_subtitle_codex_order(self):
        result = self._run("NotifyDetailChildTests.test_ai_subtitle_keeps_codex_order")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bubble_subtitle_degrades(self):
        result = self._run("NotifyDetailChildTests.test_ai_subtitle_degrades_without_duplication")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_harness_toast_enriches_flash(self):
        result = self._run("NotifyDetailChildTests.test_harness_toast_enriches_its_flash_once")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_builtin_office_apps_are_mapped_without_config_changes(self):
        result = self._run("NotifyDetailChildTests.test_builtin_office_apps_are_mapped_without_config_changes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_office_toast_keeps_title_and_body(self):
        result = self._run("NotifyDetailChildTests.test_office_toast_keeps_title_and_body")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_open_resolves_each_kind_to_its_application(self):
        result = self._run("NotifyDetailChildTests.test_open_resolves_each_kind_to_its_application")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_more_specific_user_toast_rule_wins_over_builtin_prefix(self):
        result = self._run("NotifyDetailChildTests.test_more_specific_user_toast_rule_wins_over_builtin_prefix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_alternate_executable_is_focused_and_only_its_unread_is_cleared(self):
        result = self._run("NotifyDetailChildTests.test_alternate_executable_is_focused_and_only_its_unread_is_cleared")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_failed_candidates_preserve_unread(self):
        result = self._run("NotifyDetailChildTests.test_all_failed_candidates_preserve_unread")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_full_path_launch_without_opener_requires_success(self):
        result = self._run("NotifyDetailChildTests.test_full_path_launch_without_opener_requires_success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_exe_name_is_never_launched_without_an_opener(self):
        result = self._run("NotifyDetailChildTests.test_bare_exe_name_is_never_launched_without_an_opener")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
