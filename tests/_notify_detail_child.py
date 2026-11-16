"""通知详情解析跑在子进程里（notify 会导入 PySide6）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from PySide6 import QtWidgets                     # noqa: E402

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from aisland.services import notify               # noqa: E402

FILETIME = 133_900_000_000_000_000

TOAST_TWO_LINES = (
    '<toast><visual><binding template="ToastGeneric">'
    '<text>Codex</text><text>任务完成：重构配置加载</text>'
    '</binding></visual></toast>')

TOAST_THREE_LINES = (
    '<toast><visual><binding template="ToastGeneric">'
    '<text>Codex</text><text>任务完成：重构配置加载</text>'
    '<text>用时 3 分 12 秒</text>'
    '<text placement="attribution">Codex</text>'
    '</binding></visual></toast>')


class NotifyDetailChildTests(unittest.TestCase):
    def test_texts_keep_order_and_skip_attribution(self):
        self.assertEqual(notify.toast_texts(TOAST_THREE_LINES),
                         ["Codex", "任务完成：重构配置加载", "用时 3 分 12 秒"])

    def test_doctype_payload_is_rejected(self):
        bad = '<!DOCTYPE t [<!ENTITY x "y">]><toast><text>&x;</text></toast>'
        self.assertEqual(notify.toast_texts(bad), [])

    def test_ai_toast_keeps_body(self):
        apps = {"Codex": {"kind": "codex", "name": "Codex"}}
        with mock.patch.object(notify.cfg, "get",
                               side_effect=lambda k, d=None: apps if k == "notify.toast_apps" else d):
            rows = notify.NotifyService()._decode_rows(
                [(1, "Codex.App", FILETIME, "toast", TOAST_THREE_LINES)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "任务完成：重构配置加载")
        self.assertEqual(rows[0]["body"], "用时 3 分 12 秒")

    def test_single_line_ai_toast_still_shows_text(self):
        apps = {"Codex": {"kind": "codex", "name": "Codex"}}
        with mock.patch.object(notify.cfg, "get",
                               side_effect=lambda k, d=None: apps if k == "notify.toast_apps" else d):
            rows = notify.NotifyService()._decode_rows(
                [(2, "Codex.App", FILETIME, "toast", TOAST_TWO_LINES)])
        self.assertEqual(rows[0]["title"], "任务完成：重构配置加载")
        self.assertNotIn("body", rows[0])

    def test_ai_subtitle_combines_status_and_conversation(self):
        self.assertEqual(notify.ai_subtitle("任务中断，请重试", "审查文件夹中的代码"),
                         "任务中断，请重试 · 审查文件夹中的代码")

    def test_ai_subtitle_keeps_codex_order(self):
        self.assertEqual(notify.ai_subtitle("查看 Aisland 最新进度", "你的助手想问你一个问题"),
                         "查看 Aisland 最新进度 · 你的助手想问你一个问题")

    def test_ai_subtitle_degrades_without_duplication(self):
        self.assertEqual(notify.ai_subtitle("任务已完成", ""), "任务已完成")
        self.assertEqual(notify.ai_subtitle("", "任务已完成"), "任务已完成")
        self.assertEqual(notify.ai_subtitle("任务已完成", "任务已完成"), "任务已完成")
        self.assertEqual(notify.ai_subtitle("任务已完成", "任务已完成，用时 3 分"), "任务已完成，用时 3 分")
        self.assertEqual(notify.ai_subtitle(None, None), "")
        self.assertLessEqual(len(notify.ai_subtitle("长" * 60, "也长" * 40)), 80)

    def test_duplicate_ai_toast_collapses(self):
        svc = notify.NotifyService()
        first = {"kind": "workbuddy", "name": "WorkBuddy", "source": "toast",
                 "time": 1_000.0, "title": "任务中断，请重试", "body": "审查文件夹中的代码"}
        again = dict(first, time=1_003.0)
        svc._emit(dict(first))
        svc._emit(again)
        self.assertEqual(len(svc.history), 1)
        self.assertEqual(svc.history[0]["time"], 1_003.0)
        self.assertEqual(svc.counts["workbuddy"], 1)

    def test_different_ai_content_is_kept(self):
        svc = notify.NotifyService()
        svc._emit({"kind": "workbuddy", "name": "WorkBuddy", "source": "toast",
                   "time": 1_000.0, "title": "任务已完成", "body": "审查文件夹中的代码"})
        svc._emit({"kind": "workbuddy", "name": "WorkBuddy", "source": "toast",
                   "time": 1_005.0, "title": "任务已完成", "body": "分析10月简报政策文件"})
        self.assertEqual(len(svc.history), 2)
        self.assertEqual(svc.counts["workbuddy"], 2)

    def test_stale_ai_repeat_is_kept(self):
        svc = notify.NotifyService()
        svc._emit({"kind": "codex", "name": "Codex", "source": "toast",
                   "time": 1_000.0, "title": "查看进度", "body": "想问你一个问题"})
        svc._emit({"kind": "codex", "name": "Codex", "source": "toast",
                   "time": 1_000.0 + notify.AI_TOAST_DEDUPE_S + 1,
                   "title": "查看进度", "body": "想问你一个问题"})
        self.assertEqual(len(svc.history), 2)

    def test_harness_toast_enriches_its_flash_once(self):
        service = notify.NotifyService()
        stamp = notify.filetime_to_epoch(FILETIME)
        payload = TOAST_THREE_LINES.replace("Codex", "DeepSeek Harness")
        with mock.patch.object(notify, "exe_of_hwnd", return_value="deepseek harness.exe"), \
                mock.patch.object(notify.time, "time", return_value=stamp):
            service.on_flash(101)
            rows = service._decode_rows([
                (1, "com.deepseek.dsh", FILETIME, "toast", payload),
                (2, "com.deepseek.dshmodel", FILETIME, "toast", payload),
            ])
            self.assertEqual(len(rows), 1)
            service._emit(rows[0])
            service._emit(dict(rows[0], time=stamp + 1))
        self.assertEqual(service.counts, {"deepseek_harness": 1})
        self.assertEqual(len(service.history), 1)
        self.assertEqual(service.history[0]["name"], "DeepSeek Harness")
        self.assertEqual(service.history[0]["title"], "任务完成：重构配置加载")
        self.assertEqual(service.history[0]["body"], "用时 3 分 12 秒")


    def test_builtin_office_apps_are_mapped_without_config_changes(self):
        apps = notify.toast_apps()
        self.assertEqual(apps["DingTalk"], {"name": "钉钉", "kind": "dingtalk"})
        self.assertEqual(apps["Lark"]["kind"], "feishu")
        self.assertEqual(apps["WXWork"]["kind"], "wecom")
        self.assertEqual(notify.flash_processes()["wxwork.exe"]["name"], "企业微信")
        mapper = notify.NotifyService._map_handler
        self.assertEqual(mapper("DingTalk.DingTalk_x!App", apps)["kind"], "dingtalk")
        self.assertEqual(mapper("Lark.Feishu_x!App", apps)["kind"], "feishu")
        self.assertIsNone(mapper("LarkSuite_x!App", apps))    # 前缀边界不能误命中
        with mock.patch.object(notify.cfg, "get",
                               side_effect=lambda k, d=None: {"DingTalk": {"name": "自定义", "kind": "custom"}}
                               if k == "notify.toast_apps" else d):
            self.assertEqual(notify.toast_apps()["DingTalk"]["name"], "自定义")

    def test_open_resolves_each_kind_to_its_application(self):
        service = notify.NotifyService()
        service.opener = mock.Mock(return_value="focused")
        empty_user_config = lambda k, d=None: {} if k == "notify.flash_processes" else d
        with mock.patch.object(notify.cfg, "get", side_effect=empty_user_config):
            for kind, expected in (("dingtalk", "dingtalk.exe"),
                                   ("feishu", "lark.exe"),
                                   ("wecom", "wxwork.exe"),
                                   ("deepseek_harness", "deepseek harness.exe")):
                with self.subTest(kind=kind):
                    service.opener.reset_mock()
                    self.assertTrue(service.open(kind))
                    service.opener.assert_called_once_with(expected)

        full_path = r"C:\Apps\DingTalk.exe"
        with mock.patch.object(notify.cfg, "get",
                               side_effect=lambda k, d=None: full_path
                               if k == "notify.open.dingtalk" else ({} if k == "notify.flash_processes" else d)):
            service.opener.reset_mock()
            service.open("dingtalk")
            service.opener.assert_called_once_with(full_path)

    def test_more_specific_user_toast_rule_wins_over_builtin_prefix(self):
        rules = {"DingTalk.Company": {"name": "公司提醒", "kind": "company"}}
        payload = '<toast><text>审批提醒</text></toast>'
        with mock.patch.object(notify.cfg, "get",
                               side_effect=lambda k, d=None: rules if k == "notify.toast_apps" else d):
            rows = notify.NotifyService()._decode_rows([
                (1, "DingTalk.Company_x!App", FILETIME, "toast", payload),
                (2, "DingTalk.CompanyExtension_x!App", FILETIME, "toast", payload),
            ])
        self.assertEqual([row["kind"] for row in rows], ["company", "dingtalk"])
        self.assertEqual(rows[0]["name"], "公司提醒")

    def test_alternate_executable_is_focused_and_only_its_unread_is_cleared(self):
        from aisland.services import system

        windows = system.WindowService()
        service = notify.NotifyService()
        service.opener = windows.open_or_focus
        for kind, existing, expected in (("feishu", "feishu.exe", ["lark.exe", "feishu.exe"]),
                                         ("wecom", "wecom.exe", ["wxwork.exe", "wecom.exe"]),
                                         ("wechat", "wechat.exe", ["weixin.exe", "wechat.exe"])):
            with self.subTest(kind=kind):
                service.counts = {kind: 2, "codex": 3}
                with (mock.patch.object(windows, "find_app_window", side_effect=lambda exe: 123 if exe == existing else None) as find,
                      mock.patch.object(windows, "activate", return_value=True) as activate,
                      mock.patch.object(system, "launch") as launched,
                      mock.patch.object(system, "send_hotkey") as hotkey,
                      mock.patch.object(notify.cfg, "get", side_effect=lambda k, d=None: {
                          "weixin.exe": {"kind": "wechat"}, "wechat.exe": {"kind": "wechat"}}
                          if k == "notify.flash_processes" else d)):
                    self.assertTrue(service.open(kind))
                self.assertEqual([call.args[0] for call in find.call_args_list], expected)
                activate.assert_called_once_with(123)
                launched.assert_not_called()
                hotkey.assert_not_called()
                self.assertEqual(service.counts, {kind: 0, "codex": 3})

    def test_all_failed_candidates_preserve_unread(self):
        service = notify.NotifyService()
        service.opener = mock.Mock(return_value="failed")
        service.counts = {"feishu": 2}
        with mock.patch.object(notify.cfg, "get", side_effect=lambda k, d=None: d):
            self.assertFalse(service.open("feishu"))
        self.assertEqual(service.opener.call_args_list, [mock.call("lark.exe"), mock.call("feishu.exe")])
        self.assertEqual(service.counts["feishu"], 2)

    def test_full_path_launch_without_opener_requires_success(self):
        service = notify.NotifyService()
        path = r"C:\Apps\DingTalk.exe"
        with (mock.patch.object(notify.cfg, "get", side_effect=lambda k, d=None: path if k == "notify.open.dingtalk" else d),
              mock.patch.object(notify, "launch", return_value=False) as launch):
            service.counts = {"dingtalk": 2}
            self.assertFalse(service.open("dingtalk"))
            self.assertEqual(service.counts["dingtalk"], 2)
            launch.assert_called_once_with(path)
            launch.return_value = True
            self.assertTrue(service.open("dingtalk"))
            self.assertEqual(service.counts["dingtalk"], 0)

    def test_bare_exe_name_is_never_launched_without_an_opener(self):
        service = notify.NotifyService()          # 没有注入 opener
        with mock.patch.object(notify.cfg, "get",
                               side_effect=lambda k, d=None: {} if k == "notify.flash_processes" else d), \
                mock.patch.object(notify, "launch") as launched:
            service.open("dingtalk")
            launched.assert_not_called()

    def test_office_toast_keeps_title_and_body(self):
        service = notify.NotifyService()
        payload = ('<toast><visual><binding template="ToastGeneric">'
                   '<text>审批提醒</text><text>张三提交了申请</text>'
                   '</binding></visual></toast>')
        with mock.patch.object(notify.cfg, "get", side_effect=lambda k, d=None: {} if k == "notify.toast_apps" else d):
            rows = service._decode_rows([
                (1, "DingTalk.DingTalk_x!App", FILETIME, "toast", payload),
                (2, "UnrelatedApp_x!App", FILETIME, "toast", payload),
            ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "dingtalk")
        self.assertEqual(rows[0]["name"], "钉钉")
        self.assertEqual(rows[0]["title"], "审批提醒")
        self.assertEqual(rows[0]["body"], "张三提交了申请")


if __name__ == "__main__":
    unittest.main()
