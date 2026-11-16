"""Identify supported desktop AI clients from their executable identity."""
from __future__ import annotations

from pathlib import PureWindowsPath


_EXECUTABLES = {
    "codex.exe": "codex",
    "workbuddy.exe": "workbuddy",
    "deepseek harness.exe": "deepseek_harness",
}
_PROVIDERS = frozenset(_EXECUTABLES.values())


def identify_ai_application(path: str, mappings: dict | None = None) -> str | None:
    """Use the package path when a shared executable name is ambiguous.

    Store builds of Codex use ChatGPT.exe inside the OpenAI.Codex package.
    Matching ChatGPT.exe alone would also select the ordinary ChatGPT app.
    Explicit user process mappings take precedence over the built-in rules.
    """
    executable = PureWindowsPath(path or "")
    name = executable.name.casefold()
    if not name:
        return None
    for key, entry in (mappings or {}).items():
        if str(key).casefold() == name and isinstance(entry, dict):
            provider = entry.get("kind")
            return provider if provider in _PROVIDERS else None
    if name in _EXECUTABLES:
        return _EXECUTABLES[name]
    parts = tuple(part.casefold() for part in executable.parts)
    if (name == "chatgpt.exe" and len(parts) >= 4
            and parts[-2] == "app"
            and parts[-3].startswith("openai.codex_")
            and parts[-3].endswith("__2p2nqsd0c76g0")):
        return "codex"
    return None
