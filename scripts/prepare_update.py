"""Record public release hashes and generate file-level incremental installers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat


def version_tuple(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("Invalid release version")
    result = tuple(map(int, value.split(".")))
    if any(part > 65535 for part in result):
        raise ValueError("Release version component too large")
    return result


def validate_path(value: str) -> str:
    if not isinstance(value, str) or not value or re.search(r'[\\:*?"<>|{}\x00-\x1f]', value):
        raise ValueError("Unsafe release path")
    parts = value.split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
    if any(part in ("", ".", "..") or part.endswith((" ", ".")) or
           part.split(".")[0].casefold() in reserved for part in parts):
        raise ValueError("Unsafe release path")
    lowered = value.casefold()
    if lowered != "aisland.exe" and not lowered.startswith("_internal/"):
        raise ValueError("Only packaged application files may be updated")
    if lowered in ("_internal/config.json", "_internal/private_defaults.json", "_internal/data",
                    "_internal/models/dict") or \
            lowered.startswith(("_internal/data/", "_internal/models/dict/")):
        raise ValueError("Personal state is not a public release resource")
    return value


def validate_manifest(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("platform") != "win-x64":
        raise ValueError("Unsupported release manifest")
    version_tuple(data.get("version", ""))
    files = data.get("files")
    if not isinstance(files, dict) or "Aisland.exe" not in files:
        raise ValueError("Application executable missing from manifest")
    names = set()
    for name, entry in files.items():
        key = validate_path(name).casefold()
        if key in names:
            raise ValueError("Case-insensitive release path collision")
        names.add(key)
        if not isinstance(entry, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256"))) or \
                type(entry.get("size")) is not int or entry["size"] < 0:
            raise ValueError("Invalid release file metadata")
    if any(parent.as_posix().casefold() in names for name in files
           for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
        raise ValueError("Release file conflicts with a directory")
    return data


def snapshot(bundle: Path, version: str) -> dict:
    version_tuple(version)
    files = {}
    pending = [bundle.absolute()]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Linked release resources are not supported")
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.is_file():
            name = validate_path(path.relative_to(bundle.absolute()).as_posix())
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files[name] = {"size": info.st_size, "sha256": digest}
    return validate_manifest({"schema": 1, "platform": "win-x64", "version": version,
                              "files": dict(sorted(files.items()))})


def save_manifest(data: dict, output: Path) -> None:
    validate_manifest(data)
    if output.exists():
        if json.loads(output.read_text(encoding="utf-8")) != data:
            raise ValueError("This version already has a different published manifest; use a new version")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def prepare_plan(base: dict, current: dict, bundle: Path, output: Path) -> dict:
    validate_manifest(base)
    validate_manifest(current)
    if version_tuple(base["version"]) >= version_tuple(current["version"]):
        raise ValueError("An incremental update requires an older base version")
    previous = {name.casefold(): (name, entry) for name, entry in base["files"].items()}
    following = {name.casefold(): (name, entry) for name, entry in current["files"].items()}
    # File/directory replacements need a full installer; never recursively delete here.
    for names, other in ((previous, following), (following, previous)):
        if any(parent.as_posix().casefold() in other for name in names
               for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
            raise ValueError("File/directory replacement requires the full installer")
    changed = [name for key, (name, entry) in following.items()
               if key not in previous or previous[key][1] != entry]
    removed = [name for key, (name, _entry) in previous.items() if key not in following]
    if not changed:
        raise ValueError("No changed program files to update")
    checks = [f"{entry['sha256']}|{name.replace('/', chr(92))}"
              for name, entry in base["files"].items()]
    checks += [f"-|{name.replace('/', chr(92))}" for key, (name, _) in following.items() if key not in previous]
    output.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in changed:
        destination = PurePosixPath(name).parent.as_posix().replace("/", "\\")
        destination = "{app}" + ("\\" + destination if destination != "." else "")
        source = str((bundle / name).absolute()).replace('"', '""')
        lines.append(f'Source: "{source}"; DestDir: "{destination}"; Flags: ignoreversion')
    (output / "files.iss").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    deletions = [f'Type: files; Name: "{{app}}\\{name.replace("/", chr(92))}"' for name in removed]
    (output / "delete.iss").write_text("\n".join(deletions) + "\n", encoding="utf-8-sig")
    (output / "update-checks.txt").write_text("\n".join(checks) + "\n", encoding="utf-8-sig")
    plan = {"base_version": base["version"], "version": current["version"],
            "name": f"Aisland-Update-{base['version']}-to-{current['version']}-x64.exe",
            "changed": changed, "removed": removed,
            "changed_bytes": sum(current["files"][name]["size"] for name in changed),
            "reused_bytes": sum(entry["size"] for key, (_, entry) in following.items()
                                if key in previous and previous[key][1] == entry)}
    (output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", type=Path)
    options = parser.parse_args()
    current = snapshot(options.bundle, options.version)
    if options.base:
        base = json.loads(options.base.read_text(encoding="utf-8"))
        plan = prepare_plan(base, current, options.bundle, options.output)
        print(json.dumps({key: value for key, value in plan.items() if key not in ("changed", "removed")}))
    else:
        save_manifest(current, options.output)
        print(json.dumps({"version": current["version"], "files": len(current["files"]),
                          "manifest": str(options.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
