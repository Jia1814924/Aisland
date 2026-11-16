"""Executable identity and real Qt automatic-provider switching."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aisland.services.ai_identity import identify_ai_application


CODEX_STORE = ("C:/Program Files/WindowsApps/"
               "OpenAI.Codex_26.1106.6628.0_x64__2p2nqsd0c76g0/app/ChatGPT.exe")


class AiIdentityTests(unittest.TestCase):
    def test_codex_store_shared_executable_name(self):
        self.assertEqual(identify_ai_application(CODEX_STORE), "codex")
        self.assertEqual(identify_ai_application(CODEX_STORE.upper()), "codex")

    def test_ordinary_chatgpt_and_similar_names_are_not_codex(self):
        for path in ("ChatGPT.exe", r"C:\Apps\ChatGPT.exe",
                     CODEX_STORE.replace("OpenAI.Codex_", "OpenAI.ChatGPT-Desktop_"),
                     CODEX_STORE.replace("OpenAI.Codex_", "Something.OpenAI.Codex_"),
                     CODEX_STORE.replace("__2p2nqsd0c76g0", "__otherpublisher")):
            with self.subTest(path=path):
                self.assertIsNone(identify_ai_application(path))

    def test_supported_clients_need_no_custom_mappings(self):
        for path, expected in ((r"C:\Apps\Codex.exe", "codex"),
                               (r"C:\Apps\WorkBuddy.exe", "workbuddy"),
                               (r"C:\Apps\DeepSeek Harness.exe", "deepseek_harness")):
            with self.subTest(path=path):
                self.assertEqual(identify_ai_application(path), expected)

    def test_user_rules_are_case_insensitive_and_preserved(self):
        self.assertEqual(identify_ai_application(r"C:\Apps\Custom.exe", {
            "CUSTOM.EXE": {"kind": "workbuddy"}}), "workbuddy")
        self.assertIsNone(identify_ai_application(r"C:\Apps\Codex.exe", {
            "codex.exe": {"kind": "other"}}))

    def test_empty_or_unrelated_path_does_not_select_an_ai(self):
        for path in ("", "explorer.exe", r"C:\Apps\NotCodex.exe"):
            self.assertIsNone(identify_ai_application(path))

    def test_foreground_to_two_displays_uses_real_identity_rules(self):
        with tempfile.TemporaryDirectory(prefix="aisland-identity-") as state:
            env = dict(os.environ, AISLAND_USER_HOME=state, QT_QPA_PLATFORM="offscreen")
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).with_name("_ai_identity_child.py"))],
                env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
