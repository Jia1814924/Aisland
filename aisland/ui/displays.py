"""One island per display, with a single live subtitle destination."""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui
from shiboken6 import isValid

from ..core import bg


def _origin(screen):
    # Qt scales screen sizes on Windows and keeps their native origins.
    return screen.geometry().topLeft().toTuple()


class DisplayIslands(QtCore.QObject):
    def __init__(self, app, ctx, create_view):
        super().__init__(app)
        self.app = app
        self.ctx = ctx
        self.create_view = create_view
        self.views = {}
        self._retiring = set()
        self.primary = None
        self.caption = None
        self._closed = False
        self._listening = None
        self._scan = None
        self._next_scan = 0.0
        self.timer = QtCore.QTimer(self, interval=250, timeout=self.update_layer)

    def open(self):
        for screen in self.app.screens():
            self.add_screen(screen)
        self.app.screenAdded.connect(self.add_screen)
        self.app.screenRemoved.connect(self.remove_screen)
        self.app.primaryScreenChanged.connect(self._select_primary)

    def add_screen(self, screen):
        if self._closed or screen in self.views:
            return
        view = self.create_view(screen)
        self.views[screen] = view
        view.screen_destroyed = lambda: self.remove_screen(screen)
        screen.destroyed.connect(view.screen_destroyed)
        self._select_primary()
        view.island.show()
        self.update_idle_views()
        if self._listening:
            if self.caption is None:
                self._set_caption(view)
            else:
                view.pages["trans"].set_live_display(False)

    def remove_screen(self, screen):
        if self._closed:
            return
        view = self.views.pop(screen, None)
        if view is None:
            return
        if isValid(screen):
            try:
                screen.destroyed.disconnect(view.screen_destroyed)
            except RuntimeError:
                # 信号正从 destroyed 回调里触发，对象已开始销毁，连接会随对象一起失效
                pass
        self._select_primary()
        if self.caption is view:
            self._set_caption(self.active_view())
        self._retire(view)
        self._next_scan = 0.0

    def _retire(self, view):
        island = view.island
        self._retiring.add(island)
        island.destroyed.connect(lambda: self._retiring.discard(island))
        island.dispose()

    def _discard_invalid_screens(self):
        for screen in tuple(self.views):
            if not isValid(screen):
                self.remove_screen(screen)

    def _select_primary(self, _screen=None):
        primary = self.views.get(self.app.primaryScreen())
        if primary is None or not isValid(primary.island.scr):
            primary = next((v for s, v in self.views.items() if isValid(s)), None)
        if primary is self.primary:
            return
        if self.primary:
            self.primary.island.set_native_listener(False)
        self.primary = primary
        if primary:
            primary.island.set_native_listener(True)

    def active_view(self):
        candidates = [view for screen, view in self.views.items() if isValid(screen)]
        active = self.app.activeWindow()
        for view in candidates:
            if active is view.island:
                return view
        screen = self.app.screenAt(QtGui.QCursor.pos())
        view = self.views.get(screen)
        if view is not None and isValid(screen) and view.island.isVisible():
            return view
        return next((v for v in candidates if v.island.isVisible()), None)

    def update_idle_views(self):
        if len(self.views) < 2:
            return
        pointer_screen = self.app.screenAt(QtGui.QCursor.pos())
        for screen, view in self.views.items():
            compact = view.island.compact
            if compact.view in ("clock", "pet"):
                desired = "pet" if screen is pointer_screen else "clock"
                if compact.view != desired:
                    compact.set_view(desired)
                    view.island._kick()

    def cycle_idle_views(self):
        if len(self.views) != 1:
            return
        island = next(iter(self.views.values())).island
        compact = island.compact
        if not island.expanded and not island.passthrough() and compact.view in ("clock", "pet"):
            compact.set_view("pet" if compact.view == "clock" else "clock")
            island._kick()

    def set_listening(self, on):
        self._listening = object() if on else None
        self._next_scan = 0.0
        self._set_caption(self.active_view() if on else None)
        if self.timer.isActive():
            self.update_layer()

    def _set_caption(self, view):
        if view is self.caption:
            return
        last = None
        if self.caption:
            island = self.caption.island
            compact = island.compact
            last = (compact.sub_jp, compact.sub_zh, compact.sub_t)
            compact.set_sub_running(False)
            island._kick()
        self.caption = view
        for item in self.views.values():
            item.pages["trans"].set_live_display(not self._listening or item is view)
        if view:
            island = view.island
            compact = island.compact
            compact.set_sub_running(True)
            if last:
                compact.sub_jp, compact.sub_zh, compact.sub_t = last
            if island.expanded and "focus" not in island.locks and not self.app.activeModalWidget():
                view.pages["trans"].show_live()
                island.show_page("trans")
            island._kick()

    def set_subtitle(self, jp=None, zh=None):
        if self.caption:
            self.caption.island.compact.set_sub(jp=jp, zh=zh)
            self.caption.island._kick()

    def _scan_playback(self, apps):
        now = time.monotonic()
        if len(self.views) < 2 or not apps or self._scan is not None or now < self._next_scan:
            return
        self._next_scan = now + 0.55
        session = self._listening
        current = _origin(self.caption.island.scr) if self.caption else None
        media = self.ctx.media.state
        title = media.get("app_id", ""), media.get("title", "")

        def done(monitor):
            self._scan = None
            if self._closed:
                return
            self._discard_invalid_screens()
            playing, videos = self.ctx.media.playback_apps
            if self._listening is not session or apps != (videos or playing):
                return
            for screen, view in self.views.items():
                if _origin(screen) == monitor:
                    self._set_caption(view)
                    break

        self._scan = bg.run_bg(self.ctx.windows.playing_media_monitor, apps, title, current,
                               on_done=done, on_error=lambda _exc: done(None))

    def update_layer(self):
        if self._closed:
            return
        self._discard_invalid_screens()
        self.update_idle_views()
        playing, videos = self.ctx.media.playback_apps
        hidden_monitor = None
        if self._listening:
            # Some browsers publish video playback as a generic music session.
            self._scan_playback(videos or playing)
        else:
            hidden_monitor = self.ctx.windows.foreground_fullscreen_video_monitor(videos)
        may_raise = not (self.app.activeModalWidget() or self.app.activePopupWidget())
        for screen, view in self.views.items():
            island = view.island
            hidden = _origin(screen) == hidden_monitor
            island.set_fullscreen_hidden(hidden)
            if not hidden and may_raise and "lookup" not in island.locks:
                self.ctx.windows.ensure_topmost(int(island.winId()))

    def close(self):
        self._closed = True
        self.timer.stop()
        self._listening = None
        for screen, view in self.views.items():
            if isValid(screen):
                try:
                    screen.destroyed.disconnect(view.screen_destroyed)
                except RuntimeError:
                    # A screen can already be destroying while shutdown begins.
                    pass
            self._retire(view)
        self.views.clear()
        self.primary = self.caption = None
