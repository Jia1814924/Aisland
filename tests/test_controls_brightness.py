"""Run Qt control-center regression checks in an isolated profile."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ControlsBrightnessTests(unittest.TestCase):
    def test_controls_and_monitor_routing(self):
        child = Path(__file__).with_name("_controls_brightness_child.py")
        with tempfile.TemporaryDirectory(prefix="aisland-controls-state-") as state:
            env = dict(os.environ, AISLAND_USER_HOME=state, QT_QPA_PLATFORM="offscreen")
            result = subprocess.run([sys.executable, "-B", str(child)], env=env, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
