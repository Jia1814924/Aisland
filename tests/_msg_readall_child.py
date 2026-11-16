# -*- coding: utf-8 -*-
"""消息页「一键已读」的离屏验证（由 tests/test_msg_readall.py 以子进程调用）。

主测试进程不能加载 Qt（test_maintenance 有断言），所以这里单独起进程跑。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _boot():
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from aisland.core.config import cfg
    from aisland.services.notify import NotifyService
    from aisland.ui.pages.home import MessagesPage

    return app, cfg, NotifyService, MessagesPage


def _page(cfg, NotifyService, MessagesPage, counts):
    svc = NotifyService()
    svc.counts = dict(counts)
    page = MessagesPage(types.SimpleNamespace(notify=svc))
    page._request_usage = lambda **_kw: None  # 不真去读额度，避免联网/读盘
    return page, svc


class MsgReadAllTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.cfg, cls.NotifyService, cls.MessagesPage = _boot()
        # 临时目录放在 build/ 下且由 TemporaryDirectory 负责回收，不能泄漏到项目根目录。
        staging = os.path.join(ROOT, "build")
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="msgread-", dir=staging if os.path.isdir(staging) else None)
        cls.state = cls._tmp.name
        cls.patch = mock.patch.dict(os.environ, {"AISLAND_USER_HOME": cls.state})
        cls.patch.start()
        cls.cfg.load()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.cfg.set("notify.ai_provider", "workbuddy")

    def test_button_sits_in_ai_header_and_clears_only_that_group(self):
        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage,
                          {"workbuddy": 15, "codex": 2, "wechat": 3})
        page.refresh()
        self.assertEqual(page._provider, "workbuddy")
        self.assertEqual(page.ai_count.text(), "15 未读")
        self.assertTrue(page.ai_clear.isEnabled())

        page.ai_clear.clicked.emit()
        self.assertEqual(svc.counts["workbuddy"], 0)     # 本组清零
        self.assertEqual(svc.counts["codex"], 2)         # 另一家 AI 不动
        self.assertEqual(svc.counts["wechat"], 3)        # 微信不动

        page.refresh()
        self.assertEqual(page.ai_count.text(), "0 未读")
        self.assertFalse(page.ai_clear.isEnabled())      # 没有可清的，按钮淡化

    def test_wechat_header_clears_only_wechat(self):
        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage,
                          {"workbuddy": 4, "codex": 1, "wechat": 2, "mail": 1, "dingtalk": 3})
        page.refresh()
        page.wechat_clear.clicked.emit()
        self.assertEqual(svc.counts, {"workbuddy": 4, "codex": 1, "wechat": 0,
                                      "mail": 1, "dingtalk": 3})
        page.refresh()
        self.assertFalse(page.wechat_clear.isEnabled())
        self.assertTrue(page.other_clear.isEnabled())
        self.assertTrue(page.ai_clear.isEnabled())
        # 托盘预览没有增加计数时，微信组按钮仍应能清除预览。
        svc.wechat_preview = [{"kind": "wechat", "sender": "联系人", "source": "tray", "time": 1.0}]
        page.refresh()
        self.assertTrue(page.wechat_clear.isEnabled())
        page.wechat_clear.clicked.emit()
        self.assertEqual(svc.wechat_preview, [])
        self.assertEqual(svc.counts["workbuddy"], 4)

    def test_click_without_provider_is_harmless(self):
        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage, {})
        page._provider = None
        page._clear_ai()                                 # 不抛异常
        self.assertEqual(svc.counts, {})

    def test_header_button_does_not_grow_on_refresh(self):
        page, _ = _page(self.cfg, self.NotifyService, self.MessagesPage, {"workbuddy": 1})
        page.refresh()
        first = page.ai_button.minimumWidth()
        for _ in range(6):
            page.refresh()
        self.assertEqual(page.ai_button.minimumWidth(), first)

    def test_credits_survive_quitting_the_client(self):
        """关掉 WorkBuddy、重启 Aisland 后，额度仍来自上次读数而不是跳去别的客户端。"""
        from pathlib import Path

        from aisland.services import ai_usage

        state = Path(self.state)
        self.cfg.set("notify.ai_provider", "auto", save=False)
        with (mock.patch.object(ai_usage, "CACHE_PATH", state / "ai_usage_cache.json"),
              mock.patch.object(ai_usage, "DATA_DIR", state),
              mock.patch.object(ai_usage, "installed_ai", return_value=("codex", "workbuddy"))):
            ai_usage.save_usage_cache("workbuddy", [
                ai_usage.Metric("套餐基础积分", "500", None, "剩余积分"),
                ai_usage.Metric("奖励积分", "491.85", None, "剩余积分")])

            live = self.NotifyService()
            live.counts = {"workbuddy": 0}
            live.history = [{"kind": "workbuddy", "title": "任务已完成",
                             "body": "审查文件夹中的代码", "time": 1.0}]
            page = self.MessagesPage(types.SimpleNamespace(notify=live))
            page._request_usage = lambda: None
            page.refresh()
            self.assertEqual(page._provider, "workbuddy")
            self.assertEqual(self.cfg.get(ai_usage.PROVIDER_KEY), "workbuddy")

            # 客户端退出 + Aisland 重启：没有通知，只剩磁盘上的缓存。
            self.cfg.load()
            self.assertEqual(self.cfg.get(ai_usage.PROVIDER_KEY), "workbuddy")
            quiet = self.NotifyService()
            quiet.counts = {}
            quiet.history = []
            restarted = self.MessagesPage(types.SimpleNamespace(notify=quiet))
            restarted._request_usage = lambda: None
            restarted.refresh()
            self.assertEqual(restarted._provider, "workbuddy")
            self.assertEqual([t.current.display for t in restarted.quota_tiles], ["500", "491.85"])
            # 十分钟后再看：数值还在，只是标注成「上次更新」。
            later = ai_usage.display_metrics("workbuddy", None, None, now=time.time() + 600)
            self.assertEqual([m.display for m in later], ["500", "491.85"])
            self.assertIn("上次更新", later[0].detail)

    def test_quota_tile_does_not_shadow_paintdevice_metric(self):
        """QPaintDevice.metric 是虚函数：实例字段一旦叫 .metric，离屏绘制会直接抛 TypeError。"""
        from aisland.ui.pages.home import QuotaTile

        tile = QuotaTile("5 小时额度")
        tile.resize(130, 57)
        self.assertTrue(callable(tile.metric))     # 仍是 Qt 的方法，没被数据字段覆盖
        self.assertFalse(tile.grab().isNull())     # 能真正画出来

    def test_harness_auto_selection_clear_and_shared_blue_badge(self):
        from aisland.ui.compact import CompactView

        self.cfg.set("notify.ai_provider", "auto", save=False)
        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage,
                          {"deepseek_harness": 1, "codex": 2, "wechat": 3})
        svc.history = [{"kind": "deepseek_harness", "title": "任务完成", "time": 1.0}]
        page.refresh()
        self.assertEqual(page._provider, "deepseek_harness")
        self.assertEqual(page.ai_button.text, "自动·DeepSeek Harness ▾")
        self.assertEqual(page.ai_count.text(), "1 未读")
        self.assertFalse(page.quota_tiles[0].isHidden())
        self.assertEqual(page.quota_tiles[0].label, "充值余额")
        self.assertTrue(page.quota_tiles[1].isHidden())
        self.assertEqual(page.ai_hist.rows, svc.history)

        compact = CompactView()
        compact.set_badges(svc.counts)
        self.assertEqual([color.name() for color in compact.badges], ["#07c160", "#3177e8"])
        page._clear_ai()
        self.assertEqual(svc.counts, {"deepseek_harness": 0, "codex": 2, "wechat": 3})
        compact.set_badges(svc.counts)
        self.assertEqual([color.name() for color in compact.badges], ["#07c160", "#3177e8"])
        svc.clear("codex")
        compact.set_badges(svc.counts)
        self.assertEqual([color.name() for color in compact.badges], ["#07c160"])

        self.cfg.set("notify.ai_provider", "codex", save=False)
        page.refresh()
        self.assertTrue(all(not tile.isHidden() for tile in page.quota_tiles))

    def test_quota_requests_and_callbacks_stay_with_their_provider(self):
        from aisland.services.ai_usage import Metric

        service = self.NotifyService()
        page = self.MessagesPage(types.SimpleNamespace(notify=service))
        page.active = True
        reads = []

        def run_bg(fn, *args, on_done=None, on_error=None):
            # 读取任务只记录回调；缓存落盘任务同步执行以便断言（真实通道在后台）。
            if getattr(fn, "__name__", None) == "read_ai_usage":
                reads.append(on_done)
            else:
                fn(*args)
            return object()

        with (mock.patch("aisland.ui.pages.home.bg.run_bg", side_effect=run_bg),
              mock.patch("aisland.services.ai_usage.save_usage_cache", return_value=time.time()) as save):
            page._provider = "workbuddy"
            page._request_usage()
            page._request_usage()
            self.assertEqual(len(reads), 1)
            workbuddy_done = reads[0]

            page._provider = "deepseek_harness"
            page._request_usage()
            self.assertEqual(len(reads), 2)
            harness_done = reads[1]
            workbuddy_done([Metric("套餐基础积分", "500", None)])
            self.assertEqual(page.quota_tiles[0].label, "充值余额")

            harness_done([Metric("充值余额", "¥4.25", None), Metric("赠金余额", "$0.50", None)])
            self.assertFalse(page.quota_tiles[1].isHidden())
            self.assertEqual(page.quota_tiles[1].current.display, "$0.50")
            self.assertEqual([call.args[0] for call in save.call_args_list], ["workbuddy", "deepseek_harness"])
            page._request_usage()
            self.assertEqual(len(reads), 2)
            page._request_usage(force=True)
            self.assertEqual(len(reads), 3)

    def test_two_hidden_pages_share_one_quota_read(self):
        from aisland.services.ai_usage import Metric
        from aisland.ui.pages.home import QuotaReadings

        quota = QuotaReadings()
        quota.timer.stop()
        ctx = types.SimpleNamespace(notify=self.NotifyService(), quota_readings=quota)
        pages = [self.MessagesPage(ctx), self.MessagesPage(ctx)]
        for page in pages:
            page._provider = "workbuddy"
            self.addCleanup(page.deleteLater)
        with (mock.patch("aisland.ui.pages.home.bg.run_bg") as run,
              mock.patch("aisland.services.ai_usage.save_usage_cache", return_value=time.time())):
            quota.timer.timeout.emit()
            self.assertEqual(run.call_count, 1)
            run.call_args.kwargs["on_done"]([Metric("套餐基础积分", "500", None)])
            self.assertEqual([page.quota_tiles[0].current.display for page in pages], ["500", "500"])

    def test_auto_tracks_focus_on_both_displays_without_waiting_for_notifications(self):
        from PySide6 import QtTest
        from aisland.ui.pages.home import QuotaReadings

        self.cfg.set("notify.ai_provider", "auto", save=False)
        foreground = ["codex"]
        quota = QuotaReadings(foreground_provider=lambda: foreground[0])
        self.addCleanup(quota.timer.stop)
        service = self.NotifyService()
        service.history = [{"kind": "codex", "time": 1.0, "title": "旧通知"}]
        ctx = types.SimpleNamespace(notify=service, quota_readings=quota)
        pages = [self.MessagesPage(ctx), self.MessagesPage(ctx)]
        for page in pages:
            self.addCleanup(page.deleteLater)

        with mock.patch("aisland.ui.pages.home.bg.run_bg") as run:
            foreground[0] = "workbuddy"
            changed = QtTest.QSignalSpy(quota.provider_changed)
            self.assertTrue(changed.wait(900))
            self.assertEqual([page._provider for page in pages], ["workbuddy", "workbuddy"])
            self.assertEqual(sum(call.args[1] == "workbuddy" for call in run.call_args_list), 1)

            # Focusing Aisland/Office keeps the client whose balance was being viewed.
            foreground[0] = None
            pages[0].on_show()
            self.assertEqual([page._provider for page in pages], ["workbuddy", "workbuddy"])
            quota.observe_notification("deepseek_harness")
            self.assertEqual([page._provider for page in pages], ["deepseek_harness"] * 2)

            foreground[0] = "workbuddy"
            quota.observe_notification("codex")
            self.assertEqual([page._provider for page in pages], ["workbuddy"] * 2)

            pages[0]._select_provider("codex")
            foreground[0] = "deepseek_harness"
            quota.timer.timeout.emit()
            self.assertEqual([page._provider for page in pages], ["codex"] * 2)
            pages[1]._select_provider("auto")
            self.assertEqual([page._provider for page in pages], ["deepseek_harness"] * 2)

    def test_visible_refresh_is_prompt_but_hidden_reads_stay_throttled(self):
        from aisland.services.ai_usage import Metric
        from aisland.ui.pages.home import QuotaReadings

        quota = QuotaReadings()
        quota.timer.stop()
        ctx = types.SimpleNamespace(notify=self.NotifyService(), quota_readings=quota)
        pages = [self.MessagesPage(ctx), self.MessagesPage(ctx)]
        for page in pages:
            self.addCleanup(page.deleteLater)

        reads = []

        def run_bg(fn, *args, on_done=None, on_error=None):
            # 读取任务只记录回调；缓存落盘任务同步执行（真实通道在后台）。
            if getattr(fn, "__name__", None) == "read_ai_usage":
                reads.append(on_done)
            else:
                fn(*args)
            return object()

        with (mock.patch("aisland.ui.pages.home.bg.run_bg", side_effect=run_bg),
              mock.patch("aisland.ui.pages.home.time.monotonic", return_value=100) as clock,
              mock.patch("aisland.services.ai_usage.save_usage_cache", return_value=time.time())):
            pages[0]._request_usage()
            done = reads[-1]
            clock.return_value = 103
            quota.timer.timeout.emit()
            self.assertEqual(len(reads), 1)  # In flight across both displays.
            done([Metric("套餐基础积分", "500", None)])
            quota.timer.timeout.emit()
            self.assertEqual(len(reads), 1)  # Hidden pages retain 10-second pacing.

            pages[1].on_show()
            self.assertEqual(len(reads), 2)  # Opening starts a stale read immediately.
            reads[-1](None)
            self.assertEqual([page.quota_tiles[0].current.display for page in pages], ["500"] * 2)
            clock.return_value = 104.5
            quota.timer.timeout.emit()
            self.assertEqual(len(reads), 2)
            clock.return_value = 105.1
            quota.timer.timeout.emit()
            self.assertEqual(len(reads), 3)
            reads[-1](None)

            pages[1].on_hide()
            clock.return_value = 114
            quota.timer.timeout.emit()
            self.assertEqual(len(reads), 3)
            clock.return_value = 116
            quota.timer.timeout.emit()
            self.assertEqual(len(reads), 4)

    def test_dropped_background_job_does_not_block_future_reads(self):
        page = self.MessagesPage(types.SimpleNamespace(notify=self.NotifyService()))
        page._quota.timer.stop()
        self.addCleanup(page.deleteLater)
        with mock.patch("aisland.ui.pages.home.bg.run_bg", return_value=None) as run:
            page._request_usage()
            page._request_usage()
            self.assertEqual(run.call_count, 2)
            self.assertFalse(page._quota.busy)
            self.assertFalse(page._quota.checked)


    def test_other_notices_get_their_own_block_and_a_yellow_dot(self):
        from aisland.ui.theme import KIND_COLORS

        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage,
                          {"wechat": 2, "mail": 1, "dingtalk": 3, "workbuddy": 4})
        svc.history = [
            {"kind": "wechat", "time": 1.0, "name": "微信"},
            {"kind": "dingtalk", "time": 2.0, "name": "钉钉", "title": "审批提醒"},
            {"kind": "mail", "time": 3.0, "name": "邮件"},
            {"kind": "workbuddy", "time": 4.0, "title": "任务已完成"},
        ]
        page.refresh()
        self.assertEqual([r["kind"] for r in page.other_hist.rows], ["dingtalk", "mail"])
        self.assertEqual([r["kind"] for r in page.hist.rows], ["wechat"])
        self.assertEqual([r["kind"] for r in page.ai_hist.rows], ["workbuddy"])
        self.assertEqual(page.other_count.text(), "4 未读")
        self.assertFalse(page.other_dot.isHidden())
        self.assertTrue(page.other_clear.isEnabled())
        self.assertEqual(page.other_hist.dot_color, KIND_COLORS["notice"])

        page.other_clear.clicked.emit()
        self.assertEqual(svc.counts["wechat"], 2)         # 微信不动
        self.assertEqual(svc.counts["workbuddy"], 4)      # AI 不动
        self.assertEqual(svc.counts["dingtalk"], 0)
        self.assertEqual(svc.counts["mail"], 0)

    def test_no_other_notice_hides_the_yellow_dot(self):
        page, _ = _page(self.cfg, self.NotifyService, self.MessagesPage, {"wechat": 1})
        page.refresh()
        self.assertEqual(page.other_count.text(), "0 未读")
        self.assertTrue(page.other_dot.isHidden())
        self.assertFalse(page.other_clear.isEnabled())
        self.assertEqual(page.other_hist.rows, [])

    def test_clicking_any_notice_opens_its_own_application(self):
        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage,
                          {"wechat": 1, "dingtalk": 2, "workbuddy": 3})
        svc.history = [
            {"kind": "wechat", "time": 1.0, "name": "微信"},
            {"kind": "dingtalk", "time": 2.0, "name": "钉钉"},
            {"kind": "workbuddy", "time": 3.0, "title": "任务已完成"},
        ]
        page.refresh()
        for history, kind in ((page.hist, "wechat"),
                              (page.other_hist, "dingtalk"),
                              (page.ai_hist, "workbuddy")):
            with self.subTest(kind=kind), mock.patch.object(svc, "open") as opened:
                history.row_clicked.emit(history.rows[0])
                opened.assert_called_once_with(kind)

    def test_failed_notice_open_keeps_unread_and_panel_open(self):
        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage,
                          {"workbuddy": 3, "wechat": 2})
        page.island = types.SimpleNamespace(collapse=mock.Mock())
        svc.opener = mock.Mock(return_value="failed")
        page._open_notice({"kind": "workbuddy"})
        self.assertEqual(svc.counts, {"workbuddy": 3, "wechat": 2})
        page.island.collapse.assert_not_called()
        svc.opener.return_value = "focused"
        page._open_notice({"kind": "workbuddy"})
        self.assertEqual(svc.counts, {"workbuddy": 0, "wechat": 2})
        page.island.collapse.assert_called_once()

    def test_row_at_maps_clicks_and_ignores_empty_space(self):
        from PySide6 import QtCore

        page, svc = _page(self.cfg, self.NotifyService, self.MessagesPage, {})
        svc.history = [{"kind": "dingtalk", "time": 1.0},
                       {"kind": "mail", "time": 2.0}]
        page.refresh()
        history = page.other_hist
        self.assertEqual(history.row_at(0)["kind"], "dingtalk")
        self.assertEqual(history.row_at(33.5)["kind"], "mail")
        self.assertIsNone(history.row_at(66))
        self.assertIsNone(history.row_at(-1))

        event = mock.Mock()
        event.button.return_value = QtCore.Qt.MouseButton.LeftButton
        event.position.return_value = QtCore.QPointF(10, 34)
        with mock.patch.object(svc, "open") as opened:
            history.mouseReleaseEvent(event)
            opened.assert_called_once_with("mail")
        event.accept.assert_called_once()

    def test_bare_exe_name_is_not_launched_when_no_window_exists(self):
        from aisland.services import system

        windows = system.WindowService()
        with mock.patch.object(windows, "find_app_window", return_value=None), \
                mock.patch.object(system, "launch", return_value=True) as launched:
            self.assertEqual(windows.open_or_focus("dingtalk.exe"), "failed")
            launched.assert_not_called()
            self.assertEqual(windows.open_or_focus(r"C:\Apps\DingTalk.exe"), "launched")
            launched.assert_called_once()

    def test_other_notice_badge_in_collapsed_view_is_yellow(self):
        from aisland.ui.compact import CompactView
        from aisland.ui.theme import KIND_COLORS

        view = CompactView.__new__(CompactView)
        view.set_badges({"dingtalk": 2, "wecom": 1, "wechat": 1})
        self.assertEqual(view.badges, (KIND_COLORS["wechat"], KIND_COLORS["notice"]))
        view.set_badges({"workbuddy": 1, "mail": 1})
        self.assertEqual(view.badges, (KIND_COLORS["mail"], KIND_COLORS["ai"]))
        view.set_badges({})
        self.assertEqual(view.badges, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
