"""Config write-safety regressions: corrupt configs and concurrent saves.

Loads config.py with a stubbed runtime/Qt, so no real PySide6 enters this
process and all writes stay inside a temporary directory.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import shutil
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import aisland.core  # noqa: F401  - ensure the package exists for relative imports

USER_CONFIG = {
    "weather": {"city": "合肥"},
    "translate": {"api_key": "FICTIONAL_USER_KEY"},
    "island": {"theme": "dark"},
}


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        area = PROJECT / "build" / "config-safety-tests"
        # 被中断的测试进程会留下上一轮的目录：每次开跑先清场。
        shutil.rmtree(area, ignore_errors=True)
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def load_module(self, content):
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        path = state / "config.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        runtime = ModuleType("aisland.core.runtime")
        runtime.CONFIG_PATH = path
        runtime.DATA_DIR = state / "data"
        runtime.RESOURCE_ROOT = self.root
        runtime.IS_FROZEN = False
        qt = ModuleType("PySide6")
        qt.QtCore = SimpleNamespace(QObject=object, Signal=lambda *_a: SimpleNamespace(emit=lambda: None))
        spec = importlib.util.spec_from_file_location(
            "aisland.core._config_safety_test", PROJECT / "aisland/core/config.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"aisland.core.runtime": runtime, "PySide6": qt}):
            spec.loader.exec_module(module)
        return module, path, state / "data"

    def test_corrupt_config_is_never_overwritten(self):
        broken = "{ broken json"
        module, path, data_dir = self.load_module(broken)
        module.cfg.set("island.theme", "sky")   # triggers save()
        module.cfg.save()                        # shutdown() calls this unconditionally
        self.assertEqual(path.read_text(encoding="utf-8"), broken)
        backups = list(data_dir.glob("config.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), broken)
        # The current session still works, it just does not persist.
        self.assertEqual(module.cfg.get("island.theme"), "sky")

    def test_corrupt_config_save_reports_to_the_user_once(self):
        """界面修改不会落盘时必须提醒用户（save_failed 信号），且每次会话只提醒一次。"""
        module, path, _ = self.load_module("{ broken json")
        events = []
        module.Config.save_failed = SimpleNamespace(emit=lambda: events.append(True))
        module.cfg.set("island.theme", "sky")
        module.cfg.set("island.edge", "bottom")
        self.assertEqual(events, [True])
        self.assertEqual(path.read_text(encoding="utf-8"), "{ broken json")

    def test_reload_after_repair_resumes_saving(self):
        module, path, _ = self.load_module("{ broken json")
        path.write_text(json.dumps(USER_CONFIG, ensure_ascii=False), encoding="utf-8")
        module.cfg.load()
        module.cfg.set("island.theme", "sky")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["translate"]["api_key"], "FICTIONAL_USER_KEY")
        self.assertEqual(saved["weather"]["city"], "合肥")
        self.assertEqual(saved["island"]["theme"], "sky")

    def test_load_failure_is_surfaced_and_repair_resets_the_guard(self):
        """读取失败本身也要提示；修复后再次损坏仍会提示，而不是整个会话静默。"""
        module, path, _ = self.load_module("{ broken json")
        events = []
        module.Config.save_failed = SimpleNamespace(emit=lambda: events.append(True))
        self.assertTrue(module.cfg.load_failed)
        module.cfg.load()                                # 重新加载仍是坏的：要提示
        self.assertEqual(events, [True])
        path.write_text(json.dumps(USER_CONFIG, ensure_ascii=False), encoding="utf-8")
        module.cfg.load()
        self.assertFalse(module.cfg.load_failed)
        self.assertEqual(events, [True])                 # 修复本身不发提示
        path.write_text("{ broken again", encoding="utf-8")
        module.cfg.load()
        self.assertEqual(events, [True, True])           # 再损坏要再次提示

    def test_concurrent_saves_do_not_collide(self):
        module, path, _ = self.load_module(json.dumps(USER_CONFIG, ensure_ascii=False))
        errors = []

        def worker(index):
            try:
                for step in range(15):
                    module.cfg.set("island.pos", index * 100 + step)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(saved["island"]["pos"], int)
        self.assertEqual(saved["translate"]["api_key"], "FICTIONAL_USER_KEY")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_translation_key_is_retained_on_upgrade_and_reset_on_reinstall(self):
        from aisland.core.translation_settings import Connection, INSTALLATION_MARKER, PROTECTED_KEY, PROVIDERS

        config = {**USER_CONFIG, "translate": {**USER_CONFIG["translate"], "import_from": "old-settings.json"}}
        module, path, _ = self.load_module(json.dumps(config))
        marker = self.root / INSTALLATION_MARKER
        first = uuid4()
        marker.write_text(f"upgrade:{{{first}}}", encoding="ascii")
        module.cfg.bind_translation_installation(self.root)
        self.assertEqual(module.cfg.get("translate.api_key"), "FICTIONAL_USER_KEY")
        self.assertEqual(module.cfg.get("translate.install_id"), str(first))
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(saved["translate"]["api_key"])
        self.assertTrue(saved["translate"][PROTECTED_KEY])

        marker.write_text(f"fresh:{{{uuid4()}}}", encoding="ascii")
        module.cfg.bind_translation_installation(self.root)
        reset = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(reset["translate"]["api_key"], "")
        self.assertNotIn(PROTECTED_KEY, reset["translate"])
        self.assertEqual(reset["translate"]["import_from"], "")
        self.assertFalse(reset["translate"]["use_bundled_defaults"])
        self.assertEqual(reset["weather"]["city"], "合肥")
        self.assertEqual(reset["island"]["theme"], "dark")

        vendor = PROVIDERS[1]
        module.cfg.save_translation_connection(Connection("REENTERED_TEST_KEY", vendor.base_url, vendor.model))
        module.cfg.load()
        self.assertEqual(module.cfg.get("translate.api_key"), "REENTERED_TEST_KEY")
        self.assertEqual(module.cfg.get("translate.base_url"), vendor.base_url)
        self.assertEqual(module.cfg.get("translate.model"), vendor.model)
        self.assertNotIn("REENTERED_TEST_KEY", path.read_text(encoding="utf-8"))
        module.cfg.set("translate.base_url", "https://different.example/v1")
        self.assertFalse(module.cfg.get("translate.api_key"))
        module.cfg.set("translate.base_url", vendor.base_url)
        self.assertEqual(module.cfg.get("translate.api_key"), "REENTERED_TEST_KEY")

    def test_fresh_install_rejects_a_key_imported_from_another_profile(self):
        from aisland.core.translation_settings import INSTALLATION_MARKER

        module, path, _ = self.load_module(json.dumps(USER_CONFIG))
        (self.root / INSTALLATION_MARKER).write_text(f"fresh:{uuid4()}", encoding="ascii")
        module.cfg.bind_translation_installation(self.root)
        self.assertEqual(module.cfg.get("translate.api_key"), "")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["island"]["theme"], "dark")


if __name__ == "__main__":
    unittest.main()
