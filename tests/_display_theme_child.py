"""Feed distinct desktop samples; check rendering, styles and theme ownership."""
from pathlib import Path
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
from aisland.core.config import cfg
from aisland.ui import theme as theme_module
from aisland.ui.island import Island
from aisland.ui.lookup import LookupPopup
from aisland.ui.pages.base import Page
from aisland.ui.pages.system import styled_menu
from aisland.ui.theme import G, theme, theme_for
from aisland.ui.widgets import Card, Label, LineEdit, vbox

app = QtWidgets.QApplication([])
if os.environ.get("AISLAND_THEME_PREVIEW_DIR"):
    # Qt's offscreen plugin may have no system-font database. This is only for
    # the optional preview artifact, never the application's font settings.
    font_path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/segoeui.ttf"
    if font_path.is_file():
        QtGui.QFontDatabase.addApplicationFont(str(font_path))


class Panel(Page):
    key, glyph, title = "home", "", "Home"

    def __init__(self):
        super().__init__()
        self.label = Label("Independent palette", 18)
        self.edit = LineEdit("Search")
        self.card = Card()
        self.card.setLayout(vbox(self.label, self.edit))
        self.setLayout(vbox(self.card))


class DisplayPaletteTests(unittest.TestCase):
    def setUp(self):
        cfg.set("island.expand_mode", "never", save=False)
        theme.set_mode("morandi")
        self.mode_receivers = theme.receivers(QtCore.SIGNAL("mode_changed(QString)"))
        screens = app.screens()
        self.assertEqual(len(screens), 2)
        self.windows = [Island([Panel()], screen=screen, native_listener=False) for screen in screens]

    def tearDown(self):
        for window in self.windows:
            window.timer.stop()
            window.poll.stop()
            window.dispose()
        app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertTrue(all(not isValid(window) for window in self.windows))
        self.assertEqual(theme.receivers(QtCore.SIGNAL("mode_changed(QString)")), self.mode_receivers)
        theme.set_mode("auto")

    @staticmethod
    def feed(window, lum, rgb):
        window.backdrop._snapshot = (None, QtCore.QRect(), lum, rgb)
        window._on_backdrop(False)
        window.timer.stop()

    def samples(self, first, second):
        with patch.object(theme_module.time, "monotonic", return_value=100.0) as clock:
            for _ in range(60):
                self.feed(self.windows[0], *first)
                self.feed(self.windows[1], *second)
                clock.return_value += 0.2

    def test_morandi_samples_do_not_change_the_other_window_or_global_palette(self):
        a, b = self.windows
        global_tint = theme.tint().rgba()
        self.samples((0.75, (245, 40, 50)), (0.75, (25, 65, 240)))
        self.assertNotEqual(theme_for(a).tint().rgba(), theme_for(b).tint().rgba())
        self.assertEqual(theme.tint().rgba(), global_tint)
        saved = theme_for(a).tint().rgba()
        self.feed(b, 0.75, (25, 240, 65))
        self.assertEqual(theme_for(a).tint().rgba(), saved)
        self.assertIs(theme_for(a.compact), theme_for(a))
        self.assertIs(theme_for(a.compact.pet), theme_for(a))
        # Real island painting must use the local tint, including the compact shell.
        images = [window.grab().toImage() for window in self.windows]
        colors = []
        for window, image in zip(self.windows, images):
            point = QtCore.QPoint(round(window.body.center().x()), round(window.body.top() + 6))
            dpr = image.devicePixelRatio()
            colors.append(image.pixelColor(round(point.x() * dpr), round(point.y() * dpr)).rgba())
        self.assertNotEqual(*colors)
        preview = os.environ.get("AISLAND_THEME_PREVIEW_DIR")
        if preview:
            self.save_preview(Path(preview))

    def save_preview(self, destination):
        """An explicitly requested QA artifact, not a real desktop screenshot."""
        canvas = QtGui.QImage(1200, 380, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QtGui.QColor("#edf0f5"))
        painter = QtGui.QPainter(canvas)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        font = QtGui.QFont("Segoe UI")
        font.setPixelSize(18)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#263244"))
        painter.drawText(QtCore.QRect(30, 16, 1140, 34), "Independent display palettes - synthetic backdrop test")
        for index, window in enumerate(self.windows):
            window.expand()
            window.timer.stop()
            window.sw.snap(window.sw.target)
            window.sh.snap(window.sh.target)
            window._update_geometry()
            pixmap = window.grab()
            dpr = pixmap.devicePixelRatio()
            rect = window.body.adjusted(-15, -15, 15, 15).toAlignedRect()
            physical = QtCore.QRect(round(rect.x() * dpr), round(rect.y() * dpr),
                                   round(rect.width() * dpr), round(rect.height() * dpr))
            crop = pixmap.toImage().copy(physical)
            x = 24 + index * 588
            painter.fillRect(QtCore.QRect(x, 65, 564, 282),
                             QtGui.QColor("#b17570" if index == 0 else "#6d83ad"))
            painter.drawImage(QtCore.QRect(x, 65, 564, 282), crop)
            painter.drawText(QtCore.QRect(x, 348, 564, 28),
                             "DISPLAY 1 - warm backdrop" if index == 0 else "DISPLAY 2 - cool backdrop")
        painter.end()
        destination.mkdir(parents=True, exist_ok=True)
        self.assertTrue(canvas.save(str(destination / "independent-display-theme.png")))

    def test_auto_contrast_content_nav_and_popup_follow_their_own_display(self):
        theme.set_mode("auto")
        self.samples((0.98, (245, 245, 245)), (0.02, (5, 5, 5)))
        a, b = self.windows
        self.assertFalse(theme_for(a).dark)
        self.assertTrue(theme_for(b).dark)
        for window in self.windows:
            palette = theme_for(window)
            panel = window.pages[0]
            self.assertIs(theme_for(panel.label), palette)
            self.assertIs(theme_for(window.nav), palette)
            self.assertIn(palette.qss_color(palette.text()), panel.edit.styleSheet())
            popup = LookupPopup(SimpleNamespace(), parent=panel)
            self.assertIs(theme_for(popup), palette)
            self.assertIn(palette.qss_color(palette.text()), popup.header.styleSheet())
            popup.deleteLater()
            menu = styled_menu(panel)
            self.assertIn(palette.menu_qss()[0], menu.styleSheet())
            menu.deleteLater()
        self.assertNotEqual(a.nav.grab().toImage(), b.nav.grab().toImage())

    def test_only_own_callbacks_fire_and_reparented_control_rebinds(self):
        a, b = self.windows
        counts = [0, 0]
        for index, window in enumerate(self.windows):
            theme_for(window).changed.connect(lambda i=index: counts.__setitem__(i, counts[i] + 1))
        self.feed(a, 0.8, (250, 50, 60))
        self.assertEqual(counts[1], 0)
        self.assertGreater(counts[0], 0)
        label = a.pages[0].label
        label.setParent(b.pages[0])
        self.assertIs(theme_for(label), theme_for(b))
        self.assertIs(label._theme_binding._bound, theme_for(b))

    def test_fixed_mode_stays_synchronized_without_sharing_samples(self):
        theme.set_mode("rouge")
        self.samples((0.98, (245, 245, 245)), (0.02, (5, 5, 5)))
        self.assertEqual([theme_for(w).mode for w in self.windows], ["rouge", "rouge"])
        self.assertEqual(theme_for(self.windows[0]).tint(), theme_for(self.windows[1]).tint())
        theme.set_mode("morandi")
        self.samples((0.75, (245, 40, 50)), (0.75, (25, 65, 240)))
        self.assertNotEqual(theme_for(self.windows[0]).tint(), theme_for(self.windows[1]).tint())


if __name__ == "__main__":
    unittest.main(verbosity=2)
