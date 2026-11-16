# -*- coding: utf-8 -*-
"""下载实时字幕所需的模型到 models/：

- Parakeet 日语识别（sherpa-onnx 格式，int8，约 630MB）
- SenseVoice 多语言识别（英语 / 韩语，int8，约 237MB）
- silero_vad.onnx（静音检测）

用法：.venv\\Scripts\\python.exe scripts\\download_models.py
国内网络可先设置 HF_ENDPOINT=https://hf-mirror.com
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
PARAKEET_REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
SENSEVOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
VAD_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-vad-models/resolve/main/silero_vad.onnx",
]


def _missing(directory: Path) -> bool:
    return not (directory / "model.int8.onnx").exists()


def main() -> int:
    MODELS.mkdir(exist_ok=True)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("缺少依赖 huggingface-hub，请先运行 setup.bat")
        return 1

    dest = MODELS / "parakeet-ja-0.6b-int8"
    if _missing(dest):
        print("下载 Parakeet 日语模型 …")
        snapshot_download(PARAKEET_REPO, local_dir=str(dest), allow_patterns=["*.onnx", "tokens.txt"])

    sense = MODELS / "sense-voice-int8"
    if _missing(sense):
        print("下载 SenseVoice 英语 / 韩语模型 …")
        snapshot_download(SENSEVOICE_REPO, local_dir=str(sense),
                          allow_patterns=["model.int8.onnx", "tokens.txt"])

    vad = MODELS / "silero_vad.onnx"
    if not vad.exists():
        for url in VAD_URLS:
            try:
                print("下载 silero_vad.onnx …", url)
                urllib.request.urlretrieve(url, vad)
                break
            except Exception as e:  # noqa: BLE001
                print("  失败：", e)
    if "--dict" in sys.argv:
        sys.path.insert(0, str(ROOT))
        from aisland.learn import dictionary
        from aisland.learn.dict_download import download_recommended
        print("下载词典 …")
        d = dictionary.get()
        for z in download_recommended(print):
            print("导入", z.name)
            d.import_yomitan(z)
    ok = (not _missing(dest)) and (not _missing(sense)) and vad.exists()
    print("完成" if ok else "未完成，请检查网络后重试")
    return 0 if ok else 1


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
