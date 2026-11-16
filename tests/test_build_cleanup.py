"""Structural regressions for build.ps1 release cleanup.

A full build is far too heavy for the unit suite, so these checks lock the
cleanup invariants; build.ps1 itself is syntax-checked by PowerShell in CI.
"""
from __future__ import annotations

import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = (PROJECT / "build.ps1").read_text(encoding="utf-8")


class BuildCleanupTests(unittest.TestCase):
    def test_staged_payload_is_removed_in_finally(self):
        self.assertIn("finally", SCRIPT)
        finally_block = SCRIPT.split("finally", 1)[1]
        self.assertIn("$nativeStage", finally_block)
        self.assertIn("Remove-Item", finally_block)
        self.assertIn("-ErrorAction SilentlyContinue", finally_block)
        # Guard against ever deleting the staging root itself.
        self.assertIn("$stageRoot", finally_block)
        self.assertIn("$nativeStage -ne $stageRoot", finally_block)

    def test_compilation_is_inside_try(self):
        stage = SCRIPT.index("New-Item -ItemType Directory -Path $nativeStage")
        try_start = SCRIPT.index("try {", stage)
        bootstrap = SCRIPT.index("build_setup_bootstrap.py")
        finally_start = SCRIPT.index("finally", stage)
        self.assertLess(stage, try_start)
        self.assertLess(try_start, bootstrap)
        self.assertLess(bootstrap, finally_start)

    def test_release_refreshes_hash_and_retires_only_old_installers(self):
        release = SCRIPT.split("if (-not $TestInstaller)", 1)[1].split(
            "Get-Item -LiteralPath", 1)[0]
        self.assertIn("Get-ChildItem", release)
        self.assertIn("Aisland-Setup-*.exe", release)
        self.assertIn("$old.Name -ne $setupName", release)
        self.assertIn("*-test.exe", release)
        self.assertIn("Get-FileHash -Algorithm SHA256", release)
        self.assertIn("SHA256.txt", release)
        self.assertIn("Set-Content", release)

    def test_cleanup_never_targets_project_root(self):
        self.assertNotIn("Remove-Item -LiteralPath $PSScriptRoot", SCRIPT)


class TestAreaPruneTests(unittest.TestCase):
    """被中断的测试进程留下的草稿目录：每个测试区开跑前必须清场，不无限累积。"""

    PRUNES = {
        "test_private_build.py": "shutil.rmtree(self.area, ignore_errors=True)",
        "test_config_safety.py": "shutil.rmtree(area, ignore_errors=True)",
        "test_runtime.py": "shutil.rmtree(self.test_area, ignore_errors=True)",
        "test_incremental_build.py": 'glob("incremental-*")',
        "test_resource_lifecycle.py": 'glob("aisland-resources-*")',
    }

    def test_every_scratch_area_is_pruned_before_a_run(self):
        for name, marker in self.PRUNES.items():
            content = (PROJECT / "tests" / name).read_text(encoding="utf-8")
            self.assertIn(marker, content, f"{name} 缺少开跑清场")


if __name__ == "__main__":
    unittest.main()
