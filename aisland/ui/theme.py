# -*- coding: utf-8 -*-
"""主题：浅色 / 深色毛玻璃配色、字体、图标码位。auto 模式按身后画面亮度自动切换。"""
from __future__ import annotations

import colorsys
import time
import weakref

from PySide6 import QtCore, QtGui

UI_FAMILIES = ["HarmonyOS Sans", "MiSans", "Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI"]
NUM_FAMILIES = ["HarmonyOS Sans", "Segoe UI Variable Display", "Segoe UI"]
ICON_FAMILIES = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]


class G:
    """Segoe Fluent Icons 码位。"""
    HOME = "\uE80F"
    MESSAGE = "\uE8BD"
    MAIL = "\uE715"
    STOCK = "\uEAFC"
    SUN = "\uE706"
    CLOUD = "\uE753"
    SLIDERS = "\uE9E9"
    WINDOWS = "\uE7C4"
    CPU = "\uE950"
    PULSE = "\uE9D9"
    CLIP = "\uE77F"
    COPY = "\uE8C8"
    TRANSLATE = "\uF2B7"
    VOLUME = "\uE767"
    VOLUME_LOW = "\uE993"
    MUTE = "\uE74F"
    WIFI = "\uE701"
    BT = "\uE702"
    GLOBE = "\uE774"
    SNIP = "\uE924"
    PLAY = "\uE768"
    PAUSE = "\uE769"
    PREV = "\uE892"
    NEXT = "\uE893"
    PIN = "\uE718"
    UNPIN = "\uE77A"
    CLOSE = "\uE711"
    REFRESH = "\uE72C"
    POWER = "\uE7E8"
    SPEED = "\uEC4A"
    BOLT = "\uE945"
    OPEN = "\uE8A7"
    APPS = "\uECA5"
    GEAR = "\uE713"
    IMAGE = "\uE91B"
    CHECK = "\uE73E"
    BELL = "\uEA8F"
    CHAT = "\uE8F2"
    MUSIC = "\uE8D6"
    MONITOR = "\uE7F4"
    TRASH = "\uE74D"
    UP = "\uE74A"
    DOWN = "\uE74B"
    ADD = "\uE710"
    POINTER = "\uE8B0"
    EYE = "\uE890"
    EYE_OFF = "\uED1A"
    SEARCH = "\uE721"
    LIVE = "\uE720"   # 麦克风


def ui_font(px: float, weight: QtGui.QFont.Weight = QtGui.QFont.Weight.Normal, num: bool = False) -> QtGui.QFont:
    f = QtGui.QFont()
    f.setFamilies(NUM_FAMILIES + UI_FAMILIES if num else UI_FAMILIES)
    f.setPixelSize(max(1, round(px)))
    f.setWeight(weight)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferNoHinting)
    return f


def icon_font(px: float) -> QtGui.QFont:
    f = QtGui.QFont()
    f.setFamilies(ICON_FAMILIES)
    f.setPixelSize(max(1, round(px)))
    return f


W = QtGui.QFont.Weight


# 每个主题一套色板。文字色 text + 次级 / 弱化透明度 t2 / t3，均按各自玻璃底色单独调过对比度。
PALETTES = {
    "dark": {
        "dark": True, "text": (255, 255, 255), "t2": 0.66, "t3": 0.45,
        "tint": (28, 28, 34, 120), "card": (255, 255, 255, 26), "hover": (255, 255, 255, 30),
        "pill": (255, 255, 255, 52), "track": (255, 255, 255, 34), "fill": (255, 255, 255, 235),
        "on": "#3b82f6", "up": "#ff5a52", "down": "#35c98e",
        "rim1": (255, 255, 255, 60), "rim2": (255, 255, 255, 14), "outline": (0, 0, 0, 70),
        "menu": ("rgba(36,36,40,240)", "white", "rgba(255,255,255,40)"),
    },
    # 浅色：玻璃底做得更"实"（不透出身后的深色），文字更深，保证股票 / 天气等小字可读
    "light": {
        "dark": False, "text": (16, 18, 24), "t2": 0.74, "t3": 0.56,
        "tint": (238, 240, 245, 205), "card": (255, 255, 255, 150), "hover": (0, 0, 0, 14),
        "pill": (255, 255, 255, 250), "track": (0, 0, 0, 30), "fill": (255, 255, 255, 255),
        "on": "#2f6fe0", "up": "#d42c25", "down": "#08875a",
        "rim1": (255, 255, 255, 200), "rim2": (255, 255, 255, 60), "outline": (0, 0, 0, 40),
        "menu": ("rgba(248,248,250,245)", "#14161c", "rgba(0,0,0,18)"),
    },
    # 胭脂撞色：墨黑玻璃 + 胭脂粉 FF7CA2 强调色，正文白色
    "rouge": {
        "dark": True, "text": (255, 255, 255), "t2": 0.70, "t3": 0.50,
        "tint": (0, 0, 0, 205), "card": (255, 255, 255, 24), "hover": (255, 255, 255, 34),
        "pill": (255, 124, 162, 235), "pill_fg": "#000000", "track": (255, 255, 255, 36), "fill": "#FF7CA2",
        "on": "#FF7CA2", "on_fg": "#000000", "up": "#FF7CA2", "down": "#5fe0b0",
        "rim1": (255, 124, 162, 170), "rim2": (255, 124, 162, 40), "outline": (0, 0, 0, 140),
        "menu": ("rgba(8,8,10,245)", "white", "rgba(255,124,162,90)"),
    },
    # 晴空渐变：晴空亮蓝 32ABFD → 柔白浅杏 F7EFE1，正文深海军蓝（两端对比度都足够）
    "sky": {
        "dark": False, "text": (12, 32, 56), "t2": 0.78, "t3": 0.60,
        "tint": (50, 171, 253, 215), "tint2": (247, 239, 225, 230),
        "card": (255, 255, 255, 105), "hover": (255, 255, 255, 120),
        "pill": (255, 255, 255, 190), "track": (12, 32, 56, 34), "fill": (255, 255, 255, 250),
        "on": "#1667c9", "up": "#d42c25", "down": "#06805a",
        "rim1": (255, 255, 255, 210), "rim2": (255, 255, 255, 70), "outline": (12, 32, 56, 60),
        "menu": ("rgba(247,239,225,248)", "#0c2038", "rgba(50,171,253,70)"),
    },
    # 莫兰迪：起始色板只是兜底，实际底色由身后画面主色实时推导（见 morandi_palette）
    "morandi": {
        "dark": False, "text": (20, 22, 26), "t2": 0.76, "t3": 0.58,
        "tint": (226, 222, 216, 216),
        "card": (255, 255, 255, 150), "hover": (0, 0, 0, 14),
        "pill": (255, 255, 255, 245), "track": (0, 0, 0, 30), "fill": (255, 255, 255, 255),
        "on": "#2f6fe0", "up": "#c8504a", "down": "#3f8a72",
        "rim1": (255, 255, 255, 170), "rim2": (255, 255, 255, 50), "outline": (0, 0, 0, 36),
        "menu": ("rgba(248,248,250,245)", "#14161c", "rgba(0,0,0,16)"),
    },
}

THEME_NAMES = [("auto", "跟随背景自动"), ("morandi", "莫兰迪（跟随背景）"), ("light", "浅色毛玻璃"),
               ("dark", "深色毛玻璃"), ("rouge", "胭脂撞色"), ("sky", "晴空渐变")]


def morandi_palette(hue: float, sat: float, lum: float, dark: bool) -> dict:
    """按背景色相推导莫兰迪色板：低饱和灰调底 + 与底色匹配的文字。

    hue 0..1，sat 0..1（背景饱和度），lum 0..1（背景亮度），dark 决定明暗两套。
    文字 / 强调色沿用已有浅色 / 深色色板的精调值，只替换玻璃底色，保证对比度。
    """
    base = dict(PALETTES["dark" if dark else "light"])
    if sat < 0.06:                 # 背景本身就接近灰：偏一点暖灰，避免死灰
        hue, sat = 0.08, 0.08
    s = max(0.05, min(0.22, sat * 0.30))
    if dark:
        lightness = 0.17 + 0.06 * (1.0 - lum)
        alpha = 224                # 深色底要够实，白字对比度才稳
    else:
        lightness = 0.87 + 0.05 * (1.0 - lum)
        alpha = 208
    r, g, b = colorsys.hls_to_rgb(hue % 1.0, lightness, s)
    base["tint"] = (round(r * 255), round(g * 255), round(b * 255), alpha)
    base.pop("tint2", None)
    base["rim1"] = (255, 255, 255, 64 if dark else 170)
    base["rim2"] = (255, 255, 255, 16 if dark else 50)
    base["outline"] = (0, 0, 0, 90 if dark else 36)
    base["menu"] = (("rgba(26,26,30,242)", "white", "rgba(255,255,255,28)") if dark
                    else ("rgba(248,248,250,245)", "#14161c", "rgba(0,0,0,16)"))
    return base


class Theme(QtCore.QObject):
    changed = QtCore.Signal()
    mode_changed = QtCore.Signal(str)

    def __init__(self, mode: str = "auto", parent=None):
        super().__init__(parent)
        self.mode = mode
        self._lum = 0.5
        self.pal_name = None
        # 莫兰迪：色相 / 饱和度平滑值、明暗状态与量化键（避免每次取样都重建色板）
        self._hue = 0.08
        self._sat = 0.10
        self._mor_dark = False
        self._mor_since = None
        self._mor_key = None
        self._apply(mode if mode in PALETTES else "dark")

    # --- 自动切换（带滞回，避免在中间亮度来回跳） ---
    def feed_luminance(self, lum: float):
        import time as _t
        self._lum = self._lum * 0.8 + lum * 0.2
        if self.mode != "auto":
            return
        elif self.dark and self._lum > 0.64:
            want = False
        elif not self.dark and self._lum < 0.42:
            want = True
        else:
            want = self.dark
        if want == self.dark:
            self._flip_since = None
            return
        now = _t.monotonic()
        if self.mode == "auto":
            # 身后画面要持续越过阈值 3 秒才切换，滚动的网页之类不会让玻璃来回闪
            if getattr(self, "_flip_since", None) is None:
                self._flip_since = now
                return
            if now - self._flip_since < 3.0:
                return
        self._flip_since = None
        self._apply("dark" if want else "light")

    def set_mode(self, mode: str):
        mode_changed = mode != self.mode
        self.mode = mode
        if mode == "morandi":
            # 先给一个中性莫兰迪底，等下一帧背景取样再染上主色
            self._mor_key = None
            self._apply_palette(dict(PALETTES["morandi"]), "morandi")
            if mode_changed:
                self.mode_changed.emit(mode)
            return
        pal = mode if mode in PALETTES else ("dark" if self.dark else "light")
        self._apply(pal)
        if mode_changed:
            self.mode_changed.emit(mode)

    def for_window(self, owner):
        """Share the chosen mode while keeping every window's samples local."""
        local = Theme(self.mode, owner)
        self.mode_changed.connect(local.set_mode)
        return local

    def _apply(self, pal: str):
        self._apply_palette(PALETTES[pal], pal)

    def _apply_palette(self, palette: dict, name: str):
        changed = name != getattr(self, "pal_name", None) or palette is not getattr(self, "pal", None)
        self.pal_name = name
        self.pal = palette
        self.dark = bool(palette["dark"])
        if changed:
            self.changed.emit()

    # --- 莫兰迪：跟随身后画面主色 ---
    def feed_backdrop(self, lum: float, rgb=None):
        """背景亮度 + 主色。自动主题只看亮度；莫兰迪额外用主色推导低饱和底色。"""
        self.feed_luminance(lum)
        if self.mode != "morandi" or not rgb:
            return
        try:
            r, g, b = (max(0.0, min(255.0, float(x))) / 255.0 for x in tuple(rgb)[:3])
        except Exception:  # noqa: BLE001
            return
        hue, sat, _val = colorsys.rgb_to_hsv(r, g, b)
        # 色相走圆环做 EMA，饱和度同样平滑：画面切换时颜色慢慢漂移，不闪
        delta = ((hue - self._hue + 0.5) % 1.0) - 0.5
        self._hue = (self._hue + delta * 0.25) % 1.0
        self._sat = self._sat * 0.8 + sat * 0.2
        dark = self._morandi_dark()
        key = (round(self._hue * 24), round(self._sat * 12), round(self._lum * 12), dark)
        if key == self._mor_key:
            return
        self._mor_key = key
        self._apply_palette(morandi_palette(self._hue, self._sat, self._lum, dark), "morandi")

    def _morandi_dark(self) -> bool:
        """莫兰迪自己的明暗判定：阈值更保守，且要持续 1.2 秒才翻，避免边缘来回跳。"""
        now = time.monotonic()
        current = self._mor_dark
        want = current
        if self._lum > 0.50:
            want = False
        elif self._lum < 0.32:
            want = True
        if want != current:
            if self._mor_since is None:
                self._mor_since = now
            elif now - self._mor_since >= 1.2:
                current = want
                self._mor_since = None
        else:
            self._mor_since = None
        self._mor_dark = current
        return current

    # --- 颜色（按当前主题的色板） ---
    def _c(self, key: str, a: float = 1.0) -> QtGui.QColor:
        c = QtGui.QColor(*self.pal[key]) if isinstance(self.pal[key], tuple) else QtGui.QColor(self.pal[key])
        c.setAlphaF(max(0.0, min(1.0, c.alphaF() * a)))
        return c

    def text(self, a: float = 1.0) -> QtGui.QColor:
        return self._c("text", a)

    def text2(self, a: float = 1.0) -> QtGui.QColor:
        return self._c("text", self.pal["t2"] * a)

    def text3(self, a: float = 1.0) -> QtGui.QColor:
        return self._c("text", self.pal["t3"] * a)

    def tint(self) -> QtGui.QColor:
        return self._c("tint")

    def tint_brush(self, rect: QtCore.QRectF) -> QtGui.QBrush:
        """玻璃色调：渐变主题用纵向渐变，其余为纯色。"""
        g2 = self.pal.get("tint2")
        if not g2:
            return QtGui.QBrush(self.tint())
        g = QtGui.QLinearGradient(rect.left(), rect.top(), rect.right() * 0.25 + rect.left() * 0.75, rect.bottom())
        g.setColorAt(0, self._c("tint"))
        g.setColorAt(1, QtGui.QColor(*g2))
        return QtGui.QBrush(g)

    def card(self, a: float = 1.0) -> QtGui.QColor:
        return self._c("card", a)

    def hover(self) -> QtGui.QColor:
        return self._c("hover")

    def active_pill(self) -> QtGui.QColor:
        """导航当前项的圆角底。"""
        return self._c("pill")

    def pill_text(self, a: float = 1.0) -> QtGui.QColor:
        """当前项（亮色底块）上的文字颜色。"""
        fg = self.pal.get("pill_fg")
        if not fg:
            return self.text(a)
        c = QtGui.QColor(fg)
        c.setAlphaF(max(0.0, min(1.0, a)))
        return c

    def track(self) -> QtGui.QColor:
        return self._c("track")

    def fill(self) -> QtGui.QColor:
        """滑块填充。"""
        return self._c("fill")

    @property
    def accent(self) -> QtGui.QColor:
        return self._c("on")

    def on_color(self) -> QtGui.QColor:
        return self._c("on")

    def on_fg(self, a: float = 1.0) -> QtGui.QColor:
        c = QtGui.QColor(self.pal.get("on_fg", "#ffffff"))
        c.setAlphaF(a)
        return c

    def up(self) -> QtGui.QColor:       # A 股习惯：红涨
        return self._c("up")

    def down(self) -> QtGui.QColor:     # 绿跌
        return self._c("down")

    def rim_top(self) -> QtGui.QColor:
        return self._c("rim1")

    def rim_bottom(self) -> QtGui.QColor:
        return self._c("rim2")

    def outline(self) -> QtGui.QColor:
        return self._c("outline")

    def menu_qss(self) -> tuple[str, str, str]:
        return self.pal["menu"]

    def qss_color(self, c: QtGui.QColor) -> str:
        return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


KIND_COLORS = {
    "qq": QtGui.QColor("#12b7f5"),
    "wechat": QtGui.QColor("#07c160"),
    "mail": QtGui.QColor("#ff9f0a"),
    "ai": QtGui.QColor("#3177e8"),
    "codex": QtGui.QColor("#3177e8"),
    "workbuddy": QtGui.QColor("#3177e8"),
    "deepseek_harness": QtGui.QColor("#3177e8"),
    "stock": QtGui.QColor("#ff5a52"),
    "other": QtGui.QColor("#8e8e93"),
    "notice": QtGui.QColor("#ffb020"),   # 钉钉 / 飞书 / 企业微信等「其他通知」的黄点
}

theme = Theme()


def theme_for(owner) -> Theme:
    """Resolve a widget's island palette, including parented tool windows.

    Unattached controls and non-window callers retain the application palette.
    Plain painted helpers (compact view and face) point to a theme owner too.
    """
    while owner is not None:
        local = getattr(owner, "_island_theme", None)
        if local is not None:
            return local
        parent = getattr(owner, "parentWidget", None)
        owner = parent() if parent is not None else getattr(owner, "_theme_owner", None)
    return theme


class _ThemeBinding(QtCore.QObject):
    """Rebind update/style slots when a control joins or leaves an island."""

    def __init__(self, widget):
        super().__init__(widget)
        self._widget = weakref.ref(widget)
        self._callbacks = []
        self._bound = theme_for(widget)
        self._bound.changed.connect(self._apply)
        widget.installEventFilter(self)

    def _apply(self):
        for callback in self._callbacks:
            callback()

    def rebind(self):
        widget = self._widget()
        if widget is None:
            return
        wanted = theme_for(widget)
        if wanted is self._bound:
            return
        self._bound.changed.disconnect(self._apply)
        self._bound = wanted
        wanted.changed.connect(self._apply)
        self._apply()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.ParentChange:
            bind_theme_tree(watched)
        return False


def connect_theme(widget, callback):
    """Subscribe a widget callback to its own window's palette."""
    binding = getattr(widget, "_theme_binding", None)
    if binding is None:
        binding = widget._theme_binding = _ThemeBinding(widget)
    binding._callbacks.append(callback)


def bind_theme_tree(widget):
    """An ancestor reparenting can change the palette of its whole subtree."""
    from PySide6 import QtWidgets

    for child in (widget, *widget.findChildren(QtWidgets.QWidget)):
        binding = getattr(child, "_theme_binding", None)
        if binding is not None:
            binding.rebind()
