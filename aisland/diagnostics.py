"""Explicit offline packaging check; does not start monitoring or network services."""
from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", type=Path, required=True)
    parser.add_argument("--skip-model-load", action="store_true")
    args = parser.parse_args()
    # Self-test must not touch real user configuration/history.
    temporary = None
    if not os.environ.get("AISLAND_USER_HOME"):
        temporary = tempfile.TemporaryDirectory(prefix="aisland-check-")
        os.environ["AISLAND_USER_HOME"] = temporary.name
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    report = {"version": None, "frozen": bool(getattr(sys, "frozen", False)),
              "checks": [], "network_used": False, "audio_capture_started": False}

    def check(name, fn):
        started = time.monotonic()
        try:
            detail = fn()
            report["checks"].append({"name": name, "ok": True, "detail": detail,
                                      "seconds": round(time.monotonic() - started, 3)})
        except Exception as exc:
            report["checks"].append({"name": name, "ok": False,
                "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})

    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from .core.config import DATA_DIR, CONFIG_PATH, cfg
        from .core.runtime import RESOURCE_ROOT, STATE_ROOT
        from .version import __version__
    except Exception as exc:
        report["ok"] = False
        report["checks"].append({"name": "framework_bootstrap", "ok": False,
            "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        args.self_test.parent.mkdir(parents=True, exist_ok=True)
        args.self_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if temporary is not None:
            temporary.cleanup()
        return 1
    report["version"] = __version__
    report["paths"] = {"resources": str(RESOURCE_ROOT), "state": str(STATE_ROOT),
                       "config": str(CONFIG_PATH), "data": str(DATA_DIR)}

    def icon_check():
        icon = QtGui.QIcon(str(RESOURCE_ROOT / "assets" / "aisland.ico"))
        assert not icon.isNull(), "Application icon missing"
        assert not icon.pixmap(32, 32).isNull(), "Application icon cannot render"
        return [f"{s.width()}x{s.height()}" for s in icon.availableSizes()]
    check("application_icon", icon_check)

    def personal_defaults_check():
        from .core.personal_defaults import load_personal_defaults
        personal = load_personal_defaults(RESOURCE_ROOT, frozen=report["frozen"])
        if not personal:
            return {"included": False}
        translation = personal["translate"]
        effective = str(cfg.get("translate.api_key") or "")
        assert effective, "Private translation defaults were not applied"
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {"included": True, "key_configured": bool(effective),
                "matches_bundled": effective == translation["api_key"],
                "written_to_user_config": saved.get("translate", {}).get("api_key") == translation["api_key"]}
    check("personal_translation_defaults", personal_defaults_check)

    modules = (
        "httpx", "ssl", "certifi", "psutil", "yaml", "numpy", "comtypes", "pycaw.pycaw",
        "soxr", "soundcard", "sherpa_onnx", "fugashi", "unidic_lite", "genanki",
        "winrt.windows.media", "winrt.windows.media.control", "winrt.windows.devices.radios",
        "winrt.windows.devices.bluetooth", "winrt.windows.foundation",
        "winrt.windows.foundation.collections", "winrt.windows.storage.streams",
        "aisland.live.pipeline", "aisland.ui.island", "aisland.ui.pages.home",
        "aisland.ui.pages.info", "aisland.ui.pages.tools", "aisland.ui.pages.system",
        "mutagen", "mutagen.flac", "mutagen.id3", "mutagen.mp4", "aisland.services.embedded_lyrics",
    )
    for module in modules:
        check("import:" + module, lambda name=module: str(getattr(importlib.import_module(name), "__version__", "ok")))

    def storage_check():
        assert CONFIG_PATH.is_relative_to(STATE_ROOT)
        assert DATA_DIR.is_relative_to(STATE_ROOT)
        cfg.set("packaging_self_test", "preserved")
        cfg.load()
        assert cfg.get("packaging_self_test") == "preserved"
        from .learn.dictionary import DB_PATH, Dictionary
        assert DB_PATH.is_relative_to(STATE_ROOT), "Writable dictionary is inside install resources"
        d = Dictionary()
        d.list_dicts()
        return "Config roundtrip and writable dictionary OK"
    check("persistent_storage", storage_check)

    def tokenizer_check():
        from .learn.tokenizer import available, error, tokenize
        assert available(), error()
        tokens = tokenize("今日は良い天気ですね。")
        assert len(tokens) > 2, "Dictionary was not bundled"
        return [token.surface for token in tokens]
    check("offline_japanese_tokenizer", tokenizer_check)

    def anki_check():
        from .learn.anki import export_apkg
        dest = STATE_ROOT / "diagnostic-empty.apkg"
        export_apkg([], dest, "Aisland Packaging Check")
        assert dest.stat().st_size > 0
        dest.unlink()
        return "Offline APKG export OK"
    check("offline_anki_export", anki_check)

    def embedded_lyrics_check():
        from mutagen.id3 import ID3, USLT
        from types import SimpleNamespace
        from .services.embedded_lyrics import extract_lyrics
        path = STATE_ROOT / "diagnostic-lyrics.mp3"
        tags = ID3()
        tags.add(USLT(encoding=3, lang="eng", desc="", text="[00:01.00]<00:01.00>Bright <00:01.50>morning\n[00:02.00]Quiet evening"))
        try:
            tags.save(path)
            before = path.read_bytes()
            result = extract_lyrics(SimpleNamespace(tags=ID3(path)))
            assert result and result["lines"] == [[1.0, "Bright morning"], [2.0, "Quiet evening"]]
            assert path.read_bytes() == before, "Audio tag reading modified its input"
            return {"synced_lines": 2, "enhanced_lrc_cleaned": True, "read_only": True}
        finally:
            path.unlink(missing_ok=True)
    check("offline_embedded_lyrics", embedded_lyrics_check)

    from .live.paths import MODELS
    for relative in ("silero_vad.onnx", "parakeet-ja-0.6b-int8/model.int8.onnx",
                     "parakeet-ja-0.6b-int8/tokens.txt", "sense-voice-int8/model.int8.onnx",
                     "sense-voice-int8/tokens.txt"):
        def file_check(name=relative):
            path = MODELS / name
            assert path.is_file(), f"Missing model {name}"
            return path.stat().st_size
        check("model_file:" + relative, file_check)

    if not args.skip_model_load:
        for engine, language in (("parakeet", "ja"), ("sensevoice", "auto")):
            def load_model(e=engine, lang=language):
                import numpy as np
                from .live.asr import FinalASR
                recognizer = FinalASR(e, num_threads=2, language=lang)
                recognizer.load()
                result = recognizer.transcribe(np.zeros(16000, dtype=np.float32))
                del recognizer
                gc.collect()
                return {"loaded": True, "silent_sample_seconds": result.audio_seconds}
            check("offline_asr:" + engine, load_model)

        def vad_check():
            import numpy as np
            import sherpa_onnx
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(MODELS / "silero_vad.onnx")
            config.sample_rate = 16000
            vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=2)
            vad.accept_waveform(np.zeros(16000, dtype=np.float32))
            return "VAD loaded and processed synthetic silence"
        check("offline_vad", vad_check)

    report["ok"] = all(item["ok"] for item in report["checks"])
    args.self_test.parent.mkdir(parents=True, exist_ok=True)
    args.self_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "checks": len(report["checks"]),
                      "failed": [item["name"] for item in report["checks"] if not item["ok"]]}, ensure_ascii=False))
    del app
    if temporary is not None:
        # SQLite handles may still be alive until process exit on Windows.
        temporary._ignore_cleanup_errors = True
        temporary.cleanup()
    return 0 if report["ok"] else 1
