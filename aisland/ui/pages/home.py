# -*- coding: utf-8 -*-
"""主页（正在播放与快捷操作）和消息页（微信和 AI 通知）。"""
from __future__ import annotations

import logging
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ...core import bg
from ...core.config import cfg, expand
from ...services import ai_usage
from ...services.notify import HISTORY_MAX
from ..anim import Spring, clamp
from ..theme import KIND_COLORS, G, W, icon_font, theme, ui_font, theme_for, connect_theme
from ..widgets import (Card, Hoverable, IconButton, Label, PillButton, ScrollArea, Themed, fmt_time, hbox,
                       painter, rounded_pixmap, vbox)
from .base import Page

Qt = QtCore.Qt

# Recover a lost/late callback without accepting results from a superseded read.
USAGE_BUSY_TIMEOUT = 45.0
_log = logging.getLogger(__name__)


# ======================================================================== 播放卡片
class Progress(QtWidgets.QWidget, Themed):
    seek = QtCore.Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.pos_ = 0.0
        self.dur = 0.0
        self.stamp = 0.0
        self.playing = False
        self.hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hook_theme()

    def set_state(self, m: dict):
        self.pos_ = float(m.get("position") or 0)
        self.dur = float(m.get("duration") or 0)
        self.stamp = float(m.get("stamp") or time.monotonic())
        self.playing = bool(m.get("playing"))
        self.update()

    def current(self) -> float:
        p = self.pos_ + ((time.monotonic() - self.stamp) if self.playing else 0)
        return clamp(p, 0, self.dur) if self.dur else p

    def mouseReleaseEvent(self, e):
        if self.dur > 0:
            self.seek.emit(clamp(e.position().x() / max(1, self.width()), 0, 1) * self.dur)

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect())
        y = 6
        th = 4 + 3 * self.hover
        tr = QtCore.QRectF(0, y - th / 2, r.width(), th)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.track())
        p.drawRoundedRect(tr, th / 2, th / 2)
        cur = self.current()
        if self.dur > 0:
            p.setBrush(palette.text(0.85 + 0.15 * self.hover))
            p.drawRoundedRect(QtCore.QRectF(0, y - th / 2, max(th, r.width() * cur / self.dur), th), th / 2, th / 2)
        p.setFont(ui_font(10.5, num=True))
        p.setPen(palette.text3())
        p.drawText(QtCore.QRectF(0, 10, 60, 12), Qt.AlignmentFlag.AlignLeft, fmt_time(cur))
        if self.dur > 0:
            p.drawText(QtCore.QRectF(r.width() - 60, 10, 60, 12), Qt.AlignmentFlag.AlignRight, fmt_time(self.dur))


class CoverView(QtWidgets.QWidget):
    """封面：悬停时轻微放大上浮，并在身后透出封面主色的柔光。"""
    def __init__(self, size: int, parent=None):
        super().__init__(parent)
        self.size_ = size
        self.pm: QtGui.QPixmap | None = None
        self.glow = QtGui.QColor(255, 255, 255, 0)
        self.h = 0.0
        self.setFixedSize(size + 12, size + 12)

    def set_pixmap(self, pm, glow: QtGui.QColor | None):
        self.pm = pm
        self.glow = glow or QtGui.QColor(255, 255, 255, 0)
        self.update()

    def paintEvent(self, e):
        p = painter(self)
        c = QtCore.QRectF(self.rect()).center()
        s = 1.0 + 0.07 * self.h
        r = QtCore.QRectF(0, 0, self.size_ * s, self.size_ * s)
        r.moveCenter(c - QtCore.QPointF(0, 2.5 * self.h))
        if self.h > 0.01 and self.glow.alpha():
            g = QtGui.QRadialGradient(r.center() + QtCore.QPointF(0, 6), r.width() * 0.75)
            col = QtGui.QColor(self.glow)
            col.setAlpha(round(150 * self.h))
            g.setColorAt(0, col)
            col2 = QtGui.QColor(self.glow)
            col2.setAlpha(0)
            g.setColorAt(1, col2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(r.adjusted(-8, -4, 8, 14))
        if self.pm is not None:
            p.drawPixmap(r.toRect(), self.pm)


class CoverTitle(QtWidgets.QWidget):
    """封面和标题分行居中，标题在播放卡片内省略。"""
    COVER = 48

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pm: QtGui.QPixmap | None = None
        self.glow = QtGui.QColor(255, 255, 255, 0)
        self.h = 0.0
        self.title = "未在播放"
        self.sub = ""
        self.setFixedHeight(self.COVER + 30)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        connect_theme(self, self.update)

    def set_pixmap(self, pm, glow):
        self.pm = pm
        self.glow = glow or QtGui.QColor(255, 255, 255, 0)
        self.update()

    def set_text(self, title, sub):
        self.title, self.sub = title, sub
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        W_ = self.width()
        s = 1.0 + 0.07 * self.h
        size = self.COVER * s
        cr = QtCore.QRectF(0, 0, size, size)
        cr.moveCenter(QtCore.QPointF(W_ / 2, 4 + self.COVER / 2 - 2.5 * self.h))
        if self.h > 0.01 and self.glow.alpha():
            g = QtGui.QRadialGradient(cr.center() + QtCore.QPointF(0, 6), cr.width() * 0.8)
            c1 = QtGui.QColor(self.glow)
            c1.setAlpha(round(150 * self.h))
            c2 = QtGui.QColor(self.glow)
            c2.setAlpha(0)
            g.setColorAt(0, c1)
            g.setColorAt(1, c2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(cr.adjusted(-10, -6, 10, 14))
        if self.pm is not None:
            p.drawPixmap(cr.toRect(), self.pm)
        f = ui_font(13.5, W.DemiBold)
        fm = QtGui.QFontMetrics(f)
        text = self.title + (f"  ·  {self.sub}" if self.sub else "")
        tr = QtCore.QRectF(4, self.COVER + 9, W_ - 8, self.height() - self.COVER - 9)
        p.setFont(f)
        p.setPen(palette.text())
        p.drawText(tr, Qt.AlignmentFlag.AlignCenter, fm.elidedText(text, Qt.TextElideMode.ElideRight, int(tr.width())))


class MediaCard(Card):
    def __init__(self, ctx, parent=None):
        super().__init__(parent, 20)
        self.ctx = ctx
        self.cover_px: QtGui.QPixmap | None = None
        self._cover_source = None
        self.cover = CoverTitle(self)
        self._hov = Spring(0, 260, 0.6, 0.004)
        self._ht = QtCore.QTimer(self, interval=16, timeout=self._hover_tick)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.title = Label("未在播放", 15, W.DemiBold)
        self.artist = Label("", 12.5, level=2)
        self.app = Label("", 11, level=3)
        self.lyric = Label("播放音乐时读取歌词", 11.5, level=2,
                           align=Qt.AlignmentFlag.AlignCenter)
        self._lyrics = None
        self._lyrics_status = "not_found"
        self._media_state = {}
        self.prog = Progress()
        self.prev = IconButton(G.PREV, 34, 15, "上一首")
        self.play = IconButton(G.PLAY, 40, 18, "播放 / 暂停", filled=True)
        self.next = IconButton(G.NEXT, 34, 15, "下一首")
        self.open_btn = PillButton("打开网易云", G.MUSIC, height=28)
        self.prev.clicked.connect(lambda: ctx.media.prev())
        self.next.clicked.connect(lambda: ctx.media.next())
        self.play.clicked.connect(lambda: ctx.media.play_pause())
        self.prog.seek.connect(lambda s: ctx.media.seek(s))
        self.open_btn.clicked.connect(self._open_player)
        self.open_btn.hide()
        self.title.hide()
        self.artist.hide()
        self.app.hide()
        ctrls = hbox(None, self.prev, self.play, self.next, None, spacing=6)   # 播放键正对封面
        self.setLayout(vbox(self.cover, self.prog, self.lyric, ctrls, None, spacing=0, margins=(10, 6, 10, 6)))
        self._tick = QtCore.QTimer(self, interval=250, timeout=self._playback_tick)
        self._set_cover(None)
        connect_theme(self, lambda: self._set_cover(None) if self._cover_source is None else None)

    # --- 悬停动画 ---
    def enterEvent(self, e):
        self._hov.to(1.0, 260, 0.6)
        self._ht.start()

    def leaveEvent(self, e):
        self._hov.to(0.0, 220, 0.8)
        self._ht.start()

    def _hover_tick(self):
        self._hov.step(0.016)
        h = max(0.0, self._hov.x)
        self.cover.h = h
        self.cover.update()
        self.prog.hover = h
        self.prog.update()
        self.update()
        if self._hov.rest:
            self._ht.stop()

    def paintEvent(self, e):
        """不画独立背景，直接嵌在灵动岛玻璃上（悬停时封面 / 进度条有动画）。"""
        return

    def _open_player(self):
        items = cfg.get("launcher") or []
        it = next((i for i in items if "cloudmusic" in str(i.get("path", "")).lower()), items[0] if items else None)
        if it:
            self.ctx.windows.open_or_focus(expand(it["path"]), it.get("exe"))

    def _set_cover(self, img):
        palette = theme_for(self)
        self._cover_source = img
        size = self.cover.COVER
        px = rounded_pixmap(img, size, 11)
        self.cover_px = px
        glow = None
        if img is not None:
            q = img if isinstance(img, QtGui.QImage) else img.toImage()
            glow = q.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)
            h, s_, v, _ = glow.getHsv()
            glow = QtGui.QColor.fromHsv(max(0, h), min(255, int(s_ * 1.4) + 40), max(170, v))
        if px is None:
            pm = QtGui.QPixmap(size * 2, size * 2)
            pm.fill(Qt.GlobalColor.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.card(1.6))
            p.drawRoundedRect(QtCore.QRectF(pm.rect()), 22, 22)
            p.setFont(icon_font(36))
            p.setPen(palette.text(0.6))
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, G.MUSIC)
            p.end()
            pm.setDevicePixelRatio(2)
            px = pm
        self.cover.set_pixmap(px, glow)

    def set_cover(self, img):
        self._set_cover(img)

    def set_media(self, m: dict):
        self._media_state = m
        has = bool(m.get("has_session") and m.get("title"))
        self.cover.set_text(m.get("title") or "未在播放", m.get("artist") or "")
        self.play.set_glyph(G.PAUSE if m.get("playing") else G.PLAY)
        self.prog.set_state(m)
        self.prog.setVisible(True)
        self.prog.setEnabled(has and (m.get("duration") or 0) > 0)
        # 按钮始终可用：没有媒体会话（播放器没接入系统媒体控件）时会改发键盘媒体键

        if m.get("playing"):
            self._tick.start()
        else:
            self._tick.stop()
        self._refresh_lyric()

    def set_lyrics(self, lyrics, status="not_found"):
        self._lyrics = lyrics
        self._lyrics_status = status
        self._refresh_lyric()

    def _playback_tick(self):
        self.prog.update()
        self._refresh_lyric()

    def _refresh_lyric(self):
        if not self._media_state.get("title"):
            self.lyric.setText("播放音乐时读取歌词")
            self.lyric.setToolTip("")
            return
        lyr = self._lyrics
        if lyr:
            if getattr(lyr, "synced", True):
                line = lyr.at(self.prog.current())[1] or "等待歌词开始"
                source = "歌曲内嵌歌词" if getattr(lyr, "source", "") == "embedded" else "在线歌词"
                self.lyric.setText(line)
                self.lyric.setToolTip(source + "\n" + line)
            else:
                plain = getattr(lyr, "plain_text", "")
                line = next((x.strip() for x in plain.splitlines() if x.strip()), "已读取歌词")
                self.lyric.setText(line)
                self.lyric.setToolTip("歌词无时间轴，可右键查看全文\n" + plain[:2000])
            return
        text = {"loading": "正在读取歌词…", "disabled": "歌词已关闭",
                "offline": "本地未找到歌词", "not_found": "未找到歌词，可右键添加音乐文件夹"}.get(
                    self._lyrics_status, "未找到歌词")
        self.lyric.setText(text)
        self.lyric.setToolTip(text)


# ======================================================================== 快捷操作 / 启动器
class Action(Hoverable):
    def __init__(self, glyph: str, title: str, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self.title = title
        self.on = False
        self.setFixedHeight(58)

    def set_on(self, on: bool, glyph: str | None = None, title: str | None = None):
        self.on = on
        if glyph:
            self.glyph = glyph
        if title:
            self.title = title
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        sc = self.press_scale()
        p.translate(r.center())
        p.scale(sc, sc)
        p.translate(-r.center())
        if self._hover.x > 0.01:
            c = palette.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, 16, 16)
        col = palette.on_color() if self.on else palette.text(0.9)
        p.setFont(icon_font(18))
        p.setPen(col)
        p.drawText(QtCore.QRectF(r.x(), r.y() + 6, r.width(), 26), Qt.AlignmentFlag.AlignCenter, self.glyph)
        p.setFont(ui_font(11.5, W.DemiBold if self.on else W.Medium))
        p.setPen(palette.on_color() if self.on else palette.text2())
        p.drawText(QtCore.QRectF(r.x(), r.y() + 32, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.title)


class PetWidget(QtWidgets.QWidget):
    """主页中间的大号小形象：眼睛跟随光标，鼠标靠近时开心。"""
    def __init__(self, parent=None, scale: float = 2.3):
        super().__init__(parent)
        from ..compact import PetFace
        self.face = PetFace(theme_owner=self)
        self.scale = scale
        self.setMinimumWidth(round(64 * scale))
        self._t = QtCore.QTimer(self, interval=16, timeout=self.update)
        self._t.setTimerType(Qt.TimerType.PreciseTimer)
        connect_theme(self, self.update)

    def showEvent(self, e):
        self._t.start()

    def hideEvent(self, e):
        self._t.stop()

    def paintEvent(self, e):
        p = painter(self)
        c = QtCore.QRectF(self.rect()).center()
        gc = QtCore.QPointF(self.mapToGlobal(c.toPoint()))
        look = QtCore.QPointF(QtGui.QCursor.pos()) - gc
        near = self.rect().adjusted(-24, -24, 24, 24).contains(self.mapFromGlobal(QtGui.QCursor.pos()))
        self.face.paint(p, c.x(), c.y() - 4, self.scale, 1.0, look, near)


class HomePage(Page):
    key, glyph, title = "home", G.HOME, "主页"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.media = MediaCard(ctx)
        self.media.setFixedSize(174, 180)
        self.a_pin = Action(G.UNPIN, "保持展开")
        self.a_pt = Action(G.POINTER, "可穿透")
        self.a_mute = Action(G.VOLUME, "静音")
        self.a_live = Action(G.LIVE, "实时字幕")
        self.a_mute.clicked.connect(lambda: ctx.audio.toggle_mute())
        self.a_live.clicked.connect(self._toggle_live)
        ctx.live.running_changed.connect(self._live_state)
        self._live_state(ctx.live.running)
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        grid.addWidget(self.a_pin, 0, 0)
        grid.addWidget(self.a_pt, 0, 1)
        grid.addWidget(self.a_mute, 1, 0)
        grid.addWidget(self.a_live, 1, 1)
        gw = QtWidgets.QWidget()
        gw.setLayout(grid)
        gw.setFixedWidth(132)
        self.pet = PetWidget(scale=2.0)
        self.setLayout(hbox(self.media, (self.pet, 1), gw, spacing=10))

    def _live_state(self, on):
        self.a_live.set_on(on, None, "聆听中" if on else "实时字幕")

    def _toggle_live(self):
        self.ctx.pages["trans"].toggle_live()
        if self.ctx.live.running and self.island:
            self.ctx.pages["trans"].show_live()
            self.island.show_page("trans")

    def set_muted(self, muted: bool):
        self.a_mute.set_on(muted, G.MUTE if muted else G.VOLUME, "已静音" if muted else "静音")


def cfg_path():
    from ...core.config import CONFIG_PATH
    return CONFIG_PATH


# ======================================================================== 消息
NOTICE_COLOR = KIND_COLORS["notice"]


def is_other_notice(kind) -> bool:
    """微信与三家 AI 之外的通知都归到「其他通知」这一组。"""
    return bool(kind) and kind != "wechat" and kind not in ai_usage.AI_NAMES


class NoticeDot(QtWidgets.QWidget):
    """有其他通知未读时显示的一颗黄点。"""

    def __init__(self, color: QtGui.QColor, parent=None):
        super().__init__(parent)
        self.color = color
        self.setFixedSize(8, 8)

    def paintEvent(self, _e):
        p = painter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QtCore.QPointF(self.width() / 2, self.height() / 2), 3.5, 3.5)


class HistoryList(QtWidgets.QWidget, Themed):
    ROW_HEIGHT = 33
    row_clicked = QtCore.Signal(object)   # 点击某一行，交回整条通知记录

    def __init__(self, empty_text: str, parent=None, dot_color: QtGui.QColor | None = None):
        super().__init__(parent)
        self.empty_text = empty_text
        self.dot_color = dot_color
        self.rows: list[dict] = []
        self.avatars: list[QtGui.QPixmap | None] = []
        self.setFixedHeight(self.ROW_HEIGHT)
        self._hook_theme()

    def set_rows(self, rows):
        self.rows = rows[:HISTORY_MAX]
        self.avatars = [rounded_pixmap(row.get("avatar"), 22, 11) for row in self.rows]
        self.setFixedHeight(max(1, len(self.rows)) * self.ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor if self.rows else Qt.CursorShape.ArrowCursor)
        self.update()

    def row_at(self, y: float) -> dict | None:
        """把点击纵坐标换算成通知记录，越界返回 None。"""
        index = int(y // self.ROW_HEIGHT)
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            row = self.row_at(e.position().y())
            if row is not None:
                self.row_clicked.emit(row)
        e.accept()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        p.setClipRegion(e.region())   # 隐藏内容生成展开帧时，visibleRegion() 为空
        if not self.rows:
            p.setFont(ui_font(11.5))
            p.setPen(palette.text3())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return
        for i, row in enumerate(self.rows):
            h = self.ROW_HEIGHT
            y = i * h
            kind = row.get("kind")
            tint = self.dot_color or KIND_COLORS.get(kind, KIND_COLORS["other"])
            if self.avatars[i] is not None:
                p.drawPixmap(QtCore.QRect(0, y + 5, 22, 22), self.avatars[i])
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tint)
                p.drawEllipse(QtCore.QPointF(11, y + h / 2), 4, 4)
            title = row.get("title") or row.get("sender") or row.get("name") or "任务通知"
            if kind == "wechat":
                name = row.get("sender")
                status = "未读" if row.get("source") == "tray" else "新消息"
                title = f"{name} · {status}" if name else status
            p.setFont(ui_font(12.5, W.Medium))
            p.setPen(palette.text())
            width = max(0, self.width() - 72)
            body = row.get("body")
            title_rect = QtCore.QRectF(28, y + (3 if body else 0), width, h - (17 if body else 0))
            text = QtGui.QFontMetrics(p.font()).elidedText(title, Qt.TextElideMode.ElideRight, width)
            p.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, text)
            if body:
                p.setFont(ui_font(9.5))
                p.setPen(palette.text3())
                detail = QtGui.QFontMetrics(p.font()).elidedText(str(body), Qt.TextElideMode.ElideRight, width)
                p.drawText(QtCore.QRectF(28, y + 17, width, 14), Qt.AlignmentFlag.AlignVCenter, detail)
            p.setFont(ui_font(10.5, num=True))
            p.setPen(palette.text3())
            p.drawText(QtCore.QRectF(self.width() - 42, y, 40, h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       time.strftime("%H:%M", time.localtime(row.get("time", 0))))


class QuotaTile(QtWidgets.QWidget, Themed):
    """A quota percentage, a remaining credit amount, or an account balance."""
    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.current: ai_usage.Metric | None = None    # 别叫 .metric：QPaintDevice.metric 是虚函数
        self.setFixedHeight(57)
        self._hook_theme()

    def set_metric(self, metric: ai_usage.Metric | None):
        self.current = metric
        if metric is not None:
            self.label = metric.label
        self.setAccessibleName(f"{self.label} {metric.display}" if metric is not None else self.label)
        self.setAccessibleDescription(metric.detail if metric is not None else "暂无数据")
        if metric is None:
            self.setToolTip("")
        else:
            tip = f"{self.label} {metric.display}\n{metric.detail}"
            if metric.display == "—":
                tip += "\n在客户端打开一次额度页即可读到；之后关掉客户端也会保留最后读数"
            self.setToolTip(tip)
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        rect = QtCore.QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.card(1.15))
        p.drawRoundedRect(rect, 11, 11)
        metric = self.current
        percent = metric.percent if metric is not None else None
        amount = metric.display if metric is not None else "—"
        p.setPen(palette.text2())
        p.setFont(ui_font(10.5, W.Medium))
        label_width = rect.width() - (16 if percent is None else 49)
        p.drawText(QtCore.QRectF(8, 3, label_width, 18), Qt.AlignmentFlag.AlignVCenter, self.label)

        value_font = ui_font(13, W.Bold, num=True)
        if percent is None:
            value_rect = QtCore.QRectF(8, 19, rect.width() - 16, 21)
            align = Qt.AlignmentFlag.AlignLeft
            text_width = QtGui.QFontMetricsF(value_font).horizontalAdvance(amount)
            if text_width > value_rect.width():
                value_font.setPixelSize(max(9, int(13 * value_rect.width() / text_width)))
            amount = QtGui.QFontMetrics(value_font).elidedText(
                amount, Qt.TextElideMode.ElideRight, max(0, int(value_rect.width())))
        else:
            value_rect = QtCore.QRectF(rect.width() - 48, 4, 40, 18)
            align = Qt.AlignmentFlag.AlignRight
        p.setPen(palette.text())
        p.setFont(value_font)
        p.drawText(value_rect, align | Qt.AlignmentFlag.AlignVCenter, amount)
        if percent is not None:
            track = QtCore.QRectF(8, 27, max(0, rect.width() - 16), 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.track())
            p.drawRoundedRect(track, 2, 2)
            if percent > 0:
                p.setBrush(KIND_COLORS["codex"])
                track.setWidth(track.width() * min(100.0, percent) / 100)
                p.drawRoundedRect(track, 2, 2)
        p.setPen(palette.text3())
        p.setFont(ui_font(9.5))
        detail = metric.detail if metric is not None else "暂无数据"
        p.drawText(QtCore.QRectF(8, 39 if percent is None else 35, rect.width() - 16, 15),
                   Qt.AlignmentFlag.AlignVCenter, detail)


class QuotaReadings(QtCore.QObject):
    changed = QtCore.Signal()
    provider_changed = QtCore.Signal()
    refresh_requested = QtCore.Signal()

    def __init__(self, parent=None, *, foreground_provider=None):
        super().__init__(parent)
        self._foreground_provider = foreground_provider
        self.automatic_provider = None
        self.usage: dict[str, list[ai_usage.Metric]] = {}
        self.updated: dict[str, float] = {}
        self.checked: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self.source_stamps: dict[str, float | None] = {}
        self.busy: set[str] = set()
        self.busy_since: dict[str, float] = {}
        self.request_tokens: dict[str, object] = {}
        self.timer = QtCore.QTimer(self, interval=500)
        self.timer.timeout.connect(self.detect_active)
        self.detect_active()
        self.timer.start()

    def detect_active(self):
        provider = self._foreground_provider() if self._foreground_provider else None
        # Opening the island or another app must not undo the last AI selection.
        if provider in ai_usage.AI_NAMES:
            self._set_automatic(provider)
        return provider

    def observe_notification(self, provider):
        if provider in ai_usage.AI_NAMES:
            active = self.detect_active()
            self._set_automatic(active if active in ai_usage.AI_NAMES else provider)
            # A client notification means it just did work; the credits may have
            # changed, so bypass the polling throttle for one immediate read.
            self.refresh_requested.emit()

    def _set_automatic(self, provider):
        if provider != self.automatic_provider:
            self.automatic_provider = provider
            self.provider_changed.emit()
            # Switching to another AI client should show its numbers at once,
            # not up to 10 seconds later.
            self.refresh_requested.emit()


class MessagesPage(Page):
    key, glyph, title = "msg", G.MESSAGE, "消息"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.hist = HistoryList("暂无微信消息")
        self.other_hist = HistoryList("暂无其他通知", dot_color=NOTICE_COLOR)
        self.ai_hist = HistoryList("暂无 AI 通知")
        self.wechat_count = Label("", 11, level=2)
        self.other_count = Label("", 11, level=2)
        self.other_dot = NoticeDot(NOTICE_COLOR)
        self.other_dot.setVisible(False)
        self.ai_count = Label("", 11, level=2)
        self.ai_button = PillButton("AI 通知 ▾", G.BOLT, height=26)
        self.ai_button.setMaximumWidth(190)
        self.ai_button.clicked.connect(self._choose_provider)
        wechat = PillButton("微信", G.MESSAGE, height=26)
        wechat.clicked.connect(self._open_wechat)
        clear = IconButton(G.CHECK, 24, 13, "微信全部已读")
        clear.clicked.connect(self._clear_wechat)
        self.wechat_clear = clear
        self.other_clear = IconButton(G.CHECK, 24, 13, "其他通知全部已读")
        self.other_clear.clicked.connect(self._clear_other)
        self.ai_clear = IconButton(G.CHECK, 24, 13, "本组全部已读")
        self.ai_clear.clicked.connect(self._clear_ai)
        for history in (self.hist, self.other_hist, self.ai_hist):
            history.row_clicked.connect(self._open_notice)

        other_title = Label("其他通知", 12.5, W.Medium)
        other_title.setToolTip("QQ、邮件、钉钉、飞书、企业微信等")
        self.quota_tiles = (QuotaTile("5 小时额度"), QuotaTile("周额度"))
        quota_row = hbox(*((tile, 1) for tile in self.quota_tiles), spacing=6)
        left, right = Card(radius=15, alpha=0.62), Card(radius=15, alpha=0.62)
        left.setLayout(vbox(hbox(wechat, self.wechat_count, None, clear, spacing=5),
                            (self._scroll(self.hist), 1),
                            hbox(self.other_dot, other_title, self.other_count,
                                 None, self.other_clear, spacing=5),
                            (self._scroll(self.other_hist), 1),
                            spacing=5, margins=(9, 8, 9, 6)))
        right.setLayout(vbox(hbox(self.ai_button, self.ai_count, None, self.ai_clear, spacing=5), quota_row,
                             (self._scroll(self.ai_hist), 1), spacing=6, margins=(9, 8, 9, 6)))
        self.setLayout(hbox((left, 1), (right, 1), spacing=10))
        ctx.notify.wechat_preview_changed.connect(self._preview_changed)
        # 先按上次的选择定下 provider，这样还没打开面板就开始试探读数。
        self._quota = getattr(ctx, "quota_readings", None) or QuotaReadings(self)
        self._provider = ai_usage.resolve_provider(
            cfg.get("notify.ai_provider"), None,
            str(cfg.get(ai_usage.PROVIDER_KEY) or "").strip(), self._quota.automatic_provider)
        self._quota.timer.timeout.connect(self._tick_usage)
        self._quota.changed.connect(self._show_usage)
        self._quota.provider_changed.connect(self.refresh)
        self._quota.refresh_requested.connect(self._on_refresh_requested)

    def _on_refresh_requested(self):
        self._request_usage(force=True)

    def _preview_changed(self):
        if self.active:
            self.refresh()

    @staticmethod
    def _scroll(history):
        area = ScrollArea()
        area.inner.setLayout(vbox(history, None, spacing=0))
        return area

    def _open_notice(self, row):
        """点消息条目跳到对应应用，并顺手把它标记为已读。"""
        kind = row.get("kind") if isinstance(row, dict) else None
        if not kind:
            return
        opened = self.ctx.notify.open(kind)
        if opened and self.island:
            self.island.collapse()

    def _open_wechat(self):
        self._open_notice({"kind": "wechat"})

    def _clear_wechat(self):
        self.ctx.notify.clear("wechat")

    def _clear_ai(self):
        """清除当前 AI 分组的未读计数。"""
        if self._provider:
            self.ctx.notify.clear(self._provider)

    def _clear_other(self):
        """只清「其他通知」，不动微信和 AI 三家的未读。"""
        for kind in [key for key, n in self.ctx.notify.counts.items()
                     if n and is_other_notice(key)]:
            self.ctx.notify.clear(kind)

    def _choose_provider(self):
        from .system import styled_menu

        menu = styled_menu(self)
        selected = cfg.get("notify.ai_provider")
        choices = [("auto", "自动识别（跟随当前 AI 软件）"), *ai_usage.AI_NAMES.items()]
        for value, title in choices:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(selected == value)
            action.triggered.connect(lambda checked=False, key=value: self._select_provider(key))
        if self._provider in ai_usage.USAGE_PROVIDERS:
            menu.addSeparator()
            menu.addAction("刷新额度").triggered.connect(lambda: self._request_usage(force=True))
        if self.island:
            self.island.locks.add("menu")
            menu.aboutToHide.connect(lambda: self.island.locks.discard("menu"))
        menu.popup(self.ai_button.mapToGlobal(QtCore.QPoint(0, self.ai_button.height())))

    def _select_provider(self, key):
        cfg.set("notify.ai_provider", key)
        self._quota.detect_active()
        self._quota.provider_changed.emit()

    def refresh(self):
        counts = self.ctx.notify.counts
        hist = self.ctx.notify.history
        preview = self.ctx.notify.wechat_preview
        flash_time = preview[0].get("flash_time") if preview else None
        wechat = [row for row in hist if row.get("kind") == "wechat"
                  and not (flash_time is not None and row.get("source") == "flash" and row["time"] == flash_time)]
        self.hist.set_rows(preview + wechat)
        self.wechat_count.setText(f"{counts.get('wechat', 0)} 未读")

        other = [row for row in hist if is_other_notice(row.get("kind"))]
        self.other_hist.set_rows(other)
        other_unread = sum(n for key, n in counts.items() if is_other_notice(key))
        self.other_count.setText(f"{other_unread} 未读")
        self.other_dot.setVisible(bool(other_unread))
        self.other_clear.setEnabled(bool(other_unread))

        selected = cfg.get("notify.ai_provider")
        latest = next((row["kind"] for row in hist if row.get("kind") in ai_usage.AI_NAMES), None)
        last = str(cfg.get(ai_usage.PROVIDER_KEY) or "").strip()
        provider = ai_usage.resolve_provider(selected, latest, last, self._quota.automatic_provider)
        if provider and provider != last:
            # Remember it so a restart without notifications keeps the same cards.
            cfg.set(ai_usage.PROVIDER_KEY, provider)
        self._provider = provider
        name = ai_usage.AI_NAMES.get(self._provider, "AI 通知")
        automatic = selected not in ai_usage.AI_NAMES
        self.ai_button.set_text(("自动·" if automatic else "") + name + " ▾")
        guidance = ("自动跟随当前 AI 软件；切到其他软件时保留最近识别结果"
                    if automatic else "已固定此 AI 软件；选择「自动识别」可恢复跟随")
        self.ai_button.setToolTip(name + "\n" + guidance)
        self.ai_button.setMinimumWidth(min(self.ai_button.maximumWidth(),
                                          self.ai_button.minimumWidth() + 8))
        self.ai_count.setText(f"{counts.get(self._provider, 0)} 未读" if self._provider else "")
        self.ai_count.setMinimumWidth(self.ai_count.sizeHint().width())
        self.ai_clear.setEnabled(bool(self._provider and counts.get(self._provider)))
        self.wechat_clear.setEnabled(bool(counts.get("wechat", 0) or preview))
        self.ai_hist.set_rows([row for row in hist if row.get("kind") == self._provider])
        self._show_usage()
        self._request_usage()

    def _show_usage(self):
        metrics = ai_usage.display_metrics(
            self._provider, self._quota.usage.get(self._provider),
            self._quota.updated.get(self._provider))
        for index, tile in enumerate(self.quota_tiles):
            tile.setVisible(index < len(metrics))
            if index < len(metrics):
                tile.set_metric(metrics[index])

    def _tick_usage(self):
        """Poll the native clients without redrawing the whole page."""
        if self._provider is None:
            return
        self._request_usage()

    def _request_usage(self, *, force=False):
        provider = self._provider
        quota = self._quota
        if provider not in ai_usage.USAGE_PROVIDERS:
            return
        if provider in quota.busy:
            # A read whose callback never arrived must not silence this provider
            # until the app is restarted.
            if time.monotonic() - quota.busy_since.get(provider, 0.0) < USAGE_BUSY_TIMEOUT:
                return
            _log.warning("Quota read callback expired (%s); discarding its late result", provider)
            quota.busy.discard(provider)
            quota.busy_since.pop(provider, None)
            quota.request_tokens.pop(provider, None)
        checked = quota.checked.get(provider)
        source = ai_usage.resolve_workbuddy_source(
            str(cfg.get("notify.workbuddy_quota_source") or "")) if provider == "workbuddy" else ""
        stamp = ai_usage.source_stamp(source) if provider == "workbuddy" else None
        # A producer rewriting the local JSON is a real data change: react on
        # the next 500 ms tick instead of waiting for the polling interval.
        if provider == "workbuddy" and stamp != quota.source_stamps.get(provider):
            force = True
        # Visible cards refresh promptly; hidden islands retain the slower cadence.
        # Both displays share checked/busy, so a tick never duplicates a read.
        # Only a visible panel short-retries after a miss: a hidden island has
        # nobody waiting on the numbers and must not keep scanning UIA.
        interval = 2 if self.active else (15 if provider == "codex" else 10)
        if self.active:
            interval = ai_usage.retry_interval(interval, quota.failures.get(provider, 0))
        if not force and checked is not None and time.monotonic() - checked < interval:
            return
        quota.busy.add(provider)
        token = object()
        quota.request_tokens[provider] = token
        quota.busy_since[provider] = time.monotonic()
        quota.checked[provider] = time.monotonic()
        if provider == "workbuddy":
            quota.source_stamps[provider] = stamp

        def finished(metrics):
            if quota.request_tokens.get(provider) is not token:
                return
            quota.request_tokens.pop(provider, None)
            quota.busy.discard(provider)
            quota.busy_since.pop(provider, None)
            if metrics:
                quota.failures[provider] = 0
                quota.usage[provider] = metrics
                quota.updated[provider] = ai_usage.usage_stamp(metrics)
                # 落盘放到后台：主线程每 2 秒的轮询不再做缓存文件读写。
                bg.run_bg(ai_usage.save_usage_cache, provider, metrics)
            else:
                quota.failures[provider] = quota.failures.get(provider, 0) + 1
            # A failed read deliberately keeps the previous (or cached) reading.
            quota.changed.emit()

        job = bg.run_bg(ai_usage.read_ai_usage, provider, source, on_done=finished,
                        on_error=lambda _exc: finished(None))
        if job is None and quota.request_tokens.get(provider) is token:
            quota.request_tokens.pop(provider, None)
            quota.busy.discard(provider)
            quota.busy_since.pop(provider, None)
            quota.checked.pop(provider, None)
            quota.failures[provider] = quota.failures.get(provider, 0) + 1

    def on_show(self):
        super().on_show()
        self._quota.detect_active()
        self.refresh()
        self._request_usage(force=True)   # 展开面板立即读，别被节流挡住

    def on_hide(self):
        super().on_hide()
