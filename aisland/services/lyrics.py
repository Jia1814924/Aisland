# -*- coding: utf-8 -*-
"""Local embedded lyrics first, with an optional NetEase fallback for music apps.

Failed lookups expire. Successful results remain visible during background
refreshes. ready() carries only an opaque track key; no private text is logged.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading
import time
import unicodedata

import httpx
from PySide6 import QtCore

from aisland.core.bg import run_bg, ui
from aisland.core.config import cfg
from .embedded_lyrics import clear_cache, find_lyrics

API = "https://music.163.com/api"
HEADERS = {"Referer": "https://music.163.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_WORD_TS = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_OFFSET = re.compile(r"\[offset\s*:\s*([+-]?\d+)\s*\]", re.I)
_CREDIT = re.compile(r"^\s*[^\s:：]{1,12}\s*[:：]")
_BROWSERS_AND_VIDEO = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi", "zen.exe",
                       "browser", "webview", "bilibili", "youtube", "potplayer", "vlc", "mpv", "mpc-hc")
_MUSIC_APPS = ("qqmusic", "qq音乐", "cloudmusic", "网易云", "spotify", "foobar2000", "musicbee",
               "aimp", "itunes", "applemusic", "apple music", "zunemusic", "dopamine", "yesplaymusic",
               "lx-music", "listen1", "audacious", "clementine", "strawberry", "winamp")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse Enhanced LRC; add millisecond offsets and clamp positions to zero."""
    text = text or ""
    offsets = list(_OFFSET.finditer(text))
    offset = int(offsets[-1].group(1)) / 1000.0 if offsets else 0.0
    out = []
    for raw in text.splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        body = _WORD_TS.sub("", _OFFSET.sub("", raw[stamps[-1].end():])).strip()
        for stamp in stamps:
            fraction = stamp.group(3) or "0"
            position = int(stamp.group(1)) * 60 + int(stamp.group(2)) + int(fraction) / 10 ** len(fraction)
            out.append((max(0.0, position + offset), body))
    out.sort(key=lambda item: item[0])
    index = 0
    while index < len(out) and (not out[index][1] or (out[index][0] < 40 and _CREDIT.match(out[index][1]))):
        index += 1
    return out[index:]


class Lyrics:
    def __init__(self, lines: list[tuple[float, str]], trans: list[tuple[float, str]],
                 *, plain_text: str = "", source: str = "netease"):
        self.times = [position for position, _ in lines]
        self.lines = [text for _, text in lines]
        translations = {round(position, 1): text for position, text in trans if text}
        self.trans = [translations.get(round(position, 1), "") for position in self.times]
        self.plain_text = plain_text or ""
        self.source = source if source in ("embedded", "netease") else "embedded"
        self.synced = bool(self.lines)

    def __bool__(self):
        return bool(self.lines or self.plain_text)

    def at(self, pos: float) -> tuple[int, str, str]:
        index = bisect.bisect_right(self.times, pos + 0.15) - 1
        if index < 0:
            return -1, "", ""
        return index, self.lines[index], self.trans[index]


@dataclass
class _Result:
    lyrics: Lyrics | None
    status: str


@dataclass
class _Entry:
    lyrics: Lyrics | None
    status: str
    expires: float


def _normal(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _duration(media: dict) -> float:
    try:
        value = float(media.get("duration") or 0)
        return value if math.isfinite(value) and value > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


def _artists(value: str) -> set[str]:
    return {part for part in re.split(r"\s*(?:/|;|、|&|\bfeat\.?\s|\bft\.?\s)\s*", _normal(value)) if part}


class LyricsService(QtCore.QObject):
    ready = QtCore.Signal(str)
    LOCAL_TTL = 15.0
    SUCCESS_TTL = 3600.0
    NEGATIVE_TTL = 15.0
    ERROR_TTL = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self._client = httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), headers=HEADERS)
        self._cache: dict[str, _Entry] = {}
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()
        self._generation = 0

    @staticmethod
    def _roots() -> list[str]:
        configured = cfg.get("media.music_dirs", [])
        return [value for value in configured if isinstance(value, str) and value.strip()] if isinstance(configured, list) else []

    @staticmethod
    def key(media: dict) -> str:
        duration = _duration(media)
        identity = [_normal(media.get(name)) for name in ("app_id", "app_name", "source", "song_id", "title", "artist", "album")]
        identity.extend([duration > 0, round(duration / 5.0) if duration else 0,
                         str(media.get("local_path") or media.get("file_path") or ""),
                         LyricsService._roots(), bool(cfg.get("media.lyrics_online", True))])
        encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return "track:" + hashlib.sha256(encoded).hexdigest()[:24]

    def status(self, media: dict) -> str:
        """Return disabled/loading/synced/plain/not_found/offline; no private text."""
        if not cfg.get("media.lyrics", True):
            return "disabled"
        key = self.key(media)
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.lyrics:
                return entry.status
            if key in self._pending:
                return "loading"
            return entry.status if entry else "not_found"

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._cache.clear()
            self._pending.clear()
        clear_cache()

    def get(self, media: dict) -> Lyrics | None:
        if not cfg.get("media.lyrics", True) or not (media.get("title") or media.get("local_path") or media.get("file_path")):
            return None
        key = self.key(media)
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            previous = entry.lyrics if entry else None
            if entry and entry.expires > now:
                return previous
            if key in self._pending:
                return previous
            generation = self._generation
            self._pending[key] = generation
        run_bg(self._fetch, dict(media), self._roots(), bool(cfg.get("media.lyrics_online", True)),
               on_done=lambda result: self._done(key, result, generation),
               on_error=lambda _error: self._done(key, _Result(None, "offline"), generation))
        return previous

    def _done(self, key: str, result: _Result, generation: int):
        with self._lock:
            if generation != self._generation or self._pending.get(key) != generation:
                return
            self._pending.pop(key, None)
            if result.lyrics:
                ttl = self.LOCAL_TTL if result.lyrics.source == "embedded" else self.SUCCESS_TTL
            else:
                ttl = self.ERROR_TTL if result.status == "offline" else self.NEGATIVE_TTL
            self._cache[key] = _Entry(result.lyrics, result.status, time.monotonic() + ttl)
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
        self.ready.emit(key)

    @staticmethod
    def _online_allowed(media: dict) -> bool:
        identity = _normal(" ".join(str(media.get(name) or "") for name in ("app_id", "app_name", "source")))
        if any(name in identity for name in _BROWSERS_AND_VIDEO):
            return False
        return _normal(media.get("source")) == "netease" or any(name in identity for name in _MUSIC_APPS)

    def _fetch(self, media: dict, roots: list[str] | None = None, online: bool | None = None) -> _Result:
        roots = self._roots() if roots is None else roots
        online = bool(cfg.get("media.lyrics_online", True)) if online is None else online
        try:
            embedded = find_lyrics(media, roots)
        except Exception:
            embedded = None
        if embedded:
            lines = []
            for item in embedded.get("lines", []):
                try:
                    position, text = float(item[0]), str(item[1]).strip()
                    if math.isfinite(position) and position >= 0 and text:
                        lines.append((position, _WORD_TS.sub("", text)))
                except (ValueError, TypeError, IndexError):
                    continue
            lines.sort(key=lambda item: item[0])
            lyrics = Lyrics(lines, [], plain_text=str(embedded.get("plain_text") or ""), source="embedded")
            if lyrics:
                return _Result(lyrics, "synced" if lyrics.synced else "plain")
        if not online or not self._online_allowed(media):
            return _Result(None, "offline")
        song_id = str(media.get("song_id") or "")
        if not song_id or not re.fullmatch(r"\d{1,20}", song_id):
            song_id = self._search(media)
        if not song_id:
            return _Result(None, "not_found")
        response = self._client.get(API + "/song/lyric", params={"id": song_id, "lv": 1, "tv": -1})
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("nolyric") or body.get("uncollected"):
            return _Result(None, "not_found")
        original, translated = body.get("lrc"), body.get("tlyric")
        text = original.get("lyric", "") if isinstance(original, dict) else ""
        translation = translated.get("lyric", "") if isinstance(translated, dict) else ""
        lines = parse_lrc(text)
        lyrics = Lyrics(lines, parse_lrc(translation), plain_text=text if text and not lines else "", source="netease")
        return _Result(lyrics, "synced" if lyrics.synced else "plain") if lyrics else _Result(None, "not_found")

    def _search(self, media: dict) -> str:
        if not self._online_allowed(media):
            return ""
        title, artist = str(media.get("title") or "").strip(), str(media.get("artist") or "").strip()
        duration = _duration(media)
        if not title or (duration and not 30 <= duration <= 15 * 60) or (not duration and not artist):
            return ""
        response = self._client.get(API + "/search/get/web", params={"s": f"{title} {artist}".strip(), "type": 1, "limit": 8})
        response.raise_for_status()
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        songs = result.get("songs") if isinstance(result, dict) else None
        if not isinstance(songs, list):
            return ""
        candidates = []
        for song in songs:
            if not isinstance(song, dict) or _normal(song.get("name")) != _normal(title):
                continue
            performers = song.get("artists") or song.get("ar") or []
            found_artists = _artists(" / ".join(str(item.get("name") or "") for item in performers if isinstance(item, dict))) if isinstance(performers, list) else set()
            if artist and _artists(artist) != found_artists:
                continue
            milliseconds = song.get("duration", song.get("dt", 0))
            try:
                difference = abs(float(milliseconds or 0) / 1000 - duration) if duration else 0.0
            except (ValueError, TypeError):
                continue
            if not math.isfinite(difference) or (duration and difference > 8):
                continue
            identifier = str(song.get("id") or "")
            if re.fullmatch(r"\d{1,20}", identifier):
                candidates.append((difference, identifier))
        return min(candidates)[1] if candidates else ""
