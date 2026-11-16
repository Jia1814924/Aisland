# -*- coding: utf-8 -*-
"""剪贴板历史与日语翻译。"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from ...core import bg
from ...core.config import cfg
from ...core.translation_settings import (CUSTOM_PROVIDER, Connection, PROVIDERS, Provider,
                                          provider_for)
from ...live.paths import LANGUAGE_OPTIONS
from ...live.segment import MAX_RECENT_SEGMENTS
from ..anim import clamp
from ..theme import G, W, icon_font, theme, ui_font, theme_for, connect_theme
from ..widgets import (Segmented, Card, Hoverable, IconButton, Label, LineEdit, PillButton, ScrollArea, TextEdit,
                       centered_dialog, hbox, painter, vbox)
from .base import Page

Qt = QtCore.Qt


def looks_japanese(text: str) -> bool:
    """有假名就当作日语：自动模式下只有日语行才做分词 / 点词。"""
    return any("\u3040" <= ch <= "\u30ff" for ch in text or "")


class ClipRow(Hoverable):
    """一条剪贴板记录：点击复制；右侧图钉（置顶）和 X（删除）。"""
    pin_toggled = QtCore.Signal()
    delete_clicked = QtCore.Signal()

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item = item
        self.flash = 0.0
        th = item.get("thumb")
        self.pm = QtGui.QPixmap.fromImage(th) if isinstance(th, QtGui.QImage) and not th.isNull() else None
        self.setFixedHeight(112)
        self.setMouseTracking(True)
        self._hot = None

    def _pin_rect(self):
        return QtCore.QRectF(self.width() - 66, self.height() - 34, 26, 26)

    def _del_rect(self):
        return QtCore.QRectF(self.width() - 36, self.height() - 34, 26, 26)

    def mouseMoveEvent(self, e):
        hot = "pin" if self._pin_rect().contains(e.position()) else "del" if self._del_rect().contains(e.position()) else None
        if hot != self._hot:
            self._hot = hot
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._pin_rect().contains(e.position()):
                self.pin_toggled.emit()
                return
            if self._del_rect().contains(e.position()):
                self.delete_clicked.emit()
                return
        super().mouseReleaseEvent(e)

    def mark_copied(self):
        self.flash = 1.0
        self.update()
        def off():
            try:
                self.flash = 0.0
                self.update()
            except RuntimeError:      # 行已被重建删除
                pass
        QtCore.QTimer.singleShot(900, off)

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        sc = self.press_scale()
        p.translate(r.center())
        p.scale(sc, sc)
        p.translate(-r.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.active_pill() if self.flash else palette.card(0.75 + 0.8 * self._hover.x))
        p.drawRoundedRect(r, 18, 18)
        body = QtCore.QRectF(r.x() + 12, r.y() + 12, r.width() - 24, r.height() - 48)
        if self.flash:
            p.setFont(ui_font(14, W.DemiBold))
            p.setPen(palette.pill_text())
            p.drawText(body, Qt.AlignmentFlag.AlignCenter, "已复制")
        elif self.pm is not None:
            path = QtGui.QPainterPath()
            path.addRoundedRect(body, 10, 10)
            p.save()
            p.setClipPath(path)
            p.setBrush(palette.track())
            p.drawRoundedRect(body, 10, 10)
            img = self.pm.scaled(body.size().toSize() * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            img.setDevicePixelRatio(2)
            off = QtCore.QPointF((body.width() - img.width() / 2) / 2, (body.height() - img.height() / 2) / 2)
            p.drawPixmap(body.topLeft() + off, img)
            p.restore()
        else:
            txt = " ".join((self.item.get("text") or "").split())
            p.setFont(ui_font(12.5))
            p.setPen(palette.text())
            fm = QtGui.QFontMetrics(p.font())
            lines = max(1, int(body.height() // fm.lineSpacing()))
            # 手动按宽度断行，最多 lines 行，末行省略
            out, cur = [], ""
            for ch in txt:
                if fm.horizontalAdvance(cur + ch) > body.width():
                    out.append(cur)
                    cur = ch
                    if len(out) >= lines:
                        break
                else:
                    cur += ch
            if len(out) < lines and cur:
                out.append(cur)
            if len(out) == lines and (len("".join(out)) < len(txt)):
                out[-1] = fm.elidedText(out[-1] + "…", Qt.TextElideMode.ElideRight, int(body.width()))
            for i, line in enumerate(out[:lines]):
                p.drawText(QtCore.QRectF(body.x(), body.y() + i * fm.lineSpacing(), body.width(), fm.lineSpacing()),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
        # 底栏：时间 · 置顶 · 删除
        p.setFont(ui_font(10.5, num=True))
        p.setPen(palette.text3())
        p.drawText(QtCore.QRectF(r.x() + 14, r.bottom() - 34, 80, 30), Qt.AlignmentFlag.AlignVCenter,
                   time.strftime("%H:%M", time.localtime(self.item.get("time", 0))))
        pinned = self.item.get("pinned")
        for rect, glyph, hot, base in ((self._pin_rect(), G.PIN if pinned else G.UNPIN, self._hot == "pin", 0.9 if pinned else 0.4),
                                       (self._del_rect(), G.CLOSE, self._hot == "del", 0.4)):
            if hot:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(palette.hover())
                p.drawEllipse(rect)
            p.setFont(icon_font(11))
            p.setPen(palette.text(1.0 if hot else base))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)


class ClipboardPage(Page):
    key, glyph, title = "clip", G.CLIP, "剪贴板"

    def __init__(self, ctx):
        super().__init__(ctx)
        clear = PillButton("清空", G.TRASH, height=28)
        self.clear_btn = clear
        clear.clicked.connect(lambda: ctx.clipboard.clear())
        self.sc = ScrollArea()
        self.list = QtWidgets.QGridLayout(self.sc.inner)
        self.list.setContentsMargins(4, 2, 8, 6)
        self.list.setHorizontalSpacing(10)
        self.list.setVerticalSpacing(8)
        self.list.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.list.setColumnStretch(0, 1)
        self.list.setColumnStretch(1, 1)
        self.setLayout(vbox(hbox(None, clear, margins=(0, 0, 14, 0)), (self.sc, 1), spacing=6))
        ctx.clipboard.changed.connect(self._changed)
        self._dirty = True
        self._just = None

    def _changed(self):
        self._dirty = True
        if self.active:
            self.refresh()

    def refresh(self):
        self._dirty = False
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        items = self.ctx.clipboard.items()
        for n, item in enumerate(items):
            row = ClipRow(item)
            iid = item["id"]
            row.clicked.connect(lambda iid=iid, row=row: self._copy(iid, row))
            row.pin_toggled.connect(lambda iid=iid, it=item: self.ctx.clipboard.pin(iid, not it.get("pinned")))
            row.delete_clicked.connect(lambda iid=iid: self.ctx.clipboard.remove(iid))
            self.list.addWidget(row, n // 2, n % 2)
            if self._just and self._just[0] == iid and time.monotonic() - self._just[1] < 0.9:
                row.mark_copied()
        self.clear_btn.setVisible(bool(items))

    def _copy(self, iid, row):
        """点击 = 复制到剪贴板（不粘贴）。"""
        self._just = (iid, time.monotonic())
        row.mark_copied()
        self.ctx.clipboard.copy(iid)

    def on_show(self):
        super().on_show()
        if self._dirty:
            self.refresh()


class LiveView(QtWidgets.QWidget):
    """实时字幕：日语小字（可点词查词）+ 中文大字，最新的在最下面；正在说的一句（草稿）实时更新。"""
    MAX = MAX_RECENT_SEGMENTS
    word_clicked = QtCore.Signal(object, dict)      # (Token, 所在字幕行)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []          # {sid, jp, zh, final}
        self.partial = ""
        self.partial_zh = ""
        self.hint = ""
        self.tokens_fn = None               # text -> list[Token]（学习服务提供）
        self.clickable = True               # 只有日语才按词排版 / 可点查词
        self._hits: list[tuple[QtCore.QRectF, object, dict]] = []
        self._hover = None
        self.scroll = 0.0                   # 距离底部（最新一行）的滚动距离，0 = 跟随最新
        self._content_h = 0.0
        self._flow_cache: dict = {}
        self._latest_btn: QtCore.QRectF | None = None
        self.setMouseTracking(True)
        connect_theme(self, self.update)

    def _find(self, sid):
        for r in reversed(self.rows):
            if r["sid"] == sid:
                return r
        return None

    def _row(self, sid):
        r = self._find(sid)
        if r is not None:
            return r
        r = {"sid": sid, "jp": "", "zh": "", "final": False}
        self.rows.append(r)
        del self.rows[:-self.MAX]
        return r

    def on_segment(self, sid, jp):
        before = self._content_h
        r = self._row(sid)
        r["jp"] = jp
        if not r["zh"] and self.partial_zh:
            r["zh"] = self.partial_zh          # 草稿译文先挂到这一句，等最终译文覆盖
        self.partial, self.partial_zh = "", ""
        self._grow(before)
        self.update()

    def on_live(self, sid, zh):
        r = self._find(sid)
        if r is None:
            self.partial_zh = zh               # 句子还没定稿：译文跟着草稿行显示，不单独成行
        elif not r["final"]:
            r["zh"] = zh
        self.update()

    def on_final(self, sid, zh):
        r = self._find(sid)
        if r is None:
            # Late translation for an utterance whose row is already gone:
            # never append a stale row at the bottom of the history.
            return
        before = self._content_h
        r["zh"], r["final"] = zh, True
        self._grow(before)
        self.update()

    def on_partial(self, jp):
        self.partial = jp
        self.update()

    def clear(self):
        self.rows, self.partial, self.partial_zh = [], "", ""
        self.scroll = 0.0
        self._flow_cache.clear()
        self.update()

    # ---------- 滚动：滚轮上翻看历史；有新内容时停在原处，点「回到最新」或滚到底恢复跟随 ----------
    def _max_scroll(self) -> float:
        return max(0.0, self._content_h - self.height() + 8)

    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.pixelDelta().y()
        if not d:
            e.ignore()
            return
        self.scroll = clamp(self.scroll + d / 120.0 * 52.0, 0.0, self._max_scroll())
        self.update()
        e.accept()

    def _grow(self, before: float):
        """内容变高时：如果正在看历史，就把滚动量加上增量，保持画面不动。"""
        if self.scroll > 0:
            self._measure()
            self.scroll = clamp(self.scroll + (self._content_h - before), 0.0, self._max_scroll())

    # ---------- 日语行：按词排版（可点击） ----------
    def _flow(self, text: str, w: float, fm: QtGui.QFontMetrics, clickable: bool):
        """返回 [(x, line_index, width, Token|None, piece)] 与行数（按文本 / 宽度缓存，分词只做一次）。"""
        key = (text, int(w), clickable)
        hit = self._flow_cache.get(key)
        if hit is not None:
            return hit
        res = self._flow_raw(text, w, fm, clickable)
        if len(self._flow_cache) > 400:
            self._flow_cache.clear()
        self._flow_cache[key] = res
        return res

    def _flow_raw(self, text: str, w: float, fm: QtGui.QFontMetrics, clickable: bool):
        toks = self.tokens_fn(text) if (clickable and self.tokens_fn) else []
        pieces = [(t.surface, t) for t in toks] if toks else [(ch, None) for ch in text]
        out, x, line = [], 0.0, 0
        for piece, tok in pieces:
            pw = fm.horizontalAdvance(piece)
            if x > 0 and x + pw > w:
                x, line = 0.0, line + 1
            out.append((x, line, pw, tok, piece))
            x += pw
        return out, line + 1

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        w = self.width() - 8
        self._measure()
        self.scroll = clamp(self.scroll, 0.0, self._max_scroll())
        y = self.height() - 4 + self.scroll
        fj, fz = ui_font(12.5), ui_font(16, W.Medium)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)
        lh = fmj.height() + 1
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        items = list(self.rows)
        if self.partial or self.partial_zh:
            items.append({"jp": self.partial, "zh": self.partial_zh, "final": False, "draft": True})
        self._hits = []
        if not items:
            p.setFont(ui_font(13))
            p.setPen(palette.text3())
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.hint)
            return
        for it in reversed(items):
            zh = it.get("zh", "")
            hz = fmz.boundingRect(QtCore.QRect(0, 0, w, 10000), flags, zh).height() if zh else 0
            draft = bool(it.get("draft"))
            flow, nlines = self._flow(it.get("jp", ""), w, fmj,
                                      self.clickable and not draft and looks_japanese(it.get("jp", "")))
            hj = nlines * lh
            if y - hz - hj - 2 > self.height():          # 整条都在可见区下方（正在看历史）：跳过
                y -= hz + hj + 2 + 12
                continue
            y -= hz
            if zh:
                p.setFont(fz)
                p.setPen(palette.text(1.0 if it.get("final") else 0.8))
                p.drawText(QtCore.QRect(4, int(y), w, hz), flags, zh)
            y -= hj + 2
            p.setFont(fj)
            for x, line, pw, tok, piece in flow:
                r = QtCore.QRectF(4 + x, y + line * lh, pw, lh)
                hot = tok is not None and tok.clickable and self._hover is not None \
                    and self._hover[0] == it.get("sid") and self._hover[1] == (line, round(x))
                if hot:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(palette.hover())
                    p.drawRoundedRect(r.adjusted(-2, 0, 2, 0), 5, 5)
                p.setPen(palette.text3() if draft else (palette.text() if hot else palette.text2()))
                p.drawText(r, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, piece)
                if tok is not None and tok.clickable and not draft and r.bottom() > 0:
                    self._hits.append((r, tok, it, (line, round(x))))
            y -= 12
            if y < 0:
                break
        # 正在看历史：右下角「回到最新」
        self._latest_btn = None
        if self.scroll > 1:
            f = ui_font(11.5, W.Medium)
            txt = "↓ 回到最新"
            bw = QtGui.QFontMetrics(f).horizontalAdvance(txt) + 22
            br = QtCore.QRectF(self.width() - bw - 6, self.height() - 30, bw, 24)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.card(1.8))
            p.drawRoundedRect(br, 12, 12)
            p.setFont(f)
            p.setPen(palette.text())
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, txt)
            self._latest_btn = br

    def _measure(self):
        """全部字幕的总高度（用于滚动范围）。"""
        w = self.width() - 8
        fj, fz = ui_font(12.5), ui_font(16, W.Medium)
        fmj, fmz = QtGui.QFontMetrics(fj), QtGui.QFontMetrics(fz)
        lh = fmj.height() + 1
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        items = list(self.rows)
        if self.partial or self.partial_zh:
            items.append({"jp": self.partial, "zh": self.partial_zh, "draft": True})
        total = 4.0
        for it in items:
            zh = it.get("zh", "")
            hz = fmz.boundingRect(QtCore.QRect(0, 0, w, 10000), flags, zh).height() if zh else 0
            _, nlines = self._flow(it.get("jp", ""), w, fmj,
                                   self.clickable and not it.get("draft") and looks_japanese(it.get("jp", "")))
            total += hz + nlines * lh + 2 + 12
        self._content_h = total

    def _hit(self, pos):
        for r, tok, row, key in self._hits:
            if r.contains(pos):
                return r, tok, row, key
        return None

    def mouseMoveEvent(self, e):
        h = self._hit(e.position())
        new = (h[2].get("sid"), h[3]) if h else None
        if new != self._hover:
            self._hover = new
            self.setCursor(Qt.CursorShape.PointingHandCursor if h else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        if self._hover is not None:
            self._hover = None
            self.update()

    def mouseReleaseEvent(self, e):
        if self._latest_btn is not None and self._latest_btn.contains(e.position()):
            self.scroll = 0.0
            self.update()
            return
        h = self._hit(e.position())
        if h and e.button() == Qt.MouseButton.LeftButton:
            self.word_clicked.emit(h[1], h[2])


class LevelBar(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.v = 0.0
        self.setFixedSize(46, 8)

    def set_level(self, v):
        self.v = self.v * 0.6 + float(v) * 0.4
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        r = QtCore.QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(palette.track())
        p.drawRoundedRect(r, 4, 4)
        p.setBrush(palette.on_color())
        p.drawRoundedRect(QtCore.QRectF(0, 0, max(8, r.width() * min(1.0, self.v * 1.6)), r.height()), 4, 4)


class LookupView(QtWidgets.QWidget):
    """One search field, with results appearing only after a query."""
    def __init__(self, ctx, ensure_connection):
        super().__init__()
        self.ctx = ctx
        self.ensure_connection = ensure_connection
        self._generation = 0
        self._request = -1
        self.search = LineEdit("输入单词、短语或句子")
        self.search.setAccessibleName("查词句")
        self.search.setMaxLength(1000)
        self.query_button = PillButton("查询", G.SEARCH, height=32, primary=True)
        self.query_button.setAccessibleName("查询")
        self.query_button.setEnabled(False)
        self.search.textChanged.connect(self._reset)
        self.search.returnPressed.connect(self.query)
        self.query_button.clicked.connect(self.query)

        self.result = QtWidgets.QLabel()
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.result.setFont(ui_font(13))
        sc = ScrollArea()
        sc.inner.setLayout(vbox(self.result, None, spacing=0))
        self.card = Card(radius=16)
        self.card.setLayout(vbox((sc, 1), margins=(12, 9, 9, 7)))
        self.card.hide()
        body = QtWidgets.QWidget()
        body.setLayout(vbox((self.card, 1)))
        self.setLayout(vbox(hbox((self.search, 1), self.query_button, spacing=6), (body, 1), spacing=8))
        connect_theme(self, self._style)
        self._style()
        ctx.translate.delta.connect(self._delta)
        ctx.translate.finished.connect(self._done)
        ctx.translate.failed.connect(self._fail)

    def _style(self):
        palette = theme_for(self)
        self.result.setStyleSheet(f"color:{palette.qss_color(palette.text())};background:transparent")

    def _reset(self):
        self._generation += 1
        if self._request != -1:
            self.ctx.translate.cancel("lookup")
        self._request = -1
        self.card.hide()
        self.result.clear()
        self.query_button.setEnabled(bool(self.search.text().strip()))

    def query(self):
        text = self.search.text().strip()
        if not text or not self.query_button.isEnabled():
            return
        self._reset()
        generation = self._generation
        self.query_button.setEnabled(False)
        self._show_result("正在查询…")
        bg.run_bg(self._local, text, on_done=lambda result: self._resolved(generation, text, result),
                  on_error=lambda _err: self._resolved(generation, text, ""))

    def _local(self, text: str) -> str:
        if len(text) > 40 or any(char in text for char in "。！？.!?¿¡\n"):
            return ""
        from ...learn.service import meaning_for_card

        entries, _error = self.ctx.learn.lookup(text)
        meaning = meaning_for_card(entries)
        if not meaning:
            return ""
        entry = entries[0]
        reading = f"【{entry.reading}】" if entry.reading and entry.reading != entry.term else ""
        return f"{entry.term}{reading} · 本地词典\n{meaning}"

    def _resolved(self, generation, text, local):
        if generation != self._generation:
            return
        if local:
            self._complete(local)
        elif self.ensure_connection():
            if generation == self._generation:
                self._partial = ""
                self._request = self.ctx.translate.lookup(text)
        else:
            self._complete("设置翻译连接后可查询这个词句")

    def _show_result(self, text):
        self.result.setText(text)
        self.card.show()

    def _complete(self, text):
        self._request = -1
        self._show_result(text)
        self.query_button.setEnabled(bool(self.search.text().strip()))

    def _delta(self, rid, piece):
        if rid == self._request:
            self._partial += piece
            self._show_result(self._partial)

    def _done(self, rid, text):
        if rid == self._request:
            self._complete(text)

    def _fail(self, rid, error):
        if rid == self._request:
            self._complete(error)


class TranslatePage(Page):
    key, glyph, title = "trans", G.TRANSLATE, "翻译"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.req = -1
        self.input = TextEdit("输入要翻译的文字")
        self.input.focus_wanted.connect(self.want_focus)
        self.input.submit.connect(self.go)
        go = PillButton("翻译", G.TRANSLATE, height=30, primary=True)
        go.clicked.connect(self.go)
        clipb = PillButton("翻译剪贴板", G.CLIP, height=30)
        clipb.clicked.connect(lambda: ctx.actions.translate_clipboard(show_page=True))
        self.copy = IconButton(G.COPY, 30, 13, "复制译文")
        self.copy.clicked.connect(self._copy)
        self.out_card = Card(radius=16)
        self.out = QtWidgets.QLabel()
        self.out.setWordWrap(True)
        self.out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.out.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.out.setFont(ui_font(15, W.Medium))
        sc = ScrollArea()
        lay = QtWidgets.QVBoxLayout(sc.inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.out)
        lay.addStretch(1)
        self.state = Label("", 11, level=3)
        self.out_card.setLayout(vbox(hbox(self.state, None, self.copy),
                                     (sc, 1), spacing=2, margins=(14, 6, 8, 10)))
        text_w = QtWidgets.QWidget()
        text_w.setLayout(hbox((vbox((self.input, 1), hbox(go, clipb, None, spacing=6), spacing=6), 1),
                              (self.out_card, 1), spacing=8))

        # 实时字幕（移植自译声：系统声音 → 识别 → 翻译）
        self.live_btn = PillButton("开始聆听", G.LIVE, height=30, primary=True)
        self.live_btn.clicked.connect(self.toggle_live)
        self.live_status = Label("", 11.5, level=3)
        self.level = LevelBar()
        clear = IconButton(G.TRASH, 30, 12, "清空")
        self.view = LiveView()
        clear.clicked.connect(ctx.live.clear_clips)
        self.live_elsewhere = Label("实时字幕正在播放屏显示", 12, level=3, align=Qt.AlignmentFlag.AlignCenter)
        self.live_elsewhere.hide()
        live_card = Card(radius=16)
        live_card.setLayout(vbox((self.view, 1), (self.live_elsewhere, 1), margins=(10, 8, 10, 8)))
        live_w = QtWidgets.QWidget()
        live_w.setLayout(vbox(hbox(self.live_btn, self.level, self.live_status, None, clear, spacing=8), (live_card, 1), spacing=8))

        # 字幕点词沿用原弹窗，独立的查词句页不显示单词本。
        from ..lookup import LookupPopup
        self.popup = LookupPopup(ctx.learn, parent=self)
        self.view.tokens_fn = ctx.learn.tokens
        self.view.word_clicked.connect(self._word_clicked)
        self.lookup_view = LookupView(ctx, self.ensure_connection)
        self.lookup_view.search.focus_wanted.connect(self.want_focus)

        self.mode = Segmented(["文本", "实时字幕", "查词句"])
        self.mode.setFixedWidth(216)
        self.modes = QtWidgets.QStackedWidget()
        self.modes.addWidget(text_w)
        self.modes.addWidget(live_w)
        self.modes.addWidget(self.lookup_view)
        self.mode.changed.connect(self.modes.setCurrentIndex)
        # 识别语种：日语用 Parakeet，英语 / 韩语用 SenseVoice
        self.lang_sel = Segmented([name for _code, name in LANGUAGE_OPTIONS])
        self.lang_sel.setFixedWidth(172)
        self.lang_sel.changed.connect(self._pick_lang)
        self.lang_sel.hide()
        self.modes.currentChanged.connect(lambda index: self.lang_sel.setVisible(index == 1))
        connection = PillButton("翻译连接", height=26)
        connection.setAccessibleName("翻译连接")
        connection.clicked.connect(self.edit_connection)
        self.setLayout(vbox(hbox(self.mode, None, self.lang_sel, connection, spacing=8),
                            (self.modes, 1), spacing=8))
        self._language = None
        self._sync_lang()
        cfg.changed.connect(self._sync_lang)

        lv = ctx.live
        lv.segment.connect(self.view.on_segment)
        lv.live_zh.connect(self.view.on_live)
        lv.final_zh.connect(self.view.on_final)
        lv.partial.connect(self.view.on_partial)
        lv.cleared.connect(self.view.clear)
        lv.level.connect(self.level.set_level)
        lv.status.connect(self.live_status.setText)
        lv.error.connect(self.live_status.setText)
        lv.running_changed.connect(self._live_state)
        self._live_state(lv.running)
        connect_theme(self, self._style)
        self._style()
        ctx.translate.delta.connect(self._delta)
        ctx.translate.finished.connect(self._done)
        ctx.translate.failed.connect(self._fail)
        self._buf = ""
        # 输入框右上角的清空按钮（有内容时才出现）
        self.clear_btn = IconButton(G.CLOSE, 24, 10, "清空", self.input)
        self.clear_btn.clicked.connect(self._clear_all)
        self.clear_btn.hide()
        self.input.textChanged.connect(self._sync_clear)
        self.input.installEventFilter(self)

    def ensure_connection(self) -> bool:
        return self.ctx.translate.has_key() or self.edit_connection()

    def edit_connection(self) -> bool:
        current = self.ctx.translate.connection()
        saved_key = current.api_key
        saved_base = current.base_url.strip().rstrip("/")
        selected = provider_for(current.base_url) or CUSTOM_PROVIDER
        custom_saved = selected is CUSTOM_PROVIDER
        options = [*PROVIDERS, CUSTOM_PROVIDER]

        island = self.island
        dialog = centered_dialog("翻译连接", screen=getattr(island, "scr", None))
        dialog.setMinimumWidth(376)
        # 测速在后台跑，用户可能先关窗口：回调回来时控件已经 deleteLater，不能再碰。
        alive = [True]
        last_base = None
        probing = False
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setSpacing(10)
        intro = QtWidgets.QLabel("文字翻译、实时字幕和查词句共用这个连接。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        providers = QtWidgets.QComboBox(dialog)
        providers.setAccessibleName("大模型厂商")
        for option in options:
            providers.addItem(option.name)
        providers.setCurrentIndex(options.index(selected))
        form.addRow("大模型厂商", providers)

        model = QtWidgets.QLineEdit(dialog)
        model.setAccessibleName("模型")
        form.addRow("模型", model)

        url_label = QtWidgets.QLabel("接入地址")
        url = QtWidgets.QLineEdit(dialog)
        url.setAccessibleName("接入地址")
        url.setPlaceholderText("https://api.example.com/v1")
        form.addRow(url_label, url)

        key = QtWidgets.QLineEdit(dialog)
        key.setAccessibleName("API Key")
        key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("API Key", key)
        layout.addLayout(form)

        status = QtWidgets.QLabel()
        status.setWordWrap(True)
        status.hide()
        layout.addWidget(status)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save)
        save.setText("保存")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("稍后")
        # 实时字幕的延迟几乎全在「首字」：换厂商前先量一下，比看参数量有用
        probe = buttons.addButton("测速", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        probe.setToolTip("向当前填写的厂商真发一句，量首字与全文耗时")
        layout.addWidget(buttons)

        def chosen() -> Provider:
            return options[providers.currentIndex()]

        def endpoint(option: Provider) -> str:
            if option is CUSTOM_PROVIDER:
                return url.text().strip().rstrip("/")
            return saved_base if option is selected and saved_key else option.base_url

        def keeps_saved_key(option: Provider, base: str) -> bool:
            return bool(saved_key) and option is selected and base == saved_base

        def refresh():
            nonlocal last_base
            option = chosen()
            base = endpoint(option)
            changed = last_base is not None and last_base != base
            last_base = base
            if changed:
                key.clear()
                status.hide()
            reuse = keeps_saved_key(option, base)
            key.setPlaceholderText("已配置，输入新 Key 可更换" if reuse else "粘贴该地址的 API Key")
            ready = bool(key.text().strip()) or reuse
            if option is CUSTOM_PROVIDER:
                ready = ready and bool(base) and bool(model.text().strip())
            save.setEnabled(bool(ready))
            probe.setEnabled(bool(ready) and not probing)

        def typed_connection() -> Connection:
            option = chosen()
            base = endpoint(option)
            name = model.text().strip() or option.model
            typed = key.text().strip() or (saved_key if keeps_saved_key(option, base) else "")
            return Connection(typed, base, name)

        def probe_speed():
            nonlocal probing
            from ...live.translate import probe_latency
            if not probe.isEnabled():
                return
            conn = typed_connection()
            probing = True
            refresh()
            status.setText("正在测速…")
            status.show()

            def finish(message):
                nonlocal probing
                if not alive[0]:
                    return
                probing = False
                refresh()
                if typed_connection() == conn:
                    status.setText(message)
                    status.show()
                else:
                    status.hide()

            bg.run_bg(probe_latency, conn,
                      on_done=lambda result: finish(f"首字 {result['ttft_ms']} ms · 全文 {result['total_ms']} ms"),
                      on_error=lambda exc: finish(f"测速失败：{exc}"))

        def choose_provider(_index=0):
            option = chosen()
            custom = option is CUSTOM_PROVIDER
            url_label.setVisible(custom)
            url.setVisible(custom)
            if custom:
                url.setText(current.base_url if custom_saved else "")
                model.setText(current.model if custom_saved else "")
                model.setPlaceholderText("该服务提供的模型名")
            else:
                model.setText(current.model if option is selected else option.model)
                model.setPlaceholderText(option.model)
            status.hide()
            refresh()

        def save_connection():
            if not save.isEnabled():
                return
            new = typed_connection()
            if not new.base_url.lower().startswith(("http://", "https://")):
                status.setText("接入地址要以 http:// 或 https:// 开头")
                status.show()
                return
            if not new.model:
                status.setText("请填写模型名")
                status.show()
                return
            try:
                cfg.save_translation_connection(new)
            except (OSError, ValueError) as error:
                status.setText(str(error) if isinstance(error, ValueError) else "保存失败，请检查配置文件后重试")
                status.show()
                return
            if island:
                island.activity({"glyph": G.TRANSLATE, "title": f"翻译模型：{new.model}"}, 2.2)
            dialog.accept()

        choose_provider()
        providers.currentIndexChanged.connect(choose_provider)
        for field in (key, model, url):
            field.textChanged.connect(refresh)
        probe.clicked.connect(probe_speed)
        refresh()
        (model if saved_key else key).setFocus()
        buttons.accepted.connect(save_connection)
        buttons.rejected.connect(dialog.reject)
        if island:
            island.locks.add("translation-connection")
        try:
            return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        finally:
            alive[0] = False
            if island:
                island.locks.discard("translation-connection")
            dialog.deleteLater()

    def toggle_live(self):
        if not self.ctx.live.running and cfg.get("live.translate.enabled", True):
            self.ensure_connection()
        self.ctx.live.toggle()

    def _word_clicked(self, tok, row):
        self.popup.island = self.island
        self.popup.show_for(tok.lemma or tok.surface, surface=tok.surface, reading=tok.reading, pos=tok.pos,
                            sentence=row.get("jp", ""), sentence_zh=row.get("zh", ""),
                            audio=self.ctx.live.clip(row.get("sid", "")))

    def _sync_clear(self):
        self.clear_btn.setVisible(bool(self.input.toPlainText().strip()))
        self._place_clear()

    def _place_clear(self):
        self.clear_btn.move(self.input.width() - self.clear_btn.width() - 8, 6)
        self.clear_btn.raise_()

    def eventFilter(self, obj, ev):
        if obj is self.input and ev.type() == QtCore.QEvent.Type.Resize:
            self._place_clear()
        return False

    def _clear_all(self):
        self.ctx.translate.cancel() if hasattr(self.ctx.translate, "cancel") else None
        self.req = -1
        self.input.clear()
        self.out.setText("")
        self.state.setText("")

    def show_live(self):
        self.mode.set_index(1)
        self.modes.setCurrentIndex(1)

    def set_live_display(self, on: bool):
        self.view.setVisible(on)
        self.live_elsewhere.setVisible(not on)

    def _live_state(self, on: bool):
        self.live_btn.set_text("停止聆听" if on else "开始聆听")
        self.live_btn.primary = not on
        self.live_btn.update()
        if on:
            self.live_status.setText("正在聆听系统声音…")
        else:
            self.level.set_level(0)
            self.live_status.setText("")

    # ---------- 识别语种 ----------
    def _current_lang(self) -> str:
        code = str(cfg.get("live.asr.language") or "ja")
        return code if code in dict(LANGUAGE_OPTIONS) else "ja"

    def _sync_lang(self):
        """把配置里的语种同步到选择器、点词开关和提示文字。"""
        code = self._current_lang()
        if code == self._language:
            return
        self._language = code
        self.lang_sel.set_index([c for c, _ in LANGUAGE_OPTIONS].index(code))
        name = dict(LANGUAGE_OPTIONS)[code]
        self.view.clickable = code in ("ja", "auto")    # 分词查词只有日语有（自动模式按行判断）
        if code == "auto":
            self.view.hint = "播放外语视频后点「开始聆听」（自动判断语种）"
        else:
            self.view.hint = f"播放{name}视频后点「开始聆听」"
        self.view.update()

    def _pick_lang(self, index: int):
        if not 0 <= index < len(LANGUAGE_OPTIONS):
            return
        code, name = LANGUAGE_OPTIONS[index]
        if code == self._current_lang():
            return
        cfg.set("live.asr.language", code, True)
        self._sync_lang()
        if self.ctx.live.running:               # 正在听：换模型后自动重开
            self.ctx.live.stop()
            self.ctx.live.start_listen()
        self.live_status.setText(f"识别语种：{name}")

    def _style(self):
        palette = theme_for(self)
        self.out.setStyleSheet(f"color:{palette.qss_color(palette.text())};background:transparent")

    def go(self):
        txt = self.input.toPlainText().strip()
        if txt:
            self.ctx.actions.translate_text(txt, show_page=True)

    def start(self, text: str, req: int):
        if self.input.toPlainText().strip() != text.strip():
            self.input.setPlainText(text)
        self.req = req
        self._buf = ""
        self.out.setText("")
        self.state.setText("翻译中…")

    def _delta(self, rid, piece):
        if rid == self.req:
            self._buf += piece
            self.out.setText(self._buf)

    def _done(self, rid, text):
        if rid == self.req:
            self.out.setText(text)
            self.state.setText("")

    def _fail(self, rid, err):
        if rid == self.req:
            self.state.setText("")
            self.out.setText(f"翻译失败：{err}")

    def _copy(self):
        QtGui.QGuiApplication.clipboard().setText(self.out.text())
        self.state.setText("已复制")
