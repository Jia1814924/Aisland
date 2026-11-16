# -*- coding: utf-8 -*-
"""通用控件：全部自绘、跟随主题；背景透明，直接落在毛玻璃上。"""
from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .anim import Spring, clamp
from .theme import W, icon_font, theme, ui_font, theme_for, connect_theme

Qt = QtCore.Qt
AA = QtGui.QPainter.RenderHint.Antialiasing


def painter(w: QtWidgets.QWidget) -> QtGui.QPainter:
    p = QtGui.QPainter(w)
    p.setRenderHint(AA)
    p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    return p


class Themed:
    """混入：主题切换时自动重绘。"""
    def _hook_theme(self):
        connect_theme(self, self._on_theme)

    def _on_theme(self):
        try:
            self.update()
        except RuntimeError:
            pass


class Hoverable(QtWidgets.QWidget, Themed):
    clicked = QtCore.Signal()
    right_clicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = Spring(0, 420, 1.0, 0.01)
        self._press = Spring(0, 600, 0.7, 0.01)
        self._timer = QtCore.QTimer(self, interval=16, timeout=self._tick)
        self._last = time.perf_counter()
        self._hook_theme()

    def _kick(self):
        if not self._timer.isActive():
            self._last = time.perf_counter()
            self._timer.start()

    def _tick(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._last)
        self._last = now
        self._hover.step(dt)
        self._press.step(dt)
        self.update()
        if self._hover.rest and self._press.rest:
            self._timer.stop()

    def enterEvent(self, e):
        self._hover.to(1)
        self._kick()

    def leaveEvent(self, e):
        self._hover.to(0)
        self._press.to(0)
        self._kick()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press.to(1)
            self._kick()
        e.accept()

    def mouseReleaseEvent(self, e):
        self._press.to(0)
        self._kick()
        if self.rect().contains(e.position().toPoint()):
            if e.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
            elif e.button() == Qt.MouseButton.RightButton:
                self.right_clicked.emit()
        e.accept()

    def press_scale(self) -> float:
        return 1.0 - 0.06 * self._press.x


class IconButton(Hoverable):
    """圆形图标按钮：悬停时出现半透明白底。"""
    def __init__(self, glyph: str, size: int = 32, icon_px: int = 15, tip: str = "", parent=None,
                 filled: bool = False):
        super().__init__(parent)
        self.glyph = glyph
        self.icon_px = icon_px
        self.filled = filled
        self.active = False
        self.color: QtGui.QColor | None = None
        self.setFixedSize(size, size)
        if tip:
            self.setToolTip(tip)

    def set_glyph(self, g: str):
        if g != self.glyph:
            self.glyph = g
            self.update()

    def set_active(self, on: bool):
        if on != self.active:
            self.active = on
            self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        if self.active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.color or palette.fill())
            p.drawEllipse(r)
        elif self.filled or self._hover.x > 0.01:
            bg = QtGui.QColor(palette.card() if self.filled else palette.hover())
            if not self.filled:
                bg.setAlphaF(bg.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawEllipse(r)
        p.setFont(icon_font(self.icon_px))
        fg = QtGui.QColor(20, 20, 24) if self.active else (self.color or palette.text(0.9))
        if self.active and self.color is not None:
            fg = QtGui.QColor("white")
        if not self.isEnabled():
            p.setOpacity(0.32)          # 无可清内容时淡化，占位与尺寸不变
        p.setPen(fg)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.glyph)


class PillButton(Hoverable):
    """胶囊按钮：图标 + 文字。"""
    def __init__(self, text: str, glyph: str = "", parent=None, height: int = 30, primary: bool = False):
        super().__init__(parent)
        self.text = text
        self.glyph = glyph
        self.primary = primary
        self.setFixedHeight(height)
        self._fit()

    def _fit(self):
        fm = QtGui.QFontMetrics(ui_font(12.5, W.Medium))
        w = fm.horizontalAdvance(self.text) + 26 + (20 if self.glyph else 0)
        self.setMinimumWidth(min(w, self.maximumWidth()))

    def set_text(self, t: str):
        self.text = t
        self._fit()
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        bg = palette.fill() if self.primary else palette.card(1.0 + 0.6 * self._hover.x)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        fg = QtGui.QColor(20, 20, 24) if self.primary else palette.text(0.92)
        p.setPen(fg)
        x = r.x() + 13
        if self.glyph:
            p.setFont(icon_font(13))
            p.drawText(QtCore.QRectF(x, r.y(), 16, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph)
            x += 20
        p.setFont(ui_font(12.5, W.Medium))
        tw = r.right() - x - 11
        p.drawText(QtCore.QRectF(x, r.y(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   QtGui.QFontMetrics(p.font()).elidedText(self.text, Qt.TextElideMode.ElideRight, int(tw)))


class Label(QtWidgets.QWidget, Themed):
    """主题文字：level 1/2/3 = 主/次/弱。支持省略号与可选滚动(marquee)。"""
    clicked = QtCore.Signal()

    def __init__(self, text: str = "", px: float = 13, weight=W.Normal, level: int = 1, parent=None,
                 align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, num: bool = False,
                 color: QtGui.QColor | None = None, wrap: bool = False):
        super().__init__(parent)
        self._text = text
        self.font_ = ui_font(px, weight, num)
        self.level = level
        self.align = align
        self.color = color
        self.wrap = wrap
        fm = QtGui.QFontMetrics(self.font_)
        if not wrap:
            self.setFixedHeight(fm.height() + 2)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred if not wrap else QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed if not wrap else QtWidgets.QSizePolicy.Policy.Expanding)
        self._hook_theme()

    def sizeHint(self):
        fm = QtGui.QFontMetrics(self.font_)
        return QtCore.QSize(fm.horizontalAdvance(self._text) + 4, fm.height() + 2)

    def minimumSizeHint(self):
        return QtCore.QSize(0, QtGui.QFontMetrics(self.font_).height() + 2)

    def setText(self, t: str):
        t = t or ""
        if t != self._text:
            self._text = t
            self.updateGeometry()
            self.update()

    def text(self) -> str:
        return self._text

    def set_color(self, c: QtGui.QColor | None):
        self.color = c
        self.update()

    def mouseReleaseEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        p.setFont(self.font_)
        c = self.color or (palette.text() if self.level == 1 else palette.text2() if self.level == 2 else palette.text3())
        p.setPen(c)
        r = QtCore.QRectF(self.rect())
        if self.wrap:
            p.drawText(r, int(self.align) | int(Qt.TextFlag.TextWordWrap), self._text)
        else:
            fm = QtGui.QFontMetrics(self.font_)
            p.drawText(r, self.align, fm.elidedText(self._text, Qt.TextElideMode.ElideRight, int(r.width())))


class Slider(QtWidgets.QWidget, Themed):
    """控制中心滑块：按实际比例填充，图标和文字随底色切换。"""
    moved = QtCore.Signal(float)        # 拖动中（0..1）
    released = QtCore.Signal(float)
    icon_clicked = QtCore.Signal()

    def __init__(self, glyph: str, parent=None, height: int = 34, *, icon_button: bool = False):
        super().__init__(parent)
        self.glyph = glyph
        self.value = 0.0
        self.shown = Spring(0, 520, 1.0, 0.002)
        self.dragging = False
        self.icon_button = icon_button
        self._icon_press = False
        self.title = ""
        self.label = ""
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._t = QtCore.QTimer(self, interval=16, timeout=self._tick)
        self._last = time.perf_counter()
        self._hook_theme()

    def set_value(self, v: float, animate: bool = True):
        if not self.dragging:
            self._show_value(v, animate)

    def _show_value(self, v: float, animate: bool):
        self.value = clamp(v, 0, 1)
        if animate and self.value > 0 and self.shown.x != self.value:
            self.shown.to(self.value)
            self._kick()
        else:
            self.shown.snap(self.value)
            self._t.stop()
        self.update()

    def _kick(self):
        if not self._t.isActive():
            self._last = time.perf_counter()
            self._t.start()

    def _tick(self):
        now = time.perf_counter()
        self.shown.step(min(0.05, now - self._last))
        self._last = now
        if self.shown.rest:
            self.shown.snap(self.value)
            self._t.stop()
        self.update()

    def _drag_at(self, x: float):
        self._show_value(x / max(1, self.width() - 1), animate=False)
        self.moved.emit(self.value)

    def _over_icon(self, pos: QtCore.QPointF) -> bool:
        center = self.height() / 2
        return self.icon_button and abs(pos.x() - center) <= 13 and abs(pos.y() - center) <= 13

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            e.ignore()
            return
        self._icon_press = self._over_icon(e.position())
        if not self._icon_press:
            self.dragging = True
            self._drag_at(e.position().x())

    def mouseMoveEvent(self, e):
        if self._icon_press and not self._over_icon(e.position()):
            self._icon_press = False
            self.dragging = True
        if self.dragging:
            self._drag_at(e.position().x())

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            e.ignore()
            return
        if self._icon_press:
            self._icon_press = False
            if self._over_icon(e.position()):
                self.icon_clicked.emit()
            return
        if self.dragging:
            self.dragging = False
            self.released.emit(self.value)

    def wheelEvent(self, e):
        """滑块不响应滚轮（只允许点击与拖动），滚轮事件交给外层。"""
        e.ignore()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = r.height() / 2
        path = QtGui.QPainterPath()
        path.addRoundedRect(r, rad, rad)
        p.fillPath(path, palette.track())
        level = round(self.value * 100)
        fw = r.width() * clamp(self.shown.x, 0, 1) if level else 0
        icon_area = QtCore.QRectF(r.x(), r.y(), r.height(), r.height())
        value_area = r.adjusted(0, 0, -12, 0)
        value = self.label or str(level)
        icon = icon_font(15)
        label = ui_font(12, W.Medium)
        number = ui_font(12, W.Medium, num=True)
        title_area = r.adjusted(icon_area.width(), 0, -20 - QtGui.QFontMetrics(number).horizontalAdvance(value), 0)
        title = QtGui.QFontMetrics(label).elidedText(
            self.title, Qt.TextElideMode.ElideRight, max(0, int(title_area.width())))

        def draw_labels(fg, secondary):
            p.setFont(icon)
            p.setPen(fg)
            p.drawText(icon_area, Qt.AlignmentFlag.AlignCenter, self.glyph)
            if title:
                p.setFont(label)
                p.drawText(title_area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
            p.setFont(number)
            p.setPen(secondary)
            p.drawText(value_area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, value)

        p.save()
        p.setClipRect(r.adjusted(fw, 0, 0, 0))
        draw_labels(palette.text(0.92), palette.text2())
        p.restore()
        if fw > 0:
            p.setClipRect(QtCore.QRectF(r.x(), r.y(), fw, r.height()))
            p.fillPath(path, palette.fill())
            draw_labels(QtGui.QColor(30, 30, 34), QtGui.QColor(30, 30, 34, 200))


class Tile(Hoverable):
    """控制中心磁贴：圆形图标 + 标题 + 副标题；on 时图标圆为蓝色。"""
    def __init__(self, glyph: str, title: str, parent=None, height: int = 52):
        super().__init__(parent)
        self.glyph = glyph
        self.title = title
        self.sub = ""
        self.on = False
        self.enabled_ = True
        self.on_spring = Spring(0, 380, 0.85, 0.005)
        self.setFixedHeight(height)
        self.setMinimumWidth(120)

    def set_state(self, on: bool, sub: str = "", enabled: bool = True):
        changed = on != self.on or sub != self.sub or enabled != self.enabled_
        self.on, self.sub, self.enabled_ = on, sub, enabled
        self.on_spring.to(1.0 if on else 0.0)
        if changed:
            self._kick()

    def _tick(self):
        self.on_spring.step(0.016)
        super()._tick()
        if not self.on_spring.rest and not self._timer.isActive():
            self._timer.start()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        s = self.press_scale()
        p.translate(r.center())
        p.scale(s, s)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.card(1.0 + 0.5 * self._hover.x))
        p.drawRoundedRect(r, 16, 16)
        d = r.height() - 16
        cr = QtCore.QRectF(r.x() + 8, r.y() + 8, d, d)
        t = clamp(self.on_spring.x, 0.0, 1.0)
        off = palette.track()
        on = palette.on_color()
        col = QtGui.QColor(
            round(off.red() + (on.red() - off.red()) * t),
            round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t),
            round(off.alpha() + (255 - off.alpha()) * t))
        p.setBrush(col)
        p.drawEllipse(cr)
        p.setFont(icon_font(15))
        p.setPen(palette.on_fg() if t > 0.5 else palette.text(0.85))
        p.drawText(cr, Qt.AlignmentFlag.AlignCenter, self.glyph)
        tx = cr.right() + 9
        tw = r.right() - tx - 6
        a = 1.0 if self.enabled_ else 0.45
        p.setFont(ui_font(12.5, W.DemiBold))
        p.setPen(palette.text(a))
        fm = QtGui.QFontMetrics(p.font())
        if self.sub:
            p.drawText(QtCore.QRectF(tx, r.y() + 7, tw, r.height() / 2 - 5),
                       Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
                       fm.elidedText(self.title, Qt.TextElideMode.ElideRight, int(tw)))
            p.setFont(ui_font(11))
            fm2 = QtGui.QFontMetrics(p.font())
            p.setPen(palette.text2(a))
            p.drawText(QtCore.QRectF(tx, r.center().y() + 1, tw, r.height() / 2 - 6),
                       Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                       fm2.elidedText(self.sub, Qt.TextElideMode.ElideRight, int(tw)))
        else:
            p.drawText(QtCore.QRectF(tx, r.y(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm.elidedText(self.title, Qt.TextElideMode.ElideRight, int(tw)))


class Segmented(QtWidgets.QWidget, Themed):
    """分段选择：选中项半透明白色圆角底，底块弹性滑动。"""
    changed = QtCore.Signal(int)

    def __init__(self, items: list[str], parent=None, height: int = 28):
        super().__init__(parent)
        self.items = items
        self.index = 0
        self.pos_ = Spring(0, 520, 0.72, 0.002)
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        fm = QtGui.QFontMetrics(ui_font(12, W.Medium))
        self.setMinimumWidth(sum(fm.horizontalAdvance(t) + 24 for t in items))
        self._t = QtCore.QTimer(self, interval=16, timeout=self._tick)
        self._hook_theme()

    def set_index(self, i: int, emit: bool = False):
        i = int(clamp(i, 0, len(self.items) - 1))
        if i != self.index:
            self.index = i
            self.pos_.to(i)
            self._t.start()
            if emit:
                self.changed.emit(i)

    def _tick(self):
        self.pos_.step(0.016)
        self.update()
        if self.pos_.rest:
            self._t.stop()

    def mouseReleaseEvent(self, e):
        n = len(self.items)
        self.set_index(int(e.position().x() / max(1, self.width()) * n), emit=True)

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect())
        n = len(self.items)
        cw = r.width() / n
        sel = QtCore.QRectF(r.x() + cw * self.pos_.x + 2, r.y() + 2, cw - 4, r.height() - 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.active_pill())
        p.drawRoundedRect(sel, sel.height() / 2, sel.height() / 2)
        p.setFont(ui_font(12, W.Medium))
        for i, t in enumerate(self.items):
            p.setPen(palette.pill_text() if i == self.index else palette.text(0.6))
            p.drawText(QtCore.QRectF(r.x() + cw * i, r.y(), cw, r.height()), Qt.AlignmentFlag.AlignCenter, t)


class Card(QtWidgets.QFrame, Themed):
    """轻卡片：极淡的白色圆角底（无描边、无分割线）。"""
    def __init__(self, parent=None, radius: float = 18, alpha: float = 1.0):
        super().__init__(parent)
        self.radius = radius
        self.alpha = alpha
        self._hook_theme()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.card(self.alpha))
        p.drawRoundedRect(QtCore.QRectF(self.rect()), self.radius, self.radius)


class ScrollArea(QtWidgets.QScrollArea):
    """透明滚动区，细滚动条。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QScrollArea{background:transparent;border:none}")
        self.viewport().setStyleSheet("background:transparent")
        self.inner = QtWidgets.QWidget()
        self.inner.setAutoFillBackground(False)
        self.inner.setObjectName("scrollInner")
        self.inner.setStyleSheet("#scrollInner{background:transparent}")
        self.setWidget(self.inner)
        self.inner.setAutoFillBackground(False)   # setWidget 会强制打开
        connect_theme(self, self._style_bar)
        self._style_bar()

    def _style_bar(self):
        palette = theme_for(self)
        c = palette.qss_color(palette.text(0.25))
        self.verticalScrollBar().setStyleSheet(
            "QScrollBar:vertical{background:transparent;width:6px;margin:4px 0 4px 0}"
            f"QScrollBar::handle:vertical{{background:{c};border-radius:3px;min-height:24px}}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent}")


def hbox(*items, spacing: int = 8, margins=(0, 0, 0, 0)) -> QtWidgets.QHBoxLayout:
    lay = QtWidgets.QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        _add(lay, it)
    return lay


def vbox(*items, spacing: int = 8, margins=(0, 0, 0, 0)) -> QtWidgets.QVBoxLayout:
    lay = QtWidgets.QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        _add(lay, it)
    return lay


def _add(lay, it):
    if it is None:
        lay.addStretch(1)
    elif isinstance(it, int):
        lay.addSpacing(it)
    elif isinstance(it, QtWidgets.QLayout):
        lay.addLayout(it)
    elif isinstance(it, tuple):
        if isinstance(it[0], QtWidgets.QLayout):
            lay.addLayout(it[0], it[1])
        else:
            lay.addWidget(it[0], it[1])
    else:
        lay.addWidget(it)


def rounded_pixmap(img: QtGui.QImage | QtGui.QPixmap | None, size: int, radius: float,
                   dpr: float = 2.0) -> QtGui.QPixmap | None:
    if img is None:
        return None
    src = QtGui.QPixmap.fromImage(img) if isinstance(img, QtGui.QImage) else img
    if src.isNull():
        return None
    px = round(size * dpr)
    out = QtGui.QPixmap(px, px)
    out.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(out)
    p.setRenderHint(AA)
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0, 0, px, px), radius * dpr, radius * dpr)
    p.setClipPath(path)
    scaled = src.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    p.drawPixmap((px - scaled.width()) // 2, (px - scaled.height()) // 2, scaled)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


class _CenteredDialog(QtWidgets.QDialog):
    """独立置顶的对话框，弹出时落在屏幕正中。

    灵动岛本身是「无边框 + Tool + 置顶」的窗口，把对话框挂成它的子窗口时，位置会
    跟着岛走（常常一半在屏幕外），还容易被岛自己的内容盖住。这里改成不挂父窗口的
    顶层窗口，并显式置顶。
    """

    def __init__(self, title: str, screen: QtGui.QScreen | None = None):
        super().__init__(None)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(True)
        self._screen = screen
        self._placed = False

    def _center(self):
        target = (self._screen or QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
                  or QtGui.QGuiApplication.primaryScreen())
        if target is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(target.availableGeometry().center())
        self.move(frame.topLeft())

    def showEvent(self, event):
        super().showEvent(event)
        if not self._placed:
            self._placed = True
            self._center()
        self.raise_()
        self.activateWindow()


def centered_dialog(title: str, screen: QtGui.QScreen | None = None) -> QtWidgets.QDialog:
    """建一个默认居中、始终在最上层的模态框。"""
    return _CenteredDialog(title, screen)


class LineEdit(QtWidgets.QLineEdit):
    """需要键盘焦点的输入框：点击时通知灵动岛临时接受焦点。"""
    focus_wanted = QtCore.Signal()

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setProperty("wants_focus", True)
        self.setFixedHeight(32)
        self.setFont(ui_font(13))
        connect_theme(self, self._style)
        self._style()

    def _style(self):
        palette = theme_for(self)
        self.setStyleSheet(
            f"QLineEdit{{background:{palette.qss_color(palette.card())};border:none;border-radius:16px;"
            f"padding:0 14px;color:{palette.qss_color(palette.text())};"
            f"selection-background-color:rgba(79,140,255,120)}}")
        pal = self.palette()
        pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, palette.text3())
        self.setPalette(pal)

    def mousePressEvent(self, e):
        self.focus_wanted.emit()
        super().mousePressEvent(e)


class TextEdit(QtWidgets.QPlainTextEdit):
    focus_wanted = QtCore.Signal()
    submit = QtCore.Signal()

    def __init__(self, placeholder: str = "", parent=None, px: float = 14):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setProperty("wants_focus", True)
        self.setFont(ui_font(px))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        connect_theme(self, self._style)
        self._style()

    def _style(self):
        palette = theme_for(self)
        self.setStyleSheet(
            f"QPlainTextEdit{{background:{palette.qss_color(palette.card())};border:none;border-radius:16px;"
            f"padding:8px 10px;color:{palette.qss_color(palette.text())};"
            f"selection-background-color:rgba(79,140,255,120)}}"
            "QScrollBar:vertical{width:0}")
        pal = self.palette()
        pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, palette.text3())
        self.setPalette(pal)

    def mousePressEvent(self, e):
        self.focus_wanted.emit()
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)


def fmt_bytes(n: float, per_s: bool = False) -> str:
    units = ["B", "K", "M", "G", "T"]
    i = 0
    n = float(n or 0)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    s = f"{n:.0f}" if n >= 100 or i == 0 else f"{n:.1f}"
    return f"{s}{units[i]}" + ("/s" if per_s else "")


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def glyph_pixmap(glyph: str, px: int, color: QtGui.QColor) -> QtGui.QPixmap:
    pm = QtGui.QPixmap(px * 2, px * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setFont(icon_font(px * 2 * 0.8))
    p.setPen(color)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    pm.setDevicePixelRatio(2)
    return pm


_ = math  # 保留导入（部分页面使用）
