"""在隔离进程里验证额度面板的刷新调度（避免把 Qt 带进主测试进程）。"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from PySide6 import QtCore

_APP = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

from aisland.services import ai_usage
from aisland.ui.pages import home


class _Page:
    """Only the attributes used by MessagesPage._request_usage."""

    _request_usage = home.MessagesPage._request_usage

    def __init__(self, quota=None, provider="workbuddy", active=False):
        self._provider = provider
        self.active = active
        self._quota = quota or home.QuotaReadings(foreground_provider=lambda: None)


def _run_bg_now(fn, *args, on_done=None, on_error=None):
    try:
        result = fn(*args)
    except Exception as exc:  # noqa: BLE001 - mirror the real worker contract
        if on_error:
            on_error(exc)
    else:
        if on_done:
            on_done(result)
    return object()


class QuotaRefreshTests(unittest.TestCase):
    @staticmethod
    def _stop_timer(quota):
        try:
            quota.timer.stop()
        except RuntimeError:                # Qt 已随 QObject 一起释放
            pass

    def setUp(self):
        self.quota = home.QuotaReadings(foreground_provider=lambda: None)
        self.addCleanup(self._stop_timer, self.quota)
        self.calls = []
        run_bg = patch.object(home.bg, "run_bg", side_effect=self._run_bg)
        read = patch.object(ai_usage, "read_ai_usage", side_effect=self._read)
        save = patch.object(ai_usage, "save_usage_cache", return_value=time.time())
        cfg_get = patch.object(home.cfg, "get", return_value="")
        for item in (run_bg, read, save, cfg_get):
            item.start()
            self.addCleanup(item.stop)
        self.result = None

    def _read(self, provider, source):
        self.calls.append((provider, source))
        return self.result

    def _run_bg(self, fn, *args, on_done=None, on_error=None):
        return _run_bg_now(fn, *args, on_done=on_done, on_error=on_error)

    def test_throttled_tick_is_skipped_but_a_forced_read_runs(self):
        page = _Page(self.quota)
        self.quota.checked["workbuddy"] = time.monotonic()
        page._request_usage()
        self.assertEqual(self.calls, [])
        page._request_usage(force=True)       # 通知 / 展开面板 / 切前台
        self.assertEqual(len(self.calls), 1)

    def test_a_visible_panel_short_retries_after_a_miss(self):
        visible = _Page(self.quota, active=True)
        hidden = _Page(self.quota, active=False)
        self.quota.failures["workbuddy"] = 2
        self.quota.checked["workbuddy"] = time.monotonic() - 3.5
        hidden._request_usage()
        self.assertEqual(len(self.calls), 0)   # 隐藏态仍是 10 秒节流
        visible._request_usage()
        self.assertEqual(len(self.calls), 1)   # 可见态按 3 秒退避重试
        with patch.object(home.time, "monotonic", return_value=100.0) as clock:
            for provider, delay in (("workbuddy", 10), ("codex", 15)):
                hidden._provider = provider
                self.quota.failures[provider] = 99
                self.quota.checked[provider] = 100.0
                before = len(self.calls)
                clock.return_value = 100.0 + delay - 0.1
                hidden._request_usage()
                self.assertEqual(len(self.calls), before)
                clock.return_value = 100.0 + delay + 0.1
                hidden._request_usage()
                self.assertEqual(len(self.calls), before + 1)

    def test_repeated_visible_failures_use_the_full_backoff_schedule(self):
        page = _Page(self.quota, active=True)
        with patch.object(home.time, "monotonic", return_value=100.0) as clock:
            for failures, delay in enumerate((2, 3, 5, 10, 10), start=1):
                self.quota.failures["workbuddy"] = failures
                self.quota.checked["workbuddy"] = 100.0
                before = len(self.calls)
                clock.return_value = 100.0 + delay - 0.1
                page._request_usage()
                self.assertEqual(len(self.calls), before)
                clock.return_value = 100.0 + delay + 0.1
                page._request_usage()
                self.assertEqual(len(self.calls), before + 1)
            self.result = [ai_usage.Metric("奖励积分", "628", None, "剩余积分")]
            page._request_usage(force=True)
            self.assertEqual(self.quota.failures["workbuddy"], 0)
            before = len(self.calls)
            clock.return_value += 2.1
            page._request_usage()
            self.assertEqual(len(self.calls), before + 1)

    def test_missed_panel_counts_failures_and_success_resets_them(self):
        page = _Page(self.quota)
        for expected in (1, 2, 3, 4):
            page._request_usage(force=True)
            self.assertEqual(self.quota.failures["workbuddy"], expected)
        self.assertEqual(ai_usage.retry_interval(10, 4), 10)
        self.result = [ai_usage.Metric("套餐基础积分", "386.52", None, "剩余积分")]
        page._request_usage(force=True)
        self.assertEqual(self.quota.failures["workbuddy"], 0)
        self.assertEqual(self.quota.usage["workbuddy"], self.result)

    def test_local_source_change_forces_an_immediate_read(self):
        page = _Page(self.quota)
        self.quota.checked["workbuddy"] = time.monotonic()
        stamps = iter([100.0, 100.0, 101.0])
        with (patch.object(ai_usage, "resolve_workbuddy_source", return_value="/fake/quota.json"),
              patch.object(ai_usage, "source_stamp", side_effect=lambda _s: next(stamps))):
            page._request_usage()
            self.assertEqual(len(self.calls), 1)   # 首次发现主动源
            page._request_usage()
            self.assertEqual(len(self.calls), 1)   # 文件没变，维持节流
            page._request_usage()
            self.assertEqual(len(self.calls), 2)   # 文件被重写，立刻读数

    def test_two_displays_share_one_in_flight_read(self):
        pending = []
        with patch.object(home.bg, "run_bg", side_effect=lambda fn, *a, **kw: pending.append(kw) or object()):
            first, second = _Page(self.quota), _Page(self.quota)
            first._request_usage()
            second._request_usage()
            self.assertEqual(len(pending), 1)
            pending[0]["on_done"](None)
        self.assertNotIn("workbuddy", self.quota.busy)

    def test_a_lost_callback_does_not_freeze_the_provider(self):
        page = _Page(self.quota)
        pending = []
        with patch.object(home.bg, "run_bg",
                          side_effect=lambda fn, *a, **kw: pending.append(kw) or object()):
            page._request_usage(force=True)
            self.assertEqual(len(pending), 1)      # 第一次读取占位
            page._request_usage(force=True)
            self.assertEqual(len(pending), 1)      # 回调没来，不重复发起
            self.quota.busy_since["workbuddy"] = time.monotonic() - home.USAGE_BUSY_TIMEOUT - 1
            page._request_usage(force=True)
            self.assertEqual(len(pending), 2)      # 超时视为丢失，允许重新读取
            pending[1]["on_done"](None)
        self.assertNotIn("workbuddy", self.quota.busy)
        self.assertNotIn("workbuddy", self.quota.busy_since)

    def test_old_callback_cannot_clear_a_new_reads_busy_mark(self):
        page = _Page(self.quota)
        pending = []
        with (patch.object(home.time, "monotonic", return_value=100.0) as clock,
              patch.object(home.bg, "run_bg", side_effect=lambda fn, *a, **kw: pending.append(kw) or object())):
            page._request_usage(force=True)
            clock.return_value += home.USAGE_BUSY_TIMEOUT + 1
            page._request_usage(force=True)
            started = self.quota.busy_since["workbuddy"]
            pending[0]["on_error"](RuntimeError("late old error"))
            self.assertIn("workbuddy", self.quota.busy)
            self.assertEqual(self.quota.busy_since["workbuddy"], started)
            self.assertEqual(self.quota.failures.get("workbuddy", 0), 0)
            clock.return_value += 3
            page._request_usage(force=True)
            self.assertEqual(len(pending), 2)
            pending[1]["on_done"](self.result)
        self.assertFalse(self.quota.busy)
        self.assertFalse(self.quota.request_tokens)

    def test_old_success_cannot_overwrite_newer_metrics(self):
        page = _Page(self.quota)
        pending = []
        fresh = [ai_usage.Metric("奖励积分", "628", None)]
        stale = [ai_usage.Metric("奖励积分", "528", None)]

        def run_bg(fn, *args, on_done=None, on_error=None):
            if fn is ai_usage.save_usage_cache:
                fn(*args)          # 落盘任务没有回调；同步执行以便断言（真实通道在后台）
                return object()
            pending.append({"fn": fn, "on_done": on_done, "on_error": on_error})
            return object()

        with (patch.object(home.time, "monotonic", return_value=100.0) as clock,
              patch.object(ai_usage, "usage_stamp", return_value=123.0),
              patch.object(ai_usage, "save_usage_cache", return_value=123.0) as saved,
              patch.object(home.bg, "run_bg", side_effect=run_bg)):
            page._request_usage(force=True)
            clock.return_value += home.USAGE_BUSY_TIMEOUT + 1
            page._request_usage(force=True)
            reads = [job for job in pending if job["fn"] is not ai_usage.save_usage_cache]
            reads[1]["on_done"](fresh)
            reads[0]["on_done"](stale)
            reads[1]["on_done"](stale)     # 连同重复投递也应被忽略
            self.assertEqual(self.quota.usage["workbuddy"], fresh)
            self.assertEqual(self.quota.updated["workbuddy"], 123.0)
            saved.assert_called_once_with("workbuddy", fresh)
        self.assertFalse(self.quota.request_tokens)

    def test_notification_and_foreground_switch_request_a_refresh(self):
        seen = []
        seen_provider = ["codex"]             # __init__ 内部就会调 detect_active
        quota = home.QuotaReadings(foreground_provider=lambda: seen_provider[0])
        self.addCleanup(self._stop_timer, quota)
        quota.refresh_requested.connect(lambda: seen.append(1))
        seen.clear()                          # 构造时的首次识别发生在连接之前
        quota.observe_notification("workbuddy")
        self.assertEqual(len(seen), 1)        # 通知到达（可能刚消耗积分）
        seen_provider[0] = "deepseek_harness"
        quota.detect_active()
        self.assertEqual(len(seen), 2)        # 切到另一个 AI 客户端


if __name__ == "__main__":
    unittest.main(verbosity=2)
