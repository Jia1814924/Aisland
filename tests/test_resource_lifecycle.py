"""Exercise resource ownership with real Qt in a separate process."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ResourceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 被中断的测试进程会留下 aisland-resources-* 目录：每次开跑先清场。
        import shutil
        for stale in (Path(__file__).resolve().parents[1] / "build").glob("aisland-resources-*"):
            shutil.rmtree(stale, ignore_errors=True)

    def run_child(self, name, *, dual_screen=False):
        child = Path(__file__).with_name(name)
        project = child.parents[1]
        with tempfile.TemporaryDirectory(prefix="aisland-resources-", dir=project / "build") as state:
            env = dict(os.environ, AISLAND_USER_HOME=state, QT_QPA_PLATFORM="offscreen")
            if dual_screen:
                screens = Path(state) / "screens.json"
                screens.write_text(json.dumps({"screens": [
                    {"name": "Main", "x": 0, "y": 0, "width": 1920, "height": 1280, "dpr": 1.5},
                    {"name": "Side", "x": -2560, "y": 0, "width": 2048, "height": 1280, "dpr": 1.25},
                ]}), encoding="utf-8")
                relative = screens.relative_to(project).as_posix()
                env["QT_QPA_PLATFORM"] = "offscreen:configfile=" + relative
            result = subprocess.run([sys.executable, "-B", str(child)], env=env, capture_output=True,
                                    cwd=project, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lazy_resources_and_subtitle_restart(self):
        self.run_child("_resource_lifecycle_child.py")

    def test_display_playback_routing(self):
        self.run_child("_display_routes_child.py")

    def test_real_display_window_lifecycle(self):
        self.run_child("_display_lifecycle_child.py", dual_screen=True)

    def test_settings_actions_and_navigation(self):
        self.run_child("_settings_child.py", dual_screen=True)
