"""Independent display palettes exercised with real Qt windows and controls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DisplayThemeTests(unittest.TestCase):
    def test_each_display_uses_its_own_palette(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aisland-display-theme-", dir=project / "build") as state:
            screens = Path(state) / "screens.json"
            screens.write_text(json.dumps({"screens": [
                {"name": "Bright", "x": 0, "y": 0, "width": 1600, "height": 1000, "dpr": 1.5},
                {"name": "Dark", "x": -1600, "y": 0, "width": 1600, "height": 1000, "dpr": 1.25},
            ]}), encoding="utf-8")
            env = dict(os.environ, AISLAND_USER_HOME=state,
                       QT_QPA_PLATFORM="offscreen:configfile=" + screens.relative_to(project).as_posix())
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("_display_theme_child.py"))],
                                    cwd=project, env=env, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
