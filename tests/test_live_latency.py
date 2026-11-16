"""实时字幕的延迟闸门：句首的第一趟草稿、第一次翻译、以及草稿译文复用。

这些都属于「时间」上的约定，跑真实音频测不出来，所以在这里用假信号和假线程池
把决策点单独钉住：
- 句子一开始说，就应该允许立刻做第一趟草稿（不再固定等一个间隔）；
- 只要草稿有两个字，就该立刻发出第一次翻译（以前要攒到六个字）；
- 整句识别结果和草稿一字不差时，不再重复调一次翻译。
PySide6 用桩替换，主测试进程不加载 Qt。
"""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = __import__("pathlib").Path(__file__).resolve().parents[1]


class _Signal:
    def __init__(self):
        self.events = []

    def connect(self, *_args, **_kwargs):
        return None

    def emit(self, *args):
        self.events.append(args)


class _Thread:
    def __init__(self, parent=None):
        self._parent = parent

    def start(self):
        return None

    def wait(self, *_args):
        return True


def load_pipeline():
    """只替换 PySide6 这一项，别动 sys.modules 里的其他模块。

    （用 patch.dict 整体包住会把期间导入的 numpy 子模块一起回滚，
    再导入就会报 "cannot load module more than once"。）
    """
    qt = types.ModuleType("PySide6")
    qt.QtCore = SimpleNamespace(QThread=_Thread, Signal=lambda *_a: _Signal(), QObject=object)
    sys.path.insert(0, str(PROJECT))
    saved = sys.modules.get("PySide6")
    sys.modules["PySide6"] = qt
    try:
        sys.modules.pop("aisland.live.pipeline", None)
        import aisland.live.pipeline as module
    finally:
        if saved is None:
            sys.modules.pop("PySide6", None)
        else:
            sys.modules["PySide6"] = saved
    return module


class _Cfg:
    def __init__(self, **values):
        self.values = {
            "asr.draft_min_s": 0.3, "asr.draft_interval_ms": 250, "asr.max_segment_s": 10,
            "translate.live": True, "translate.enabled": True,
            "translate.live_min_interval_ms": 500, "translate.live_min_new_chars": 2,
            "translate.context_lines": 3,
        }
        self.values.update(values)

    def get(self, path, default=None):
        return self.values.get(path, default)


class LiveGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipeline = load_pipeline()

    def make(self, **cfg):
        import numpy as np

        p = self.pipeline.Pipeline(_Cfg(**cfg))
        p._draft_engine = object()
        p._draft_min_samples = int(16000 * 0.3)
        p.submitted = []

        def submit(pool, fn, *args):
            p.submitted.append((fn.__name__, args))
            return None

        p._submit = submit
        p.sig_partial = _Signal()
        p.sig_live_zh = _Signal()
        p.sig_final_zh = _Signal()
        p.sig_segment = _Signal()
        self._np = np
        self.addCleanup(lambda: [pool.shutdown(wait=False, cancel_futures=True)
                                 for pool in (p._final_pool, p._draft_pool, p._trans_pool, p._live_pool)])
        return p

    # ------------------------------------------------------------------ 草稿
    def test_first_draft_is_not_held_back_by_an_interval(self):
        p = self.make()
        p._active = True
        p._utterance = [self._np.zeros(int(16000 * 0.3), dtype=self._np.float32)]
        p._draft_next_ok = 0.0                     # 句子刚开始说
        p._maybe_rolling_draft(now=1000.0)
        self.assertEqual([name for name, _ in p.submitted], ["_run_draft"])

    def test_draft_waits_until_enough_audio(self):
        p = self.make()
        p._active = True
        p._utterance = [self._np.zeros(1000, dtype=self._np.float32)]
        p._draft_next_ok = 0.0
        p._maybe_rolling_draft(now=1000.0)
        self.assertEqual(p.submitted, [])

    # ---------------------------------------------------------------- 翻译闸门
    def test_two_characters_are_enough_for_the_first_translation(self):
        p = self.make()
        p._active = True
        p._partial = "ちょ"
        p._maybe_live_translate(now=1000.0)
        self.assertEqual([name for name, _ in p.submitted], ["_run_translate_live"])
        self.assertEqual(p._live["text"], "ちょ")

    def test_one_character_still_waits(self):
        p = self.make()
        p._active = True
        p._partial = "ち"
        p._maybe_live_translate(now=1000.0)
        self.assertEqual(p.submitted, [])

    def test_growing_tail_needs_two_more_characters(self):
        p = self.make()
        p._active = True
        p._live["text"] = "ちょっと待って"
        p._partial = "ちょっと待ってよ"            # 只多一个字
        p._maybe_live_translate(now=1000.0)
        self.assertEqual(p.submitted, [])

    def test_live_request_is_not_gapped_for_a_fast_api(self):
        p = self.make()
        p._active = True
        p._live.update(text="ちょっと待って", last_req=1000.0, latency=0.9)
        p._partial = "ちょっと待ってよ、そんな"
        p._maybe_live_translate(now=1000.4)        # 0.4s < 0.5s 间隔
        self.assertEqual(p.submitted, [])
        p._maybe_live_translate(now=1000.6)        # 过了 0.5s 就该发
        self.assertEqual([name for name, _ in p.submitted], ["_run_translate_live"])

    # ------------------------------------------------------------ 草稿译文复用
    def _final(self, p, seg, text):
        result = SimpleNamespace(text=text, lang="ja", seconds=0.2, rtf=0.1, audio_seconds=2.0)
        p._emit_final(seg, result, audio=None)

    def test_identical_text_reuses_the_live_translation(self):
        p = self.make()
        calls = []
        p.enqueue_translation = lambda *a, **k: calls.append(a)
        seg = self.pipeline.Segment(sid="s1", jp="", live_jp="ちょっと待って", live_zh="等一下")
        self._final(p, seg, "ちょっと待って")
        self.assertEqual(calls, [])
        self.assertEqual(p.sig_final_zh.events, [("s1", "等一下")])

    def test_corrected_text_is_translated_again(self):
        p = self.make()
        calls = []
        p.enqueue_translation = lambda *a, **k: calls.append(a)
        seg = self.pipeline.Segment(sid="s2", jp="", live_jp="ちょっとまって", live_zh="等一下")
        self._final(p, seg, "ちょっと待ってください")
        self.assertEqual(len(calls), 1)
        self.assertEqual(p.sig_final_zh.events, [])

    def test_empty_live_translation_falls_back_to_translating(self):
        p = self.make()
        calls = []
        p.enqueue_translation = lambda *a, **k: calls.append(a)
        seg = self.pipeline.Segment(sid="s3", jp="", live_jp="ちょっと待って", live_zh="")
        self._final(p, seg, "ちょっと待って")
        self.assertEqual(len(calls), 1)

    def test_grown_draft_drops_the_translation_of_the_shorter_text(self):
        """收句时草稿已经变长、新译文还没回来：旧译文会少半句，不能带进 Segment。"""
        p = self.make()
        p._active = True
        short = "ちょっと待って"
        p._live.update(text=short, translated=(p._live["gen"], short, "等一下"))
        p._partial = "ちょっと待って、そんなの"
        submit = p._submit

        def hold_request(*args):
            submit(*args)
            return object()

        p._submit = hold_request
        p._maybe_live_translate(now=1000.0)
        self.assertTrue(p._live["busy"])
        self.assertEqual(p._live["text"], p._partial)
        p._finalize_utterance(audio=self._np.zeros(8000, dtype=self._np.float32), start=0.0)
        seg = next(args[0] for name, args in p.submitted if name == "_run_final")
        self.assertEqual(seg.live_jp, "ちょっと待って、そんなの")
        self.assertEqual(seg.live_zh, "")
        queued = []
        p.enqueue_translation = lambda *args: queued.append(args)
        self._final(p, seg, seg.live_jp)
        self.assertEqual(len(queued), 1)
        self.assertEqual(p.sig_final_zh.events, [])

    def test_matching_draft_keeps_its_translation(self):
        p = self.make()
        p._active = True
        p._partial = "ちょっと待って"
        p._live.update(text=p._partial, translated=(p._live["gen"], p._partial, "等一下"))
        p._finalize_utterance(audio=self._np.zeros(8000, dtype=self._np.float32), start=0.0)
        seg = next(args[0] for name, args in p.submitted if name == "_run_final")
        self.assertEqual(seg.live_zh, "等一下")

    # ------------------------------------------------ 日语复核失败降级
    def test_ja_recheck_failure_keeps_the_sensevoice_result(self):
        """复核引擎抛异常只降级，不能把 SenseVoice 已识别成功的整句丢掉。"""
        p = self.make()
        p.sig_error = _Signal()
        p.enqueue_translation = lambda *a, **k: None
        seg = self.pipeline.Segment(sid="s9", jp="", live_jp="せんす", live_zh="")
        audio = self._np.zeros(8000, dtype=self._np.float32)
        p._final = SimpleNamespace(
            transcribe=lambda _a: SimpleNamespace(text="こんにちは", lang="ja", seconds=0.2, rtf=0.1))

        def boom(_a):
            raise RuntimeError("parakeet load failed")

        p._ja_final = SimpleNamespace(transcribe=boom)
        p._run_final(seg, audio)
        self.assertEqual([e[0].jp for e in p.sig_segment.events], ["こんにちは"])
        self.assertEqual(p.sig_error.events, [])      # 复核失败不是整句失败

    def test_primary_final_failure_still_reports_and_drops(self):
        p = self.make()
        p.sig_error = _Signal()
        seg = self.pipeline.Segment(sid="s10", jp="", live_jp="x", live_zh="")
        audio = self._np.zeros(8000, dtype=self._np.float32)

        def boom(_a):
            raise RuntimeError("sensevoice failed")

        p._final = SimpleNamespace(transcribe=boom)
        p._run_final(seg, audio)
        self.assertEqual(p.sig_segment.events, [])
        self.assertEqual(len(p.sig_error.events), 1)


class TranslationRetryClassificationTests(unittest.TestCase):
    """重试按原始 HTTP 状态码判断，不再匹配厂商/语言的本地化错误文案。"""

    @classmethod
    def setUpClass(cls):
        cls.pipeline = load_pipeline()

    @staticmethod
    def _error(status=None, text="boom"):
        exc = RuntimeError(text)
        if status is not None:
            class _Cause(Exception):
                response = SimpleNamespace(status_code=status)
            exc.__cause__ = _Cause()
        return exc

    def test_client_errors_fail_fast(self):
        fatal = self.pipeline._fatal_translation_error
        for status in (401, 402, 403, 404, 422):
            with self.subTest(status=status):
                # 任何厂商语言的文案都一样：4xx 重试也不会成功。
                self.assertTrue(fatal(self._error(status, "vendor-specific message")))

    def test_transient_failures_get_their_retry(self):
        fatal = self.pipeline._fatal_translation_error
        for status in (408, 429, 500, 503):
            with self.subTest(status=status):
                self.assertFalse(fatal(self._error(status)))
        self.assertFalse(fatal(self._error()))            # 网络/超时异常没有状态码
        self.assertFalse(fatal(RuntimeError("请求超时")))

    def test_unconfigured_connection_fails_fast(self):
        self.assertTrue(self.pipeline._fatal_translation_error(RuntimeError("请先设置翻译连接")))


class ClockDisciplineTests(unittest.TestCase):
    """节流必须用单调时钟：系统校时回拨不能让草稿/实时翻译卡住几分钟。"""

    def test_pipeline_throttles_do_not_use_wall_clock(self):
        import ast

        tree = ast.parse((PROJECT / "aisland/live/pipeline.py").read_text(encoding="utf-8"))
        wall = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "time" and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "time"]
        self.assertEqual(wall, [])


if __name__ == "__main__":
    unittest.main()
