"""AI panel readings: WorkBuddy credit parsing, quota metrics and the last-reading cache."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from aisland.services import ai_usage, native_ai_usage


class WorkbuddyCreditsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aisland-quota-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source(self, payload, name="credits.json"):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_nested_standard_keys(self):
        metrics = ai_usage.read_workbuddy_credits(
            self.source({"account": {"baseCredit": 2000, "bonusCredit": 500}}))
        self.assertEqual([(m.label, m.display, m.percent) for m in metrics],
                         [("套餐基础积分", "2,000", None), ("奖励积分", "500", None)])

    def test_chinese_keys_and_numeric_strings(self):
        metrics = ai_usage.read_workbuddy_credits(
            self.source({"data": {"套餐基础积分": "1,800", "奖励积分": 250.5}}))
        self.assertEqual([m.display for m in metrics], ["1,800", "250.50"])

    def test_missing_or_unreadable_source_returns_none(self):
        self.assertIsNone(ai_usage.read_workbuddy_credits(""))
        self.assertIsNone(ai_usage.read_workbuddy_credits(
            self.source({"account": {"baseCredit": 10}})))
        self.assertIsNone(ai_usage.read_workbuddy_credits(str(self.root / "missing.json")))
        (self.root / "broken.json").write_text("{bad", encoding="utf-8")
        self.assertIsNone(ai_usage.read_workbuddy_credits(str(self.root / "broken.json")))

    def test_remote_url_is_rejected_without_a_request(self):
        with patch("httpx.get", side_effect=AssertionError("no remote call")):
            self.assertIsNone(ai_usage.read_workbuddy_credits("https://example.com/credits"))


class QuotaMetricTests(unittest.TestCase):
    def test_quota_windows_become_display_metrics(self):
        short = ai_usage.QuotaWindow(13, 300, None)
        week = ai_usage.QuotaWindow(18, 10080, None)
        metrics = ai_usage.quota_metrics((short, week))
        self.assertEqual([(m.label, m.display, m.percent) for m in metrics],
                         [("5 小时", "13%", 13), ("周用量", "18%", 18)])

    def test_missing_window_keeps_placeholder(self):
        metrics = ai_usage.quota_metrics((ai_usage.QuotaWindow(0, 300, None), None))
        self.assertEqual(metrics[1].display, "—")
        self.assertEqual(metrics[1].detail, "暂无数据")

    def test_dispatch_never_runs_codex_for_workbuddy(self):
        with (patch.object(ai_usage, "read_codex_limits", side_effect=AssertionError("wrong provider")),
              patch.object(native_ai_usage, "read_native_usage", return_value=None) as native):
            self.assertIsNone(ai_usage.read_ai_usage("unknown"))
            self.assertIsNone(ai_usage.read_ai_usage("workbuddy", ""))
            native.assert_called_once_with("workbuddy")

    def test_harness_uses_its_native_account_page(self):
        metrics = [ai_usage.Metric("充值余额", "¥8.50", None)]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ai_usage, "_dsh_home", return_value=Path(tmp)), \
                patch.object(native_ai_usage, "read_native_usage", return_value=metrics) as native:
            # 本机没有挂件账本时，才轮到 UI 自动化这条老路。
            self.assertEqual(ai_usage.read_ai_usage("deepseek_harness"), metrics)
            native.assert_called_once_with("deepseek_harness")


class NativeUsageTests(unittest.TestCase):
    def test_workbuddy_uses_summary_remaining_values_instead_of_usage_details(self):
        cards = native_ai_usage.workbuddy_metrics([
            "体验版", "1,100.25", "总剩余积分", "套餐基础积分", "0", "套餐赠送积分", "100.25",
            "平台奖励积分", "1,000", "已加入", "积分明细", "套餐基础积分", "已使用", "500", "/", "500",
            "剩余0%", "平台奖励积分", "已使用", "10", "/", "1,010", "剩余99%",
        ])
        self.assertEqual([(m.label, m.display) for m in cards],
                         [("套餐基础积分", "0"), ("奖励积分", "1,000")])

    def test_workbuddy_requires_a_complete_summary(self):
        self.assertIsNone(native_ai_usage.workbuddy_metrics(
            ["总剩余积分", "套餐基础积分", "500", "平台奖励积分", "0", "积分明细"]))
        self.assertIsNone(native_ai_usage.workbuddy_metrics(
            ["500", "总剩余积分", "套餐基础积分", "加载中", "积分明细"]))

    def test_workbuddy_can_show_a_single_reported_bucket(self):
        cards = native_ai_usage.workbuddy_metrics(["500", "总剩余积分", "套餐基础积分", "500", "积分明细"])
        self.assertEqual([m.display for m in cards], ["500", "—"])
        self.assertEqual(cards[1].detail, "本页未显示")

    def test_harness_keeps_native_currency_and_only_shows_reported_bonus(self):
        cards = native_ai_usage.harness_metrics(
            ["已登录 DeepSeek", "账号资料暂不可用", "充值余额", "¥0.00", "更多"])
        self.assertEqual([(m.label, m.display) for m in cards], [("充值余额", "¥0.00")])
        cards = native_ai_usage.harness_metrics(
            ["Topped-up balance", "-$0.01", "¥1,000.20", "Granted balance", "<¥0.01", "More"])
        self.assertEqual([(m.label, m.display) for m in cards],
                         [("充值余额", "-$0.01 · ¥1,000.20"), ("赠金余额", "<¥0.01")])

    def test_harness_ignores_unavailable_or_unrelated_balances(self):
        self.assertIsNone(native_ai_usage.harness_metrics(
            ["充值余额", "前往开放平台查看", "赠金余额", "前往开放平台查看", "更多"]))
        self.assertIsNone(native_ai_usage.harness_metrics(["DeepSeek 余额", "¥50.00"]))

    def test_native_document_must_be_the_expected_origin_and_page(self):
        is_usage = native_ai_usage._usage_document
        self.assertTrue(is_usage("workbuddy", "https://www.workbuddy.cn/profile/credits-usage?embed=1"))
        self.assertFalse(is_usage("workbuddy", "https://www.workbuddy.cn/profile"))
        self.assertFalse(is_usage("workbuddy", "https://www.workbuddy.cn.example.org/profile/credits-usage"))
        self.assertTrue(is_usage("deepseek_harness", "dsh-app://app/"))
        self.assertFalse(is_usage("deepseek_harness", "https://platform.deepseek.com/usage"))

    def test_workbuddy_falls_back_to_local_renderer_documents(self):
        """新版客户端把积分页放进本地渲染页时，远端 URL 不再出现。"""
        order = native_ai_usage.usage_page_order
        local = "file:///C:/Program%20Files/WorkBuddy/resources/app.asar/renderer/index.html"
        self.assertEqual(order("workbuddy", [local, "https://www.workbuddy.cn/profile/credits-usage?embed=1"]),
                         (1,))
        self.assertEqual(order("workbuddy", [local]), (0,))
        self.assertEqual(order("workbuddy", ["https://example.org/fake", local]), (0, 1))
        # 其它客户端仍只认严格匹配的文档。
        self.assertEqual(order("deepseek_harness", [local]), ())

    def test_fallback_rows_are_only_accepted_with_a_complete_summary(self):
        rows = ["500", "总剩余积分", "套餐基础积分", "386.52", "平台奖励积分", "528", "积分明细"]
        cards = native_ai_usage.workbuddy_metrics(rows)
        self.assertEqual([m.display for m in cards], ["386.52", "528"])
        self.assertIsNone(native_ai_usage.workbuddy_metrics(["Jia.Xie", "助理", "最近"]))


class ActiveSourceTests(unittest.TestCase):
    """方案 2：Aisland 侧的零配置主动数据源，完全不碰 WorkBuddy。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aisland-quota-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(ai_usage.QUOTA_SOURCE_ENV, None)

    def source(self, payload):
        path = self.root / "workbuddy_quota.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_explicit_config_wins_over_everything(self):
        configured = str(self.source({"baseCredit": 1, "bonusCredit": 2}))
        os.environ[ai_usage.QUOTA_SOURCE_ENV] = "http://127.0.0.1:9/other"
        self.assertEqual(ai_usage.resolve_workbuddy_source(configured, self.root), configured)

    def test_environment_override_wins_over_drop_in_file(self):
        self.source({"baseCredit": 1, "bonusCredit": 2})
        os.environ[ai_usage.QUOTA_SOURCE_ENV] = "http://127.0.0.1:9/quota"
        self.assertEqual(ai_usage.resolve_workbuddy_source("", self.root),
                         "http://127.0.0.1:9/quota")

    def test_drop_in_file_is_discovered_without_any_configuration(self):
        path = self.source({"baseCredit": 1, "bonusCredit": 2})
        self.assertEqual(ai_usage.resolve_workbuddy_source("", self.root), str(path))
        self.assertEqual(ai_usage.resolve_workbuddy_source("", self.root / "empty"), "")

    def test_source_stamp_tracks_the_file_and_changes_when_rewritten(self):
        path = self.source({"baseCredit": 1, "bonusCredit": 2})
        first = ai_usage.source_stamp(str(path))
        self.assertIsNotNone(first)
        os.utime(path, (first + 5, first + 5))
        self.assertEqual(ai_usage.source_stamp(str(path)), first + 5)
        self.assertIsNone(ai_usage.source_stamp("http://127.0.0.1:9/quota"))
        self.assertIsNone(ai_usage.source_stamp(""))
        self.assertIsNone(ai_usage.source_stamp(str(self.root / "missing.json")))

    def test_file_metrics_carry_the_producer_timestamp(self):
        path = self.source({"account": {"baseCredit": 386.52, "bonusCredit": 628}})
        stamp = path.stat().st_mtime
        metrics = ai_usage.read_workbuddy_credits(str(path))
        self.assertEqual([(m.label, m.display) for m in metrics],
                         [("套餐基础积分", "386.52"), ("奖励积分", "628")])
        self.assertEqual([m.observed_at for m in metrics], [stamp, stamp])


class RetryIntervalTests(unittest.TestCase):
    def test_no_failure_keeps_the_normal_cadence(self):
        self.assertEqual(ai_usage.retry_interval(10, 0), 10)
        self.assertEqual(ai_usage.retry_interval(2, 0), 2)

    def test_missed_panel_retries_quickly_then_backs_off(self):
        self.assertEqual([ai_usage.retry_interval(10, n) for n in range(1, 6)],
                         [2, 3, 5, 10, 10])

    def test_visible_panel_backs_off_after_repeated_failures(self):
        self.assertEqual([ai_usage.retry_interval(2, n) for n in range(1, 6)], [2, 3, 5, 10, 10])


class ProviderChoiceTests(unittest.TestCase):
    """The card must not jump to another client just because the app restarted."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aisland-provider-choice-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patches = (patch.object(ai_usage, "CACHE_PATH", self.root / "ai_usage_cache.json"),
                   patch.object(ai_usage, "DATA_DIR", self.root))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_explicit_selection_wins(self):
        self.assertEqual(ai_usage.resolve_provider("codex", "workbuddy", "workbuddy"), "codex")
        self.assertEqual(ai_usage.resolve_provider("codex", "workbuddy", "workbuddy",
                                                   "deepseek_harness"), "codex")

    def test_active_client_wins_over_old_notifications_in_auto_mode(self):
        self.assertEqual(ai_usage.resolve_provider("auto", "codex", "codex", "workbuddy"),
                         "workbuddy")
        self.assertEqual(ai_usage.resolve_provider("auto", "codex", "workbuddy", "wechat"),
                         "codex")

    def test_latest_notification_wins_in_auto_mode(self):
        self.assertEqual(ai_usage.resolve_provider("auto", "workbuddy", "codex"), "workbuddy")

    def test_last_provider_survives_a_restart(self):
        with patch.object(ai_usage, "installed_ai", return_value=("codex", "workbuddy")):
            self.assertEqual(ai_usage.resolve_provider("auto", None, "workbuddy"), "workbuddy")
            self.assertEqual(ai_usage.resolve_provider(None, None, "deepseek_harness"),
                             "deepseek_harness")

    def test_first_run_prefers_the_reading_that_is_still_cached(self):
        """升级后第一次启动：没有通知、也没记住选择，别把 WorkBuddy 的读数换成 Codex。"""
        ai_usage.save_usage_cache("codex", [ai_usage.Metric("5 小时", "40%", 40, "已用")])
        older = ai_usage.load_usage_cache()["codex"]["updated"]
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "500", None, "剩余积分")])
        data = ai_usage.load_usage_cache()
        data["codex"]["updated"] = older - 60          # 明确让 WorkBuddy 更新
        (self.root / "ai_usage_cache.json").write_text(json.dumps(data), encoding="utf-8")
        with patch.object(ai_usage, "installed_ai", return_value=("codex", "workbuddy")):
            self.assertEqual(ai_usage.resolve_provider("auto", None, ""), "workbuddy")

    def test_unknown_values_fall_back_to_what_is_installed(self):
        with patch.object(ai_usage, "installed_ai", return_value=("codex",)):
            self.assertEqual(ai_usage.resolve_provider("auto", "wechat", ""), "codex")
        with patch.object(ai_usage, "installed_ai", return_value=()):
            self.assertIsNone(ai_usage.resolve_provider("auto", None, None))

    def test_provider_key_is_a_real_config_path(self):
        self.assertEqual(ai_usage.PROVIDER_KEY, "notify.ai_provider_last")


class UsageCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aisland-quota-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "ai_usage_cache.json"
        self.patcher = patch.object(ai_usage, "CACHE_PATH", self.cache)
        self.data_patcher = patch.object(ai_usage, "DATA_DIR", self.root)
        self.patcher.start()
        self.data_patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.data_patcher.stop)

    def test_round_trip_and_timestamp(self):
        metrics = [ai_usage.Metric("套餐基础积分", "2,000", None, "积分"),
                   ai_usage.Metric("奖励积分", "500", None, "积分")]
        ai_usage.save_usage_cache("workbuddy", metrics)
        self.assertEqual(ai_usage.cached_metrics("workbuddy"), metrics)
        self.assertIsNotNone(ai_usage.cached_updated("workbuddy"))
        self.assertIsNone(ai_usage.cached_metrics("codex"))

    def test_two_providers_do_not_overwrite_each_other(self):
        ai_usage.save_usage_cache("codex", [ai_usage.Metric("5 小时", "10%", 10, "已用")])
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "1", None, "")])
        self.assertEqual(len(ai_usage.load_usage_cache()), 2)
        self.assertEqual(ai_usage.cached_metrics("codex")[0].percent, 10)

    def test_corrupt_cache_is_ignored(self):
        self.cache.write_text("{bad", encoding="utf-8")
        self.assertEqual(ai_usage.load_usage_cache(), {})
        self.assertIsNone(ai_usage.cached_metrics("workbuddy"))

    def test_percent_is_validated(self):
        self.cache.write_text(json.dumps({"codex": {"updated": 1, "metrics": [
            {"label": "x", "display": "x", "percent": float("nan"), "detail": ""}]}}),
            encoding="utf-8")
        self.assertIsNone(ai_usage.cached_metrics("codex")[0].percent)

    def test_new_reading_replaces_the_previous_period(self):
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "999", None, "剩余积分")])
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "500", None, "剩余积分")])
        self.assertEqual([m.display for m in ai_usage.cached_metrics("workbuddy")], ["500"])
        self.assertEqual(list(ai_usage.load_usage_cache()), ["workbuddy"])
        leftovers = [p.name for p in self.root.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_native_readings_do_not_rewrite_the_cache_each_poll(self):
        """native 读数没有 observed_at：只有指标内容决定是否重写，不再每 2 秒落盘。"""
        metrics = [ai_usage.Metric("套餐基础积分", "2,000", None, "积分")]
        first = ai_usage.save_usage_cache("workbuddy", metrics)
        before = self.cache.stat().st_mtime_ns
        second = ai_usage.save_usage_cache("workbuddy", metrics)
        self.assertEqual(second, first)
        self.assertEqual(self.cache.stat().st_mtime_ns, before)
        self.assertEqual(ai_usage.cached_updated("workbuddy"), first)

    def test_identical_metrics_keep_a_newer_producer_timestamp(self):
        for stamp in (1000.0, 2000.0):
            metrics = [ai_usage.Metric("奖励积分", "500", None, observed_at=stamp)]
            self.assertEqual(ai_usage.save_usage_cache("workbuddy", metrics), stamp)
        self.assertEqual(ai_usage.cached_updated("workbuddy"), 2000.0)
        # A restart must also recover the newer timestamp from disk.
        with patch.object(ai_usage, "_cache_path", ""):
            self.assertEqual(ai_usage.cached_updated("workbuddy"), 2000.0)

    def test_identical_metrics_with_old_or_same_producer_time_do_not_rewrite(self):
        metrics = [ai_usage.Metric("奖励积分", "500", None, observed_at=2000.0)]
        ai_usage.save_usage_cache("workbuddy", metrics)
        with patch.object(ai_usage.os, "replace", side_effect=AssertionError("redundant write")):
            for stamp in (2000.0, 1000.0):
                older = [ai_usage.Metric("奖励积分", "500", None, observed_at=stamp)]
                self.assertEqual(ai_usage.save_usage_cache("workbuddy", older), 2000.0)
        self.assertEqual(ai_usage.cached_updated("workbuddy"), 2000.0)

    def test_changed_content_still_rewrites(self):
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "2,000", None)])
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "1,800", None)])
        self.assertEqual(ai_usage.cached_metrics("workbuddy")[0].display, "1,800")

    def test_reads_are_memoized_until_the_file_changes(self):
        metrics = [ai_usage.Metric("充值余额", "¥8.50", None)]
        ai_usage.save_usage_cache("deepseek_harness", metrics)
        with patch.object(ai_usage, "_read_cache_file", side_effect=AssertionError("不该再读盘")):
            self.assertEqual(ai_usage.cached_metrics("deepseek_harness"), metrics)
            self.assertIsNotNone(ai_usage.cached_updated("deepseek_harness"))
        # 文件被外部重写后，同一缓存路径上的新内容必须被重新读出。
        data = ai_usage.load_usage_cache()
        data["deepseek_harness"]["metrics"][0]["display"] = "¥9.00"
        self.cache.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(ai_usage.cached_metrics("deepseek_harness")[0].display, "¥9.00")


class DisplayMetricsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aisland-quota-display-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cache_patch = patch.object(ai_usage, "CACHE_PATH", self.root / "ai_usage_cache.json")
        data_patch = patch.object(ai_usage, "DATA_DIR", self.root)
        cache_patch.start()
        data_patch.start()
        self.addCleanup(cache_patch.stop)
        self.addCleanup(data_patch.stop)

    def test_placeholders_follow_the_selected_provider(self):
        for provider, labels in (("codex", ("5 小时剩余", "周剩余")),
                                 ("workbuddy", ("套餐基础积分", "奖励积分")),
                                 ("deepseek_harness", ("充值余额",))):
            with self.subTest(provider=provider):
                cards = ai_usage.display_metrics(provider, [], None, now=1_000_000)
                self.assertEqual(tuple(c.label for c in cards), labels)
                self.assertEqual([c.display for c in cards], ["—"] * len(labels))

    def test_codex_card_and_bar_show_remaining_quota_and_keep_reset_time(self):
        readings = [ai_usage.Metric("5 小时", "5%", 5, "01:35 重置"),
                    ai_usage.Metric("周用量", "53%", 53, "11/16 14:58 重置")]
        shown = ai_usage.display_metrics("codex", readings, 1_000_000, now=1_000_010)
        self.assertEqual(shown, [ai_usage.Metric("5 小时剩余", "95%", 95, "01:35 重置"),
                                 ai_usage.Metric("周剩余", "47%", 47, "11/16 14:58 重置")])

    def test_codex_remaining_limits_and_missing_window(self):
        for used, remaining in ((0, 100), (100, 0)):
            readings = ai_usage.quota_metrics((ai_usage.QuotaWindow(used, 300, None), None))
            shown = ai_usage.display_metrics("codex", readings, 1_000_000, now=1_000_010)
            self.assertEqual(shown[0].percent, remaining)
            self.assertEqual(shown[0].display, f"{remaining}%")
            self.assertEqual(shown[1], ai_usage.Metric("周剩余", "—", None, "暂无数据"))

    def test_stale_reading_is_labelled_instead_of_cleared(self):
        cards = [ai_usage.Metric("套餐基础积分", "2,000", None, "积分")]
        shown = ai_usage.display_metrics("workbuddy", cards, 1_000_000, now=1_000_600)
        self.assertEqual(shown[0].display, "2,000")
        self.assertIn("上次更新", shown[0].detail)

    def test_restart_restores_the_last_persisted_reading(self):
        cards = [ai_usage.Metric("奖励积分", "500", None, "积分")]
        ai_usage.save_usage_cache("workbuddy", [ai_usage.Metric("套餐基础积分", "2,000", None, "积分"),
                                                cards[0]])
        shown = ai_usage.display_metrics("workbuddy", None, None, now=time.time() + 600)
        self.assertEqual([c.display for c in shown], ["2,000", "500"])
        self.assertTrue(all("上次更新" in c.detail for c in shown))

    def test_empty_memory_does_not_hide_cached_values(self):
        ai_usage.save_usage_cache("codex", [ai_usage.Metric("5 小时", "40%", 40, "已用")])
        shown = ai_usage.display_metrics("codex", [], None, now=time.time() + 600)
        self.assertEqual(shown[0].display, "60%")
        self.assertEqual(shown[0].percent, 60)
        self.assertIn("上次更新", shown[0].detail)
        self.assertEqual(ai_usage.cached_metrics("codex")[0].percent, 40)


class NativeReadFailsafeTests(unittest.TestCase):
    """A stuck UI Automation call must never freeze the following readings."""

    def test_vanished_windows_are_dropped(self):
        self.assertEqual(native_ai_usage._live_handles([0, 0x7FFFFFF0]), [])


if __name__ == "__main__":
    unittest.main()
