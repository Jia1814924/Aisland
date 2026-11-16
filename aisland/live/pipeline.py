# -*- coding: utf-8 -*-
"""主流水线：音频采集 → 静音检测断句 → 流式出字 → 整句高精度校对 → 翻译。"""
from __future__ import annotations

import collections
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PySide6 import QtCore

from .asr import FinalASR
from .audio import AudioCapture, normalize_gain
from .paths import MODELS, engine_for, lang_name
from .segment import ClipHistory, Segment
from .translate import Translator, TranslationCancelled, TranslationStream

# 草稿重识别最多占用这么多比例的时间，剩下的留给整句校对和翻译；
# 一句话越长，重识别一次越慢，草稿就会自动跟着放慢（否则会越跑越卡）。
DRAFT_CPU_BUDGET = 0.5
# 实时草稿译文每跑 1 秒，至少留这么多秒给整句翻译，避免本地模型被草稿占满。
LIVE_DUTY_GAP = 1.2


def _fatal_translation_error(exc: Exception) -> bool:
    """鉴权、余额、参数类的 4xx 重试也是白费；网络抖动、超时、5xx/429 才值得再试。

    翻译失败信息在 RuntimeError 里是 friendly_error 的本地化文案，换厂商或
    换语言后按字符串判断会失真，所以看挂在 ``__cause__`` 上的原始 httpx
    状态码；连接/凭据未配置的错误同样直接报告。
    """
    cause = getattr(exc, "__cause__", None)
    response = getattr(cause, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return 400 <= code < 500 and code not in (408, 429)
    return str(exc).startswith("请先")


class Pipeline(QtCore.QThread):
    """在后台线程中运行整条识别翻译流水线。"""

    sig_status = QtCore.Signal(str)
    sig_error = QtCore.Signal(str)
    sig_partial = QtCore.Signal(str)             # 流式日语文本（空串=清空）
    sig_segment = QtCore.Signal(object)          # Segment 最终日语
    sig_live_zh = QtCore.Signal(str, str)        # (sid, 实时译文增量后的全文)
    sig_final_zh = QtCore.Signal(str, str)       # (sid, 最终译文)
    sig_level = QtCore.Signal(float)             # 0~1 音量

    def __init__(self, config, parent=None, *, clips: ClipHistory | None = None):
        super().__init__(parent)
        self.cfg = config
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._reset_pending = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._capture: AudioCapture | None = None
        self._final_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="final-asr")
        self._draft_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="draft-asr")
        self._trans_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="translate")   # API 可并发
        self._live_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translate-live")
        self._pools_closed = False
        self._translation_state_lock = threading.Lock()
        self._pending_final: set[str] = set()
        self._live_task_inflight = False

        # 运行状态
        self._fed = 0                    # 已处理样本数（16k）
        self._pre_roll: collections.deque = collections.deque(maxlen=18)   # ~0.56s 预卷
        self._raw_frames: collections.deque = collections.deque(maxlen=1250)  # 40 秒原始音频
        self._vad_origin = 0
        self._active = False
        self._utterance: list[np.ndarray] = []
        self._utterance_start = 0.0
        self._partial = ""
        self._last_final_jp: list[str] = []
        self._live = {"gen": 0, "sid": "", "busy": False, "last_req": 0.0,
                      "text": "", "translated": None, "latency": 0.0}
        self._draft_engine: FinalASR | None = None
        self._draft_busy = False
        self._draft_started = 0.0
        self._draft_next_ok = 0.0
        self._final_busy = 0
        self._final: FinalASR | None = None
        self._ja_final: FinalASR | None = None      # 自动模式：判定为日语时用来复核
        self._draft_lang = ""                        # 草稿识别出的语种
        self._vad = None
        self._level_counter = 0          # sig_level 降采样计数器
        self._vad_pending = np.zeros(0, dtype=np.float32)
        self._vad_window = 512
        self._draft_min_samples = int(16000 * 0.3)     # 在 _setup 里按配置覆盖
        self._queue_warned = False
        self._clips = clips if clips is not None else ClipHistory()

    # ------------------------------------------------------------- 生命周期
    def _submit(self, pool, fn, *args):
        """往线程池投任务。线程池已经关了就当没这回事——停止瞬间的收尾任务丢掉没关系。"""
        if self._pools_closed:
            return
        try:
            return pool.submit(fn, *args)
        except RuntimeError:      # 池已关闭
            pass

    def _shutdown_pools(self) -> None:
        if self._pools_closed:
            return
        self._pools_closed = True
        # 网络请求可能在读超时前没有返回。撤销排队请求，不让它们阻塞模型的释放。
        for pool in (self._trans_pool, self._live_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        # 当前运行在流水线线程。等已开始的本地推理结束，再释放识别器。
        for pool in (self._draft_pool, self._final_pool):
            pool.shutdown(wait=True, cancel_futures=True)

    def request_stop(self) -> None:
        """主线程只发出停止信号。模型及音频在流水线线程收尾。"""
        self._stop.set()

    def _release(self) -> None:
        self._stop.set()
        capture, self._capture = self._capture, None
        try:
            if capture is not None:
                capture.stop()
                capture.join(timeout=2.0)
        finally:
            self._shutdown_pools()
            self._draft_engine = self._final = self._ja_final = self._vad = None
            self._pre_roll.clear()
            self._raw_frames.clear()
            self._utterance.clear()
            self._vad_pending = np.zeros(0, dtype=np.float32)
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def pause(self, flag: bool) -> None:
        if flag:
            self._pause.set()
        else:
            self._reset_pending.set()
            self._pause.clear()

    # ------------------------------------------------------------- 主循环
    def run(self) -> None:  # noqa: C901
        try:
            if self._stop.is_set():
                return
            try:
                self._setup()
            except Exception as e:  # noqa: BLE001
                self.sig_error.emit(str(e))
                self.sig_status.emit("启动失败")
                return

            if self._stop.is_set():
                return
            self.sig_status.emit("正在聆听…")
            while not self._stop.is_set():
                try:
                    chunk, rms = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self._stop.is_set():
                    break
                if self._pause.is_set():
                    continue
                if self._reset_pending.is_set():
                    self._reset_pending.clear()
                    self._pre_roll.clear()
                    self._active = False
                    self._utterance = []
                    self._vad_pending = np.zeros(0, dtype=np.float32)
                    self._vad.reset()
                    self._vad_origin = self._fed
                    self._raw_frames.clear()
                    self._live['gen'] += 1
                try:
                    self._process_chunk(chunk, rms)
                except Exception as e:  # noqa: BLE001
                    self.sig_error.emit(f"处理音频出错：{e}")

            self.sig_level.emit(0.0)
        finally:
            self._release()

    # ------------------------------------------------------------- 初始化
    def _setup(self) -> None:
        cfg = self.cfg
        language = str(cfg.get("asr.language", "ja") or "ja")
        auto = language == "auto"
        engine = engine_for(language)
        if not FinalASR.available(engine):
            raise FileNotFoundError(
                f"{lang_name(language)}识别模型不完整，请先运行 scripts/download_models.py")
        if auto and not FinalASR.available("parakeet"):
            raise FileNotFoundError(
                "自动识别还需要日语模型 parakeet-ja-0.6b-int8，请先运行 scripts/download_models.py")
        final_threads = int(cfg.get("asr.final_threads", 6))
        self._final = FinalASR(engine, num_threads=final_threads, language=language)
        if cfg.get("asr.partial", True) and str(cfg.get("asr.draft_mode", "rolling")) != "off":
            self._draft_engine = self._final
        self.sig_status.emit(f"正在加载{'多语种' if auto else lang_name(language)}识别模型…")
        self._final.load()
        if self._stop.is_set():
            return
        if auto:
            self.sig_status.emit("正在加载日语复核模型…")
            self._ja_final = FinalASR("parakeet", num_threads=final_threads, language="ja")
            self._ja_final.load()
        if self._stop.is_set():
            return
        # 静音检测
        import sherpa_onnx

        vad_path = MODELS / "silero_vad.onnx"
        if not vad_path.exists():
            raise FileNotFoundError("缺少 silero_vad.onnx，请先运行 scripts/download_models.py")
        vcfg = sherpa_onnx.VadModelConfig()
        vcfg.silero_vad.model = str(vad_path)
        vcfg.silero_vad.window_size = self._vad_window
        vcfg.silero_vad.threshold = float(cfg.get("vad.threshold", 0.5))
        vcfg.silero_vad.min_silence_duration = int(cfg.get("vad.min_silence_ms", 350)) / 1000.0
        vcfg.silero_vad.min_speech_duration = int(cfg.get("vad.min_speech_ms", 200)) / 1000.0
        vcfg.silero_vad.max_speech_duration = float(cfg.get("asr.max_segment_s", 24))
        vcfg.sample_rate = 16000
        vcfg.num_threads = 1
        self._vad = sherpa_onnx.VoiceActivityDetector(vcfg, buffer_size_in_seconds=60)
        if self._stop.is_set():
            return
        # 音频采集
        self.sig_status.emit("正在打开音频设备…")
        capture = AudioCapture(
            source=str(cfg.get("audio.source", "loopback")),
            device_name=str(cfg.get("audio.device", "") or ""),
            on_chunk=self._on_audio,
            on_error=lambda m: self.sig_error.emit(m),
            block_ms=int(cfg.get("audio.block_ms", 80)),
            on_status=lambda m: self.sig_status.emit(m),
        )
        capture.start()
        self._capture = capture
        self._fed = 0
        # 攒够多少语音就允许第一次草稿识别。以前写死 0.7 秒，等于每句话都先白等半秒。
        self._draft_min_samples = max(1600, int(16000 * float(cfg.get("asr.draft_min_s", 0.3))))

    # ------------------------------------------------------------- 音频处理
    def _on_audio(self, chunk: np.ndarray, rms: float) -> None:
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait((chunk, rms))
        except queue.Full:
            if not self._queue_warned:
                self._queue_warned = True
                self.sig_error.emit("音频处理跟不上播放速度，请关闭实时草稿或减少后台程序")

    def _process_chunk(self, chunk: np.ndarray, rms: float) -> None:
        level = min(1.0, max(0.0, (20 * np.log10(rms + 1e-6) + 60) / 60)) if rms > 1e-6 else 0.0
        self._level_counter += 1
        if self._level_counter % 4 == 0:
            self.sig_level.emit(level)
        samples = np.concatenate((self._vad_pending, chunk))
        end = samples.size - samples.size % self._vad_window
        for offset in range(0, end, self._vad_window):
            self._process_frame(samples[offset:offset + self._vad_window].copy())
        self._vad_pending = samples[end:].copy()

    def _process_frame(self, frame: np.ndarray) -> None:
        # 单调时钟：系统校时回拨不会让草稿/实时翻译的节流卡住几分钟。
        now = time.monotonic()
        self._fed += frame.size
        self._raw_frames.append(frame)
        # VAD 以固定窗口运行。整句必须使用 VAD 保存的音频，不能把队列直接 pop 掉。
        self._vad.accept_waveform(normalize_gain(frame, max_gain=6.0))
        speech = bool(self._vad.is_speech_detected())
        if speech and not self._active:
            self._active = True
            self._utterance = list(self._pre_roll)
            self._utterance_start = (self._fed - frame.size - sum(c.size for c in self._pre_roll)) / 16000
            self._live['gen'] += 1
            self._live.update(sid=f"L{self._live['gen']}", busy=False, last_req=0.0, text='', translated=None)
            # 第一趟草稿不要压一个完整间隔：攒够 draft_min_s 就立刻识别，
            # 这一趟的延迟直接决定「声音出来 → 第一个字」。
            self._draft_next_ok = 0.0
        if self._active:
            self._utterance.append(frame)
        self._pre_roll.append(frame)
        while not self._vad.empty():
            segment = self._vad.front
            # VAD 接收的逐帧增益音频只用于检测；识别必须使用原始录音。
            # 否则每 32ms 一次的独立放大会改变音色，并放大停顿里的背景噪声。
            history = np.concatenate(self._raw_frames)
            history_start = self._fed - history.size
            segment_start = self._vad_origin + segment.start
            begin = max(history_start, segment_start - 6400)
            end = min(self._fed, segment_start + len(segment.samples) + 2560)
            audio = history[begin-history_start:end-history_start].copy()
            start = begin / 16000.0
            self._vad.pop()
            self._finalize_utterance(audio=audio, start=start)
        if self._active:
            self._maybe_rolling_draft(now)

    def _finalize_utterance(self, keep_active: bool = False,
                            audio: np.ndarray | None = None, start: float | None = None) -> None:
        if audio is None:
            audio = np.concatenate(self._utterance) if self._utterance else np.zeros(0, dtype=np.float32)
        live_text = self._partial
        # 已完成的译文随原文和句子编号保存，在途请求更新 text 时不能把旧译文带过去。
        translated = self._live["translated"]
        live_zh = (translated[2] if translated is not None
                   and translated[:2] == (self._live["gen"], live_text.strip()) else "")
        self._utterance = []
        self._active = keep_active
        self._partial = ''
        self._live['gen'] += 1  # 已结束句子的慢草稿不能在最终结果之后重新冒出来
        self._live["translated"] = None
        self.sig_partial.emit('')
        self._draft_next_ok = 0.0
        if audio.size < 4000 or self._stop.is_set():
            return
        t0 = self._utterance_start if start is None else start
        seg = Segment(sid=uuid.uuid4().hex[:8], jp='', t0=t0,
                      t1=t0 + audio.size / 16000.0, live_jp=live_text, live_zh=live_zh)
        self._submit(self._final_pool, self._run_final, seg, audio)

    def _run_final(self, seg: Segment, audio: np.ndarray) -> None:
        if self._stop.is_set():
            return
        self._final_busy += 1
        try:
            try:
                result = self._final.transcribe(audio)
            except Exception as e:  # noqa: BLE001
                self.sig_error.emit(f"整句识别失败：{e}")
                return
            if not self._stop.is_set() and self._ja_final is not None and result.lang == "ja":
                # 自动模式：SenseVoice 觉得是日语，就用 Parakeet 重认一遍拿更准的文本。
                # 复核引擎失败只是拿不到更准的版本，不能把已经识别成功的整句丢掉。
                try:
                    better = self._ja_final.transcribe(audio)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                    better = None
                if better is not None and better.text:
                    result = better
            self._emit_final(seg, result, audio)
        finally:
            self._final_busy -= 1

    def get_clip(self, sid: str):
        return self._clips.get(sid)

    def _emit_final(self, seg: Segment, result, audio: np.ndarray | None = None) -> None:
        text = result.text.strip()
        if self._stop.is_set():
            return
        if not text:
            self.sig_partial.emit("")
            return
        seg.jp = text
        seg.lang = getattr(result, "lang", "") or "ja"
        seg.asr_seconds = result.seconds
        if audio is not None:
            self._clips.add(seg.sid, audio)
        seg.rtf = result.rtf
        context = self._context_lines()
        self._last_final_jp.append(text)
        if len(self._last_final_jp) > 8:
            self._last_final_jp = self._last_final_jp[-8:]
        self.sig_segment.emit(seg)
        if self.cfg.get("translate.enabled", True):
            sid = seg.sid
            live_zh = (seg.live_zh or "").strip()
            if live_zh and seg.live_jp.strip() == text and seg.lang not in ("zh", "yue"):
                # 整句识别结果和草稿一字不差，屏幕上那句中文就是它的译文：
                # 直接定稿，不用再花一次近 1 秒的首字延迟去换同一句话。
                self.sig_final_zh.emit(sid, live_zh)
            else:
                self.enqueue_translation(sid, text, context, seg.lang)

    # ------------------------------------------------------------- 滚动草稿
    def _maybe_rolling_draft(self, now: float) -> None:
        if self._draft_engine is None or self._draft_busy:
            return
        if self._final_busy:
            return          # 整句校对才是要用的结果，草稿先让路
        if now < self._draft_next_ok:
            return
        total = sum(len(c) for c in self._utterance)
        if total < self._draft_min_samples:
            return
        self._draft_busy = True
        self._draft_started = now
        gen = self._live["gen"]
        audio = np.concatenate(self._utterance)
        self._submit(self._draft_pool, self._run_draft, gen, audio)

    def _run_draft(self, gen: int, audio: np.ndarray) -> None:
        if self._stop.is_set():
            return
        text = ""
        lang = ""
        try:
            result = self._draft_engine.transcribe(audio)
            text = result.text.strip()
            lang = result.lang
        except Exception:  # noqa: BLE001
            text = ""
        finally:
            self._draft_busy = False
            # 这一趟花了多久，下一趟就按比例多等一会儿：草稿最多吃掉 DRAFT_CPU_BUDGET 的时间。
            # （以前是固定 0.6 秒一次，一句话越长重识别越慢，最后会把 CPU 全占满）
            spent = max(0.0, time.monotonic() - self._draft_started)
            interval = int(self.cfg.get("asr.draft_interval_ms", 600)) / 1000.0
            self._draft_next_ok = self._draft_started + max(interval, spent / DRAFT_CPU_BUDGET)
        if not text or self._stop.is_set() or gen != self._live["gen"] or not self._active or self._final_busy:
            return
        if text != self._partial:
            self._partial = text
            self._draft_lang = lang
            self.sig_partial.emit(text)
            self._maybe_live_translate(time.monotonic(), lang)

    # ------------------------------------------------------------- 翻译
    def _context_lines(self) -> list[str]:
        n = int(self.cfg.get("translate.context_lines", 3))
        return self._last_final_jp[-n:] if n > 0 else []

    def enqueue_translation(self, sid, text, context=None, lang: str = ""):
        if self._stop.is_set() or not text.strip():
            return
        with self._translation_state_lock:
            if sid in self._pending_final:
                return
            self._pending_final.add(sid)
        future = self._submit(self._trans_pool, self._run_translate_final, sid, text, context, lang)

        def finished(_future=None):
            with self._translation_state_lock:
                self._pending_final.discard(sid)

        if future is None:
            finished()
        else:
            future.add_done_callback(finished)

    def _run_translate_final(self, sid: str, text: str, context: list[str] | None = None,
                             lang: str = "") -> None:
        if self._stop.is_set():
            return
        if lang in ("zh", "yue"):
            # 本来就是中文（含粤语）：不用翻，直接把识别文本当译文
            self.sig_final_zh.emit(sid, text)
            return
        translator = Translator(self.cfg)
        for attempt in range(2):
            parts = []
            last_emit = [0.0]
            def delta(piece):
                parts.append(piece)
                now = time.monotonic()
                if now - last_emit[0] >= 0.04:
                    self.sig_final_zh.emit(sid, ''.join(parts))
                    last_emit[0] = now
            control = TranslationStream(delta, self._stop.is_set,
                                        seconds=max(30.0, min(90.0, len(text) * 0.6)),
                                        read_timeout=15.0)
            try:
                control.checkpoint()
                out = translator.translate(text, context or [], on_delta=control, language=lang)
                control.checkpoint()
                break
            except TranslationCancelled:
                return
            except Exception as exc:
                # Retry the same configured service once; never silently change models.
                # 4xx（鉴权/余额/参数）与未配置的连接重试也不会成功，直接报告。
                if attempt or _fatal_translation_error(exc):
                    self.sig_error.emit(f"翻译失败：{exc}")
                    return
                if self._stop.wait(0.2):
                    return
        if self._stop.is_set():
            return
        self.sig_final_zh.emit(sid, out)

    def _maybe_live_translate(self, now: float, lang: str = "") -> None:
        if not self.cfg.get("translate.live", True) or not self.cfg.get("translate.enabled", True):
            return
        live = self._live
        interval = int(self.cfg.get("translate.live_min_interval_ms", 500)) / 1000.0
        min_new = int(self.cfg.get("translate.live_min_new_chars", 2))
        text = self._partial.strip()
        if not text or live["busy"]:
            return
        if lang in ("zh", "yue"):
            # 中文内容不用调 API：直接把草稿当译文
            self.sig_live_zh.emit(live["sid"], text)
            return
        # 这一句的第一次翻译：不再攒到 6 个字。翻译服务的首字要等近 1 秒，
        # 让它从句首就开始排队，才可能与后面的语音同时到。
        first = not live["text"]
        if len(text) < (2 if first else 6):
            return  # 太短的草稿容易翻出乱码，等积累一点再翻
        if not first and len(text) - len(live["text"]) < min_new:
            return
        # 实测的翻译耗时决定节奏：只有慢到 1.2 秒以上（本地模型）才额外留冷却，
        # 否则 API 本身已经是串行的，再空等一段只是把字幕往后推。
        latency = float(live.get("latency") or 0.0)
        gap = max(interval, latency * LIVE_DUTY_GAP) if latency > 1.2 else interval
        if now - live["last_req"] < gap:
            return
        with self._translation_state_lock:
            if self._live_task_inflight:
                return
            self._live_task_inflight = True
        live["busy"] = True
        live["last_req"] = now
        live["text"] = text
        gen = live["gen"]
        sid = live["sid"]
        if self._submit(self._live_pool, self._run_translate_live, gen, sid, text, lang) is None:
            with self._translation_state_lock:
                self._live_task_inflight = False
            live["busy"] = False

    def _run_translate_live(self, gen: int, sid: str, text: str, lang: str = "") -> None:
        def obsolete():
            return (self._stop.is_set() or self._pause.is_set() or not self._active
                    or gen != self._live["gen"])

        ran = False
        t0 = time.monotonic()
        try:
            if obsolete():
                return
            translator = Translator(self.cfg)
            ran = True
            parts = []
            last_emit = [0.0]
            def delta(piece):
                parts.append(piece)
                now = time.monotonic()
                if now - last_emit[0] >= 0.04:
                    self.sig_live_zh.emit(sid, ''.join(parts))
                    last_emit[0] = now
            control = TranslationStream(delta, obsolete, seconds=10.0, read_timeout=6.0)
            out = translator.translate(text, self._context_lines(), on_delta=control, language=lang)
            control.checkpoint()
            if out and out.count("*") < 4 and "＊＊" not in out:
                if gen == self._live["gen"]:
                    self._live["translated"] = (gen, text, out)
                self.sig_live_zh.emit(sid, out)
        except TranslationCancelled:
            pass
        except Exception:
            # Optional preview failures must not stop final translation or occupy the slot forever.
            pass
        finally:
            with self._translation_state_lock:
                self._live_task_inflight = False
            current_gen = gen == self._live["gen"]
            if current_gen:
                self._live["busy"] = False
            if ran:
                spent = max(0.0, time.monotonic() - t0)
                old = float(self._live.get("latency") or 0.0)
                self._live["latency"] = spent if old <= 0 else round(old * 0.6 + spent * 0.4, 3)
            # 翻译回来的这段时间里草稿通常又长了一截：立刻接着翻下一段，
            # 不要空等到下一次草稿识别才重新排队（那会白白多出一整段延迟）。
            if current_gen and not self._stop.is_set() and not self._pause.is_set():
                try:
                    self._maybe_live_translate(time.monotonic(), lang)
                except Exception:  # noqa: BLE001
                    pass
