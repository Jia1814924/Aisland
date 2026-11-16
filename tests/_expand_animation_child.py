from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from aisland.core.config import cfg
from aisland.ui.glass import Backdrop
from aisland.ui.island import Island
from aisland.ui.pages.base import Page
from aisland.ui.pages.home import HistoryList
from aisland.ui.theme import G, theme


class ColorPage(Page):
    key, glyph, title = "home", G.HOME, "主页"

    def __init__(self):
        super().__init__()
        self.color = QtGui.QColor(244, 35, 195)
        self.button = QtWidgets.QPushButton("验证点击", self)
        self.button.setGeometry(12, 12, 96, 28)
        self.clicks = 0
        self.button.clicked.connect(self.clicked)

    def clicked(self):
        self.clicks += 1

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.color)
        painter.end()


class ExpandChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication([])
        cfg.set("island.expand_mode", "never", save=False)
        cfg.set("island.passthrough", False, save=False)
        cfg.set("island.pinned", False, save=False)
        cfg.set("hotkeys", {}, save=False)

    def make_island(self, edge="top"):
        cfg.set("island.edge", edge, save=False)
        page = ColorPage()
        other = ColorPage()
        other.key, other.title = "other", "另一页"
        other.color = QtGui.QColor(20, 220, 240)
        with patch.object(Backdrop, "start"), patch.object(Backdrop, "request"):
            island = Island([page, other], native_listener=False)
            island.show()
        island.poll.stop()
        island.timer.stop()
        island.backdrop.request = lambda *_args, **_kwargs: None
        self.addCleanup(island.close)
        return island, page

    def step(self, island, count=1):
        island.timer.stop()
        for _ in range(count):
            island._last = time.perf_counter() - 1 / 60
            island._tick()
        island.timer.stop()

    def partial(self, island):
        island.expand()
        for _ in range(30):
            self.step(island)
            if 0.35 < island._content_progress < 0.8:
                return
        self.fail("No simultaneous intermediate frame")

    def colored_pixels(self, island):
        image = island.grab().toImage()
        dpr = image.devicePixelRatio()
        colored = []
        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                color = image.pixelColor(x, y)
                if color.red() > color.green() + 40 and color.blue() > color.green() + 40:
                    colored.append(QtCore.QPointF((x + 0.5) / dpr, (y + 0.5) / dpr))
        return colored

    def test_content_is_painted_inside_growing_shell_and_hands_off(self):
        for edge in ("top", "bottom", "left", "right"):
            with self.subTest(edge=edge):
                island, page = self.make_island(edge)
                self.partial(island)
                self.assertLess(island.body.height(), island.sh.target)
                self.assertFalse(island.content.isVisible())
                pixels = self.colored_pixels(island)
                self.assertGreater(len(pixels), 30, "Content is missing while the shell grows")
                # 曲线边缘的抗锯齿可覆盖相邻物理像素；超过一像素才算内容溢出。
                stroker = QtGui.QPainterPathStroker()
                stroker.setWidth(2.0 / island.devicePixelRatioF())
                clipped = island.path.united(stroker.createStroke(island.path))
                self.assertTrue(all(clipped.contains(point) for point in pixels),
                                "Transition content escaped the glass outline")
                self.step(island, 70)
                self.assertTrue(island.content.isVisible())
                self.assertIsNone(island._content_frame)
                self.assertTrue(island.sw.rest and island.sh.rest)
                QtTest.QTest.mouseClick(page.button, QtCore.Qt.MouseButton.LeftButton)
                self.assertEqual(page.clicks, 1)
                island.close()

    def test_reopen_reverse_and_page_change_refresh_content(self):
        island, page = self.make_island()
        for _ in range(3):
            self.partial(island)
            before = island._content_progress
            island.collapse()
            self.assertIsNone(island._content_frame)
            island.expand()
            self.assertAlmostEqual(island._content_progress, before, places=5)
            page.color = QtGui.QColor(20, 220, 240)
            self.step(island)
            self.assertEqual(self.colored_pixels(island), [], "Transition contains stale page data")
            page.color = QtGui.QColor(244, 35, 195)
            island.show_page("other")
            self.step(island)
            self.assertEqual(self.colored_pixels(island), [], "Transition contains the previous page")
            island.show_page("home")
            self.step(island, 70)
            island.collapse()
            self.step(island, 70)
            self.assertFalse(island.content.isVisible())
        island.close()
        self.assertIsNone(island._content_frame)

    def test_theme_and_device_pixel_ratio(self):
        island, _page = self.make_island()
        for dark in (False, True):
            theme.set_mode("dark" if dark else "light")
            for dpr in (1.25, 1.5):
                with self.subTest(dark=dark, dpr=dpr), patch.object(island, "devicePixelRatioF", return_value=dpr):
                    self.partial(island)
                    self.assertEqual(island._content_frame.devicePixelRatio(), dpr)
                    self.assertEqual(island._content_frame.size(), island.content.size() * dpr)
                    self.assertGreater(len(self.colored_pixels(island)), 30)
                    self.step(island, 70)
                    self.assertTrue(island.content.isVisible())
                    island.collapse()
                    self.step(island, 70)

    def test_message_history_is_present_in_hidden_content_render(self):
        parent = QtWidgets.QWidget()
        history = HistoryList("暂无消息", parent)
        history.resize(400, history.height())
        for rows in ([], [{"kind": "codex", "title": "完成", "body": "预览内容", "time": 0}]):
            history.set_rows(rows)
            frame = QtGui.QPixmap(history.size())
            frame.fill(QtCore.Qt.GlobalColor.transparent)
            history.render(frame, QtCore.QPoint(), QtGui.QRegion(), QtWidgets.QWidget.RenderFlag.DrawChildren)
            image = frame.toImage()
            count = sum(image.pixelColor(x, y).alpha() > 0
                        for y in range(image.height()) for x in range(image.width()))
            self.assertGreater(count, 20, "Message history disappeared while rendering the transition")


if __name__ == "__main__":
    unittest.main()
