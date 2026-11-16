"""Wrap the native Inno installer in a single-file, isolated-temp launcher."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest().upper()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    payload = options.payload.resolve()
    output = options.output.resolve()
    import pefile
    with pefile.PE(str(payload), fast_load=True) as pe:
        if pe.OPTIONAL_HEADER.DATA_DIRECTORY[14].VirtualAddress:
            raise ValueError("Payload is already a .NET bootstrap; use the native Inno installer.")
    version_tree = ast.parse((root / "aisland" / "version.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in version_tree.body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets))
    payload_hash = digest(payload)
    if payload == output:
        preserved = root / "build" / "installer-payload" / (payload_hash + ".exe")
        preserved.parent.mkdir(parents=True, exist_ok=True)
        if not preserved.exists():
            shutil.copy2(payload, preserved)
        if digest(preserved) != payload_hash:
            raise ValueError("Preserved native payload differs from input")
        payload = preserved
    stage = root / "build" / "setup-bootstrap" / payload_hash[:16]
    stage.mkdir(parents=True, exist_ok=True)
    source = (root / "installer" / "SetupBootstrap.cs").read_text(encoding="utf-8")
    source = source.replace("__VERSION__", version).replace("__PAYLOAD_SHA256__", payload_hash)
    source = source.replace("__PAYLOAD_LENGTH__", str(payload.stat().st_size))
    generated = stage / "SetupBootstrap.cs"
    generated.write_text(source, encoding="utf-8-sig")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    compiler = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise FileNotFoundError("The Windows .NET Framework compiler was not found")
    staged_exe = stage / output.name
    command = [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
        "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
        "/win32icon:" + str(root / "assets" / "aisland.ico"),
        "/win32manifest:" + str(root / "installer" / "SetupBootstrap.manifest"),
        "/resource:" + str(payload) + ",Aisland.SetupPayload",
        "/out:" + str(staged_exe), str(generated)]
    subprocess.run(command, cwd=root, check=True, timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
    if staged_exe.stat().st_size < payload.stat().st_size:
        raise ValueError("Embedded installer resource is incomplete")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Staging and destination are inside the project; replacement is atomic.
    os.replace(staged_exe, output)
    info = {"version": version, "installer_revision": 3, "output": str(output),
        "bytes": output.stat().st_size, "sha256": digest(output),
        "native_payload": str(payload), "native_payload_sha256": payload_hash,
        "temp_scope": "Child process only; stable application-owned cache",
        "bootstrap_requires_admin": False,
        "installation_requires_admin": not output.name.endswith("-test.exe")}
    (stage / "result.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
