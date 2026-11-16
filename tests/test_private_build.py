"""Public-build guards, using fictional settings and non-executable fixtures only."""
from __future__ import annotations

import ast
import os
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid


PROJECT = Path(__file__).resolve().parents[1]


def load_tool(name):
    path = PROJECT / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("private_build_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrivateBuildTests(unittest.TestCase):
    def setUp(self):
        self.area = PROJECT / "build" / "private-build-tests"
        # 被中断的测试进程会留下上一轮的目录：每次开跑先清场，目录不无限累积。
        shutil.rmtree(self.area, ignore_errors=True)
        self.area.mkdir(parents=True, exist_ok=True)
        self.root = self.area / uuid.uuid4().hex
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.prepare = load_tool("prepare_personal_defaults")
        self.verify = load_tool("verify_distribution")
        self.translation = {"api_key": "FICTIONAL-KEY-NEVER-VALID",
                            "base_url": "https://translation.invalid", "model": "fixture-model"}

    def cleanup(self):
        if self.root.resolve().parent != self.area.resolve():
            raise RuntimeError("Unexpected private-build test cleanup path")
        shutil.rmtree(self.root)

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_spec_rejects_private_environment_flag_before_collecting_resources(self):
        tree = ast.parse((PROJECT / "Aisland.spec").read_text(encoding="utf-8"))
        guard = next(node for node in tree.body if isinstance(node, ast.If)
                     and "AISLAND_BUNDLE_PERSONAL_DEFAULTS" in ast.unparse(node.test))
        code = compile(ast.Module(body=[guard], type_ignores=[]), "Aisland.spec", "exec")
        for value in ("1", "0", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ,
                    {"AISLAND_BUNDLE_PERSONAL_DEFAULTS": value}):
                if value == "1":
                    with self.assertRaisesRegex(ValueError, "cannot bundle"):
                        exec(code, {"os": os})
                else:
                    exec(code, {"os": os})
        self.assertNotIn("private_defaults.json", ast.unparse(tree))

    def test_build_always_prepares_and_verifies_credential_free_package(self):
        script = (PROJECT / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("$WithoutPersonalDefaults = $true", script)
        self.assertIn("$env:AISLAND_BUNDLE_PERSONAL_DEFAULTS = '0'", script)
        prepare = next(line for line in script.splitlines() if line.startswith("$prepareArgs ="))
        verify = next(line for line in script.splitlines() if line.startswith("$verifyArgs ="))
        self.assertIn("'--without-personal-defaults'", prepare)
        self.assertIn("'--without-personal-defaults'", verify)
        self.assertNotIn("'--personal-defaults'", script)

    def test_installer_rejects_private_resources_and_does_not_adopt_old_keys(self):
        installer = (PROJECT / "installer" / "Aisland.iss").read_text(encoding="utf-8")
        for relative in (r"\dist\Aisland\private_defaults.json",
                         r"\dist\Aisland\_internal\private_defaults.json"):
            guard = installer.split('#if FileExists(ProjectRoot + "' + relative + '")', 1)[1].split("#endif", 1)[0]
            self.assertIn("#error", guard)
        self.assertNotIn("StagedTranslationDefaults", installer)
        self.assertNotIn("PreviousDefaults", installer)
        self.assertNotIn("--adopt-translation-defaults", installer)

    def invoke_prepare(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(self.prepare, "__file__", str(self.root / "scripts" / "prepare.py")), \
             mock.patch.object(sys, "argv", ["prepare_personal_defaults.py", *arguments]), \
             redirect_stdout(output):
            status = self.prepare.main()
        return status, output.getvalue()

    def test_default_preparation_never_reads_config_and_removes_stale_credentials(self):
        self.write_json("config.json", {"translate": self.translation, "private": "DO-NOT-COPY"})
        seed = self.write_json("build/personal-defaults/private_defaults.json", {"translate": self.translation})
        pending = seed.with_suffix(".tmp")
        pending.write_text("FICTIONAL-PARTIAL-KEY", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("read user config")):
            status, output = self.invoke_prepare()
        self.assertEqual(status, 0)
        self.assertFalse(seed.exists())
        self.assertFalse(pending.exists())
        self.assertEqual(json.loads(output), {"personal_defaults_included": False})
        self.assertNotIn(self.translation["api_key"], output)
        self.assertNotIn("DO-NOT-COPY", output)

    def test_invalid_config_does_not_prevent_credential_cleanup(self):
        config = self.root / "config.json"
        config.write_text("invalid JSON", encoding="utf-8")
        status, output = self.invoke_prepare()
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(output)["personal_defaults_included"])
        self.assertEqual(config.read_text(encoding="utf-8"), "invalid JSON")

    def test_without_personal_defaults_removes_staged_seed_without_reading_config(self):
        seed = self.write_json("build/personal-defaults/private_defaults.json", {"translate": self.translation})
        self.assertFalse((self.root / "config.json").exists())
        status, output = self.invoke_prepare("--without-personal-defaults")
        self.assertEqual(status, 0)
        self.assertFalse(seed.exists())
        self.assertFalse(json.loads(output)["personal_defaults_included"])

    def fixture_bundle(self):
        bundle = self.root / "dist" / "Aisland"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "Aisland.exe").write_bytes(b"THIS IS NOT AN EXECUTABLE")
        version = self.root / "aisland" / "version.py"
        version.parent.mkdir(exist_ok=True)
        version.write_text('__version__ = "1.0.3"\n', encoding="utf-8")
        return bundle

    def invoke_guard(self, *arguments):
        self.fixture_bundle()
        with mock.patch.object(self.verify, "__file__", str(self.root / "scripts" / "verify.py")), \
             mock.patch.object(sys, "argv", ["verify_distribution.py", *arguments]), \
             mock.patch.object(self.verify.subprocess, "run") as run:
            try:
                self.verify.main()
            finally:
                run.assert_not_called()

    def test_skip_freeze_rejects_missing_changed_and_overbroad_seed_before_launch(self):
        expected = self.write_json("requested-seed.json", {"translate": self.translation})
        arguments = ("--personal-defaults", str(expected))
        with self.assertRaisesRegex(AssertionError, "defaults missing"):
            self.invoke_guard(*arguments)
        seed_path = "dist/Aisland/_internal/private_defaults.json"
        self.write_json(seed_path, {"translate": {**self.translation, "api_key": "DIFFERENT-FICTIONAL-KEY"}})
        with self.assertRaisesRegex(AssertionError, "differ"):
            self.invoke_guard(*arguments)
        overbroad = {"translate": self.translation, "mail": {"password": "FICTIONAL-MAIL"}}
        self.write_json("requested-seed.json", overbroad)
        self.write_json(seed_path, overbroad)
        with self.assertRaisesRegex(AssertionError, "sections"):
            self.invoke_guard(*arguments)

    def test_credential_free_guard_rejects_previous_personal_bundle_before_launch(self):
        self.write_json("dist/Aisland/_internal/private_defaults.json", {"translate": self.translation})
        with self.assertRaisesRegex(AssertionError, "credential-free"):
            self.invoke_guard("--without-personal-defaults")

    def test_credential_free_guard_rejects_adjacent_seed_before_launch(self):
        self.write_json("dist/Aisland/private_defaults.json", {"translate": self.translation})
        with self.assertRaisesRegex(AssertionError, "internal resource directory"):
            self.invoke_guard("--without-personal-defaults")

    def test_personal_verifier_requires_effective_non_persisted_default(self):
        self.fixture_bundle()
        expected = self.write_json("requested-seed.json", {"translate": self.translation})
        self.write_json("dist/Aisland/_internal/private_defaults.json", {"translate": self.translation})
        good = {"included": True, "key_configured": True, "matches_bundled": True,
                "written_to_user_config": False}
        cases = [(good, 0)]
        for field in ("included", "key_configured", "matches_bundled", "written_to_user_config"):
            cases.append(({**good, field: not good[field]}, 1))
        cases.append(({}, 1))
        for detail, expected_status in cases:
            with self.subTest(detail=detail):
                def fake_run(command, **_options):
                    report = {"version": "1.0.3", "ok": True,
                              "checks": [{"name": "personal_translation_defaults", "ok": True,
                                          "detail": detail}]}
                    Path(command[-1]).write_text(json.dumps(report), encoding="utf-8")
                    return mock.Mock(returncode=0)

                output = io.StringIO()
                with mock.patch.object(self.verify, "__file__", str(self.root / "scripts" / "verify.py")), \
                     mock.patch.object(sys, "argv", ["verify_distribution.py", "--personal-defaults", str(expected)]), \
                     mock.patch.object(self.verify.subprocess, "run", side_effect=fake_run), redirect_stdout(output):
                    self.assertEqual(self.verify.main(), expected_status)
                self.assertNotIn(self.translation["api_key"], output.getvalue())


if __name__ == "__main__":
    unittest.main()
