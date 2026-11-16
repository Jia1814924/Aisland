"""Explicit read-only Windows access diagnostics, before app/Qt initialization."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import uuid


def _token_details() -> dict:
    if os.name != "nt":
        return {"supported": False}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.GetTokenInformation.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        def dword_value(kind: int) -> int:
            value, needed = wintypes.DWORD(), wintypes.DWORD()
            if not advapi.GetTokenInformation(token, kind, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            return value.value

        result = {"is_app_container": bool(dword_value(29)),
                  "elevation_type": dword_value(18), "elevated": bool(dword_value(20))}
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 25, buffer, len(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_MANDATORY_LABEL begins with SID_AND_ATTRIBUTES; only the SID is
        # needed. The final sub-authority is the integrity RID, not a user ID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        advapi.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
        advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        count = advapi.GetSidSubAuthorityCount(sid)[0]
        rid = advapi.GetSidSubAuthority(sid, count - 1)[0]
        result["integrity_rid"] = rid
        result["integrity_level"] = ("system" if rid >= 16384 else "high" if rid >= 12288
                                     else "medium" if rid >= 8192 else "low")
        return result
    finally:
        kernel.CloseHandle(token)


def _known_local_app_data() -> str | None:
    if os.name != "nt":
        return None
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_wchar_p))
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    folder_id = ctypes.create_string_buffer(uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le)
    value = ctypes.c_wchar_p()
    status = shell.SHGetKnownFolderPath(folder_id, 0, None, ctypes.byref(value))
    if status < 0:
        raise OSError("SHGetKnownFolderPath failed with HRESULT 0x%08X" % (status & 0xFFFFFFFF))
    try:
        return value.value
    finally:
        ole.CoTaskMemFree(value)


def _path_status(path: Path) -> dict:
    result = {"path": str(path)}
    try:
        result["exists"] = path.exists()
        result["is_directory"] = path.is_dir()
        result["read_access_advisory"] = os.access(path, os.R_OK)
        if result["is_directory"]:
            # Opening a directory handle without enumerating any names/content.
            with os.scandir(path):
                pass
            result["directory_handle_readable"] = True
    except Exception as error:
        result["error"] = {"type": type(error).__name__, "winerror": getattr(error, "winerror", None)}
    return result


def collect_access_report() -> dict:
    report = {"frozen": bool(getattr(sys, "frozen", False)), "filesystem_write_probe": False}
    for name, function in (("token", _token_details), ("known_folder_local_app_data", _known_local_app_data)):
        try:
            report[name] = function()
        except Exception as error:
            report[name] = {"error": type(error).__name__, "winerror": getattr(error, "winerror", None)}
    local = os.environ.get("LOCALAPPDATA")
    report["environment_local_app_data"] = local
    override = (os.environ.get("AISLAND_USER_HOME") or "").strip()
    expected = Path(os.path.expandvars(os.path.expanduser(override))) if override else (
        (Path(local) if local else Path.home() / "AppData" / "Local") / "Aisland")
    report["user_home_overridden"] = bool(override)
    report["expected_user_state"] = _path_status(expected)
    report["expected_parent"] = _path_status(expected.parent)
    known = report.get("known_folder_local_app_data")
    if isinstance(known, str) and local:
        report["known_folder_matches_environment"] = os.path.normcase(known) == os.path.normcase(local)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Windows access diagnostics")
    parser.add_argument("--diagnose-access", type=Path, required=True)
    options = parser.parse_args()
    output = options.diagnose_access.expanduser().resolve()
    result = collect_access_report()
    # The sole write is the caller's explicitly requested report destination.
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
