"""Best-effort maintenance logging; no application or Qt initialization."""
from __future__ import annotations

import tempfile
import traceback
from pathlib import Path


def log_import_failure(data_dir: Path, error_text: str | None = None) -> Path | None:
    """Log an import failure without turning a permissions error into a crash.

    The original traceback is captured before attempting filesystem operations.
    A blocked user-data directory must not prevent the installer receiving exit 1.
    """
    detail = traceback.format_exc() if error_text is None else error_text
    candidates = [Path(data_dir) / "import-error.log"]
    try:
        candidates.append(Path(tempfile.gettempdir()) / "aisland-import-error.log")
    except Exception:
        pass
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(detail)
                if not detail.endswith("\n"):
                    stream.write("\n")
            return path
        except Exception:
            continue
    return None
