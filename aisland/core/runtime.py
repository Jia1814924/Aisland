"""Runtime locations and one-time, local import from an older source install.

This module deliberately has no Qt imports or filesystem side effects. Packaged
resources are read from the bundle; all mutable state belongs to the current user.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimePaths:
    frozen: bool
    install_root: Path
    resource_root: Path
    state_root: Path
    user_home_overridden: bool = False

    @property
    def config_path(self) -> Path:
        return self.state_root / "config.json"

    @property
    def data_dir(self) -> Path:
        return self.state_root / "data"


STATE_DIR_NAME = "Aisland"
# 1.0.x 及更早版本用旧名字建过用户数据目录，升级时整目录接管，配置与词库不丢。
LEGACY_STATE_DIR_NAME = "Wisland"
LEGACY_HOME_ENV = "WISLAND_USER_HOME"


def _local_app_data(env: Mapping[str, str]) -> Path:
    local = env.get("LOCALAPPDATA")
    return Path(local) if local else Path.home() / "AppData" / "Local"


def resolve_paths(*, source_root: Path | None = None, frozen: bool | None = None,
                  executable: Path | None = None, bundle_root: Path | None = None,
                  environ: Mapping[str, str] | None = None) -> RuntimePaths:
    """Resolve locations without creating them; arguments also permit isolated tests."""
    env = os.environ if environ is None else environ
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    source = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
    exe = Path(executable or sys.executable).resolve()
    install = exe.parent if is_frozen else source
    resource = Path(bundle_root or getattr(sys, "_MEIPASS", install)).resolve() if is_frozen else source
    # 旧环境变量只在没有新变量时兜底，已有脚本/便携包不必改。
    override = (env.get("AISLAND_USER_HOME") or env.get(LEGACY_HOME_ENV) or "").strip()
    if override:
        state = Path(os.path.expandvars(os.path.expanduser(override))).resolve()
    elif is_frozen:
        state = _local_app_data(env) / STATE_DIR_NAME
    else:
        state = source
    return RuntimePaths(is_frozen, install, resource, state, bool(override))


PATHS = resolve_paths()
IS_FROZEN = PATHS.frozen
INSTALL_ROOT = PATHS.install_root
RESOURCE_ROOT = PATHS.resource_root
STATE_ROOT = PATHS.state_root
CONFIG_PATH = PATHS.config_path
DATA_DIR = PATHS.data_dir
USER_HOME_OVERRIDDEN = PATHS.user_home_overridden


def resource_path(relative: str | Path) -> Path:
    """Support both PyInstaller's internal data directory and adjacent resources."""
    bundled = RESOURCE_ROOT / relative
    adjacent = INSTALL_ROOT / relative
    return bundled if bundled.exists() or not adjacent.exists() else adjacent


LOG_ROTATE_BYTES = 2 * 1024 * 1024


def rotate_log(log_path: Path, limit: int = LOG_ROTATE_BYTES) -> bool:
    """启动时轮转日志：超过 limit 就翻一代（只保留一代旧日志）。

    逐条追加的 stderr/stdout（如 soundcard 的告警）会让日志长期增长；
    每次启动检查一次即可封顶，日志缺失或读不了大小时静默跳过。
    """
    try:
        if log_path.stat().st_size <= limit:
            return False
        log_path.replace(log_path.with_name(log_path.name + ".1"))
        return True
    except OSError:
        return False


def adopt_legacy_state(paths: RuntimePaths = PATHS,
                       environ: Mapping[str, str] | None = None) -> str:
    """安装版首次启动时接管旧品牌的用户数据目录；目标已存在就什么都不做。

    优先整目录改名（原子、最快）。旧版进程还开着时改名会失败，退化为复制，
    并且不带上锁文件、日志和 SQLite 边车文件。
    """
    if not paths.frozen or paths.user_home_overridden:
        return "skipped"
    target = paths.state_root
    if target.exists():
        return "exists"
    env = os.environ if environ is None else environ
    legacy = _local_app_data(env) / LEGACY_STATE_DIR_NAME
    if not legacy.is_dir():
        return "no_legacy"
    status = "renamed"
    try:
        legacy.rename(target)
    except OSError:
        status = "copied"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
            "*.lock", "*.log", "*-wal", "*-shm", "SetupCache"))
    _remap_clipboard(target / "data" / "clipboard.json", legacy / "data", target / "data")
    return status


def autostart_command(paths: RuntimePaths = PATHS, executable: Path | None = None) -> str:
    exe = Path(executable or sys.executable)
    if paths.frozen:
        return subprocess.list2cmdline([str(exe)])
    windowless = exe.with_name("pythonw.exe")
    if windowless.exists():
        exe = windowless
    return subprocess.list2cmdline([str(exe), str(paths.install_root / "run.pyw")])


def _copy_snapshot(source: Path, destination: Path) -> None:
    """Copy SQLite through its backup API so committed WAL contents are included."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".db":
        with source.open("rb") as f:
            sqlite_header = f.read(16) == b"SQLite format 3\x00"
        if sqlite_header:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as src:
                with closing(sqlite3.connect(str(destination))) as dst:
                    src.backup(dst)
            return
    shutil.copy2(source, destination)


def _copy_data_tree(source: Path, destination: Path) -> int:
    if not source.is_dir():
        return 0
    copied = 0
    for item in source.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        # Locks, process logs and SQLite sidecars must not become a new install's state.
        if item.suffix.lower() in (".lock", ".log", ".tmp", ".part") or item.name.endswith(("-shm", "-wal")):
            continue
        _copy_snapshot(item.resolve(), destination / item.relative_to(source))
        copied += 1
    return copied


def _remap_clipboard(path: Path, old_data: Path, new_data: Path) -> None:
    if not path.exists():
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for item in value.get("items", []):
        if not isinstance(item, dict) or not item.get("image_path"):
            continue
        old_image = Path(item["image_path"])
        if not old_image.is_absolute():
            old_image = old_data.parent / old_image
        try:
            relative = old_image.resolve().relative_to(old_data.resolve())
        except ValueError:
            continue
        item["image_path"] = str(new_data / relative)
        changed = True
    if changed:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _publish_missing(source: Path, destination: Path) -> bool:
    """Publish a complete staged file without replacing an existing user file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Staging is on the same volume; NTFS hard links publish atomically.
        os.link(source, destination)
        return True
    except FileExistsError:
        return False
    except OSError:
        # Also work on volumes that do not implement hard links.
        created = False
        try:
            with destination.open("xb") as dest:
                created = True
                with source.open("rb") as src:
                    shutil.copyfileobj(src, dest)
            return True
        except FileExistsError:
            return False
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise


@contextmanager
def _migration_stage(state_root: Path):
    # Inherit the per-user state directory ACL instead of tempfile's Windows DACL.
    # This also permits verification in a restricted Windows app container.
    stage = state_root / (".aisland-import-" + uuid.uuid4().hex)
    stage.mkdir()
    try:
        yield stage
    finally:
        if stage.resolve().parent != state_root.resolve() or not stage.name.startswith(".aisland-import-"):
            raise RuntimeError("迁移临时目录不在用户数据目录内")
        shutil.rmtree(stage)


def import_legacy_data(source_root: str | Path, paths: RuntimePaths = PATHS) -> dict:
    """Import only before the first config is created; never replace existing user state.

    Secrets are read and copied on this computer only, never included in the installer.
    Data is staged before a config is committed, so failed imports can be retried.
    """
    if paths.config_path.exists():
        return {"status": "kept_existing", "copied_files": 0}
    source = Path(source_root).expanduser().resolve()
    source_config = source / "config.json"
    if not source_config.is_file():
        raise ValueError("所选旧版目录没有 config.json")
    raw_config = source_config.read_text(encoding="utf-8-sig")
    config = json.loads(raw_config)
    if not isinstance(config, dict):
        raise ValueError("旧版配置必须是 JSON 对象")
    paths.state_root.mkdir(parents=True, exist_ok=True)
    with _migration_stage(paths.state_root) as stage:
        copied = _copy_data_tree(source / "data", stage / "data")
        _remap_clipboard(stage / "data" / "clipboard.json", source / "data", paths.data_dir)
        # Dictionaries are writable SQLite data, not versioned program resources.
        model_setting = ((config.get("live") or {}).get("models_dir") or "models")
        model_root = Path(os.path.expandvars(os.path.expanduser(str(model_setting))))
        if not model_root.is_absolute():
            model_root = source / model_root
        copied += _copy_data_tree(model_root / "dict", stage / "dictionary")
        # A custom relative model directory may be outside the shipped default models.
        if str(model_setting) != "models" and not Path(str(model_setting)).is_absolute():
            if model_root.is_dir():
                config.setdefault("live", {})["models_dir"] = str(model_root)
                raw_config = json.dumps(config, ensure_ascii=False, indent=2)
        for staged in stage.rglob("*"):
            if not staged.is_file():
                continue
            target = paths.state_root / staged.relative_to(stage)
            _publish_missing(staged, target)
        # Commit a complete config last; an interrupted import can be retried.
        staged_config = stage / "config.json"
        staged_config.write_text(raw_config, encoding="utf-8")
        if not _publish_missing(staged_config, paths.config_path):
            return {"status": "kept_existing", "copied_files": copied}
    return {"status": "imported", "copied_files": copied}


def acquire_app_mutex() -> tuple[int, bool]:
    """Keep a named handle alive so installers and second launches can detect the app."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, r"Local\Aisland.App")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle), ctypes.get_last_error() == 183


def release_app_mutex(handle: int | None) -> None:
    if handle:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CloseHandle(handle)
