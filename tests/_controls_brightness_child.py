from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from aisland.services import audio
from aisland.ui.pages.system import ControlsPage
from aisland.ui.theme import G, theme
from aisland.ui.widgets import Slider


def render(widget, background):
    result = QtGui.QImage(widget.size(), QtGui.QImage.Format.Format_RGB32)
    result.fill(QtGui.QColor(background))
    widget.render(result, QtCore.QPoint(), QtGui.QRegion(), QtWidgets.QWidget.RenderFlag.DrawChildren)
    return result


class ControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication([])

    def test_zero_fill_and_icon_contrast_in_both_themes(self):
        slider = Slider(G.SUN, height=44)
        slider.resize(240, 44)
        for mode, background in (("dark", "#30384b"), ("light", "#e3e7ee")):
            with self.subTest(theme=mode):
                theme.set_mode(mode)
                slider.set_value(0.75, animate=False)
                slider.set_value(0)
                self.assertEqual(slider.shown.x, 0)
                zero = render(slider, background)
                samples = [zero.pixelColor(x, y).lightness() for x in range(14, 30) for y in range(15, 30)]
                track = zero.pixelColor(32, 22).lightness()
                contrast = max(samples) - track if mode == "dark" else track - min(samples)
                self.assertGreater(contrast, 45)
                for level, inside, outside in ((0.03, 5, 10), (0.11, 12, 35)):
                    slider.set_value(level, animate=False)
                    low = render(slider, background)
                    self.assertNotEqual(low.pixelColor(inside, 22), zero.pixelColor(inside, 22))
                    self.assertEqual(low.pixelColor(outside, 22), zero.pixelColor(outside, 22))
                slider.set_value(1, animate=False)
                full = render(slider, background)
                self.assertGreater(full.pixelColor(32, 22).lightness() - min(
                    full.pixelColor(x, y).lightness() for x in range(14, 30) for y in range(15, 30)), 100)

    def test_sliders_target_only_their_own_screen(self):
        ctx = SimpleNamespace(audio=Mock(), brightness=Mock(), clash=Mock(), radio=Mock())
        ctx.audio.muted.return_value = False
        page = ControlsPage(ctx)
        self.addCleanup(page.close)
        rows = [{"id": "laptop", "name": "内置屏幕", "brightness": 0},
                {"id": "external", "name": "ViewSonic VX2780-4K-HD-2", "brightness": 11}]
        page.set_brightness(rows)
        page.set_radio({"wifi_on": True, "bt_on": True, "vpn_connections": [("tun0", False)]})
        page.resize(512, 182)
        page.show()
        self.app.processEvents()
        laptop, external = page._brightness_sliders.values()
        self.assertEqual((laptop.value, external.value), (0.0, 0.11))
        self.assertLessEqual(page.vpn_card.mapTo(page, QtCore.QPoint()).y() + page.vpn_card.height(), page.height())
        left = QtCore.Qt.MouseButton.LeftButton
        QtTest.QTest.mouseClick(laptop, left, pos=QtCore.QPoint(laptop.width() - 1, 22))
        QtTest.QTest.mouseClick(external, left, pos=QtCore.QPoint(0, 22))
        self.assertEqual([c.args for c in ctx.brightness.set.call_args_list],
                         [("laptop", 100), ("external", 0)])
        QtTest.QTest.mouseClick(page.vol, left, pos=QtCore.QPoint(22, 22))
        ctx.audio.toggle_mute.assert_called_once()
        QtTest.QTest.mouseClick(page.vol, left, pos=QtCore.QPoint(0, 22))
        ctx.audio.set_volume.assert_called_once_with(0)
        page.set_brightness([rows[1]])
        self.assertIs(page._brightness_sliders["external"], external)
        page.set_brightness([])
        self.assertFalse(page._brightness_empty.isEnabled())

    def test_wmi_and_ddc_use_device_identity(self):
        svc = audio.BrightnessService()
        self.addCleanup(svc.stop)
        instance = r"DISPLAY\EDO1420\4&TEST&UID10_0"
        panel = r"\\?\DISPLAY#EDO1420#4&TEST&UID10"
        external = r"\\?\DISPLAY#VSC0000#4&TEST&UID11"
        panel_id, external_id = panel.casefold(), external.casefold()
        phys = [(external_id, "DISPLAY5", 52, "ViewSonic", external),
                (panel_id, "DISPLAY1", 51, "Notebook", panel)]
        with (patch.object(svc._wmi, "read", return_value=[(instance, 8, 8, 100)]),
              patch.object(svc._wmi, "set", return_value=True) as wmi_set,
              patch.object(svc, "_twinkle_exe", return_value=""),
              patch.object(audio, "_enum_physical", return_value=(phys, [])) as enumerate_displays,
              patch.object(audio, "_ddc_get", return_value=27),
              patch.object(audio, "_ddc_set", return_value=True) as ddc_set):
            inside, outside = svc._read_all()
            self.assertEqual((inside["id"], inside["brightness"], outside["id"], outside["brightness"]),
                             (panel_id, 0, external_id, 27))
            self.assertTrue(svc._apply([(outside, 0)]))
            ddc_set.assert_called_once_with(52, 0)
            wmi_set.assert_not_called()
            self.assertTrue(svc._apply([(inside, 0)]))
            wmi_set.assert_called_once_with(8, instance)
            self.assertEqual(ddc_set.call_count, 1)
            replacement = ("replacement-id", "DISPLAY5", 53, "ViewSonic", "")
            enumerate_displays.return_value = ([replacement, phys[1]], [])
            self.assertFalse(svc._apply([(outside, 80)]))
            self.assertEqual(ddc_set.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
