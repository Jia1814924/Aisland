# -*- coding: utf-8 -*-
"""「翻译连接」对话框的离屏验证（由 tests/test_dialog.py 以子进程调用）。

主测试进程不能加载 Qt（test_maintenance 有断言），所以这里单独起进程跑。
"""
from __future__ import annotations

import os
import sys
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtWidgets


class ConnectionDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from aisland.core import translation_settings as ts
        from aisland.ui.pages.tools import TranslatePage
        from aisland.ui.widgets import centered_dialog

        cls.ts, cls.Page = ts, TranslatePage
        cls.centered_dialog = staticmethod(centered_dialog)

    # ---------------- 通用对话框：独立、置顶、居中 ----------------
    def test_dialog_is_top_level_and_centered(self):
        dialog = self.centered_dialog("测试")
        dialog.resize(320, 180)
        dialog.show()
        self.app.processEvents()
        try:
            self.assertIsNone(dialog.parentWidget(), "对话框不该挂成灵动岛的子窗口")
            self.assertTrue(dialog.windowFlags() & QtCore.Qt.WindowType.WindowStaysOnTopHint)
            screen = self.app.screenAt(QtCore.QPoint(0, 0)) or self.app.primaryScreen()
            area, frame = screen.availableGeometry(), dialog.frameGeometry()
            self.assertLessEqual(abs(frame.center().x() - area.center().x()), 2)
            self.assertLessEqual(abs(frame.center().y() - area.center().y()), 2)
            self.assertTrue(area.contains(frame), f"对话框越界：{frame} / {area}")
        finally:
            dialog.hide()
            dialog.deleteLater()

    # ---------------- 翻译连接：厂商表与字段显隐 ----------------
    def _page(self, connection=None):
        class FakeTranslate:
            def connection(inner):
                return connection or self.ts.Connection("sk-test-0123456789", "https://api.deepseek.com",
                                                        "deepseek-flash")

        page = self.Page.__new__(self.Page)
        page.ctx = types.SimpleNamespace(translate=FakeTranslate())
        page.island = types.SimpleNamespace(scr=self.app.primaryScreen(), locks=set(),
                                            activity=lambda *a, **k: None)
        return page

    def _inspect(self, page, provider_index=None, check=None):
        state: dict = {}
        original = QtWidgets.QDialog.exec

        def fake_exec(dialog):
            dialog.show()
            self.app.processEvents()
            combo = dialog.findChildren(QtWidgets.QComboBox)[0]
            if provider_index is not None:
                combo.setCurrentIndex(provider_index)
                self.app.processEvents()
            fields = {w.accessibleName(): w for w in dialog.findChildren(QtWidgets.QLineEdit)}
            box = dialog.findChildren(QtWidgets.QDialogButtonBox)[0]
            probe = next((b for b in box.buttons() if b.text() == "测速"), None)
            if check is not None:
                check(fields, box.button(QtWidgets.QDialogButtonBox.StandardButton.Save), probe)
            state.update(
                names=[combo.itemText(i) for i in range(combo.count())],
                url_visible=fields["接入地址"].isVisible(),
                model=fields["模型"].text(),
                save_enabled=box.button(QtWidgets.QDialogButtonBox.StandardButton.Save).isEnabled(),
                probe_enabled=bool(probe and probe.isEnabled()),
                probe_text=probe.text() if probe else "",
                geometry=dialog.frameGeometry().size(),
            )
            dialog.hide()
            return QtWidgets.QDialog.DialogCode.Rejected

        QtWidgets.QDialog.exec = fake_exec  # type: ignore[method-assign]
        try:
            self.assertFalse(page.edit_connection())
        finally:
            QtWidgets.QDialog.exec = original  # type: ignore[method-assign]
        return state

    def test_vendor_list_and_known_provider_layout(self):
        state = self._inspect(self._page())
        names = state["names"]
        self.assertGreaterEqual(len(names), 16)
        for vendor in ("Kimi", "MiniMax", "Claude", "ChatGPT", "Gemini", "Grok",
                       "豆包", "混元", "文心", "自定义"):
            self.assertTrue(any(vendor in name for name in names), vendor)
        self.assertFalse(state["url_visible"], "内置厂商不需要手填地址")
        self.assertEqual(state["model"], "deepseek-flash")
        self.assertTrue(state["save_enabled"], "已配置 Key 时允许只改模型")

    def test_custom_provider_reveals_endpoint_field(self):
        state = self._inspect(self._page(), provider_index=99)
        self.assertTrue(state["url_visible"])
        self.assertEqual(state["model"], "")
        self.assertFalse(state["save_enabled"], "地址和模型都没填时不能保存")
        self.assertFalse(state["probe_enabled"], "没填地址和模型时也不该能测速")

    def test_speed_probe_button_is_available(self):
        state = self._inspect(self._page())
        self.assertEqual(state["probe_text"], "测速")
        self.assertTrue(state["probe_enabled"], "已配置 Key 时应该能直接测速")

    def test_custom_address_change_blocks_saving_and_probing_with_old_key(self):
        conn = self.ts.Connection("sk-old", "https://old-gateway.example/v1", "test-model")

        def check(fields, save, probe):
            self.assertTrue(save.isEnabled())
            self.assertTrue(probe.isEnabled())
            fields["接入地址"].setText("https://new-gateway.example/v1")
            self.assertFalse(save.isEnabled())
            self.assertFalse(probe.isEnabled())
            fields["API Key"].setText("sk-new")
            self.assertTrue(save.isEnabled())
            self.assertTrue(probe.isEnabled())
            fields["接入地址"].setText("https://new-gateway.example/v2")
            self.assertEqual(fields["API Key"].text(), "")
            self.assertFalse(save.isEnabled())
            self.assertFalse(probe.isEnabled())

        self._inspect(self._page(conn), check=check)


if __name__ == "__main__":
    unittest.main()
