"""展开时真实 Qt 内容应随外框出现，并在交接后保持可交互。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ExpandAnimationTests(unittest.TestCase):
    def test_content_and_shell_expand_together(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aisland-expand-") as state:
            env = dict(os.environ, AISLAND_USER_HOME=state, QT_QPA_PLATFORM="offscreen")
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).with_name("_expand_animation_child.py"))],
                cwd=project, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
