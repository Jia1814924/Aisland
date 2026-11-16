# -*- coding: utf-8 -*-
"""顶部导航：透明顶栏 + 图标组。

- 所有标签中心固定、等距，整组居中；
- 当前项随弹簧从中心向两侧展开成「图标 + 文字」胶囊，其余只留图标；
- 展开宽度受相邻间距约束，快速连点也不会互相重叠；
- 悬停显示页面名提示；长按空白处 -> drag_requested 拖动整个灵动岛。
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from .anim import Spring, clamp
from .theme import W, icon_font, theme, ui_font, theme_for, connect_theme
from .widgets import IconButton, painter

Qt = QtCore.Qt

ICON_W = 36.0
GAP = 14.0
TEXT_GAP = 6.0
LONG_PRESS_MS = 380


class NavBar(QtWidgets.QWidget):
    changed = QtCore.Signal(str)
    drag_requested = QtCore.Signal(QtCore.QPoint)

    def __init__(self, items: list[tuple[str, str, str]], parent=None, height: int = 36):
        super().__init__(parent)
        self.items = items                       # (key, glyph, label)
        self.index = 0
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self.font_ = ui_font(12.5, W.DemiBold)
        self.icon_font_ = icon_font(16)
        fm = QtGui.QFontMetrics(self.font_)
        icons = QtGui.QFontMetrics(self.icon_font_)
        self.label_w = [fm.horizontalAdvance(label) for _, _, label in items]
        self.glyph_w = [max(15, icons.horizontalAdvance(glyph)) for _, glyph, _ in items]
        # 展开后刚好容纳「图标 + 间距 + 文字 + 两侧内边距」，不额外撑宽
        self.full_w = [ICON_W + width + 22 for width in self.label_w]
        self.w = [Spring(ICON_W, 260, 0.56, 0.05) for _ in items]
        self.hover = [Spring(0, 400, 1.0, 0.01) for _ in items]
        self.badges: dict[str, int] = {}
        self.w[0].snap(self.full_w[0])           # 初始选中项即为展开态
        self._hover_i = -1
        self._press_pos: QtCore.QPoint | None = None
        self._lp = QtCore.QTimer(self, singleShot=True, interval=LONG_PRESS_MS, timeout=self._long_press)
        self._t = QtCore.QTimer(self, interval=16, timeout=self._tick)
        self._last = time.perf_counter()
        connect_theme(self, self.update)

        # 右侧按钮（不属于图标组）；保留接口供其它入口使用
        self.extra = QtWidgets.QHBoxLayout()
        self.right_buttons: list[IconButton] = []

    def add_right_button(self, glyph: str, tip: str, cb) -> IconButton:
        b = IconButton(glyph, 30, 13, tip, self)
        b.clicked.connect(cb)
        self.right_buttons.append(b)
        self._place_right()
        return b

    def resizeEvent(self, e):
        self._place_right()

    def _place_right(self):
        x = self.width()
        for b in reversed(self.right_buttons):
            x -= b.width()
            b.move(x, (self.height() - b.height()) // 2)
            x -= 2

    # --- 状态 ---
    def keys(self) -> list[str]:
        return [k for k, _, _ in self.items]

    def set_current(self, key: str, emit: bool = False):
        keys = self.keys()
        if key not in keys:
            return
        i = keys.index(key)
        if i == self.index:
            return
        self.index = i
        for j, s in enumerate(self.w):
            s.to(self.full_w[j] if j == i else ICON_W)
        self._kick()
        if emit:
            self.changed.emit(key)

    def current(self) -> str:
        return self.items[self.index][0]

    def set_badge(self, key: str, n: int):
        if self.badges.get(key, 0) != n:
            self.badges[key] = n
            self.update()

    def _kick(self):
        if not self._t.isActive():
            self._last = time.perf_counter()
            self._t.start()

    def _tick(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._last)
        self._last = now
        rest = True
        for s in self.w + self.hover:
            s.step(dt)
            rest = rest and s.rest
        self.update()
        if rest:
            self._t.stop()

    def _rects(self) -> list[QtCore.QRectF]:
        """中心固定等距、整组居中；展开只向两侧增宽，不推挤相邻项。"""
        n = len(self.w)
        right = sum(b.width() + 2 for b in self.right_buttons)
        start = 2.0
        end = max(start, self.width() - right - 6.0)
        step = min((end - start) / n, (max(self.full_w) + ICON_W) / 2 + GAP)
        first = (start + end - (n - 1) * step) / 2
        base = min(ICON_W, step)
        extra = [max(0.0, s.x - ICON_W) for s in self.w]
        adjacent = max((a + b for a, b in zip(extra, extra[1:])), default=0.0)
        # 快速连点可能让相邻两项同时展开，限制宽度以保持点击区域互不重叠
        budget = 2 * max(0.0, step - base - GAP / 2)
        scale = min(1.0, budget / adjacent) if adjacent else 1.0
        out = []
        for i, extension in enumerate(extra):
            center = first + i * step
            width = min(base + extension * scale, 2 * min(center - start, end - center))
            out.append(QtCore.QRectF(center - width / 2, 2, width, self.height() - 4))
        return out

    def _content_rects(self, i: int, r: QtCore.QRectF, progress: float):
        icon_w = min(r.width(), self.glyph_w[i])
        gap = TEXT_GAP * progress
        label_w = min(self.label_w[i] * progress, max(0.0, r.width() - icon_w - gap - GAP / 2))
        if not label_w:
            gap = 0.0
        x = r.center().x() - (icon_w + gap + label_w) / 2
        icon = QtCore.QRectF(x, r.y(), icon_w, r.height())
        label = QtCore.QRectF(icon.right() + gap, r.y(), label_w, r.height())
        return icon, label

    def _hit(self, pos: QtCore.QPointF) -> int:
        for i, r in enumerate(self._rects()):
            if r.contains(pos):
                return i
        return -1

    # --- 交互 ---
    def mouseMoveEvent(self, e):
        i = self._hit(e.position())
        if i != self._hover_i:
            if self._hover_i >= 0:
                self.hover[self._hover_i].to(0)
            if i >= 0:
                self.hover[i].to(1)
                QtWidgets.QToolTip.showText(
                    self.mapToGlobal(e.position().toPoint()) + QtCore.QPoint(0, 20),
                    self.items[i][2], self)
            else:
                QtWidgets.QToolTip.hideText()
            self._hover_i = i
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self._kick()
        if self._press_pos is not None and (e.position().toPoint() - self._press_pos).manhattanLength() > 4:
            # 在空白处按住并移动 -> 立即拖动灵动岛
            self._press_pos = None
            self._lp.stop()
            self.drag_requested.emit(QtGui.QCursor.pos())

    def leaveEvent(self, e):
        QtWidgets.QToolTip.hideText()
        if self._hover_i >= 0:
            self.hover[self._hover_i].to(0)
            self._hover_i = -1
            self._kick()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        i = self._hit(e.position())
        if i >= 0:
            self.set_current(self.items[i][0], emit=True)
            self._press_pos = None
        else:
            self._press_pos = e.position().toPoint()
            self._lp.start()

    def mouseReleaseEvent(self, e):
        self._lp.stop()
        self._press_pos = None

    def _long_press(self):
        if self._press_pos is not None:
            self._press_pos = None
            self.drag_requested.emit(QtGui.QCursor.pos())

    def wheelEvent(self, e):
        d = -1 if e.angleDelta().y() > 0 else 1
        self.set_current(self.items[int(clamp(self.index + d, 0, len(self.items) - 1))][0], emit=True)

    # --- 绘制 ---
    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        rects = self._rects()
        ar = rects[self.index]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.active_pill())
        p.drawRoundedRect(ar, ar.height() / 2, ar.height() / 2)
        for i, (key, glyph, label) in enumerate(self.items):
            r = rects[i]
            active = i == self.index
            if not active and self.hover[i].x > 0.01:
                c = palette.hover()
                c.setAlphaF(c.alphaF() * self.hover[i].x * 0.8)
                p.setBrush(c)
                p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            a = 1.0 if active else 0.55 + 0.4 * self.hover[i].x
            prog = clamp((self.w[i].x - ICON_W) / (self.full_w[i] - ICON_W), 0.0, 1.0)
            icon_rect, label_rect = self._content_rects(i, r, prog)
            p.setFont(self.icon_font_)
            p.setPen(palette.pill_text() if active else palette.text(a))
            p.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, glyph)
            if label_rect.width() > 0.5:
                p.save()
                p.setClipRect(label_rect)
                p.setFont(self.font_)
                p.setPen(palette.pill_text(prog) if active else palette.text(a * prog))
                p.drawText(QtCore.QRectF(label_rect.x(), r.y(), self.label_w[i] + 2, r.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
                p.restore()
            n = self.badges.get(key, 0)
            if n:
                p.setBrush(QtGui.QColor("#ff453a"))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QtCore.QPointF(icon_rect.center().x() + 8, r.y() + 8), 3.2, 3.2)
