# -*- coding: utf-8 -*-
"""实时字幕服务：包装移植来的 Pipeline，供翻译页和灵动岛收起态使用。"""
from __future__ import annotations

from PySide6 import QtCore

from ..core.bg import run_bg
from ..core.config import cfg
from ..core.translation_settings import resolve_connection
from .paths import MODELS, engine_for, lang_name, model_available
from .segment import ClipHistory


class _LiveConfig:
    """给 Pipeline / Translator 用的配置适配器：读 aisland config.json 的 live.* 节，
    翻译 Key 与 aisland 翻译页共用（translate.api_key，或 translate.import_from 指向的译声配置）。"""

    def get(self, path: str, default=None):
        if path == "translate.connection":
            return resolve_connection(cfg.get("translate"))
        if path == "translate.source_lang":
            return str(cfg.get("live.asr.language") or "ja")
        v = cfg.get("live." + path)
        if path == "translate.enabled":
            return bool(default if v is None else v) and bool(self.get("translate.connection").api_key)
        return default if v is None else v

    def set(self, path, value, save=True):
        cfg.set("live." + path, value, save)


class LiveService(QtCore.QObject):
    status = QtCore.Signal(str)
    error = QtCore.Signal(str)
    partial = QtCore.Signal(str)                 # 正在说的日语（流式草稿）
    segment = QtCore.Signal(str, str)            # (sid, 日语整句)
    live_zh = QtCore.Signal(str, str)            # (sid, 实时译文)
    final_zh = QtCore.Signal(str, str)           # (sid, 最终译文)
    level = QtCore.Signal(float)
    running_changed = QtCore.Signal(bool)
    cleared = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pipe = None
        self.running = False
        self._ticket = None
        self._restart_pending = False
        self._clips = ClipHistory()

    def available(self) -> tuple[bool, str]:
        language = str(cfg.get("live.asr.language", "ja") or "ja")
        if not model_available(engine_for(language)):
            return False, f"缺少{lang_name(language)}识别模型，先运行 scripts/download_models.py 下载"
        if language == "auto" and not model_available("parakeet"):
            return False, "自动识别还需要日语模型，先运行 scripts/download_models.py 下载"
        if not (MODELS / "silero_vad.onnx").exists():
            return False, "缺少 silero_vad.onnx，先运行 scripts/download_models.py 下载"
        return True, ""

    def toggle(self):
        self.stop() if self.running else self.start_listen()

    def start_listen(self):
        if self.running:
            return
        if self.pipe is not None:
            self._restart_pending = True
            self._set_running(True)
            self.status.emit("正在释放上一轮识别模型…")
            return
        self._load_pipeline()

    def _load_pipeline(self):
        ok, msg = self.available()
        if not ok:
            self._set_running(False)
            self.error.emit(msg)
            return
        ticket = self._ticket = object()
        self._set_running(True)
        self.status.emit("正在准备实时字幕…")
        job = run_bg(self._import_pipeline,
                     on_done=lambda pipeline_type: self._start_pipeline(ticket, pipeline_type),
                     on_error=lambda error: self._load_failed(ticket, error))
        if job is None and self._ticket is ticket:
            self._ticket = None
            self._set_running(False)

    @staticmethod
    def _import_pipeline():
        from .pipeline import Pipeline

        return Pipeline

    def _start_pipeline(self, ticket, pipeline_type):
        if self._ticket is not ticket:
            return

        def relay(emit):
            def forward(*args):
                if self._ticket is ticket:
                    emit(*args)
            return forward

        try:
            p = pipeline_type(_LiveConfig(), clips=self._clips)
        except Exception as error:
            self._load_failed(ticket, error)
            return
        queued = QtCore.Qt.ConnectionType.QueuedConnection
        for signal, emit in ((p.sig_status, self.status.emit), (p.sig_error, self.error.emit),
                             (p.sig_partial, self.partial.emit),
                             (p.sig_segment, lambda seg: self.segment.emit(seg.sid, seg.jp)),
                             (p.sig_live_zh, self.live_zh.emit), (p.sig_final_zh, self.final_zh.emit),
                             (p.sig_level, self.level.emit)):
            signal.connect(relay(emit), queued)
        p.finished.connect(self._finished, queued)
        self.pipe = p
        try:
            p.start()
        except Exception as error:
            self.pipe = None
            self._load_failed(ticket, error)

    def _load_failed(self, ticket, error):
        if self._ticket is ticket:
            self._ticket = None
            self._set_running(False)
            self.error.emit(f"实时字幕启动失败：{error}")

    def clip(self, sid: str):
        return self._clips.get(sid)

    def clear_clips(self):
        self._clips.clear()
        self.cleared.emit()

    def stop(self, wait: bool = False):
        self._ticket = None
        self._restart_pending = False
        p = self.pipe
        if p is not None:
            p.request_stop()
        self._set_running(False)
        if p is not None and wait:
            p.wait()
            self.pipe = None

    @QtCore.Slot()
    def _finished(self):
        if self.sender() is not self.pipe:
            return
        self.pipe = None
        self._ticket = None
        if self._restart_pending:
            self._restart_pending = False
            self._load_pipeline()
        else:
            self._set_running(False)

    def _set_running(self, on: bool):
        if self.running != on:
            self.running = on
            self.running_changed.emit(on)
