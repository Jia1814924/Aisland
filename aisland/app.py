# -*- coding: utf-8 -*-
"""aisland 入口：创建服务、页面、灵动岛，把信号连成"活动"提示。"""
from __future__ import annotations

import os
import argparse
import json
import sys
import time
import traceback
import winreg
from types import SimpleNamespace
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .core import bg
from .core.config import CONFIG_PATH, DATA_DIR, cfg, expand
from .core.runtime import INSTALL_ROOT, IS_FROZEN, acquire_app_mutex, autostart_command, release_app_mutex, resource_path
from .version import APP_NAME, __version__

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class Actions(QtCore.QObject):
    """跨页面的动作（热键 / 快捷按钮共用）。"""

    def __init__(self, ctx):
        super().__init__(ctx.island)
        self.ctx = ctx
        self._trans_req = -1

    def translate_clipboard(self, show_page: bool = False):
        text = QtGui.QGuiApplication.clipboard().text().strip()
        if not text:
            self.ctx.island.activity({"glyph": _G().TRANSLATE, "title": "剪贴板里没有文字"}, 2.0)
            return
        self.translate_text(text, show_page)

    def translate_text(self, text: str, show_page: bool = False):
        from .ui.theme import G
        text = text.strip()[:4000]
        if not text:
            return
        page = self.ctx.pages["trans"]
        if not page.ensure_connection():
            return
        rid = self.ctx.translate.translate(text)
        self._trans_req = rid
        page.start(text, rid)
        isl = self.ctx.island
        if show_page:
            isl.expand("trans")
        elif not isl.expanded:
            isl.activity({"glyph": G.TRANSLATE, "title": "翻译中…", "sub": text[:30]}, 20)

    def on_translated(self, rid: int, out: str):
        from .ui.theme import G
        isl = self.ctx.island
        if rid != self._trans_req or isl.expanded:
            return
        first = " ".join(out.split())
        isl.activity({"glyph": G.TRANSLATE, "title": first[:60], "sub": ""}, min(12.0, 3.5 + len(first) * 0.12))

    def on_trans_failed(self, rid: int, err: str):
        from .ui.theme import G
        if rid == self._trans_req and not self.ctx.island.expanded:
            self.ctx.island.activity({"glyph": G.TRANSLATE, "title": "翻译失败", "sub": err}, 4)


def _G():
    from .ui.theme import G
    return G


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "aisland")
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "aisland", 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, "aisland")
            except OSError:
                pass
        # 旧品牌留下的启动项指向已经不存在的可执行文件，顺手清掉。
        try:
            winreg.DeleteValue(k, "wisland")
        except OSError:
            pass
    cfg.changed.emit()


def adopt_legacy_autostart():
    old_executable = Path(sys.executable).with_name("Wisland.exe")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            old_command, _ = winreg.QueryValueEx(key, "wisland")
            if os.path.normcase(str(old_command).strip().strip('"')) != os.path.normcase(str(old_executable)):
                return
            try:
                winreg.QueryValueEx(key, "aisland")
            except FileNotFoundError:
                winreg.SetValueEx(key, "aisland", 0, winreg.REG_SZ, autostart_command())
            winreg.DeleteValue(key, "wisland")
    except OSError:
        pass


def main():
    # 音频使用逐样本运算，模型推理有独立线程设置。避免 NumPy 启动闲置的 BLAS 线程池。
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    verify_output = None
    verify_live_lyrics = False
    verify_track = None
    if "--verify-ui" in sys.argv:
        if not os.environ.get("AISLAND_USER_HOME", "").strip():
            raise RuntimeError("UI verification requires isolated AISLAND_USER_HOME")
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--verify-ui", required=True)
        parser.add_argument("--verify-live-lyrics", action="store_true")
        parser.add_argument("--verify-lyrics-track", type=Path)
        options, _ = parser.parse_known_args()
        verify_output = Path(options.verify_ui).expanduser().resolve()
        verify_live_lyrics = options.verify_live_lyrics or options.verify_lyrics_track is not None
        if options.verify_lyrics_track is not None:
            verify_track = json.loads(options.verify_lyrics_track.read_text(encoding="utf-8"))
        # This is an explicit test mode, not an alternative normal startup path.
        # No constructor warm-up jobs or accidental page action may contact a network.
        import socket
        def no_network(*_args, **_kwargs):
            raise RuntimeError("Network disabled during UI verification")
        if verify_live_lyrics:
            # Windows asyncio uses a local socket pair to wake its event loop.
            connect = socket.socket.connect

            def connect_loopback(sock, address):
                if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
                    return connect(sock, address)
                return no_network()

            socket.socket.connect = connect_loopback
        else:
            socket.socket.connect = no_network
        socket.socket.connect_ex = no_network
        socket.create_connection = no_network
        socket.getaddrinfo = no_network
        run_background = bg.run_bg
        bg.run_bg = lambda *_args, **_kwargs: None
        cfg.set("island.last_tab", "home", save=False)
        cfg.set("island.expand_mode", "always", save=False)
        cfg.set("island.passthrough", False, save=False)
        cfg.set("island.auto_cycle_s", 0, save=False)
        cfg.set("hotkeys", {}, save=False)
        if verify_live_lyrics:
            cfg.set("media.lyrics_online", False, save=False)
            cfg.set("media.netease_log", False, save=False)
    mutex_handle = None
    if IS_FROZEN and verify_output is None:
        mutex_handle, already_running = acquire_app_mutex()
        if already_running:
            release_app_mutex(mutex_handle)
            print(f"{APP_NAME} 已在运行")
            return 0
    try:
        import ctypes
        identity = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        identity.argtypes = (ctypes.c_wchar_p,)
        identity.restype = ctypes.c_long
        identity("Aisland.Desktop")
    except OSError:
        pass
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("aisland")
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QtGui.QIcon(str(resource_path("assets/aisland.ico"))))
    bg.init()
    lock = QtCore.QLockFile(str(DATA_DIR / "aisland.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        release_app_mutex(mutex_handle)
        print(f"{APP_NAME} 已在运行")
        return 0
    if verify_output is None:
        if IS_FROZEN:
            cfg.bind_translation_installation(INSTALL_ROOT)
            adopt_legacy_autostart()
        from .core.music_setup import apply_pending_music_dirs
        try:
            apply_pending_music_dirs(cfg, DATA_DIR / "pending_music_dirs.json")
        except Exception:
            print("本地音乐目录暂未导入，将在下次启动时重试。")

    from .services.audio import AudioService, BrightnessService
    from .services.ai_usage import AI_NAMES
    from .services.ai_identity import identify_ai_application
    from .services.clash import ClashService
    from .services.clipboard import ClipboardService
    from .services.mailcheck import MailCheckService
    from .services.media import MediaService
    from .services.notify import NotifyService, ai_subtitle
    from .services.system import RadioService, WindowService
    from .services.lyrics import LyricsService
    if verify_live_lyrics:
        from .services import lyrics as lyrics_module
        lyrics_module.run_bg = run_background
    from .services.translate import TranslateService
    from .services.weather import WeatherService, weather_glyph
    from .ui.displays import DisplayIslands
    from .ui.island import Island
    from .ui.widgets import centered_dialog, rounded_pixmap
    from .ui.pages.home import HomePage, MessagesPage, QuotaReadings
    from .ui.pages.info import WeatherPage
    from .ui.pages.settings import SettingsPage
    from .ui.pages.system import ControlsPage, styled_menu
    from .ui.pages.tools import ClipboardPage, TranslatePage
    from .ui.theme import KIND_COLORS, THEME_NAMES, G, theme

    if verify_output is not None:
        # Even if a test folder contains fixtures, don't display or rewrite its history.
        ClipboardService._load = lambda _self: None
        TranslateService._load_history = lambda _self: []

    theme.set_mode(cfg.get("island.theme", "auto"))
    ctx = SimpleNamespace()
    ctx.media = MediaService()
    ctx.audio = AudioService()
    ctx.brightness = BrightnessService()
    ctx.radio = RadioService()
    ctx.windows = WindowService()
    ctx.notify = NotifyService()
    ctx.weather = WeatherService()
    ctx.clash = ClashService()
    ctx.translate = TranslateService()
    ctx.lyrics = LyricsService()
    ctx.clipboard = ClipboardService()
    ctx.mailcheck = MailCheckService()
    from .live.service import LiveService
    ctx.live = LiveService()
    from .learn.service import LearnService
    ctx.learn = LearnService()

    def foreground_ai():
        path = ctx.windows.foreground_application_path()
        mapping = cfg.get("notify.flash_processes") or {}
        return identify_ai_application(path, mapping if isinstance(mapping, dict) else None)

    ctx.quota_readings = QuotaReadings(app, foreground_provider=foreground_ai)
    ctx.notify.opener = lambda path: ctx.windows.open_or_focus(path)
    peak_at = peak_value = 0.0

    def peak():
        nonlocal peak_at, peak_value
        if verify_output is not None:
            return 0.0
        now = time.monotonic()
        if now - peak_at >= 0.008:
            peak_at = now
            try:
                peak_value = ctx.audio.peak()
            except Exception:  # noqa: BLE001
                peak_value = 0.0
        return peak_value

    def activity(act, duration):
        view = displays.active_view()
        if view:
            view.island.activity(act, duration)
    ctx.windows.notice.connect(lambda t: activity({"glyph": G.MESSAGE, "title": t}, 5.0))
    # 配置文件读不出来时，界面修改不会落盘：必须让用户看见，而不是只在日志里留一行。
    cfg.save_failed.connect(lambda: ctx.windows.notice.emit(
        "配置文件读取失败，界面上这次的修改不会被保存；修复后请选「重新加载配置」"))
    if cfg.load_failed:
        # 启动时的损坏在 load() 里发过一次信号但没人监听：这里补上，用户
        # 不改设置也能知道这一会话的修改不会被保存。
        ctx.windows.notice.emit("配置文件读取失败，本次的界面修改不会被保存；修复后请选「重新加载配置」")

    # ---------------- 媒体 ----------------
    media_st = {"key": None, "lyr": None, "playing": False, "state": ctx.media.state, "cover": None}
    back_timer = QtCore.QTimer(app, singleShot=True, interval=8000)

    def update_lyrics(m):
        k = ctx.lyrics.key(m) if m.get("title") else None
        changed = k != media_st["lyr"]
        media_st["lyr"] = k
        lyrics = ctx.lyrics.get(m) if k else None
        status = ctx.lyrics.status(m) if k else ""
        for view in displays.views.values():
            if changed or view.compact.lyrics is not lyrics:
                view.compact.set_lyrics(lyrics)
                view.island._kick()
                if view.pages["set"].active:
                    view.pages["set"].refresh()
            view.pages["home"].media.set_lyrics(lyrics, status)

    def on_lyrics_ready(k):
        if k == media_st["lyr"]:
            update_lyrics(media_st["state"])
    ctx.lyrics.ready.connect(on_lyrics_ready)
    # Metadata can remain unchanged for a whole song; retry temporary failures
    # and refresh local file tags without waiting for a track-change signal.
    def poll_lyrics():
        # 没有曲目时无需重试临时失败 / 刷新内嵌歌词，跳过这一轮
        if media_st["state"].get("title"):
            update_lyrics(media_st["state"])
    lyrics_timer = QtCore.QTimer(app, interval=750, timeout=poll_lyrics)

    def back_to_view():
        # 暂停一段时间后回到原来的收起视图（时间天气 / 小形象）
        for view in displays.views.values():
            compact = view.compact
            if not compact.playing() and view.previous_view and compact.view == "music":
                compact.set_view(view.previous_view)
                view.island._kick()
            view.previous_view = None
    back_timer.timeout.connect(back_to_view)

    def on_media(m):
        media_st["state"] = m
        playing = bool(m.get("playing") and m.get("title"))
        key = (m.get("app_id"), m.get("title"), m.get("artist"))
        auto_show = cfg.get("media.auto_show", True)
        started = auto_show and playing and (not media_st["playing"] or key != media_st["key"])
        if started:
            back_timer.stop()
        elif auto_show and not playing and media_st["playing"] and any(
                v.previous_view for v in displays.views.values()):
            back_timer.start()
        for view in displays.views.values():
            compact = view.compact
            compact.set_media(m)
            view.pages["home"].media.set_media(m)
            if started and compact.view in ("clock", "pet"):
                view.previous_view = compact.view
                compact.set_view("music")
            view.island._kick()
        media_st["playing"], media_st["key"] = playing, key
        update_lyrics(m)
    ctx.media.changed.connect(on_media)

    def on_cover(img):
        media_st["cover"] = img
        for view in displays.views.values():
            view.compact.set_cover(img)
            view.pages["home"].media.set_cover(img)
            view.island._kick()
    ctx.media.cover_changed.connect(on_cover)

    # ---------------- 音量（外部改动也弹出提示，像手机一样） ----------------
    vol_state = {"init": True, "last": None}

    def on_volume(v, muted):
        for view in displays.views.values():
            view.pages["ctl"].set_volume(v, muted)
            view.pages["home"].set_muted(muted)
        key = (round(v, 3), muted)
        view = displays.active_view()
        if vol_state["init"]:
            vol_state["init"] = False
        elif key != vol_state["last"] and view and not view.island.expanded:
            island, compact = view.island, view.compact
            act = {"type": "volume", "glyph": G.MUTE if muted else G.VOLUME, "value": v, "muted": muted}
            if compact.activity and compact.activity.get("type") == "volume":
                compact.update_activity(**act, until=time.monotonic() + 1.8)
            else:
                island.activity(act, 1.8)
            island._kick()
        vol_state["last"] = key
    ctx.audio.volume_changed.connect(on_volume)

    brightness_rows = []

    def on_brightness(rows):
        nonlocal brightness_rows
        brightness_rows = rows
        for view in displays.views.values():
            view.pages["ctl"].set_brightness(rows)

    def on_radio(state):
        for view in displays.views.values():
            view.pages["ctl"].set_radio(state)

    ctx.brightness.changed.connect(on_brightness)
    ctx.radio.changed.connect(on_radio)

    # ---------------- Clash ----------------
    clash_state = {"mode": None}

    def on_clash(d):
        for view in displays.views.values():
            view.pages["ctl"].set_clash(d)
        m = d.get("mode")
        view = displays.active_view()
        if d.get("ok") and clash_state["mode"] and m != clash_state["mode"] and view and not view.island.expanded:
            names = {"rule": "规则模式", "global": "全局模式", "direct": "直连模式"}
            view.island.activity({"glyph": G.GLOBE, "title": "Clash", "sub": names.get(m, m)}, 2.2)
        if d.get("ok"):
            clash_state["mode"] = m
    ctx.clash.updated.connect(on_clash)


    # ---------------- 消息提醒 ----------------
    latest_wechat_flash = None

    def on_notified(n):
        nonlocal latest_wechat_flash
        kind = n.get("kind", "other")
        ctx.quota_readings.observe_notification(kind)
        glyph = G.BOLT if kind in AI_NAMES else {"mail": G.MAIL, "qq": G.CHAT, "wechat": G.MESSAGE}.get(kind, G.BELL)
        title = n.get("name", kind)
        sub = "新邮件" if kind == "mail" else (n.get("sender") or "新消息")
        icon = None
        latest_wechat_flash = None
        if kind == "wechat":
            title = n.get("sender") or title
            sub = "新消息"
            icon = rounded_pixmap(n.get("avatar"), 26, 13)
            if n.get("source") == "flash":
                latest_wechat_flash = n["time"]
        elif kind in AI_NAMES:
            # 保留通知里的任务状态和会话名。
            sub = ai_subtitle(n.get("title"), n.get("body")) or "任务通知"
        view = displays.active_view()
        if view:
            view.compact.pet.react("surprise")
            view.pages["home"].pet.face.react("surprise")
            view.island.activity({"glyph": glyph, "color": KIND_COLORS.get(kind, KIND_COLORS["other"]),
                                  "title": title, "sub": sub, "icon": icon, "wechat_flash": latest_wechat_flash}, 4.0)
        for item in displays.views.values():
            if item.pages["msg"].active:
                item.pages["msg"].refresh()

    def on_wechat_preview():
        rows = ctx.notify.wechat_preview
        if latest_wechat_flash is None or len(rows) != 1 or rows[0].get("flash_time") != latest_wechat_flash:
            return
        row = rows[0]
        preview = {"title": row["sender"], "sub": "微信未读", "icon": rounded_pixmap(row["avatar"], 26, 13)}
        view = next((item for item in displays.views.values()
                     if (item.compact.activity or {}).get("wechat_flash") == latest_wechat_flash),
                    displays.active_view())
        if not view:
            return
        island, compact = view.island, view.compact
        current = compact.activity
        now = time.monotonic()
        if current and current["until"] > now:
            if current.get("wechat_flash") == latest_wechat_flash:
                compact.update_activity(**preview, until=max(current["until"], now + 3.0))
                island._kick()
        else:
            island.activity(dict(preview, glyph=G.MESSAGE, color=KIND_COLORS["wechat"],
                                 wechat_flash=latest_wechat_flash), 3.0)

    def on_counts(c):
        total = sum(c.values())
        for view in displays.views.values():
            view.compact.set_badges(c)
            view.island.nav.set_badge("msg", total)
            if view.pages["msg"].active:
                view.pages["msg"].refresh()
            view.island._kick()
    ctx.notify.notified.connect(on_notified)
    ctx.notify.wechat_preview_changed.connect(on_wechat_preview)
    ctx.notify.counts_changed.connect(on_counts)
    ctx.mailcheck.new_mail.connect(lambda user, n: ctx.notify._emit(
        {"kind": "mail", "name": "邮件", "source": "imap", "time": time.time()}))

    # ---------------- 天气 ----------------
    def set_weather(view, d):
        view.pages["weather"].set_data(d)
        if d.get("ok") or d.get("temp") is not None:
            g, fam = weather_glyph(d.get("icon"))
            view.compact.weather = (g, f"{round(d.get('temp', 0))}°", fam)
        view.island._kick()

    def on_weather(d):
        for view in displays.views.values():
            set_weather(view, d)
    ctx.weather.updated.connect(on_weather)

    # ---------------- 实时字幕：收起态显示最新一句 ----------------
    # 收起态两行显示：上面日语、下面中文；有人说话时优先显示，安静几秒后回到原视图（不打断音乐）
    from .live.subtitle_state import SubtitleGate
    subtitle_gate = SubtitleGate()

    def on_partial(jp):
        if subtitle_gate.begin_if_new(jp):
            displays.set_subtitle(jp=jp, zh="")   # 新句开始，清掉上一句的中文
        elif jp:
            displays.set_subtitle(jp=jp)

    def on_segment(sid, jp):
        subtitle_gate.add_segment(sid)
        displays.set_subtitle(jp=jp)

    def on_live_zh(sid, zh):
        if zh and subtitle_gate.accept_live(sid):
            displays.set_subtitle(zh=zh)

    def on_final_zh(sid, zh):
        if zh and subtitle_gate.accept_final(sid):
            displays.set_subtitle(zh=zh)

    def on_running(on):
        displays.set_listening(on)
    ctx.live.partial.connect(on_partial)
    ctx.live.segment.connect(on_segment)
    ctx.live.live_zh.connect(on_live_zh)
    ctx.live.final_zh.connect(on_final_zh)
    ctx.live.running_changed.connect(on_running)
    ctx.learn.notice.connect(lambda t: activity({"glyph": G.CHECK, "title": t}, 2.6))
    ctx.live.error.connect(lambda m: activity({"glyph": G.LIVE, "title": "实时字幕", "sub": m}, 4))

    # ---------------- 热键 ----------------
    def on_hotkey(name):
        view = displays.active_view()
        if not view:
            return
        if name == "toggle":
            view.island.toggle()
        elif name == "translate":
            view.actions.translate_clipboard()
        elif name == "clipboard":
            view.island.toggle("clip")

    # ---------------- 菜单 / 托盘 ----------------
    def reload_cfg(island):
        cfg.load()
        theme.set_mode(cfg.get("island.theme", "auto"))
        if displays.primary:
            displays.primary.island.register_hotkeys()
        sync_mode()
        sync_passthrough()
        sync_capture()
        apply_cycle()
        ctx.weather.start()          # 重设刷新间隔，并立即刷新一次
        ctx.lyrics.invalidate()
        update_lyrics(media_st["state"])
        island.activity({"glyph": G.CHECK, "title": "配置已重新加载"}, 1.8)

    def add_music_directory(island):
        island.locks.add("music-folder")
        try:
            roots = cfg.get("media.music_dirs") or []
            if not isinstance(roots, list):
                roots = []
            initial = str(roots[0]) if roots else str(Path.home() / "Music")
            chosen = QtWidgets.QFileDialog.getExistingDirectory(island, "选择音乐文件夹（只读歌词）", initial)
            if not chosen:
                return
            existing = [str(item) for item in roots if isinstance(item, str)]
            if os.path.normcase(os.path.abspath(chosen)) not in {
                    os.path.normcase(os.path.abspath(item)) for item in existing}:
                cfg.set("media.music_dirs", existing + [chosen])
            ctx.lyrics.invalidate()
            update_lyrics(media_st["state"])
            island.activity({"glyph": G.MUSIC, "title": "已添加音乐文件夹"}, 2.0)
        finally:
            island.locks.discard("music-folder")

    def refresh_lyrics(island):
        ctx.lyrics.invalidate()
        update_lyrics(media_st["state"])
        island.activity({"glyph": G.MUSIC, "title": "正在重新读取歌词"}, 1.8)

    def set_online_lyrics(island, on):
        on = bool(on)
        if bool(cfg.get("media.lyrics_online", True)) != on:
            cfg.set("media.lyrics_online", on)
            refresh_lyrics(island)

    def show_current_lyrics(island):
        compact = island.compact
        lyr = compact.lyrics
        if not lyr:
            return
        dialog = centered_dialog((compact.media.get("title") or "当前歌曲") + " · 歌词",
                                 screen=getattr(island, "scr", None))
        dialog.resize(520, 600)
        layout = QtWidgets.QVBoxLayout(dialog)
        source = "歌曲内嵌歌词" if getattr(lyr, "source", "") == "embedded" else "在线歌词"
        kind = "同步歌词" if getattr(lyr, "synced", True) else "普通歌词（无时间轴）"
        layout.addWidget(QtWidgets.QLabel(source + " · " + kind))
        body = QtWidgets.QPlainTextEdit(dialog)
        body.setReadOnly(True)
        body.setPlainText("\n".join(lyr.lines) if getattr(lyr, "synced", True) else getattr(lyr, "plain_text", ""))
        layout.addWidget(body)
        island.locks.add("lyrics-dialog")
        try:
            dialog.exec()
        finally:
            island.locks.discard("lyrics-dialog")
            dialog.deleteLater()

    # 岛内设置页与托盘/右键菜单共用同一套动作和状态读取。
    # 每个动作统一签名 action(island, *args)，由设置页传入它所属的岛。
    ctx.settings = SimpleNamespace(
        edit_config=lambda island: os.startfile(str(CONFIG_PATH)),  # noqa: S606
        reload=lambda island: reload_cfg(island),
        wechat_preview=lambda island, on: cfg.set("notify.wechat_auto_preview", bool(on)),
        show_lyrics=lambda island: show_current_lyrics(island),
        add_music=lambda island: add_music_directory(island),
        refresh_lyrics=lambda island: refresh_lyrics(island),
        online_lyrics=lambda island, on: set_online_lyrics(island, on),
        theme=lambda island, mode: (cfg.set("island.theme", mode), theme.set_mode(mode)),
        expand_mode=lambda island, mode: island.set_expand_mode(mode),
        passthrough=lambda island, on: island.set_passthrough(bool(on)),
        capture=lambda island, on: island.set_capture_visible(bool(on)),
        autostart=lambda island, on: set_autostart(bool(on)),
        open_data=lambda island: os.startfile(str(DATA_DIR)),  # noqa: S606
        quit=lambda island: app.quit(),
    )

    def settings_state(island):
        return {
            "wechat_preview": bool(cfg.get("notify.wechat_auto_preview")),
            "autostart": autostart_enabled(),
            "theme": cfg.get("island.theme"),
            "expand_mode": island.expand_mode(),
            "passthrough": island.passthrough(),
            "capture": island.capture_visible(),
            "online_lyrics": bool(cfg.get("media.lyrics_online", True)),
            "has_lyrics": bool(island.compact.lyrics),
        }
    ctx.settings_state = settings_state

    def build_menu(menu: QtWidgets.QMenu, island):
        a, st = ctx.settings, settings_state(island)
        menu.addAction("编辑配置 config.json").triggered.connect(lambda: a.edit_config(island))
        menu.addAction("重新加载配置").triggered.connect(lambda: a.reload(island))
        preview = menu.addAction("自动预览微信消息")
        preview.setCheckable(True)
        preview.setChecked(st["wechat_preview"])
        preview.triggered.connect(lambda checked: a.wechat_preview(island, checked))
        lm = menu.addMenu("本地歌词")
        current = lm.addAction("查看当前歌词")
        current.setEnabled(st["has_lyrics"])
        current.triggered.connect(lambda: a.show_lyrics(island))
        lm.addAction("添加音乐文件夹…").triggered.connect(lambda: a.add_music(island))
        lm.addAction("重新读取歌词").triggered.connect(lambda: a.refresh_lyrics(island))
        online = lm.addAction("允许联网补充歌词")
        online.setCheckable(True)
        online.setChecked(st["online_lyrics"])
        online.triggered.connect(lambda checked: a.online_lyrics(island, checked))
        tm = menu.addMenu("主题")
        for mode, label in THEME_NAMES:
            item = tm.addAction(("✓  " if st["theme"] == mode else "    ") + label)
            item.triggered.connect(lambda _=False, m=mode: a.theme(island, m))
        em = menu.addMenu("展开方式")
        for mode, label in (("hover", "悬停展开"), ("always", "常驻展开"), ("never", "长关")):
            item = em.addAction(("✓  " if st["expand_mode"] == mode else "    ") + label)
            item.triggered.connect(lambda _=False, m=mode: a.expand_mode(island, m))
        for text, key, action in (("可穿透", "passthrough", a.passthrough),
                                  ("参与截图", "capture", a.capture),
                                  ("开机自启", "autostart", a.autostart)):
            item = menu.addAction(("✓  " if st[key] else "    ") + text)
            item.triggered.connect(lambda _checked=False, act=action, k=key:
                                   act(island, not settings_state(island)[k]))
        menu.addAction("打开数据目录").triggered.connect(lambda: a.open_data(island))
        menu.addSeparator()
        menu.addAction(f"退出 {APP_NAME}").triggered.connect(lambda: a.quit(island))

    def show_menu(island, pos):
        m = styled_menu(island)
        build_menu(m, island)
        island.locks.add("menu")
        m.aboutToHide.connect(lambda: island.locks.discard("menu"))
        m.aboutToHide.connect(m.deleteLater)
        m.popup(pos)

    def update_pin(view):
        pinned = view.island.pinned()
        view.pages["home"].a_pin.set_on(pinned, G.PIN if pinned else G.EYE, "常驻展开" if pinned else "悬停展开")

    def update_passthrough(view):
        on = view.island.passthrough()
        view.pages["home"].a_pt.set_on(on, None, "可穿透")
        if on:
            view.compact.set_view("pet")
            view.island._kick()

    def sync_mode(source=None):
        for view in displays.views.values():
            if view.island is not source:
                view.island.apply_expand_mode()
            update_pin(view)

    def sync_passthrough(source=None):
        for view in displays.views.values():
            if view.island is not source:
                view.island.apply_passthrough()
            update_passthrough(view)
        displays.update_idle_views()

    def sync_capture(source=None):
        for view in displays.views.values():
            if view.island is not source:
                view.island.apply_capture_mode()

    def create_view(screen):
        view = SimpleNamespace(**vars(ctx), previous_view=None)
        pages = [HomePage(view), MessagesPage(view), WeatherPage(view), ControlsPage(view),
                 ClipboardPage(view), TranslatePage(view), SettingsPage(view)]
        island = Island(pages, peak_fn=peak, screen=screen, native_listener=False)
        view.island = island
        view.pages = {page.key: page for page in pages}
        view.actions = Actions(view)
        view.compact = island.compact
        if verify_output is not None:
            island.backdrop.start = lambda: None
        island.setWindowIcon(app.windowIcon())
        ctx.translate.finished.connect(view.actions.on_translated)
        ctx.translate.failed.connect(view.actions.on_trans_failed)
        island.flash.connect(ctx.notify.on_flash)
        island.hotkey.connect(on_hotkey)
        island.play_pause_requested.connect(ctx.media.play_pause)
        island.menu_requested.connect(lambda pos: show_menu(island, pos))
        island.mode_changed.connect(lambda _mode: sync_mode(island))
        island.passthrough_changed.connect(lambda _on: sync_passthrough(island))
        island.capture_changed.connect(lambda: sync_capture(island))

        home = view.pages["home"]
        # 导航整组居中，不再用封面锚点压缩标签间距。
        home.a_pin.clicked.connect(lambda: island.set_expand_mode("hover" if island.pinned() else "always"))
        home.a_pt.clicked.connect(lambda: island.set_passthrough(not island.passthrough()))

        def on_wheel(direction):
            cfg.set("island.compact_view", view.compact.cycle_view(direction))
            displays.update_idle_views()
            island._kick()

        island.wheel_compact.connect(on_wheel)
        compact = view.compact
        compact.set_view(cfg.get("island.compact_view", "clock"))
        compact.set_media(media_st["state"])
        compact.set_cover(media_st["cover"])
        home.media.set_media(media_st["state"])
        home.media.set_cover(media_st["cover"])
        if media_st["playing"] and cfg.get("media.auto_show", True) and compact.view in ("clock", "pet"):
            view.previous_view = compact.view
            compact.set_view("music")
        update_pin(view)
        update_passthrough(view)
        if vol_state["last"] is not None:
            volume, muted = vol_state["last"]
            view.pages["ctl"].set_volume(volume, muted)
            home.set_muted(muted)
        if brightness_rows:
            view.pages["ctl"].set_brightness(brightness_rows)
        view.pages["ctl"].set_radio(ctx.radio.state)
        if ctx.clash.last:
            view.pages["ctl"].set_clash(ctx.clash.last)
        compact.set_badges(ctx.notify.counts)
        island.nav.set_badge("msg", sum(ctx.notify.counts.values()))
        if ctx.weather.last:
            set_weather(view, ctx.weather.last)
        if ctx.weather.cities_data:
            view.pages["weather"].set_cities(ctx.weather.cities_data)
        if displays.primary:
            other = displays.primary
            compact.set_lyrics(other.compact.lyrics)
            home.media.set_lyrics(other.compact.lyrics, ctx.lyrics.status(media_st["state"]))
            src, dst = other.pages["trans"].view, view.pages["trans"].view
            dst.rows = [dict(row) for row in src.rows]
            dst.partial, dst.partial_zh = src.partial, src.partial_zh
        return view

    displays = DisplayIslands(app, ctx, create_view)
    displays.open()

    def toggle_active():
        view = displays.active_view()
        if view:
            view.island.toggle()

    def rebuild_tray_menu():
        tmenu.clear()
        view = displays.active_view()
        if view:
            tmenu.addAction("展开 / 收起").triggered.connect(toggle_active)
            build_menu(tmenu, view.island)

    tray = QtWidgets.QSystemTrayIcon(app.windowIcon(), app)
    tray.setToolTip(APP_NAME)
    tmenu = QtWidgets.QMenu()
    tmenu.aboutToShow.connect(rebuild_tray_menu)
    tray.setContextMenu(tmenu)
    tray.activated.connect(lambda r: toggle_active() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
    if verify_output is None:
        tray.show()

    # 单屏按配置轮换。多屏的待机视图随鼠标所在屏幕更新。
    cycle_timer = QtCore.QTimer(app, timeout=displays.cycle_idle_views)

    def apply_cycle():
        try:
            interval_ms = int(cfg.get("island.auto_cycle_s", 30) or 0) * 1000
        except (TypeError, ValueError, OverflowError):
            interval_ms = 30_000
        if interval_ms > 0:
            cycle_timer.start(min(interval_ms, 2_147_483_647))   # Qt 使用 32 位整数毫秒
        else:
            cycle_timer.stop()

    apply_cycle()

    # Keep one authoritative list so start and shutdown can never drift apart.
    services = (ctx.media, ctx.audio, ctx.brightness, ctx.radio, ctx.windows, ctx.notify,
                ctx.weather, ctx.clash, ctx.translate, ctx.clipboard, ctx.mailcheck, ctx.learn)

    # ---------------- 启动 ----------------
    if verify_live_lyrics:
        lyrics_timer.start()
        if verify_track is None:
            ctx.media.start()
        else:
            on_media({**verify_track, "stamp": time.monotonic()})
    if verify_output is None:
        lyrics_timer.start()
        for s in services:
            try:
                if hasattr(s, "start"):
                    s.start()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        displays.timer.start()
        displays.update_layer()

    def shutdown():
        displays.timer.stop()
        lyrics_timer.stop()
        cycle_timer.stop()
        back_timer.stop()
        ctx.quota_readings.timer.stop()
        from .services.native_ai_worker import shutdown_native_readers
        shutdown_native_readers()
        ctx.live.stop(wait=True)
        displays.close()
        for s in services:
            try:
                s.stop()
            except Exception:  # noqa: BLE001
                pass
        bg.shutdown_pool()
        if verify_output is None:
            cfg.save()
        lock.unlock()
        release_app_mutex(mutex_handle)
    app.aboutToQuit.connect(shutdown)
    if verify_output is not None:
        def finish_verification():
            exit_code = 1
            try:
                primary = displays.primary
                pages = list(primary.pages) if primary else []
                windows = [{"name": screen.name(), "origin": list(screen.geometry().topLeft().toTuple()),
                            "scale": screen.devicePixelRatio(),
                            "visible": view.island.isVisible(),
                            "on_screen": view.island.windowHandle().screen() == screen}
                           for screen, view in displays.views.items()]
                visible = bool(windows) and all(w["visible"] and w["on_screen"] for w in windows)
                result = {"ok": visible and len(pages) == 7 and len(windows) == len(app.screens()),
                          "version": __version__, "pages": pages, "window_visible": visible, "islands": windows,
                          "services_started": verify_live_lyrics and verify_track is None, "network_disabled": True,
                          "desktop_capture_disabled": True}
                if verify_live_lyrics:
                    compact = primary.compact
                    lyrics = compact.lyrics
                    result["live_lyrics"] = {
                        "track_detected": bool(compact.media.get("title")),
                        "playing": bool(compact.media.get("playing")),
                        "configured_root_count": len(ctx.lyrics._roots()),
                        "status": ctx.lyrics.status(compact.media),
                        "embedded": bool(lyrics and lyrics.source == "embedded"),
                        "timed_lines": len(lyrics.lines) if lyrics else 0,
                        "current_line": bool(compact.lyric_now()[1]),
                        "compact_size": list(compact.size_hint()),
                    }
                    result["ok"] = result["ok"] and result["live_lyrics"]["embedded"]
                    if not result["ok"] and compact.media.get("title"):
                        from .services import embedded_lyrics

                        files, _ = embedded_lyrics._snapshot(ctx.lyrics._roots())
                        result["live_lyrics"]["indexed_files"] = len(files)
                        sample = next((path for path in files if compact.media["title"] in path.stem), None)
                        if sample is not None:
                            try:
                                tags = embedded_lyrics.extract_lyrics(embedded_lyrics._open_audio(sample))
                                result["live_lyrics"]["sample_lines"] = len(tags["lines"]) if tags else 0
                            except Exception as exc:
                                result["live_lyrics"]["read_error"] = f"{type(exc).__name__}: {exc}"
                verify_output.parent.mkdir(parents=True, exist_ok=True)
                temporary = verify_output.with_suffix(verify_output.suffix + ".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, verify_output)
                exit_code = 0 if result["ok"] else 1
            finally:
                app.exit(exit_code)
        QtCore.QTimer.singleShot(8000 if verify_live_lyrics else 1000, finish_verification)
    return app.exec()
