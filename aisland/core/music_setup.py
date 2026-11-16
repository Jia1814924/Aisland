"""Apply a local, pending music-folder request at application startup.

The old running app may save its configuration on exit, so external setup stages
only this small request. No directories are scanned and no commands are run.
"""
from __future__ import annotations

import copy
import json
import ntpath
from pathlib import Path, PureWindowsPath


_ERROR = "Invalid local music directory setup request"
_MAX_REQUEST_BYTES = 64 * 1024


def _validated_list(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(_ERROR)
    result = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(_ERROR)
        text = entry.strip()
        path = PureWindowsPath(text)
        if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":" or
                not path.drive[0].isascii() or not path.drive[0].isalpha() or
                text.startswith(("\\\\", "//")) or ":" in text[2:] or
                ".." in path.parts or any(ord(character) < 32 for character in text) or
                any(character in '<>"|?*' for character in text)):
            raise ValueError(_ERROR)
        result.append(text)
    return result


def _merge(existing: list[str], pending: list[str]) -> list[str]:
    result, seen = [], set()
    for path in existing + pending:
        identity = ntpath.normcase(ntpath.normpath(path))
        if identity not in seen:
            seen.add(identity)
            result.append(path)
    return result


def apply_pending_music_dirs(config, pending_path: Path) -> bool:
    """Merge an exact staged request, saving before removing the pending file.

    Missing/unreadable requests return False; malformed input raises a generic
    ValueError without its contents. On save failure restore the previous in-
    memory setting and retain the request. A successful save returns True even
    if cleanup is blocked: the retained request can safely be applied again.
    """
    path = Path(pending_path)
    try:
        if path.is_symlink():
            raise ValueError(_ERROR)
        with path.open("rb") as stream:
            raw = stream.read(_MAX_REQUEST_BYTES + 1)
    except OSError:
        return False
    try:
        if len(raw) > _MAX_REQUEST_BYTES:
            raise ValueError(_ERROR)
        request = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(request, dict) or set(request) != {"music_dirs"}:
            raise ValueError(_ERROR)
        pending = _validated_list(request["music_dirs"])
        original = copy.deepcopy(config.get("media.music_dirs", []))
        existing = _validated_list(original)
        merged = _merge(existing, pending)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(_ERROR) from None
    try:
        config.set("media.music_dirs", merged, save=True)
    except Exception:
        # Config.set can update memory before its atomic file replacement fails.
        try:
            config.set("media.music_dirs", original, save=False)
        except Exception:
            raise ValueError("Music directory settings could not be restored") from None
        return False
    try:
        # A newer setup request must not be removed after applying an older one.
        if path.read_bytes() == raw:
            path.unlink()
    except OSError:
        pass
    return True
