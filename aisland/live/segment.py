# -*- coding: utf-8 -*-
"""字幕句（Segment）。

单独放在这里，是为了让界面模块（transcript/window）不必为了一个数据类去导入
识别、翻译那些重型依赖——那样会让软件启动慢好几秒。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

MAX_RECENT_SEGMENTS = 80


class ClipHistory:
    """Across listening sessions, retain audio for the same recent rows shown in the UI."""

    def __init__(self):
        self._clips = OrderedDict()
        self._lock = Lock()

    def add(self, sid: str, audio):
        with self._lock:
            self._clips[sid] = audio
            self._clips.move_to_end(sid)
            if len(self._clips) > MAX_RECENT_SEGMENTS:
                self._clips.popitem(last=False)

    def get(self, sid: str):
        with self._lock:
            return self._clips.get(sid)

    def clear(self):
        with self._lock:
            self._clips.clear()


@dataclass
class Segment:
    sid: str
    jp: str
    zh: str = ""
    t0: float = 0.0          # 相对会话开始的秒数
    t1: float = 0.0
    rtf: float = 0.0
    asr_seconds: float = 0.0
    provider: str = ""
    lang: str = ""           # 识别出的语种：ja / en / ko / zh / yue（自动模式用）
    live_jp: str = ""        # 流式引擎当时的草稿（用于对比）
    live_zh: str = ""        # 收句前已经显示的草稿译文（草稿与整句文本相同时直接当最终译文）
    done: bool = False
