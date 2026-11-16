"""Exercise real Island widgets and both Qt screens through the app entry point."""
from pathlib import Path
import os
import sys
import threading
import time
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from aisland import app as application
from aisland.core.config import cfg
from aisland.ui.displays import DisplayIslands
from aisland.ui.glass import Backdrop


def wait_until(app, condition):
    deadline = time.monotonic() + 3
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        time.sleep(0.005)
    assert condition(), "Qt objects or worker threads did not finish"


def glass_threads():
    return sum(thread.name == "aisland-glass" for thread in threading.enumerate())


original_open = DisplayIslands.open


def exercise(self):
    original_factory = self.create_view

    def create(screen):
        view = original_factory(screen)
        backdrop = view.island.backdrop
        backdrop._capture = lambda *_args: None
        backdrop.start = MethodType(Backdrop.start, backdrop)
        return view

    self.create_view = create
    original_open(self)
    app = QtWidgets.QApplication.instance()
    main, side = app.screens()
    assert len(self.views) == 2 and glass_threads() == 2
    cfg.set("island.expand_mode", "never", save=False)

    # Every expansion must include intermediate sizes, including a reopened panel.
    island = self.views[main].island
    for _ in range(3):
        island.expand()
        island.timer.stop()
        widths = []
        for _ in range(180):
            island._last = time.perf_counter() - 1 / 60
            island._tick()
            widths.append(island.sw.x)
        assert sum(190 < width < island.sw.target - 1 for width in widths) >= 3, widths[:5]
        assert island.sw.rest and island.sh.rest
        island.collapse()
        island.timer.stop()
        for _ in range(180):
            island._last = time.perf_counter() - 1 / 60
            island._tick()
        assert island.sw.rest and island.sh.rest
    island._kick()

    def connections():
        return (
            self.ctx.live.receivers(QtCore.SIGNAL("partial(QString)")),
            self.ctx.live.receivers(QtCore.SIGNAL("running_changed(bool)")),
            self.ctx.notify.receivers(QtCore.SIGNAL("wechat_preview_changed()")),
            self.ctx.quota_readings.timer.receivers(QtCore.SIGNAL("timeout()")),
            cfg.receivers(QtCore.SIGNAL("changed()")),
        )

    baseline = connections()
    assert all(baseline), baseline
    old = self.views[side].island
    captured, release = threading.Event(), threading.Event()

    def blocked_capture(*_args):
        captured.set()
        release.wait(3)

    old.backdrop._capture = blocked_capture
    old.backdrop.request(side, QtCore.QRect(-2500, 0, 200, 80), 0.1, urgent=True)
    assert captured.wait(1), "Capture fixture did not start"
    try:
        start = time.perf_counter()
        self.remove_screen(side)
        assert time.perf_counter() - start < 0.15, "Removing a display blocked the GUI"
        assert isValid(old) and not old.isVisible()
        app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        assert isValid(old), "The window was destroyed before its capture finished"
    finally:
        release.set()
    wait_until(app, lambda: not isValid(old) and not self._retiring and glass_threads() == 1)
    self.add_screen(side)
    assert connections() == baseline

    for _ in range(8):
        old = self.views[side].island
        self.remove_screen(side)
        wait_until(app, lambda: not isValid(old) and not self._retiring and glass_threads() == 1)
        self.add_screen(side)
        assert connections() == baseline, (connections(), baseline)
        assert glass_threads() == 2

    with patch.object(app, "screenAt", return_value=side):
        self.update_idle_views()
    assert self.views[main].compact.view == "clock"
    assert self.views[side].compact.view == "pet"
    self.ctx.live._set_running(True)
    self._set_caption(self.views[side])
    self.set_subtitle(jp="テスト", zh="字幕")
    assert self.views[side].compact.sub_active()
    assert not self.views[main].compact.sub_active()
    self.ctx.live._set_running(False)

    print({"real_screens": 2, "hotplug_rounds": 9, "stable_connections": baseline,
           "glass_threads": glass_threads(), "repeated_expansion_animates": True}, flush=True)


report = Path(os.environ["AISLAND_USER_HOME"]) / "ui.json"
sys.argv = ["aisland", "--verify-ui", str(report)]
with patch.object(DisplayIslands, "open", exercise):
    code = application.main()
assert code == 0
assert report.exists()
raise SystemExit(code)
