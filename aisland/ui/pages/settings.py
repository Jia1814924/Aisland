# -*- coding: utf-8 -*-
"""岛内设置页：把托盘菜单里的配置项搬进展开的灵动岛。

动作由 app 注入到 ctx.settings（统一为 action(island, *args)），
状态由 ctx.settings_state(island) 提供；托盘菜单与这里共用同一套逻辑。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...core.config import cfg
from ..theme import G, W, THEME_NAMES
from ..widgets import Card, Label, PillButton, ScrollArea, Segmented, hbox, vbox
from .base import Page

Qt = QtCore.Qt

THEME_SHORT = {"auto": "自动", "morandi": "莫兰迪", "light": "浅色", "dark": "深色",
               "rouge": "胭脂", "sky": "晴空"}
EXPAND_MODES = ("hover", "always", "never")


class SwitchRow(QtWidgets.QWidget):
    """一行「说明 + 开/关」按钮。"""
    toggled = QtCore.Signal(bool)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.on = False
        self.label = Label(text, 12.5, level=1)
        self.btn = PillButton("关", height=26)
        self.btn.setFixedWidth(60)
        self.btn.clicked.connect(self._click)
        self.setLayout(hbox(self.label, None, self.btn, spacing=8))

    def _click(self):
        self.set_on(not self.on)
        self.toggled.emit(self.on)

    def set_on(self, on: bool):
        self.on = bool(on)
        self.btn.text = "开" if self.on else "关"
        self.btn.primary = self.on
        self.btn.update()


class SettingsPage(Page):
    key, glyph, title = "set", G.GEAR, "设置"

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self._rows: dict[str, SwitchRow] = {}
        scroll = ScrollArea()
        inner = scroll.inner
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setSpacing(8)
        lay.setContentsMargins(2, 2, 2, 2)

        def group(title: str, rows: list):
            card = Card(radius=16)
            box = QtWidgets.QVBoxLayout(card)
            box.setSpacing(2)
            box.setContentsMargins(12, 8, 12, 8)
            box.addWidget(Label(title, 11.5, W.DemiBold, level=2))
            for row in rows:
                if isinstance(row, QtWidgets.QLayout):
                    box.addLayout(row)
                else:
                    box.addWidget(row)
            lay.addWidget(card)

        def switch(name: str, text: str) -> SwitchRow:
            row = SwitchRow(text)
            self._rows[name] = row
            row.toggled.connect(lambda on, n=name: (self._act(n, on), self.refresh()))
            return row

        def button(text: str, glyph: str, action: str, *args, primary: bool = False) -> PillButton:
            b = PillButton(text, glyph, height=28, primary=primary)
            b.clicked.connect(lambda a=action, av=args: self._act(a, *av))
            return b

        # --- 常规 ---
        group("常规", [switch("wechat_preview", "自动预览微信消息"),
                       switch("autostart", "开机自启")])

        # --- 外观 ---
        modes = [mode for mode, _ in THEME_NAMES]
        self.theme_seg = Segmented([THEME_SHORT.get(m, label) for m, label in THEME_NAMES], height=26)
        self.theme_seg.changed.connect(lambda i: self._act("theme", modes[i]))
        self.expand_seg = Segmented(["悬停", "常驻", "长关"], height=26)
        self.expand_seg.changed.connect(lambda i: self._act("expand_mode", EXPAND_MODES[i]))
        self._segments = {"theme": (self.theme_seg, modes),
                          "expand_mode": (self.expand_seg, list(EXPAND_MODES))}
        group("外观", [hbox(Label("主题", 12, level=2), self.theme_seg, spacing=8),
                       hbox(Label("展开方式", 12, level=2), self.expand_seg, spacing=8),
                       switch("passthrough", "可穿透"),
                       switch("capture", "参与截图")])

        # --- 本地歌词 ---
        self.btn_lyrics = button("查看当前", G.EYE, "show_lyrics")
        group("本地歌词", [
            hbox(self.btn_lyrics,
                 button("添加目录", G.ADD, "add_music"),
                 button("重新读取", G.REFRESH, "refresh_lyrics"), spacing=6),
            switch("online_lyrics", "允许联网补充歌词")])

        # --- 数据与维护 ---
        group("数据与维护", [
            hbox(button("编辑配置", G.OPEN, "edit_config"),
                 button("重新加载", G.REFRESH, "reload"),
                 button("数据目录", G.APPS, "open_data"), spacing=6),
            hbox(button("退出 Aisland", G.POWER, "quit", primary=True), None, spacing=6)])

        lay.addStretch(1)
        self.setLayout(vbox(scroll, spacing=0, margins=(0, 0, 0, 0)))
        cfg.changed.connect(self._settings_changed)

    def _settings_changed(self):
        if self.active:
            self.refresh()

    def _act(self, name: str, *args):
        actions = getattr(self.ctx, "settings", None)
        if actions is None:
            return
        fn = getattr(actions, name, None)
        if fn is not None:
            fn(self.island, *args)

    def on_show(self):
        super().on_show()
        self.refresh()

    def refresh(self):
        if self.ctx is None or self.island is None:
            return
        getter = getattr(self.ctx, "settings_state", None)
        state = getter(self.island) if getter is not None else {}
        for name, row in self._rows.items():
            row.set_on(bool(state.get(name)))
        for name, (segment, values) in self._segments.items():
            current = state.get(name)
            if current in values:
                segment.set_index(values.index(current))
        self.btn_lyrics.setEnabled(bool(state.get("has_lyrics")))
