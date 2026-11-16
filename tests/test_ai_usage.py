"""Quota snapshots use the Codex bucket and preserve unavailable windows."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aisland.services.ai_usage import QuotaWindow, parse_codex_limits


class CodexQuotaTests(unittest.TestCase):
    def test_prefer_codex_bucket(self):
        short = {"usedPercent": 13, "windowDurationMins": 300, "resetsAt": 1794242947}
        week = {"usedPercent": 18, "windowDurationMins": 10080, "resetsAt": 1794808722}
        self.assertEqual(
            parse_codex_limits({
                "rateLimits": {"primary": {"usedPercent": 90}},
                "rateLimitsByLimitId": {"codex": {"primary": short, "secondary": week}},
            }),
            (QuotaWindow(13, 300, 1794242947), QuotaWindow(18, 10080, 1794808722)),
        )

    def test_legacy_bucket_and_missing_week(self):
        self.assertEqual(
            parse_codex_limits({"rateLimits": {"primary": {"usedPercent": 0}, "secondary": None}}),
            (QuotaWindow(0, None, None), None),
        )

    def test_unrelated_bucket_does_not_become_codex_usage(self):
        self.assertEqual(parse_codex_limits({
            "rateLimits": {"limitId": "other", "primary": {"usedPercent": 33}},
            "rateLimitsByLimitId": {"other": {"primary": {"usedPercent": 33}}},
        }), (None, None))

    def test_invalid_values_and_bounded_percentage(self):
        self.assertEqual(parse_codex_limits({"rateLimits": {
            "primary": {"usedPercent": float("nan"), "windowDurationMins": 300},
            "secondary": {"usedPercent": 101, "windowDurationMins": -1, "resetsAt": "unknown"},
        }}), (None, QuotaWindow(100, None, None)))


class CodexLogDiscoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_a_single_unreadable_log_does_not_clear_the_candidates(self):
        """一个被占用/消失的日志只跳过它自己，不能把整个候选集清空。"""
        import json
        import os
        import time
        from unittest.mock import patch

        from aisland.services import ai_usage

        sessions = self.home / "sessions" / "a"
        sessions.mkdir(parents=True)
        record = {"timestamp": "2026-11-12T05:00:00+00:00",
                  "payload": {"rate_limits": {"primary": {"used_percent": 20, "window_minutes": 300}}}}
        older = sessions / "older.jsonl"
        older.write_text(json.dumps(record) + "\n", encoding="utf-8")
        good = sessions / "good.jsonl"
        good.write_text(json.dumps(record) + "\n", encoding="utf-8")
        poison = sessions / "locked.jsonl"          # 更新，但 stat 一直失败
        poison.write_text("{}", encoding="utf-8")
        os.utime(older, (time.time() - 3600,) * 2)
        os.utime(poison, (time.time() + 60,) * 2)

        real_stat = Path.stat

        def flaky(self, *args, **kwargs):
            if self.name == "locked.jsonl":
                raise OSError("file is locked")
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", flaky):
            primary, _week = ai_usage.read_codex_log_limits(home=self.home)
        self.assertIsNotNone(primary)
        self.assertEqual(primary.used_percent, 20)
        self.assertEqual(primary.minutes, 300)
