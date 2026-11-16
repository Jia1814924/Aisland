"""Incremental payload selection and native installer preflight/data preservation."""
from __future__ import annotations

import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_update", PROJECT / "scripts/prepare_update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class IncrementalBuildTests(unittest.TestCase):
    def setUp(self):
        # 被中断的测试进程会留下 incremental-* 目录：每次开跑先清场。
        for stale in (PROJECT / "build").glob("incremental-*"):
            shutil.rmtree(stale, ignore_errors=True)
        temporary = tempfile.TemporaryDirectory(prefix="incremental-", dir=PROJECT / "build")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "base"
        self.target = self.root / "target"
        self.write(self.base, "Aisland.exe", b"old application")
        self.write(self.base, "_internal/code.zip", b"old code")
        self.write(self.base, "_internal/models/voice.onnx", b"unchanged model" * 100)
        self.write(self.base, "_internal/obsolete.dll", b"obsolete")
        shutil.copytree(self.base, self.target)
        self.write(self.target, "Aisland.exe", b"new application")
        self.write(self.target, "_internal/code.zip", b"new code")
        self.write(self.target, "_internal/new/nested.dll", b"new dependency")
        (self.target / "_internal/obsolete.dll").unlink()
        self.previous = update.snapshot(self.base, "1.0.21")
        self.current = update.snapshot(self.target, "1.0.22")
        self.plan_dir = self.root / "plan"

    @staticmethod
    def write(root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def plan(self):
        return update.prepare_plan(self.previous, self.current, self.target, self.plan_dir)

    def test_only_changed_and_new_files_are_packed(self):
        plan = self.plan()
        self.assertEqual(set(plan["changed"]), {"Aisland.exe", "_internal/code.zip", "_internal/new/nested.dll"})
        self.assertEqual(plan["removed"], ["_internal/obsolete.dll"])
        self.assertGreater(plan["reused_bytes"], plan["changed_bytes"])
        self.assertNotIn("voice.onnx", (self.plan_dir / "files.iss").read_text(encoding="utf-8-sig"))
        checks = (self.plan_dir / "update-checks.txt").read_text(encoding="utf-8-sig")
        self.assertIn(self.previous["files"]["Aisland.exe"]["sha256"], checks)
        self.assertIn("-|_internal\\new\\nested.dll", checks)
        delete = (self.plan_dir / "delete.iss").read_text(encoding="utf-8-sig")
        self.assertNotIn("filesandordirs", delete)
        self.assertNotIn("*", delete)

    def test_manifest_keeps_no_absolute_paths_and_is_immutable(self):
        output = self.root / "published.json"
        update.save_manifest(self.previous, output)
        update.save_manifest(self.previous, output)
        self.assertNotIn(str(self.root), output.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "different published"):
            update.save_manifest({**self.current, "version": "1.0.21"}, output)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), self.previous)

    def test_manifest_rejects_traversal_private_paths_and_script_injection(self):
        paths = ("../outside", "_internal/../../outside", "_internal/x:stream", "_internal/{app}",
                 "_internal/*.dll", '_internal/x"; Flags: deleteafterinstall', "_internal/x\nfile",
                 "config.json", "unins000.dat", "_internal/private_defaults.json", "_internal/data/db",
                 "_internal/models/dict/private.db", "_internal/nul.txt", "_internal/dot.")
        for name in paths:
            with self.subTest(name=name), self.assertRaises(ValueError):
                update.validate_path(name)

    def test_private_bundle_is_rejected_before_hash_manifest_is_written(self):
        self.write(self.target, "_internal/private_defaults.json", b"private-test-placeholder")
        with self.assertRaisesRegex(ValueError, "Personal state"):
            update.snapshot(self.target, "1.0.22")

    def test_invalid_hash_case_collision_and_downgrade_are_rejected(self):
        bad = copy.deepcopy(self.previous)
        bad["files"]["_internal/VOICE.dll"] = bad["files"]["Aisland.exe"]
        bad["files"]["_internal/voice.dll"] = bad["files"]["Aisland.exe"]
        with self.assertRaisesRegex(ValueError, "collision"):
            update.validate_manifest(bad)
        bad = copy.deepcopy(self.previous)
        bad["files"]["Aisland.exe"]["sha256"] = "broken"
        with self.assertRaises(ValueError):
            update.validate_manifest(bad)
        for version in ("1.0.20", "1.0.21"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "older base"):
                update.prepare_plan(self.previous, {**self.current, "version": version}, self.target, self.plan_dir)

    def test_file_directory_transition_requires_full_installer(self):
        self.write(self.target, "_internal/obsolete.dll/child", b"new directory")
        target = update.snapshot(self.target, "1.0.22")
        with self.assertRaisesRegex(ValueError, "File/directory"):
            update.prepare_plan(self.previous, target, self.target, self.plan_dir)

    @unittest.skipUnless(os.name == "nt" and (PROJECT / ".build-tools/InnoSetup/ISCC.exe").exists(),
                         "Native installer compiler is not installed")
    def test_native_update_preserves_models_data_and_rejects_bad_bases(self):
        self.plan()
        native = self.root / "native"
        native.mkdir()
        cleanup = base64.b64encode((PROJECT / "installer/clear_translation_key.ps1").read_text(
            encoding="utf-8").encode("utf-16-le")).decode("ascii")
        command = [str(PROJECT / ".build-tools/InnoSetup/ISCC.exe"), "/Qp", "/DAppVersion=1.0.22",
                   "/DInstallerTestMode=1", "/DIncrementalUpdate=1", "/DUpdateBaseVersion=1.0.21",
                   "/DProjectRoot=" + str(PROJECT), "/DTranslationCleanupCommand=" + cleanup,
                   "/DUpdateFiles=" + str(self.plan_dir / "files.iss"),
                   "/DUpdateDeletes=" + str(self.plan_dir / "delete.iss"),
                   "/DUpdateChecks=" + str(self.plan_dir / "update-checks.txt"),
                   "/O" + str(native), str(PROJECT / "installer/Aisland.iss")]
        result = subprocess.run(command, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))
        installer = native / "Aisland-Update-1.0.21-to-1.0.22-x64-test.exe"
        result = subprocess.run(["icacls.exe", str(installer), "/setintegritylevel", "M"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace"))

        def installed(name):
            root = self.root / name
            shutil.copytree(self.base, root)
            shutil.copy2(PROJECT / "installer/aisland-install.marker", root)
            self.write(root, "translation-install.id", b"keep-installation-identity")
            self.write(root, "config.json", b"keep-config")
            self.write(root, "data/history.json", b"keep-history")
            self.write(root, "_internal/user-plugin.txt", b"keep-unknown-file")
            return root

        def apply(root):
            env = dict(os.environ, AISLAND_USER_HOME=str(self.root / "profile"))
            result = subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                     "/NORUN=1", "/DIR=" + str(root), "/LOG=" + str(self.root / "install.log")],
                                    env=env, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            return result.returncode

        valid = installed("valid")
        model = valid / "_internal/models/voice.onnx"
        os.utime(model, (1_700_000_000, 1_700_000_000))
        modified = model.stat().st_mtime_ns
        self.assertEqual(apply(valid), 0)
        self.assertEqual(update.snapshot(self.target, "1.0.22"), self.current)
        for name, metadata in self.current["files"].items():
            self.assertEqual((valid / name).read_bytes(), (self.target / name).read_bytes())
        self.assertEqual(model.stat().st_mtime_ns, modified)
        self.assertFalse((valid / "_internal/obsolete.dll").exists())
        for path, content in (("config.json", b"keep-config"), ("data/history.json", b"keep-history"),
                              ("_internal/user-plugin.txt", b"keep-unknown-file"),
                              ("translation-install.id", b"keep-installation-identity")):
            self.assertEqual((valid / path).read_bytes(), content)

        for case in ("wrong-version", "missing-model", "new-file-conflict", "fresh", "already-updated"):
            with self.subTest(case=case):
                root = installed(case)
                if case == "wrong-version":
                    self.write(root, "Aisland.exe", b"other version")
                elif case == "missing-model":
                    (root / "_internal/models/voice.onnx").unlink()
                elif case == "new-file-conflict":
                    self.write(root, "_internal/new/nested.dll", b"user-owned-file")
                elif case == "fresh":
                    (root / "aisland-install.marker").unlink()
                else:
                    self.write(root, "Aisland.exe", b"new application")
                before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                self.assertNotEqual(apply(root), 0)
                after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                self.assertEqual(before, after)

        # A newly added dependency must not be written through a directory junction.
        root = installed("linked-directory")
        external = self.root / "outside-update"
        external.mkdir()
        junction = root / "_internal/new"
        script = "New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''") + \
                 "' -Target '" + str(external).replace("'", "''") + "' | Out-Null"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        try:
            self.assertNotEqual(apply(root), 0)
            self.assertEqual((root / "Aisland.exe").read_bytes(), b"old application")
            self.assertEqual(list(external.iterdir()), [])
        finally:
            os.rmdir(junction)  # Remove the junction itself without traversing its target.


if __name__ == "__main__":
    unittest.main()
