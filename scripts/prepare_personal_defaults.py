"""Remove stale private build resources before creating a public package.

This public source distribution never reads config.json or extracts a key.
The old command-line flag remains accepted for existing build commands.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--without-personal-defaults", action="store_true",
                        help="Compatibility option; personal defaults are always excluded")
    parser.parse_args()
    output = root / "build" / "personal-defaults" / "private_defaults.json"
    output.unlink(missing_ok=True)
    output.with_suffix(".tmp").unlink(missing_ok=True)
    print(json.dumps({"personal_defaults_included": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
