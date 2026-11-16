# -*- coding: utf-8 -*-
"""天气页面。"""
from __future__ import annotations

import datetime as dt

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.config import cfg
from ..anim import clamp
from ..theme import G, W, theme, ui_font, theme_for, connect_theme
from ..widgets import (Card, Hoverable, IconButton, Label, LineEdit, PillButton, ScrollArea, Segmented,
                       Themed, hbox, painter, vbox)
from .base import Page

Qt = QtCore.Qt


# ======================================================================== 天气
class GlyphLabel(QtWidgets.QWidget, Themed):
    def __init__(self, px: int, parent=None):
        super().__init__(parent)
        self.px = px
        self.glyph = ("", "Segoe Fluent Icons")
        self.setFixedSize(px + 10, px + 10)
        self._hook_theme()

    def set_glyph(self, g):
        self.glyph = g
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        f = QtGui.QFont()
        f.setFamilies([self.glyph[1], "Segoe Fluent Icons", "Segoe UI Symbol"])
        f.setPixelSize(self.px)
        p.setFont(f)
        p.setPen(palette.text(0.9))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.glyph[0])


class Hourly(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.tz = None
        self.setFixedHeight(51)
        self._hook_theme()

    def set_items(self, items):
        self.items = items[:8]
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        from ...services.weather import weather_glyph
        p = painter(self)
        n = max(1, len(self.items))
        cw = self.width() / n
        for i, it in enumerate(self.items):
            x = i * cw
            p.setFont(ui_font(11, num=True))
            p.setPen(palette.text3())
            p.drawText(QtCore.QRectF(x, 0, cw, 14), Qt.AlignmentFlag.AlignCenter,
                       (dt.datetime.fromtimestamp(it.get("time", 0) + self.tz, dt.timezone.utc) if self.tz is not None
                        else dt.datetime.fromtimestamp(it.get("time", 0))).strftime("%H时"))
            g, fam = weather_glyph(it.get("icon"))
            f = QtGui.QFont()
            f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            f.setPixelSize(16)
            p.setFont(f)
            p.setPen(palette.text(0.9))
            p.drawText(QtCore.QRectF(x, 14, cw, 20), Qt.AlignmentFlag.AlignCenter, g)
            temp = it.get("temp")
            value = f"{round(temp)}°" if temp is not None else "—"
            pop = it.get("pop") or 0
            if pop >= 0.2:
                value += f"  {round(pop * 100)}%"
            p.setFont(ui_font(11 if pop >= 0.2 else 12, W.DemiBold, num=True))
            p.setPen(palette.text())
            p.drawText(QtCore.QRectF(x, 34, cw, 17), Qt.AlignmentFlag.AlignCenter, value)


class Daily(QtWidgets.QWidget, Themed):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.today = dt.date.today()
        self._hook_theme()

    def set_items(self, items):
        self.items = [day for day in items if day.get("min") is not None and day.get("max") is not None][:5]
        self.setToolTip("* 由 Open-Meteo 补充的预报" if any(day.get("source") == "Open-Meteo" for day in self.items) else "")
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        from ...services.weather import weather_glyph
        if not self.items:
            return
        p = painter(self)
        lo = min(i["min"] for i in self.items)
        hi = max(i["max"] for i in self.items)
        supplemented = any(day.get("source") == "Open-Meteo" for day in self.items)
        footer = 12 if supplemented else 0
        rh = (self.height() - footer) / 5
        wk = "一二三四五六日"
        for k, it in enumerate(self.items):
            y = k * rh
            d = it.get("date")
            try:
                day = dt.date.fromisoformat(str(d))
                name = "今天" if day == self.today else "周" + wk[day.weekday()]
            except Exception:  # noqa: BLE001
                name = str(d)
            if it.get("source") == "Open-Meteo":
                name += "*"
            p.setFont(ui_font(11, W.Medium))
            p.setPen(palette.text())
            p.drawText(QtCore.QRectF(0, y, 32, rh), Qt.AlignmentFlag.AlignVCenter, name)
            g, fam = weather_glyph(it.get("icon"))
            f = QtGui.QFont()
            f.setFamilies([fam, "Segoe Fluent Icons", "Segoe UI Symbol"])
            f.setPixelSize(13)
            p.setFont(f)
            p.drawText(QtCore.QRectF(32, y, 20, rh), Qt.AlignmentFlag.AlignCenter, g)
            p.setFont(ui_font(11, num=True))
            p.setPen(palette.text2())
            p.drawText(QtCore.QRectF(52, y, 27, rh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{round(it['min'])}°")
            p.setPen(palette.text())
            p.drawText(QtCore.QRectF(self.width() - 27, y, 27, rh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{round(it['max'])}°")
            bx, bw = 85, max(0, self.width() - 117)
            span = max(1e-6, hi - lo)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.track())
            p.drawRoundedRect(QtCore.QRectF(bx, y + rh / 2 - 2.5, bw, 5), 2.5, 2.5)
            x0 = bx + bw * (it["min"] - lo) / span
            x1 = bx + bw * (it["max"] - lo) / span
            g2 = QtGui.QLinearGradient(bx, 0, bx + bw, 0)
            g2.setColorAt(0, QtGui.QColor("#5ac8fa"))
            g2.setColorAt(1, QtGui.QColor("#ff9f0a"))
            p.setBrush(g2)
            p.drawRoundedRect(QtCore.QRectF(min(x0, bx + bw - 3), y + rh / 2 - 2.5, max(3, x1 - x0), 5), 2.5, 2.5)
        if supplemented:
            p.setFont(ui_font(9.5))
            p.setPen(palette.text3())
            p.drawText(QtCore.QRectF(0, self.height() - footer, self.width(), footer),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "* Open-Meteo")


class CityChip(Hoverable):
    """城市胶囊：名称 + 当前温度；选中时半透明白底。"""
    def __init__(self, idx: int, name: str, parent=None):
        super().__init__(parent)
        self.idx, self.name = idx, name
        self.temp = ""
        self.glyph = ("", "Segoe Fluent Icons")
        self.selected = False
        self.default = idx == 0
        self.setFixedHeight(28)
        self._fit()

    def _fit(self):
        fm = QtGui.QFontMetrics(ui_font(12.5, W.DemiBold))
        self.setFixedWidth(min(150, fm.horizontalAdvance(self.name) + fm.horizontalAdvance(self.temp or "--°") + 50))

    def set_info(self, temp: str, glyph):
        self.temp, self.glyph = temp, glyph
        self._fit()
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        from ..widgets import painter as _p
        p = _p(self)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.active_pill())
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        elif self._hover.x > 0.01:
            c = palette.hover()
            c.setAlphaF(c.alphaF() * self._hover.x)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        f = QtGui.QFont()
        f.setFamilies([self.glyph[1], "Segoe Fluent Icons", "Segoe UI Symbol"])
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(palette.text(0.9))
        p.drawText(QtCore.QRectF(r.x() + 8, r.y(), 18, r.height()), Qt.AlignmentFlag.AlignCenter, self.glyph[0])
        p.setFont(ui_font(12.5, W.DemiBold))
        p.setPen(palette.pill_text() if self.selected else palette.text(0.75))
        fm = QtGui.QFontMetrics(p.font())
        x = r.x() + 30
        temp_font = ui_font(12, W.Medium, num=True)
        temp = self.temp or "--°"
        temp_w = QtGui.QFontMetrics(temp_font).horizontalAdvance(temp) + 2
        name = fm.elidedText(self.name, Qt.TextElideMode.ElideRight, int(r.right() - x - temp_w - 14))
        name_w = fm.horizontalAdvance(name) + 2
        p.drawText(QtCore.QRectF(x, r.y(), name_w, r.height()), Qt.AlignmentFlag.AlignVCenter, name)
        x += name_w + 4
        p.setFont(temp_font)
        p.setPen(palette.text2())
        p.drawText(QtCore.QRectF(x, r.y(), temp_w, r.height()), Qt.AlignmentFlag.AlignVCenter, temp)
        if self.default:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.on_color())
            p.drawEllipse(QtCore.QPointF(r.right() - 6, r.y() + 6), 2.5, 2.5)


def _fmt_clock(ts, tz_off):
    if not ts:
        return "--"
    return dt.datetime.fromtimestamp(int(ts) + int(tz_off or 0), dt.timezone.utc).strftime("%H:%M")


def _wind_dir(deg):
    if deg is None:
        return ""
    return ["北", "东北", "东", "东南", "南", "西南", "西", "西北"][int(((deg + 22.5) % 360) // 45)] + "风"


class WeatherPage(Page):
    key, glyph, title = "weather", G.CLOUD, "天气"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.sel = 0
        self.data: list[dict] = []
        self.chips: list[CityChip] = []
        # 顶部：城市胶囊 + 添加
        self.chip_scroll = ScrollArea()
        self.chip_scroll.setFixedHeight(28)
        self.chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chip_scroll.viewport().installEventFilter(self)
        self.chip_row = QtWidgets.QHBoxLayout(self.chip_scroll.inner)
        self.chip_row.setSpacing(4)
        self.chip_row.setContentsMargins(0, 0, 0, 0)
        self.chip_row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.add_btn = IconButton(G.ADD, 28, 12, "添加城市")
        self.add_btn.clicked.connect(self._start_add)
        self.add_edit = LineEdit("城市名")
        self.add_edit.setFixedWidth(164)
        self.add_edit.setFixedHeight(28)
        self.add_edit.hide()
        self.add_edit.focus_wanted.connect(self.want_focus)
        self.add_edit.returnPressed.connect(self._do_add)
        self.add_edit.installEventFilter(self)
        refresh = IconButton(G.REFRESH, 28, 12, "刷新")
        refresh.clicked.connect(lambda: ctx.weather.refresh())
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(0, 0, 0, 0)
        self.chip_row.addWidget(self.add_btn)
        head.addWidget(self.chip_scroll, 1)
        head.addWidget(self.add_edit)
        self.msg = Label("", 11.5, level=3)
        self.msg.setMaximumWidth(128)
        head.addWidget(self.msg)
        head.addWidget(refresh)

        # 详情
        self.icon = GlyphLabel(29)
        self.temp = Label("--°", 33, W.Light, num=True)
        self.temp.setFixedHeight(42)
        self.desc = Label("", 13, W.Medium)
        self.city = Label("", 11, level=2)
        self.source = PillButton("Open-Meteo", height=20)
        self.source.setToolTip("天气数据来源：Open-Meteo（CC BY 4.0）")
        self.source.clicked.connect(self._open_source)
        self.hourly = Hourly()
        self.daily = Daily()
        self.daily.setFixedWidth(172)
        self.facts = Facts()
        left = QtWidgets.QWidget()
        left.setFixedWidth(110)
        left.setLayout(vbox(hbox(self.icon, (self.temp, 1), spacing=3), self.desc, self.city, None, self.source, spacing=2))
        self.body = QtWidgets.QWidget()
        self.body.setLayout(vbox((hbox(left, (self.facts, 1), self.daily, spacing=10), 1), self.hourly, spacing=4))
        self.err = Label("", 13, level=2, align=Qt.AlignmentFlag.AlignCenter, wrap=True)
        self.err.hide()
        self.setLayout(vbox(head, (self.body, 1), (self.err, 1), spacing=5))
        ctx.weather.cities_updated.connect(self.set_cities)
        ctx.weather.city_added.connect(self._added)

    # ---------- 城市 ----------
    def set_data(self, d: dict):
        """兼容：第一个城市的数据 / 错误。"""
        if not d.get("ok") and not self.data:
            self.err.setText(d.get("error") or "暂无天气数据")
            self.err.show()
            self.body.hide()

    def set_cities(self, data: list):
        from ...services.weather import weather_glyph
        self.data = data
        names = [d.get("name", "?") for d in data]
        if [c.name for c in self.chips] != names:
            for c in self.chips:
                self.chip_row.removeWidget(c)
                c.hide()
                c.deleteLater()
            self.chips = []
            for i, n in enumerate(names):
                c = CityChip(i, n)
                c.clicked.connect(lambda i=i: self.select(i))
                c.right_clicked.connect(lambda i=i: self._menu(i))
                self.chip_row.insertWidget(self.chip_row.count() - 1, c)
                self.chips.append(c)
        for c, d in zip(self.chips, data):
            c.default = c.idx == 0
            c.set_info(f"{round(d['temp'])}°" if d.get("temp") is not None else "--°", weather_glyph(d.get("icon")))
        self.sel = min(self.sel, max(0, len(data) - 1))
        self.select(self.sel)

    def select(self, i: int):
        self.sel = i
        for c in self.chips:
            c.selected = c.idx == i
            c.update()
        if 0 <= i < len(self.chips):
            self.chip_scroll.ensureWidgetVisible(self.chips[i], 3, 0)
        if not self.data:
            return
        self._show(self.data[i])

    def _show(self, d: dict):
        from ...services.weather import weather_glyph
        if d.get("temp") is None:
            self.err.setText(d.get("error") or "暂无数据")
            self.err.show()
            self.body.hide()
            return
        self.err.hide()
        self.body.show()
        source = d.get("source") or ""
        self.source.setVisible(source in ("Open-Meteo", "OpenWeatherMap"))
        self.source.set_text("OpenWeather" if source == "OpenWeatherMap" else source)
        self.source.setToolTip("天气数据来源：" + source + ("（CC BY 4.0）" if source == "Open-Meteo" else ""))
        self.msg.setText(d.get("error") or "")
        self.icon.set_glyph(weather_glyph(d.get("icon")))
        self.temp.setText(f"{round(d.get('temp', 0))}°")
        self.desc.setText(d.get("desc", ""))
        lo, hi = d.get("temp_min"), d.get("temp_max")
        rng = f"  {round(lo)}° / {round(hi)}°" if lo is not None and hi is not None else ""
        self.city.setText(f"{d.get('name', '')}{rng}")
        tz = d.get("tz_offset", 0)
        feels = d.get("feels_like")
        wind = d.get("wind")
        vis = d.get("visibility")

        def fact(key, unit):
            value = d.get(key)
            return f"{value}{unit}" if value is not None else "--"

        self.facts.set_items([
            ("体感", f"{round(feels)}°" if feels is not None else "--"),
            ("湿度", fact("humidity", "%")),
            ("风", f"{_wind_dir(d.get('wind_deg'))} {wind}m/s" if wind is not None else "--"),
            ("气压", fact("pressure", "hPa")),
            ("能见度", f"{vis / 1000:.0f}km" if vis is not None else "--"),
            ("云量", fact("clouds", "%")),
            ("日出", _fmt_clock(d.get("sunrise"), tz)),
            ("日落", _fmt_clock(d.get("sunset"), tz)),
        ])
        self.hourly.tz = tz
        self.hourly.set_items(d.get("hourly") or [])
        self.daily.today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tz)).date()
        self.daily.set_items(d.get("daily") or [])

    def _open_source(self):
        source = self.data[self.sel].get("source") if 0 <= self.sel < len(self.data) else "Open-Meteo"
        url = {"Open-Meteo": "https://open-meteo.com/", "OpenWeatherMap": "https://openweathermap.org/"}.get(source)
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _menu(self, i: int):
        from .system import styled_menu
        m = styled_menu(self)
        if i > 0:
            m.addAction("设为默认").triggered.connect(lambda: self._default(i))
        a = m.addAction("删除该城市")
        a.setEnabled(len(self.data) > 1)
        a.triggered.connect(lambda: self._remove(i))
        if self.island:
            self.island.locks.add("menu")
            m.aboutToHide.connect(lambda: self.island.locks.discard("menu"))
        m.popup(QtGui.QCursor.pos())

    def _default(self, i):
        self.sel = 0
        self.ctx.weather.make_default(i)

    def _remove(self, i):
        if self.sel >= i and self.sel > 0:
            self.sel -= 1
        self.ctx.weather.remove_city(i)

    def _start_add(self):
        self.chip_scroll.hide()
        self.add_edit.clear()
        self.add_edit.show()
        self.want_focus()
        self.add_edit.setFocus()
        # 点到输入框以外的任何地方就取消（灵动岛内外都算）
        import ctypes
        u32 = ctypes.WinDLL("user32")
        st = {"armed": False}
        if getattr(self, "_add_watch", None) is None:
            self._add_watch = QtCore.QTimer(self, interval=25)

        def tick():
            if not self.add_edit.isVisible():
                self._add_watch.stop()
                return
            down = any(u32.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02))
            if not st["armed"]:
                st["armed"] = not down
                return
            if down:
                r = QtCore.QRect(self.add_edit.mapToGlobal(QtCore.QPoint(0, 0)), self.add_edit.size())
                if not r.contains(QtGui.QCursor.pos()):
                    self._end_add()
        try:
            self._add_watch.timeout.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._add_watch.timeout.connect(tick)
        self._add_watch.start()

    def _end_add(self):
        self.add_edit.hide()
        self.chip_scroll.show()
        if getattr(self, "_add_watch", None) is not None:
            self._add_watch.stop()
        if self.island is not None:
            self.island.release_focus()

    def _do_add(self):
        q = self.add_edit.text().strip()
        if q:
            self.msg.setText(f"正在查找 {q}…")
            self.ctx.weather.add_city(q)
        self._end_add()

    def _added(self, ok: bool, text: str):
        self.msg.setText(text)
        if ok:
            self.sel = len(self.data)       # 新城市在末尾，刷新后选中它
        QtCore.QTimer.singleShot(4000, self, lambda: self.msg.setText(""))

    def eventFilter(self, obj, ev):
        if obj is self.chip_scroll.viewport():
            if ev.type() == QtCore.QEvent.Type.Wheel:
                bar = self.chip_scroll.horizontalScrollBar()
                if bar.maximum() > 0:
                    bar.setValue(bar.value() - (ev.angleDelta().x() or ev.angleDelta().y()))
                    return True
            return super().eventFilter(obj, ev)
        if obj is self.add_edit and ev.type() == QtCore.QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
            self._end_add()
            return True
        if obj is self.add_edit and ev.type() == QtCore.QEvent.Type.FocusOut and not self.add_edit.text().strip():
            self._end_add()
        return False

    def on_show(self):
        super().on_show()
        if self.data:
            self.select(self.sel)


class Facts(QtWidgets.QWidget, Themed):
    """当前天气中间的两列详情。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[tuple[str, str]] = []
        self._hook_theme()

    def set_items(self, items):
        self.items = items
        self.setToolTip("\n".join(f"{name}  {value}" for name, value in items))
        self.update()

    def paintEvent(self, e):
        palette = theme_for(self)
        p = painter(self)
        cols = 2
        cw = self.width() / cols
        rh = self.height() / 4
        for i, (k, v) in enumerate(self.items[:8]):
            x, y = (i % cols) * cw, (i // cols) * rh
            p.setFont(ui_font(10.5))
            p.setPen(palette.text3())
            key_w = QtGui.QFontMetrics(p.font()).horizontalAdvance(k) + 4
            p.drawText(QtCore.QRectF(x, y, key_w, rh), Qt.AlignmentFlag.AlignVCenter, k)
            p.setFont(ui_font(11, W.Medium, num=True))
            p.setPen(palette.text())
            value_w = max(0, int(cw - key_w - 4))
            p.drawText(QtCore.QRectF(x + key_w, y, value_w, rh), Qt.AlignmentFlag.AlignVCenter,
                       QtGui.QFontMetrics(p.font()).elidedText(v, Qt.TextElideMode.ElideRight, value_w))


_ = (cfg, clamp)
