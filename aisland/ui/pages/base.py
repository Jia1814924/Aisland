# -*- coding: utf-8 -*-
from __future__ import annotations

from PySide6 import QtWidgets


class Page(QtWidgets.QWidget):
    key = "page"
    glyph = ""
    title = "页面"

    def __init__(self, ctx=None):
        super().__init__()
        self.ctx = ctx
        self.island = None
        self.active = False

    def on_show(self):
        self.active = True

    def on_hide(self):
        self.active = False

    def want_focus(self):
        if self.island is not None:
            self.island.request_focus()
