"""额度读数不必起进程：Codex 的 rollout 日志和 DSH 的挂件账本都能直接读盘。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisland.services import ai_usage

SHORT = {"used_percent": 92.0, "window_minutes": 300, "resets_at": 1794304853}
WEEK = {"used_percent": 52.0, "window_minutes": 10080, "resets_at": 1794808722}
LOG_STAMP = datetime.fromisoformat("2026-11-10T08:59:08+00:00").timestamp()


def _rollout(path: Path, payload: dict, timestamp: str = "2026-11-10T08:59:08Z") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": timestamp, "type": "event_msg", "payload": payload}
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


class CodexLogTests(unittest.TestCase):
    def test_takes_the_last_snapshot_from_the_newest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/2026/11/10/rollout-old.jsonl",
                     {"rate_limits": {"limit_id": "codex",
                                      "primary": {"used_percent": 10.0},
                                      "secondary": None}})
            _rollout(home / "sessions/2026/11/10/rollout-new.jsonl",
                     {"rate_limits": {"limit_id": "codex", "primary": SHORT, "secondary": WEEK}})
            self.assertEqual(
                ai_usage.read_codex_log_limits(home=home),
                (ai_usage.QuotaWindow(92.0, 300, 1794304853, LOG_STAMP),
                 ai_usage.QuotaWindow(52.0, 10080, 1794808722, LOG_STAMP)),
            )

    def test_other_buckets_and_garbage_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/a.jsonl", {"rate_limits": {"limit_id": "other",
                                                                "primary": {"used_percent": 33.0}}})
            (home / "sessions/b.jsonl").write_text("not json\n", encoding="utf-8")
            self.assertEqual(ai_usage.read_codex_log_limits(home=home), (None, None))

    def test_appended_log_lines_are_parsed_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"limit_id": "codex", "primary": SHORT, "secondary": WEEK}})
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": "2026-11-11T09:00:00Z", "type": "event_msg",
                                         "payload": {"rate_limits": {"limit_id": "codex", "primary": {
                                             "used_percent": 30.0, "window_minutes": 300,
                                             "resets_at": 1794304853}}}}) + "\n")
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 30.0)

    def test_a_partial_line_is_not_parsed_until_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            with path.open("ab") as handle:
                handle.write(b'{"payload":{"rate_limits":{"primary":{"used_percent": 3.0}}}')
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            with path.open("ab") as handle:
                handle.write(b'}\n')
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 3.0)

    def test_unchanged_log_is_not_reopened_or_reparsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/a.jsonl", {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            first = ai_usage.read_codex_log_limits(home=home)
            with (mock.patch.object(ai_usage, "_seed_codex_state",
                                   side_effect=AssertionError("不该从头重播")),
                  mock.patch.object(ai_usage, "_advance_codex_state",
                                   side_effect=AssertionError("不该增量重读"))):
                self.assertEqual(ai_usage.read_codex_log_limits(home=home), first)

    def test_a_new_session_file_is_found_after_the_scan_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/a.jsonl", {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            _rollout(home / "sessions/b.jsonl", {"rate_limits": {"primary": {
                "used_percent": 5.0, "window_minutes": 300, "resets_at": 1794304853}}})
            # 扫描间隔内：候选列表没刷新，仍是旧会话的读数。
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            with mock.patch.object(ai_usage, "CODEX_SCAN_TTL", 0.0):
                self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 5.0)

    def test_truncated_log_is_reseeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            ai_usage.read_codex_log_limits(home=home)
            path.write_text(json.dumps({"payload": {"rate_limits": {
                "primary": {"used_percent": 7.0}}}}) + "\n", encoding="utf-8")
            with mock.patch.object(ai_usage, "CODEX_SCAN_TTL", 0.0):
                self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 7.0)

    def test_same_length_rewrite_replaces_the_saved_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"primary": {"used_percent": 92.0}}})
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            stamp = path.stat().st_mtime
            size = path.stat().st_size
            _rollout(path, {"rate_limits": {"primary": {"used_percent": 30.0}}})
            os.utime(path, (stamp + 1, stamp + 1))
            self.assertEqual(path.stat().st_size, size)
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 30.0)

    def test_larger_rewrite_detects_changed_quota_before_an_unchanged_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"primary": {"used_percent": 92.0}}})
            filler = json.dumps({"payload": {"text": "x" * 6000}}) + "\n"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(filler)
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            _rollout(path, {"rate_limits": {"primary": {"used_percent": 30.0}}})
            with path.open("a", encoding="utf-8") as handle:
                handle.write(filler + "{}\n")
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 30.0)

    def test_replaced_file_is_detected_even_with_matching_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"primary": {"used_percent": 92.0}}})
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 92.0)
            stat = path.stat()
            replacement = home / "replacement"
            _rollout(replacement, {"rate_limits": {"primary": {"used_percent": 30.0}}})
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(replacement, path)
            self.assertEqual(path.stat().st_size, stat.st_size)
            self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
            self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 30.0)

    def test_a_true_append_never_reseeds_an_existing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/a.jsonl"
            _rollout(path, {"rate_limits": {"primary": {"used_percent": 92.0}}})
            ai_usage.read_codex_log_limits(home=home)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"payload": {"rate_limits": {
                    "primary": {"used_percent": 30.0}}}}) + "\n")
            with mock.patch.object(ai_usage, "_seed_codex_state", side_effect=AssertionError("append reseeded")):
                self.assertEqual(ai_usage.read_codex_log_limits(home=home)[0].used_percent, 30.0)

    def test_reads_only_the_tail_of_a_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "sessions/big.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            filler = json.dumps({"payload": {"blob": "x" * 4000}}) + "\n"
            with path.open("w", encoding="utf-8") as handle:
                for _ in range(80):
                    handle.write(filler)
                handle.write(json.dumps({"payload": {"rate_limits": {"primary": SHORT}}}) + "\n")
            with mock.patch.object(ai_usage, "CODEX_TAIL_BYTES", 8192):
                self.assertEqual(ai_usage.read_codex_log_limits(home=home),
                                 (ai_usage.QuotaWindow(92.0, 300, 1794304853, path.stat().st_mtime), None))

    def test_usage_prefers_the_log_and_never_spawns_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/a.jsonl", {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            with mock.patch.object(ai_usage, "_codex_home", return_value=home), \
                    mock.patch.object(ai_usage.time, "time", return_value=LOG_STAMP + 5), \
                    mock.patch.object(ai_usage, "read_codex_limits", side_effect=AssertionError("不该起进程")):
                metrics = ai_usage.read_ai_usage("codex")
        self.assertEqual([m.display for m in metrics], ["92%", "52%"])

    def test_expired_log_window_is_refreshed_by_the_app_server(self):
        now = SHORT["resets_at"] + 1
        refreshed = (ai_usage.QuotaWindow(3.0, 300, now + 3000), ai_usage.QuotaWindow(52.0, 10080, None))
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/a.jsonl", {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            with (mock.patch.object(ai_usage, "_codex_home", return_value=home),
                  mock.patch.object(ai_usage.time, "time", return_value=now),
                  mock.patch.object(ai_usage, "read_codex_limits", return_value=refreshed) as live):
                metrics = ai_usage.read_ai_usage("codex")
            live.assert_called_once_with()
        self.assertEqual([m.display for m in metrics], ["3%", "52%"])

    def test_repeated_disk_reads_keep_the_original_snapshot_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _rollout(home / "sessions/a.jsonl", {"rate_limits": {"primary": SHORT, "secondary": WEEK}})
            with (mock.patch.object(ai_usage, "_codex_home", return_value=home),
                  mock.patch.object(ai_usage.time, "time", return_value=LOG_STAMP + 600),
                  mock.patch.object(ai_usage, "CACHE_PATH", home / "usage.json"),
                  mock.patch.object(ai_usage, "DATA_DIR", home)):
                for _ in range(2):
                    metrics = ai_usage.read_ai_usage("codex")
                    self.assertEqual(ai_usage.save_usage_cache("codex", metrics), LOG_STAMP)
                self.assertEqual(ai_usage.cached_updated("codex"), LOG_STAMP)
                shown = ai_usage.display_metrics("codex", now=LOG_STAMP + 601)
                self.assertEqual([m.display for m in shown], ["8%", "48%"])
                self.assertTrue(all("上次更新" in m.detail for m in shown))

    def test_cached_reading_is_marked_as_old_as_soon_as_its_window_resets(self):
        stamp = SHORT["resets_at"] - 1
        metric = ai_usage.Metric("5 小时", "92%", 92, "18:00 重置",
                                 observed_at=stamp, resets_at=SHORT["resets_at"])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with (mock.patch.object(ai_usage, "CACHE_PATH", directory / "usage.json"),
                  mock.patch.object(ai_usage, "DATA_DIR", directory)):
                ai_usage.save_usage_cache("codex", [metric])
                shown = ai_usage.display_metrics("codex", now=stamp + 2)
        self.assertEqual(shown[0].display, "8%")
        self.assertIn("上次更新", shown[0].detail)

    def test_falls_back_to_the_app_server_when_no_log(self):
        windows = (ai_usage.QuotaWindow(1.0, 300, None), None)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ai_usage, "_codex_home", return_value=Path(tmp)), \
                mock.patch.object(ai_usage, "read_codex_limits", return_value=windows):
            metrics = ai_usage.read_ai_usage("codex")
        self.assertEqual(metrics[0].display, "1%")


class DeepSeekLedgerTests(unittest.TestCase):
    def test_balance_and_today_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".dshw-usage.json").write_text(json.dumps({
                "lastBalance": 8.764183, "todayUsage": 0.006882, "currency": "CNY"}), encoding="utf-8")
            metrics = ai_usage.read_deepseek_ledger(home=home)
        self.assertEqual(metrics[0].display, "¥8.76")
        self.assertEqual(metrics[0].detail, "今日已用 ¥0.007")
        self.assertEqual(metrics[1].detail, "日初 ¥8.77")

    def test_missing_or_broken_ledger_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ai_usage.read_deepseek_ledger(home=Path(tmp)))
            (Path(tmp) / ".dshw-usage.json").write_text("{oops", encoding="utf-8")
            self.assertIsNone(ai_usage.read_deepseek_ledger(home=Path(tmp)))
            (Path(tmp) / ".dshw-usage.json").write_text(json.dumps({"lastBalance": "8"}), encoding="utf-8")
            self.assertIsNone(ai_usage.read_deepseek_ledger(home=Path(tmp)))

    def test_stale_ledger_does_not_report_yesterdays_spend_as_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".dshw-usage.json").write_text(json.dumps({
                "date": "2026-11-09", "lastBalance": 8.764183, "todayUsage": 0.006882}), encoding="utf-8")
            metrics = ai_usage.read_deepseek_ledger(home=home)
        self.assertEqual(metrics[0].display, "¥8.76")          # 余额仍是最新已知值
        self.assertEqual(metrics[0].detail, "账本停在 2026-11-09")
        self.assertEqual(metrics[1].display, "—")              # 昨天的消耗不能算今天
        self.assertEqual(metrics[1].detail, "挂件今天没跑")

    def test_ledger_dated_today_still_reports_today_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".dshw-usage.json").write_text(json.dumps({
                "date": time.strftime("%Y-%m-%d"), "lastBalance": 8.764183,
                "todayUsage": 0.006882}), encoding="utf-8")
            metrics = ai_usage.read_deepseek_ledger(home=home)
        self.assertEqual(metrics[1].display, "¥0.007")

    def test_usage_prefers_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ai_usage, "_dsh_home", return_value=Path(tmp)):
            (Path(tmp) / ".dshw-usage.json").write_text(
                json.dumps({"lastBalance": 2.0, "todayUsage": 0.5}), encoding="utf-8")
            with mock.patch("aisland.services.native_ai_usage.read_native_usage",
                            side_effect=AssertionError("账本可用时不该走 UI 自动化")):
                metrics = ai_usage.read_ai_usage("deepseek_harness")
        self.assertEqual([m.display for m in metrics], ["¥2.00", "¥0.500"])


if __name__ == "__main__":
    unittest.main()
