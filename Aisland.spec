# -*- mode: python ; coding: utf-8 -*-
"""Standalone x64 public distribution; bundled credentials are prohibited."""
from pathlib import Path
import os
import runpy
import sys
from comtypes.client import GetModule
from PyInstaller.utils.hooks import (
    collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata,
)
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
    VarFileInfo, VarStruct,
)

root = Path(SPECPATH)
if os.environ.get("AISLAND_BUNDLE_PERSONAL_DEFAULTS") == "1":
    raise ValueError("Public builds cannot bundle personal translation credentials")
# Build shells can inherit DLL directories from unrelated desktop tools. Restrict
# dependency discovery to this environment and Windows, especially for ICU/UCRT.
windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
os.environ["PATH"] = os.pathsep.join(str(p) for p in (
    root / ".venv" / "Scripts", Path(sys.base_prefix), Path(sys.base_prefix) / "DLLs",
    windows / "System32", windows, windows / "System32" / "Wbem",
))
product = runpy.run_path(str(root / "aisland" / "version.py"))
app_name = product["APP_NAME"]
version = product["__version__"]
version_tuple = tuple(int(x) for x in version.split(".")) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple,
                      mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("CompanyName", app_name),
        StringStruct("FileDescription", f"{app_name} Desktop Island"),
        StringStruct("FileVersion", version),
        StringStruct("InternalName", "Aisland"),
        StringStruct("OriginalFilename", "Aisland.exe"),
        StringStruct("ProductName", app_name),
        StringStruct("ProductVersion", version),
        StringStruct("LegalCopyright", "MIT License; see bundled third-party notices"),
    ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
)

datas = [
    (str(root / "assets" / "aisland.ico"), "assets"),
    (str(root / "assets" / "aisland.png"), "assets"),
    (str(root / "LICENSE"), "."),
    (str(root / "aisland" / "live" / "LICENSE-YishengTranslator.txt"), "licenses"),
    (str(root / ".venv" / "Lib" / "site-packages" / "mutagen-1.48.1.dist-info" / "licenses" / "COPYING"), "licenses/mutagen"),
]
for relative in (
    "silero_vad.onnx",
    "parakeet-ja-0.6b-int8/model.int8.onnx",
    "parakeet-ja-0.6b-int8/tokens.txt",
    "sense-voice-int8/model.int8.onnx",
    "sense-voice-int8/tokens.txt",
):
    path = root / "models" / relative
    if not path.is_file():
        raise FileNotFoundError(f"Required offline model missing: {path}")
    datas.append((str(path), str(Path("models") / Path(relative).parent)))

# SoundCard reads its C declarations with open(); MeCab needs its dictionary.
for package in ("soundcard", "unidic_lite", "fugashi", "certifi"):
    datas += collect_data_files(package)
binaries = []
for package in ("sherpa_onnx", "soxr", "fugashi"):
    binaries += collect_dynamic_libs(package)
for path in (root / ".venv" / "Lib" / "site-packages" / "fugashi.libs").glob("*.dll"):
    binaries.append((str(path), "fugashi.libs"))

# Prepare the accessibility bindings at build time for read-only installations.
GetModule(str(windows / "System32" / "UIAutomationCore.dll"))
# Windows Runtime and COM packages load projection modules dynamically.
hiddenimports = []
for package in ("winrt", "pycaw", "comtypes", "sherpa_onnx", "fugashi", "unidic_lite", "genanki", "mutagen"):
    hiddenimports += collect_submodules(package, filter=lambda n: ".tests" not in n and ".test" not in n)
for distribution in (
    "sherpa-onnx", "sherpa-onnx-core", "fugashi", "unidic-lite", "soundcard",
    "genanki", "comtypes", "pycaw", "winrt-runtime", "mutagen",
):
    datas += copy_metadata(distribution)

a = Analysis(
    [str(root / "run.pyw")], pathex=[str(root)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False, optimize=0,
)
allowed_binary_roots = (root.resolve(), Path(sys.base_prefix).resolve(), windows.resolve())
foreign_binaries = [source for _, source, _ in a.binaries
                    if not any(Path(source).resolve().is_relative_to(base) for base in allowed_binary_roots)]
if foreign_binaries:
    raise RuntimeError("Unexpected external DLL sources: " + repr(foreign_binaries))
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Aisland",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    icon=str(root / "assets" / "aisland.ico"), version=version_info,
    contents_directory="_internal",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Aisland")
