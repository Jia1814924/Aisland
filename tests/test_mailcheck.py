"""IMAP TLS regression checks.

Each check runs in a child process so Qt is never imported into the main test
process (test_maintenance asserts that). No real mail server is contacted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CHILD = PROJECT / "tests" / "_imap_tls_child.py"


class ImapTlsTests(unittest.TestCase):
    def _run_child(self, test_name: str):
        with tempfile.TemporaryDirectory(prefix="aisland-imap-state-") as state:
            env = os.environ.copy()
            env["AISLAND_USER_HOME"] = state
            return subprocess.run(
                [sys.executable, "-B", str(CHILD), test_name],
                env=env, capture_output=True, text=True, timeout=90)

    def test_verified_context(self):
        result = self._run_child("ImapTlsChildTests.test_verified_context")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_self_signed_certificate_is_rejected(self):
        result = self._run_child("ImapTlsChildTests.test_self_signed_rejected")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
