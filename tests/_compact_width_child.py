"""收起态宽度检查跑在子进程里。

test_maintenance 断言主测试进程没有导入 PySide6，所以任何会 import Qt 的模块
都必须在子进程里加载。这里只计算排版，不绘制窗口。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from PySide6 import QtWidgets                     # noqa: E402

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from aisland.ui import compact                    # noqa: E402

WEATHER = ("\ue9b9", "11°", None)


def view(kind: str, badges: int, weather=None):
    cv = compact.CompactView()
    cv.view = kind
    cv.weather = weather
    cv.badges = tuple("#f1bf27" for _ in range(badges))
    return cv


class CompactWidthTests(unittest.TestCase):
    def widths(self, badges: int):
        return {
            "pet": view("pet", badges).size_hint(),
            "clock": view("clock", badges).size_hint(),
            "clock_weather": view("clock", badges, WEATHER).size_hint(),
        }

    def test_clock_and_pet_share_one_width(self):
        for badges in range(0, 5):
            got = self.widths(badges)
            self.assertEqual(got["pet"], got["clock"], f"badges={badges}")
            self.assertEqual(got["pet"], got["clock_weather"], f"weather must not widen the pill; badges={badges}")

    def test_weather_never_overlaps_clock(self):
        for badges in range(0, 5):
            cv = view("clock", badges, WEATHER)
            lay = cv.idle_layout()
            self.assertEqual(lay["width"], cv.size_hint()[0])
            w = lay["weather"]
            if w is None:                      # 字体偏宽时允许不画，但绝不能重叠
                continue
            self.assertGreaterEqual(w["left"], lay["clock_x"] + lay["clock_w"] + 6, f"badges={badges}")
            self.assertLessEqual(w["right"], lay["width"] - 12, f"badges={badges}")

    def test_weather_is_drawn_when_there_is_room(self):
        with mock.patch.object(compact, "W_IDLE", 320):
            lay = view("clock", 1, WEATHER).idle_layout()
        w = lay["weather"]
        self.assertIsNotNone(w)
        self.assertGreaterEqual(w["left"], lay["clock_x"] + lay["clock_w"] + 6)
        self.assertLessEqual(w["right"], lay["width"] - 12)

    def test_weather_is_dropped_when_there_is_no_room(self):
        with mock.patch.object(compact, "W_IDLE", 80):
            lay = view("clock", 4, WEATHER).idle_layout()
        self.assertIsNone(lay["weather"])          # 宁可不画，也不压到时间上
        self.assertIsNone(view("clock", 0, None).idle_layout()["weather"])

    def test_activity_with_status_and_task_stays_within_cap(self):
        for sub in ("任务中断，请重试 · 审查文件夹中的代码",
                    "查看 Aisland 最新进度 · 你的助手想问你一个问题"):
            cv = compact.CompactView()
            cv.show_activity({"title": "WorkBuddy", "sub": sub}, 4.0)
            w, h = cv.size_hint()
            self.assertLessEqual(w, 460)
            self.assertGreater(w, 220)
            self.assertEqual(h, compact.H_ACT)


if __name__ == "__main__":
    unittest.main()
