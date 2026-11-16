"""Private-release credential fallback checks using only fictional credentials."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from aisland.core.personal_defaults import apply_personal_defaults, load_personal_defaults

PRIVATE = {"translate": {"api_key": "FICTIONAL_BUNDLED_KEY",
                         "base_url": "https://private-api.example/v1", "model": "private-model"}}
DEFAULT = {"translate": {"api_key": "", "base_url": "https://public-api.example",
                         "model": "public-model", "import_from": "", "use_bundled_defaults": True},
           "island": {"theme": "auto"}}


class PersonalDefaultsTests(unittest.TestCase):
    def setUp(self):
        area = PROJECT / "build" / "personal-defaults-tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def seed(self, content=PRIVATE):
        path = self.root / "private_defaults.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def effective(self, translation=None):
        user = {"translate": translation or {}}
        config = copy.deepcopy(DEFAULT)
        config["translate"].update(user["translate"])
        return apply_personal_defaults(config, user, PRIVATE)

    def test_source_mode_does_not_read_private_resource(self):
        self.seed()
        with patch.object(Path, "read_text", side_effect=AssertionError("source read private defaults")):
            self.assertEqual(load_personal_defaults(self.root, frozen=False), {})

    def test_frozen_loads_only_allowed_resource_fields(self):
        content = copy.deepcopy(PRIVATE)
        content["translate"]["import_from"] = "DO_NOT_IMPORT"
        content["clipboard"] = {"max": 999}
        self.seed(content)
        self.assertEqual(load_personal_defaults(self.root, frozen=True), PRIVATE)
        # Looking elsewhere must not discover this file as a profile fallback.
        other = self.root / "different-bundle"
        other.mkdir()
        self.assertEqual(load_personal_defaults(other, frozen=True), {})

    def test_invalid_or_incomplete_resources_fail_closed(self):
        path = self.seed()
        for content in ([], {"translate": {"api_key": "FICTIONAL"}},
                        {"translate": {**PRIVATE["translate"], "base_url": "http://private-api.example"}},
                        {"translate": {**PRIVATE["translate"], "base_url": "https://name:pass@private-api.example"}}):
            with self.subTest(content_type=type(content).__name__):
                path.write_text(json.dumps(content), encoding="utf-8")
                self.assertEqual(load_personal_defaults(self.root, frozen=True), {})
        path.write_text("invalid JSON", encoding="utf-8")
        self.assertEqual(load_personal_defaults(self.root, frozen=True), {})

    def test_new_profile_uses_complete_private_triple_without_mutating_inputs(self):
        original = copy.deepcopy(DEFAULT)
        user = {"island": {"theme": "sky"}}
        output = apply_personal_defaults(DEFAULT, user, PRIVATE)
        for field, value in PRIVATE["translate"].items():
            self.assertEqual(output["translate"][field], value)
        self.assertEqual(DEFAULT, original)
        self.assertEqual(user, {"island": {"theme": "sky"}})

    def test_blank_key_and_equivalent_base_can_use_private_key(self):
        output = self.effective({"api_key": "  ", "base_url": "https://PRIVATE-API.example:443/v1/",
                                 "model": "user-model"})
        self.assertEqual(output["translate"]["api_key"], "FICTIONAL_BUNDLED_KEY")
        self.assertEqual(output["translate"]["model"], "user-model")

    def test_user_key_and_import_source_take_precedence(self):
        for selected in ({"api_key": "FICTIONAL_USER_KEY", "base_url": "https://other.example"},
                         {"import_from": "user-settings.json"}, {"use_bundled_defaults": False}):
            with self.subTest(selected=selected):
                output = self.effective(selected)
                self.assertEqual(output["translate"]["api_key"], selected.get("api_key", ""))

    def test_different_hosts_ports_paths_or_schemes_never_receive_private_key(self):
        for base in ("https://other.example/v1", "https://private-api.example:444/v1",
                     "https://private-api.example/v2", "http://private-api.example/v1",
                     "https://private-api.example/v1?redirect=1", "https://private-api.example/v1#fragment"):
            with self.subTest(base=base):
                self.assertFalse(self.effective({"base_url": base})["translate"]["api_key"])

    def load_config_module(self, user=None, frozen=True):
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        config_path = state / "config.json"
        if user is not None:
            config_path.write_text(json.dumps(user), encoding="utf-8")
        runtime = ModuleType("aisland.core.runtime")
        runtime.CONFIG_PATH = config_path
        runtime.DATA_DIR = state / "data"
        runtime.RESOURCE_ROOT = self.root
        runtime.IS_FROZEN = frozen
        qt = ModuleType("PySide6")
        qt.QtCore = SimpleNamespace(QObject=object, Signal=lambda *_args: SimpleNamespace(emit=lambda: None))
        spec = importlib.util.spec_from_file_location("aisland.core._config_private_test", PROJECT / "aisland/core/config.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"aisland.core.runtime": runtime, "PySide6": qt}):
            spec.loader.exec_module(module)
        return module, config_path

    def test_config_save_reload_and_endpoint_changes_do_not_persist_borrowed_key(self):
        self.seed()
        module, path = self.load_config_module({"translate": {"use_bundled_defaults": True}})
        config = module.cfg
        self.assertEqual(config.get("translate.api_key"), "FICTIONAL_BUNDLED_KEY")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["translate"].get("api_key", ""), "")
        config.set("island.theme", "sky")
        config.load()
        self.assertEqual(config.get("translate.api_key"), "FICTIONAL_BUNDLED_KEY")
        config.set("translate.base_url", "https://different.example/v1", save=False)
        self.assertFalse(config.get("translate.api_key"))
        config.save()
        config.load()
        self.assertFalse(config.get("translate.api_key"))
        self.assertEqual(config.get("island.theme"), "sky")
        config.set("translate.base_url", PRIVATE["translate"]["base_url"])
        self.assertEqual(config.get("translate.api_key"), "FICTIONAL_BUNDLED_KEY")
        config.set("translate.use_bundled_defaults", False)
        self.assertFalse(config.get("translate.api_key"))
        config.load()
        self.assertFalse(config.get("translate.api_key"))

    def test_existing_user_configuration_and_history_are_preserved_on_load(self):
        self.seed()
        user = {"translate": {"api_key": "FICTIONAL_USER_KEY", "base_url": "https://other.example",
                              "model": "user-model"}, "island": {"theme": "dark"}}
        state = self.root / "state"
        state.mkdir()
        history = state / "history.json"
        history.write_text("FICTIONAL_HISTORY", encoding="utf-8")
        module, path = self.load_config_module(user)
        original = path.read_bytes()
        self.assertEqual(module.cfg.get("translate.api_key"), "FICTIONAL_USER_KEY")
        self.assertEqual(module.cfg.get("translate.base_url"), "https://other.example")
        self.assertEqual(module.cfg.get("island.theme"), "dark")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(history.read_text(encoding="utf-8"), "FICTIONAL_HISTORY")
        module.cfg.set("island.theme", "light")
        module.cfg.load()
        self.assertEqual(module.cfg.get("translate.api_key"), "FICTIONAL_USER_KEY")

    def test_public_default_does_not_borrow_bundled_credentials(self):
        self.seed()
        module, _ = self.load_config_module()
        self.assertFalse(module.cfg.get("translate.api_key"))
        self.assertFalse(module.cfg.get("translate.use_bundled_defaults"))

    def test_public_upgrade_never_imports_old_private_key_or_changes_user_key(self):
        self.seed()
        for user_key in ("", "FICTIONAL_USER_KEY"):
            with self.subTest(user_key=user_key):
                module, path = self.load_config_module({"translate": {"api_key": user_key,
                    "base_url": "https://user-api.example", "model": "user-model"}}, frozen=False)
                config = module.cfg
                config._translation_installation = SimpleNamespace(id="test-upgrade", upgrade=True)
                previous = path.read_bytes()
                with patch.object(module, "load_personal_defaults",
                                  side_effect=AssertionError("Read old private package")):
                    config.adopt_translation_defaults(self.root)
                self.assertEqual(config.get("translate.api_key"), user_key)
                self.assertEqual(config.get("translate.base_url"), "https://user-api.example")
                self.assertEqual(path.read_bytes(), previous)

    def test_source_config_does_not_use_private_defaults(self):
        self.seed()
        module, _ = self.load_config_module(frozen=False)
        self.assertFalse(module.cfg.get("translate.api_key"))


if __name__ == "__main__":
    unittest.main()
