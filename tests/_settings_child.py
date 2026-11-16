"""Exercise settings and menu actions through the real app without user registry writes."""
from contextlib import nullcontext
from pathlib import Path
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtTest, QtWidgets

from aisland import app as application
from aisland.core.config import cfg
from aisland.services.ai_usage import AI_NAMES
from aisland.ui.displays import DisplayIslands
from aisland.ui.island import Island
from aisland.ui.nav import LONG_PRESS_MS, NavBar
from aisland.ui.theme import G, KIND_COLORS

registry = {}


def query(_key, name):
    if name not in registry:
        raise FileNotFoundError(name)
    return registry[name], 1


def delete(_key, name):
    if name not in registry:
        raise FileNotFoundError(name)
    del registry[name]


fake_registry = SimpleNamespace(
    HKEY_CURRENT_USER=0, KEY_SET_VALUE=2, REG_SZ=1,
    OpenKey=lambda *_args: nullcontext(object()),
    CreateKeyEx=lambda *_args: nullcontext(object()),
    QueryValueEx=query, DeleteValue=delete,
    SetValueEx=lambda _key, name, _reserved, _kind, value: registry.__setitem__(name, value))

original_open = DisplayIslands.open


def settle(island):
    island.timer.stop()
    for _ in range(180):
        island._last = time.perf_counter() - 1 / 60
        island._tick()
    QtWidgets.QApplication.processEvents()


def save_preview(island, filename):
    pixmap = island.grab()
    dpr = pixmap.devicePixelRatioF()
    body = island.body.adjusted(-12, -12, 12, 12).toAlignedRect()
    crop = QtCore.QRect(round(body.x() * dpr), round(body.y() * dpr),
                        round(body.width() * dpr), round(body.height() * dpr))
    assert pixmap.toImage().copy(crop).save(str(filename))


def exercise(self):
    cfg.set("island.expand_mode", "never", save=False)
    original_open(self)
    views = list(self.views.values())
    assert all("set" in view.pages for view in views)

    # Verify the real app maps foreground processes and keeps both quota pages in sync.
    cfg.set("notify.ai_provider", "auto", save=False)
    with patch.object(self.ctx.windows, "foreground_application_path", return_value="C:/Apps/WorkBuddy.exe") as foreground:
        self.ctx.quota_readings.timer.timeout.emit()
        assert all(view.pages["msg"]._provider == "workbuddy" for view in views)
        foreground.return_value = ("C:/Program Files/WindowsApps/"
                                   "OpenAI.Codex_26.1106.6628.0_x64__2p2nqsd0c76g0/app/ChatGPT.exe")
        self.ctx.quota_readings.timer.timeout.emit()
        assert all(view.pages["msg"]._provider == "codex" for view in views)
        foreground.return_value = "aisland.exe"
        views[0].pages["msg"].on_show()
        assert all(view.pages["msg"]._provider == "codex" for view in views)
        self.ctx.notify.notified.emit({"kind": "deepseek_harness", "name": "AI", "title": "测试通知"})
        assert all(view.pages["msg"]._provider == "deepseek_harness" for view in views)
        views[0].pages["msg"].on_hide()

    for view in views:
        view.pages["set"].on_show()

    source = views[0]
    with patch.object(Island, "_apply_passthrough"):
        source.island.menu_requested.emit(QtCore.QPoint(20, 20))
        menu = next(menu for menu in source.island.findChildren(QtWidgets.QMenu)
                    if any(action.text().endswith("开机自启") for action in menu.actions()))
        toggles = {key: next(action for action in menu.actions() if action.text().endswith(label))
                   for key, label in (("passthrough", "可穿透"), ("capture", "参与截图"), ("autostart", "开机自启"))}
        toggles["passthrough"].trigger()
        assert cfg.get("island.passthrough") is True and "aisland" not in registry
        assert all(view.pages["set"]._rows["passthrough"].on for view in views)
        toggles["capture"].trigger()
        assert cfg.get("island.exclude_from_capture") is True and "aisland" not in registry
        assert all(not view.pages["set"]._rows["capture"].on for view in views)
        toggles["autostart"].trigger()
        assert "aisland" in registry
        assert all(view.pages["set"]._rows["autostart"].on for view in views)
        menu.close()
        self.ctx.settings.passthrough(source.island, False)
        self.ctx.settings.capture(source.island, True)
        self.ctx.settings.autostart(source.island, False)
        before = source.pages["set"]._rows["wechat_preview"].on
        source.pages["set"]._rows["wechat_preview"].btn.clicked.emit()
        assert all(view.pages["set"]._rows["wechat_preview"].on != before for view in views)
        source.pages["set"].theme_seg.set_index(2, emit=True)
        assert all(view.pages["set"].theme_seg.index == 2 for view in views)

    for kind in AI_NAMES:
        assert KIND_COLORS[kind] == KIND_COLORS["ai"]
        for view in views:
            view.compact.set_badges({kind: 1})
            assert view.compact.badges == (KIND_COLORS["ai"],)
    self.ctx.notify.notified.emit({"kind": "deepseek_harness", "name": "AI", "title": "测试通知", "body": "任务已完成"})
    notice = next(view for view in views if (view.compact.activity or {}).get("glyph") == G.BOLT)
    assert notice.compact.activity["color"] == KIND_COLORS["ai"]

    nav = NavBar([(str(index), G.GEAR, str(index)) for index in range(7)])
    nav.resize(510, 36)
    rects = nav._rects()
    gaps = [right.center().x() - left.center().x() for left, right in zip(rects, rects[1:])]
    assert max(gaps) - min(gaps) < 0.01
    # 选中项展开成「图标 + 文字」胶囊，其余等宽；中心仍固定等距且互不重叠
    assert rects[0].width() > rects[1].width()
    assert len({round(rect.width(), 4) for rect in rects[1:]}) == 1
    assert rects[0].right() < rects[1].left()
    nav.set_current("3")
    for _ in range(120):
        nav._last = 0.0
        nav._tick()
    moved = nav._rects()
    assert moved[3].width() > moved[0].width()
    assert round(moved[0].width(), 4) == round(moved[1].width(), 4)
    assert moved[2].right() < moved[3].left()
    drags = QtTest.QSignalSpy(nav.drag_requested)
    # 选中胶囊展开后左边缘贴近 x=0，长按改点标签矩形之外的底边空白
    blank = QtCore.QPoint(1, nav.height() - 1)
    QtTest.QTest.mousePress(nav, QtCore.Qt.MouseButton.LeftButton, pos=blank)
    QtTest.QTest.qWait(LONG_PRESS_MS + 60)
    assert drags.count() == 1
    QtTest.QTest.mouseRelease(nav, QtCore.Qt.MouseButton.LeftButton, pos=blank)
    nav.deleteLater()

    output = os.environ.get("AISLAND_UI_PREVIEW_DIR")
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        cfg.set("island.expand_mode", "never", save=False)
        for mode in ("dark", "light"):
            self.ctx.settings.theme(source.island, mode)
            source.island.locks.add("preview")
            source.island.expand("set")
            settle(source.island)
            QtTest.QTest.qWait(600)
            assert source.island.expanded
            save_preview(source.island, destination / ("settings-" + mode + ".png"))
            scroll = source.pages["set"].findChild(QtWidgets.QScrollArea)
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            QtWidgets.QApplication.processEvents()
            save_preview(source.island, destination / ("settings-bottom-" + mode + ".png"))
            scroll.verticalScrollBar().setValue(0)
            source.island.locks.discard("preview")
            source.island.collapse()
            source.island.activity({"glyph": G.BOLT, "color": KIND_COLORS["ai"],
                                    "title": "AI 任务已完成", "sub": "蓝色消息提醒"}, 10)
            settle(source.island)
            save_preview(source.island, destination / ("notification-" + mode + ".png"))


report = Path(os.environ["AISLAND_USER_HOME"]) / "ui.json"
sys.argv = ["aisland", "--verify-ui", str(report)]
with patch.object(application, "winreg", fake_registry), patch.object(DisplayIslands, "open", exercise):
    result = application.main()
assert result == 0
raise SystemExit(result)
