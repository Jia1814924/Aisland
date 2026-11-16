# -*- coding: utf-8 -*-
"""语音识别引擎：日语用 Parakeet 0.6B INT8，英语 / 韩语（以及自动判断）用 SenseVoice INT8。

两种模型都是 sherpa-onnx 的离线模型，接口一致；引擎由语种决定：
日语继续走专门的 Parakeet（准确率比通用模型好），英韩共用一个 SenseVoice 模型。
「自动」模式先让 SenseVoice 识别并判断语种：判定为日语时再用 Parakeet 复核，
其余语言直接采用 SenseVoice 的结果。
"""
from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import normalize_gain
from .paths import model_available, model_dir


SENSE_VOICE_LANGS = ("auto", "zh", "en", "ja", "ko", "yue")


def normalize_lang(raw: str) -> str:
    """把 SenseVoice 的 `<|ja|>` 之类转成 `ja`。"""
    return str(raw or "").strip().strip("<|>")


@dataclass
class AsrResult:
    text: str
    seconds: float
    audio_seconds: float
    lang: str = ""          # 识别出的语种：ja / en / ko / zh / yue（Parakeet 固定 ja）

    @property
    def rtf(self) -> float:
        return self.seconds / self.audio_seconds if self.audio_seconds > 0 else 0.0


class _Parakeet:
    def __init__(self, num_threads: int):
        self.num_threads = num_threads
        self._recognizer = None

    def load(self) -> None:
        if self._recognizer is not None:
            return
        import sherpa_onnx

        directory = model_dir("parakeet")
        model = directory / "model.int8.onnx"
        tokens = directory / "tokens.txt"
        if not (model.is_file() and tokens.is_file()):
            raise FileNotFoundError("日语识别模型不完整，请重新安装软件")
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(model), tokens=str(tokens), num_threads=self.num_threads,
        )

    def transcribe(self, samples: np.ndarray) -> AsrResult:
        self.load()
        started = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, normalize_gain(samples))
        self._recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        seconds = time.perf_counter() - started
        return AsrResult(text=text, seconds=seconds, audio_seconds=len(samples) / 16000, lang="ja")


class _SenseVoice:
    """SenseVoice：一个模型覆盖中 / 英 / 日 / 韩 / 粤，自动判断语种。"""

    def __init__(self, num_threads: int, language: str = "auto"):
        self.num_threads = num_threads
        value = str(language or "auto").lower()
        self.language = value if value in SENSE_VOICE_LANGS else "auto"
        self._recognizer = None

    def load(self) -> None:
        if self._recognizer is not None:
            return
        import sherpa_onnx

        directory = model_dir("sensevoice")
        model = directory / "model.int8.onnx"
        tokens = directory / "tokens.txt"
        if not (model.is_file() and tokens.is_file()):
            raise FileNotFoundError("英语 / 韩语识别模型不完整，请重新运行 scripts/download_models.py")
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=self.num_threads,
            language=self.language, use_itn=True,
        )

    def transcribe(self, samples: np.ndarray) -> AsrResult:
        self.load()
        started = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, normalize_gain(samples))
        self._recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        lang = normalize_lang(getattr(stream.result, "lang", ""))
        seconds = time.perf_counter() - started
        return AsrResult(text=text, seconds=seconds, audio_seconds=len(samples) / 16000, lang=lang)


class FinalASR:
    """统一的识别入口：按 engine 选 Parakeet / SenseVoice。"""

    def __init__(self, engine: str = "parakeet", num_threads: int = 6, language: str | None = None):
        self.engine = engine if engine in ("parakeet", "sensevoice") else "parakeet"
        self.language = str(language or ("ja" if self.engine == "parakeet" else "auto"))
        self.num_threads = num_threads
        self._impl = None
        self._lock = threading.RLock()

    def load(self) -> None:
        with self._lock:
            if self._impl is None:
                impl = (_Parakeet(self.num_threads) if self.engine == "parakeet"
                        else _SenseVoice(self.num_threads, self.language))
                impl.load()
                self._impl = impl

    def transcribe(self, samples: np.ndarray) -> AsrResult:
        with self._lock:
            self.load()
            return self._impl.transcribe(samples)

    @staticmethod
    def available(engine: str = "parakeet") -> bool:
        return model_available(engine)


def decode_wav(path: str | Path) -> np.ndarray:
    """读取 16-bit PCM WAV，转换为 16kHz 单声道 float32。"""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(frames)
    if width != 2:
        raise ValueError("仅支持 16bit PCM wav")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        import soxr

        data = soxr.resample(data, rate, 16000)
    return data.astype(np.float32)
