"""Private release defaults, read only from frozen application resources.

This module uses only the standard library. A bundled credential is a runtime
fallback for its own endpoint, never a replacement for a user credential.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit


def _endpoint(value: object) -> tuple | None:
    """Compare complete HTTPS API bases, including their case-sensitive paths."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment):
            return None
        return ("https", parsed.hostname.lower(), parsed.port or 443, parsed.path.rstrip("/"))
    except ValueError:
        return None


def _validated(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("translate"), dict):
        return {}
    translation = value["translate"]
    fields = {}
    for field in ("api_key", "base_url", "model"):
        content = translation.get(field)
        if not isinstance(content, str) or not content.strip():
            return {}
        content = content.strip()
        if any(ord(character) < 32 for character in content):
            return {}
        fields[field] = content
    if _endpoint(fields["base_url"]) is None:
        return {}
    fields["base_url"] = fields["base_url"].rstrip("/")
    return {"translate": fields}


def load_personal_defaults(resource_root: Path, *, frozen: bool) -> dict:
    """Load an allowlisted triple from the bundle, never a source/user profile.

    Missing or invalid private resources behave like a public release. Failures
    intentionally produce no log message containing the resource contents.
    """
    if not frozen:
        return {}
    try:
        root = Path(resource_root).resolve()
        candidate = root / "private_defaults.json"
        if candidate.is_symlink() or candidate.resolve().parent != root:
            return {}
        return _validated(json.loads(candidate.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError):
        return {}


def apply_personal_defaults(config: dict, user: dict, personal: dict) -> dict:
    """Return effective configuration without mutating any input.

    ``config`` already contains normal defaults plus user overrides. A complete
    private triple can fill an absent/blank user key only when import_from is
    blank, bundled defaults are enabled, and the user has no API base or has the
    same complete base. A nonblank user model remains selected on that endpoint.
    Callers must persist the ordinary configuration, not this effective key.
    """
    output = copy.deepcopy(config)
    private = _validated(personal)
    if not private:
        return output
    selected = user.get("translate", {})
    if not isinstance(selected, dict):
        return output
    translation = output.get("translate")
    if not isinstance(translation, dict):
        return output
    if selected.get("use_bundled_defaults", translation.get("use_bundled_defaults", False)) is not True:
        return output
    # Match the existing consumers' truthiness/stripping for user credentials.
    if str(selected.get("api_key") or "").strip():
        return output
    if str(selected.get("import_from") or "").strip():
        return output
    base = selected.get("base_url")
    bundled = private["translate"]
    if base and (not isinstance(base, str) or base.strip()):
        if _endpoint(base) != _endpoint(bundled["base_url"]):
            return output
    translation["api_key"] = bundled["api_key"]
    # Preserve user spelling of an equivalent base; always fill blank bases.
    translation["base_url"] = base.strip().rstrip("/") if isinstance(base, str) and base.strip() else bundled["base_url"]
    model = selected.get("model")
    translation["model"] = model.strip() if isinstance(model, str) and model.strip() else bundled["model"]
    return output
