"""Use real window resolution, timer and pages; stub only Windows boundaries."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtTest, QtWidgets
from aisland.core.config import cfg
from aisland.services import system
from aisland.services.ai_identity import identify_ai_application
from aisland.services.notify import NotifyService
from aisland.ui.pages.home import MessagesPage, QuotaReadings

app = QtWidgets.QApplication([])
CODEX = ("C:/Program Files/WindowsApps/"
         "OpenAI.Codex_26.1106.6628.0_x64__2p2nqsd0c76g0/app/ChatGPT.exe")


class AutomaticIdentityTests(unittest.TestCase):
    def test_window_identity_drives_both_displays_and_respects_fixed_mode(self):
        cfg.set("notify.ai_provider", "auto", save=False)
        windows = system.WindowService()
        paths = {11: CODEX, 12: "C:/Apps/WorkBuddy.exe",
                 13: "C:/Apps/DeepSeek Harness.exe", 14: "C:/Windows/explorer.exe"}
        foreground = [11]
        with (patch.object(system._u32, "GetForegroundWindow", side_effect=lambda: foreground[0]),
              patch.object(system, "_pid", side_effect=lambda hwnd: hwnd),
              patch.object(windows, "_exe_for", side_effect=lambda pid: paths.get(pid, "")),
              patch("aisland.ui.pages.home.bg.run_bg", return_value=object()) as run):
            quota = QuotaReadings(foreground_provider=lambda: identify_ai_application(
                windows.foreground_application_path(), cfg.get("notify.flash_processes")))
            self.addCleanup(quota.timer.stop)
            ctx = SimpleNamespace(notify=NotifyService(), quota_readings=quota)
            pages = [MessagesPage(ctx), MessagesPage(ctx)]
            for page in pages:
                self.addCleanup(page.deleteLater)
                page.refresh()
            self.assertEqual([page._provider for page in pages], ["codex"] * 2)
            for pid, provider in ((12, "workbuddy"), (13, "deepseek_harness"), (11, "codex")):
                foreground[0] = pid
                changed = QtTest.QSignalSpy(quota.provider_changed)
                self.assertTrue(changed.wait(1000))
                self.assertEqual([page._provider for page in pages], [provider] * 2)
            foreground[0] = 14
            quota.timer.timeout.emit()
            self.assertEqual(quota.automatic_provider, "codex")
            pages[0]._select_provider("workbuddy")
            foreground[0] = 13
            quota.timer.timeout.emit()
            self.assertEqual([page._provider for page in pages], ["workbuddy"] * 2)
            pages[1]._select_provider("auto")
            self.assertEqual([page._provider for page in pages], ["deepseek_harness"] * 2)
            self.assertTrue(any(call.args[1] == "codex" for call in run.call_args_list))

    def test_long_provider_keeps_unread_count_and_both_tiles_visible(self):
        from aisland.services.ai_usage import Metric

        cfg.set("notify.ai_provider", "auto", save=False)
        with patch("aisland.ui.pages.home.bg.run_bg", return_value=object()):
            quota = QuotaReadings(foreground_provider=lambda: "deepseek_harness")
            quota.timer.stop()
            quota.usage["deepseek_harness"] = [Metric("充值余额", "¥8.50", None),
                                               Metric("赠金余额", "¥1.00", None)]
            notices = NotifyService()
            notices.counts = {"deepseek_harness": 100}
            page = MessagesPage(SimpleNamespace(notify=notices, quota_readings=quota))
            self.addCleanup(page.deleteLater)
            self.addCleanup(page.close)
            for mode, guidance in (("auto", "自动跟随"), ("deepseek_harness", "已固定")):
                with self.subTest(mode=mode):
                    cfg.set("notify.ai_provider", mode, save=False)
                    page.refresh()
                    page.resize(512, 188)
                    page.show()
                    app.processEvents()
                    self.assertEqual(page.width(), 512)
                    self.assertLessEqual(page.minimumSizeHint().width(), 512)
                    self.assertEqual(page.ai_count.text(), "100 未读")
                    self.assertGreaterEqual(page.ai_count.width(), page.ai_count.sizeHint().width())
                    self.assertIn("DeepSeek Harness", page.ai_button.toolTip())
                    self.assertIn(guidance, page.ai_button.toolTip())
                    for child in (page.ai_button, page.ai_count, page.ai_clear, *page.quota_tiles):
                        self.assertTrue(child.isVisible())
                        self.assertGreater(child.width(), 0)
                        self.assertGreaterEqual(child.geometry().left(), 0)
                        self.assertLess(child.geometry().right(), child.parentWidget().width())
                    self.assertLess(page.quota_tiles[0].geometry().right(),
                                    page.quota_tiles[1].geometry().left())

    def test_hosted_window_resolves_its_real_application(self):
        windows = system.WindowService()
        with (patch.object(system._u32, "GetForegroundWindow", return_value=20),
              patch.object(system, "_pid", return_value=21),
              patch.object(windows, "_exe_for", side_effect=lambda pid:
                           "C:/Windows/ApplicationFrameHost.exe" if pid == 21 else CODEX),
              patch.object(windows, "_real_pid", return_value=22)):
            self.assertEqual(identify_ai_application(windows.foreground_application_path()), "codex")


if __name__ == "__main__":
    unittest.main()
