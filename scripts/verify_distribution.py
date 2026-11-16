"""Verify the frozen distribution using isolated data and no Python on PATH."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=root / "dist" / "Aisland")
    parser.add_argument("--work", type=Path, default=root / "build" / "bundle-checks")
    parser.add_argument("--ui", action="store_true")
    personal = parser.add_mutually_exclusive_group()
    personal.add_argument("--personal-defaults", type=Path)
    personal.add_argument("--without-personal-defaults", action="store_true")
    options = parser.parse_args()
    bundle = options.bundle.resolve()
    work = options.work.resolve() / uuid.uuid4().hex
    work.mkdir(parents=True)
    executable = bundle / "Aisland.exe"
    assert executable.is_file(), "Frozen executable missing"
    version_tree = ast.parse((root / "aisland" / "version.py").read_text(encoding="utf-8"))
    expected_version = next(ast.literal_eval(node.value) for node in version_tree.body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets))
    for private in ("config.json", "data", "_internal/config.json", "_internal/data", "_internal/models/dict"):
        assert not (bundle / private).exists(), f"Private state was bundled: {private}"
    defaults_path = bundle / "_internal" / "private_defaults.json"
    assert not (bundle / "private_defaults.json").exists(), "Personal defaults must only appear in the internal resource directory"
    if options.without_personal_defaults:
        assert not defaults_path.exists(), "A credential-free build must not contain personal defaults; freeze again"
    if options.personal_defaults:
        assert defaults_path.is_file(), "Personal translation defaults missing; freeze again"
        expected = json.loads(options.personal_defaults.read_text(encoding="utf-8"))
        actual = json.loads(defaults_path.read_text(encoding="utf-8"))
        assert actual == expected, "Personal defaults differ from the requested build; freeze again"
        assert set(actual) == {"translate"}, "Unexpected personal defaults sections"
        assert set(actual["translate"]) == {"api_key", "base_url", "model"}, "Unexpected personal defaults fields"
        assert str(actual["translate"]["api_key"]).strip(), "Empty bundled translation key"
    for path in (bundle / "_internal").rglob("*.dll"):
        assert path.name.lower() not in ("icuuc.dll", "icuin.dll", "icudt.dll"), "Foreign ICU must not shadow Windows ICU"

    env = {key.upper(): value for key, value in os.environ.items()}
    windows = Path(env.get("SYSTEMROOT", r"C:\Windows"))
    env["PATH"] = os.pathsep.join([str(windows / "System32"), str(windows)])
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    outcomes = []
    commands = [("self-test", "--self-test")]
    if options.ui:
        commands.append(("ui", "--verify-ui"))
    for label, flag in commands:
        state = work / (label + "-state")
        output = work / (label + ".json")
        env["AISLAND_USER_HOME"] = str(state)
        process = subprocess.run([str(executable), flag, str(output)], cwd=bundle, env=env,
                                 timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {"ok": False, "missing_output": True}
        if result.get("version") != expected_version:
            result["ok"] = False
            result["version_mismatch"] = {"expected": expected_version, "actual": result.get("version")}
        if options.personal_defaults and label == "self-test":
            defaults_check = next((item for item in result.get("checks", [])
                                   if item.get("name") == "personal_translation_defaults"), {})
            detail = defaults_check.get("detail", {})
            if not (defaults_check.get("ok") and detail.get("included") and
                    detail.get("key_configured") and detail.get("matches_bundled") and
                    detail.get("written_to_user_config") is False):
                result["ok"] = False
                result["personal_defaults_verification_failed"] = True
        outcomes.append({"name": label, "exit_code": process.returncode, "result": result})
        if process.returncode != 0 or not result.get("ok"):
            break
    report = {"ok": all(v["exit_code"] == 0 and v["result"].get("ok") for v in outcomes),
              "bundle": str(bundle), "python_on_path": False, "checks": outcomes}
    (work / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(work / "result.json"),
                      "checks": [{"name": x["name"], "exit_code": x["exit_code"], "ok": x["result"].get("ok")} for x in outcomes]}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
