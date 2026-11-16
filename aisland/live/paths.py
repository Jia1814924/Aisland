# -*- coding: utf-8 -*-
"""实时字幕模型位置；默认模型使用安装包内的只读资源。"""
from __future__ import annotations

from pathlib import Path

from ..core.config import cfg, expand
from ..core.runtime import resource_path


def models_dir() -> Path:
    p = Path(expand(cfg.get("live.models_dir") or "models"))
    return p if p.is_absolute() else resource_path(p)


MODELS = models_dir()

LANGUAGE_OPTIONS = (("auto", "自动"), ("ja", "日语"), ("en", "英语"), ("ko", "韩语"))
_LANG_NAMES = dict(LANGUAGE_OPTIONS)
_ENGINE_BY_LANG = {"ja": "parakeet", "en": "sensevoice", "ko": "sensevoice", "auto": "sensevoice"}


def lang_name(language: str) -> str:
    return _LANG_NAMES.get(language, "日语")


def engine_for(language: str) -> str:
    return _ENGINE_BY_LANG.get(language, "parakeet")


def model_dir(engine: str) -> Path:
    folder = "sense-voice-int8" if engine == "sensevoice" else "parakeet-ja-0.6b-int8"
    return MODELS / folder


def model_available(engine: str) -> bool:
    directory = model_dir(engine)
    return all((directory / name).is_file() for name in ("model.int8.onnx", "tokens.txt"))
