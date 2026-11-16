# -*- coding: utf-8 -*-
"""控制中心：音量、各屏幕亮度、WLAN、蓝牙、Clash Verge 和 VPN。"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ...core.bg import launch
from ..theme import G, W, theme, ui_font, theme_for, connect_theme
from ..widgets import Card, IconButton, Label, PillButton, ScrollArea, Segmented, Slider, Tile, fmt_bytes, hbox, vbox
from .base import Page

Qt = QtCore.Qt
MODES = [("rule", "规则"), ("global", "全局"), ("direct", "直连")]


def short_node(name: str) -> str:
    """"JMS-1411881@c51s2.xxx:443" -> "c51s2"（@ 后面 5 个字符）。"""
    if "@" in name:
        return name.split("@", 1)[1][:5]
    return name


def _close_on_outside_click(m: QtWidgets.QMenu):
    """灵动岛不抢焦点，Qt 的"点外面关闭菜单"不会触发；这里轮询鼠标键，点到菜单外就关闭。"""
    import ctypes
    u32 = ctypes.WinDLL("user32")
    st = {"armed": False}
    t = QtCore.QTimer(m)
    t.setInterval(25)

    def down() -> bool:
        return any(u32.GetAsyncKeyState(k) & 0x8000 for k in (0x01, 0x02, 0x04))

    def tick():
        if not m.isVisible():
            t.stop()
            return
        if not st["armed"]:
            st["armed"] = not down()          # 先等打开菜单的那次按键松开
            return
        if down():
            pos = QtGui.QCursor.pos()
            menus = [w for w in QtWidgets.QApplication.topLevelWidgets()
                     if isinstance(w, QtWidgets.QMenu) and w.isVisible()]
            if not any(w.geometry().contains(pos) for w in menus):
                m.close()
                t.stop()
    t.timeout.connect(tick)
    m.aboutToShow.connect(lambda: (st.update(armed=False), t.start()))
    m.aboutToHide.connect(t.stop)


def styled_menu(parent=None) -> QtWidgets.QMenu:
    m = QtWidgets.QMenu(parent)
    _close_on_outside_click(m)
    m.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    m.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    m.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
    bg, fg, sel = theme_for(parent).menu_qss()
    m.setStyleSheet(
        f"QMenu{{background:{bg};border-radius:14px;padding:6px;color:{fg};font-family:'HarmonyOS Sans','Microsoft YaHei UI';font-size:13px}}"
        f"QMenu::item{{padding:6px 22px 6px 14px;border-radius:9px}}"
        f"QMenu::item:selected{{background:{sel}}}"
        f"QMenu::item:disabled{{color:gray}}"
        "QMenu::separator{height:6px;background:transparent}"
        "QMenu::indicator{width:0}")
    return m


class ControlsPage(Page):
    key, glyph, title = "ctl", G.SLIDERS, "控制"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.vol = Slider(G.VOLUME, height=44, icon_button=True)
        self.vol.title = "音量"
        self.vol.setAccessibleName("音量")
        self._brightness_sliders: dict[str, Slider] = {}
        self._brightness_empty = Slider(G.SUN, height=44)
        self._brightness_empty.title = "亮度"
        self._brightness_empty.label = "检测中"
        self._brightness_empty.setEnabled(False)
        self.vol.moved.connect(self._set_vol)
        self.vol.icon_clicked.connect(lambda: ctx.audio.toggle_mute())
        self.wifi = Tile(G.WIFI, "WLAN", height=44)
        self.bt = Tile(G.BT, "蓝牙", height=44)
        self.proxy = Tile(G.GLOBE, "系统代理", height=44)
        self.tun = Tile(G.BOLT, "TUN 模式", height=44)
        self._radio = {}
        self._clash = {}
        self.wifi.clicked.connect(lambda: self._toggle("wifi"))
        self.bt.clicked.connect(lambda: self._toggle("bt"))
        self.proxy.clicked.connect(lambda: ctx.clash.set_sysproxy(not self._clash.get("sysproxy")))
        self.tun.clicked.connect(lambda: ctx.clash.set_tun(not self._clash.get("tun")))
        grid = QtWidgets.QGridLayout()
        self._grid = grid
        grid.setSpacing(6)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.vol, 0, 0)
        grid.addWidget(self._brightness_empty, 1, 0)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        for i, t in enumerate((self.wifi, self.bt, self.proxy, self.tun)):
            grid.addWidget(t, i // 2, i % 2 + 1)

        # Clash 卡片
        self.card = Card(radius=18)
        self.mode = Segmented([n for _, n in MODES])
        self.mode.setFixedWidth(168)
        self.mode.changed.connect(lambda i: ctx.clash.set_mode(MODES[i][0]))
        self.node = PillButton("节点", G.GLOBE, height=28)
        self.node.clicked.connect(self._node_menu)
        self.node.setMaximumWidth(190)
        self.speed = Label("", 11.5, level=2, num=True)
        self.clash_state = Label("Clash Verge", 13, W.DemiBold)
        openv = IconButton(G.OPEN, 28, 12, "打开 Clash Verge")
        openv.clicked.connect(lambda: ctx.clash.open_app())
        self.card.setLayout(vbox(hbox(self.clash_state, None, self.speed, openv, spacing=6),
                                 hbox(self.mode, None, self.node, spacing=8), spacing=4, margins=(12, 6, 8, 6)))
        self.vpn_card = Card(radius=18)
        self.vpn_title = Label("VPN", 13, W.DemiBold)
        self.vpn_status = Label("", 11.5, level=2)
        self.vpn_name = Label("", 12, level=2)
        self.vpn_settings = IconButton(G.OPEN, 28, 12, "打开 Windows 网络设置")
        self.vpn_settings.clicked.connect(self._open_network_settings)
        self.vpn_card.setLayout(vbox(hbox(self.vpn_title, None, self.vpn_status, self.vpn_settings, spacing=6),
                                     self.vpn_name, spacing=6, margins=(14, 10, 10, 10)))
        for w in (self.card, self.vpn_card, self.proxy, self.tun):
            w.hide()
        scroll = ScrollArea()
        scroll.inner.setLayout(vbox(grid, self.card, self.vpn_card, None, spacing=6))
        self.setLayout(vbox(scroll, spacing=0))

    def _toggle(self, kind: str):
        """可用就切换开关；检测不到（台式机没蓝牙 / 驱动不支持）时打开对应的系统设置页。"""
        import os
        if not self._radio.get(f"{kind}_available", False):
            os.startfile("ms-settings:" + ("bluetooth" if kind == "bt" else "network-wifi"))  # noqa: S606
            if self.island:
                self.island.collapse()
            return
        if kind == "bt":
            self.ctx.radio.set_bluetooth(not self._radio.get("bt_on"))
        else:
            self.ctx.radio.set_wifi(not self._radio.get("wifi_on"))

    def _set_vol(self, v: float):
        a = self.ctx.audio
        a.set_volume(v)
        if v > 0.001 and a.muted():
            a.set_muted(False)          # 调大音量时顺便取消静音（否则看起来"没反应"）

    # --- 数据 ---
    def set_volume(self, v: float, muted: bool):
        self.vol.glyph = G.MUTE if muted else (G.VOLUME_LOW if v < 0.34 else G.VOLUME)
        self.vol.label = "静音" if muted else ""
        self.vol.set_value(v)

    def set_brightness(self, mons: list):
        monitor_ids = tuple(m["id"] for m in mons)
        if monitor_ids != tuple(self._brightness_sliders):
            for mid, slider in self._brightness_sliders.items():
                self._grid.removeWidget(slider)
                if mid not in monitor_ids:
                    slider.hide()
                    slider.deleteLater()
            sliders = {}
            for i, mid in enumerate(monitor_ids):
                slider = self._brightness_sliders.get(mid)
                if slider is None:
                    slider = Slider(G.SUN, height=44)
                    slider.moved.connect(lambda v, monitor_id=mid: self.ctx.brightness.set(monitor_id, round(v * 100)))
                col = i % 2
                self._grid.addWidget(slider, 1 + i // 2, col, 1, 1 + col)
                sliders[mid] = slider
            self._brightness_sliders = sliders
            radio_row = 1 if len(mons) < 2 else 1 + (len(mons) + 1) // 2
            self._grid.addWidget(self.proxy, radio_row, 1)
            self._grid.addWidget(self.tun, radio_row, 2)
        self._brightness_empty.setVisible(not mons)
        self._brightness_empty.label = "未检测到"
        self._brightness_empty.update()
        for m in mons:
            slider = self._brightness_sliders[m["id"]]
            value = m.get("brightness")
            adjustable = m.get("controllable", value is not None)
            slider.title = m["name"]
            slider.setAccessibleName(f"{slider.title}亮度")
            slider.setToolTip(slider.title if adjustable else f"{slider.title}无法调节亮度")
            slider.setEnabled(adjustable)
            slider.label = "" if value is not None else ("—" if adjustable else "不支持")
            slider.set_value((value or 0) / 100)
            slider.update()

    def set_radio(self, d: dict):
        self._radio = d
        self.wifi.set_state(bool(d.get("wifi_on")),
                            (d.get("wifi_ssid") or ("已开启" if d.get("wifi_on") else "已关闭"))
                            + (f" · {d['wifi_signal']}%" if d.get("wifi_signal") and d.get("wifi_ssid") else ""),
                            d.get("wifi_available", True))
        bt_ok = d.get("bt_available", True)
        self.bt.set_state(bool(d.get("bt_on")),
                          ("已开启" if d.get("bt_on") else "已关闭") if bt_ok else "未检测到 · 点击打开设置", bt_ok)
        self._update_network_cards()

    def set_clash(self, d: dict):
        self._clash = d
        ok = d.get("ok")
        self.proxy.set_state(bool(d.get("sysproxy")), "127.0.0.1" if d.get("sysproxy") else "已关闭")
        self.tun.set_state(bool(d.get("tun")), "已开启" if d.get("tun") else "已关闭", bool(ok))
        if ok:
            self.clash_state.setText("Clash Verge")
            self.speed.setText(f"↑ {fmt_bytes(d.get('up', 0), True)}   ↓ {fmt_bytes(d.get('down', 0), True)}")
            keys = [k for k, _ in MODES]
            if d.get("mode") in keys:
                self.mode.set_index(keys.index(d["mode"]))
            delay = d.get("delay")
            node = short_node(d.get("node") or "—")
            self.node.set_text(f"{node}" + (f"  {delay}ms" if delay else ""))
        else:
            self.clash_state.setText("Clash 未运行")
            self.speed.setText(d.get("error") or "")
            self.node.set_text("—")
        self._update_network_cards()

    def _update_network_cards(self):
        clash = bool(self._clash.get("ok"))
        for widget in (self.card, self.proxy, self.tun):
            widget.setVisible(clash)
        connections = self._radio.get("vpn_connections") or ()
        proxy_on = bool(self._clash.get("sysproxy"))
        show_vpn = not clash and bool(connections or proxy_on)
        self.vpn_card.setVisible(show_vpn)
        if not show_vpn:
            return
        if connections:
            name, connected = connections[0]
            self.vpn_title.setText("VPN")
            self.vpn_status.setText("已连接" if connected else "隧道接口活动")
            self.vpn_name.setText(name if len(connections) == 1 else f"{name} · 共 {len(connections)} 个")
            self.vpn_name.setToolTip("\n".join(name for name, _ in connections))
        else:
            self.vpn_title.setText("系统代理")
            self.vpn_status.setText("已开启")
            self.vpn_name.setText("Windows 手动代理")
            self.vpn_name.setToolTip("")

    def _open_network_settings(self):
        connections = self._radio.get("vpn_connections") or ()
        if connections:
            page = "network-vpn" if connections[0][1] else "network-status"
        else:
            page = "network-proxy"
        if launch(f"ms-settings:{page}") and self.island:
            self.island.collapse()

    def _node_menu(self):
        d = self._clash
        nodes = d.get("nodes") or []
        if not nodes:
            return
        m = styled_menu(self)
        group = d.get("group")
        for n in nodes[:60]:
            a = m.addAction(("✓  " if n == d.get("node") else "    ") + short_node(n) + ("" if "@" not in n else f"    {n.split('@')[0]}"))
            a.triggered.connect(lambda _=False, n=n: self.ctx.clash.select(group, n))
        m.addSeparator()
        t = m.addAction("    测试当前节点延迟")
        t.triggered.connect(lambda: self.ctx.clash.test_delay(d.get("node"), lambda ms: self.node.set_text(
            f"{short_node(d.get('node') or '')}  {ms}ms" if ms else f"{short_node(d.get('node') or '')}  超时")))
        if self.island:
            self.island.locks.add("menu")
            m.aboutToHide.connect(lambda: self.island.locks.discard("menu"))
        m.popup(QtGui.QCursor.pos())

    def on_show(self):
        super().on_show()
        self.ctx.brightness.refresh()
        self.ctx.radio.refresh()
