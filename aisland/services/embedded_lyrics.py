"""Read embedded lyrics from explicitly selected local music, without writing it.

``find_lyrics`` performs disk/tag I/O and belongs on a worker thread. Searches
stay inside configured roots; an explicit media file path takes precedence.
Cloud placeholders are not opened. Directory snapshots expire after 15 seconds
and tag records are reused until the file's size/mtime changes.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit
from urllib.request import url2pathname

_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".mp4"}
_PLACEHOLDER = 0x1000 | 0x40000 | 0x400000  # OFFLINE / RECALL_ON_OPEN / RECALL_ON_DATA_ACCESS
_SCAN_TTL = 15.0
_MAX_FILES, _MAX_DIRS, _MAX_DEPTH = 10000, 2000, 12
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_OFFSET = re.compile(r"\[offset\s*:\s*([+-]?\d+)\]", re.I)
_ARTIST_SPLIT = re.compile(r"\s*(?:/|、|;|；|\s&\s|\sfeat\.?\s|\sft\.?\s)\s*", re.I)
_SALT_STORE_SESSION = "sakawish.saltplayerforwindows_q65q631pyh094!saltplayerforwindows"
_LOCK = threading.RLock()
_SCANS: OrderedDict = OrderedDict()
_RECORDS: OrderedDict = OrderedDict()


def clear_cache() -> None:
    """Force the next background lookup to rebuild directory/tag snapshots."""
    with _LOCK:
        _SCANS.clear()
        _RECORDS.clear()


def _text(value) -> str:
    if isinstance(value, bytes):
        if value.startswith((b"\xff\xfe", b"\xfe\xff")):
            return value.decode("utf-16", "replace")
        if value.count(b"\0") > len(value) // 4:
            try:
                return value.decode("utf-16-le")
            except UnicodeError:
                pass
        for encoding in ("utf-8-sig", "gb18030", "latin-1"):
            try:
                return value.decode(encoding)
            except UnicodeError:
                pass
        return ""
    return value if isinstance(value, str) else ""


def _values(tags, names: set[str]) -> list[str]:
    result = []
    if tags is None:
        return result
    # VorbisComment iteration yields (name, value), not just a key.
    try:
        keys = tags.keys()
    except AttributeError:
        return result
    for key in keys:
        if str(key).casefold() not in names:
            continue
        value = tags[key]
        value = getattr(value, "text", value)
        for item in value if isinstance(value, (tuple, list)) else [value]:
            text = _text(item).strip()
            if text:
                result.append(text)
    return result


def _lines(text: str) -> list[list]:
    result = []
    offset = _OFFSET.search(text)
    offset = int(offset.group(1)) / 1000.0 if offset else 0.0
    for raw in text.splitlines():
        stamps = list(_STAMP.finditer(raw))
        pending = []
        for index, match in enumerate(stamps):
            fraction = match.group(3) or "0"
            seconds = int(match.group(1)) * 60 + int(match.group(2)) + int(fraction) / 10 ** len(fraction)
            pending.append(seconds)
            end = stamps[index + 1].start() if index + 1 < len(stamps) else len(raw)
            body = _WORD_STAMP.sub("", raw[match.end():end]).strip()
            if body:
                result.extend([[max(0.0, stamp + offset), body] for stamp in pending])
                pending = []
    return _ordered_lines(result)


def _ordered_lines(lines) -> list[list]:
    result = []
    for seconds, body in sorted(lines, key=lambda item: item[0]):
        if not math.isfinite(seconds) or seconds < 0 or not body:
            continue
        body = " ".join(body.split())
        if result and abs(result[-1][0] - seconds) < 0.001:
            if body not in result[-1][1].split(" / "):
                result[-1][1] += " / " + body
        else:
            result.append([seconds, body])
    return result


def extract_lyrics(audio) -> dict | None:
    """Extract tag payload; untimed lyrics always have an empty ``lines`` list.

    Supports ID3 USLT/SYLT, Vorbis/FLAC comments and MP4 ©lyr. SYLT format 2 is
    milliseconds; format 1 is MPEG frames and is retained only as plain text.
    Enhanced-LRC word timestamps are stripped while line timestamps are kept.
    """
    tags = getattr(audio, "tags", None)
    texts = _values(tags, {"lyrics", "unsyncedlyrics", "syncedlyrics", "synced lyrics", "synced_lyrics", "©lyr"})
    timed = []
    getall = getattr(tags, "getall", None)
    if getall is not None:
        for frame in getall("USLT"):
            text = _text(getattr(frame, "text", "")).strip()
            if text:
                texts.append(text)
        for frame in getall("SYLT"):
            pairs = getattr(frame, "text", [])
            plain, synchronised = [], []
            for pair in pairs if isinstance(pairs, (tuple, list)) else []:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    continue
                body = _text(pair[0]).strip()
                if not body:
                    continue
                plain.append(body)
                if getattr(frame, "format", None) == 2:
                    try:
                        synchronised.append([float(pair[1]) / 1000.0, body])
                    except (TypeError, ValueError, OverflowError):
                        pass
            if plain:
                texts.append("\n".join(plain))
            lines = _ordered_lines(synchronised)
            if lines:
                timed.append(lines)
    for text in texts:
        lines = _lines(text)
        if lines:
            timed.append(lines)
    if timed:
        lines = max(timed, key=lambda item: (len(item), sum(len(body) for _, body in item)))
        return {"lines": lines, "plain_text": "\n".join(body for _, body in lines)}
    if texts:
        return {"lines": [], "plain_text": max(texts, key=len)}
    return None


def _stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _available(stat) -> bool:
    return stat is not None and not (getattr(stat, "st_file_attributes", 0) & _PLACEHOLDER)


def _fingerprint(path: Path, stat) -> str:
    return str(path) + ":" + str(stat.st_mtime_ns) + ":" + str(stat.st_size)


def _roots(roots) -> list[Path]:
    result = []
    seen = set()
    for root in roots or []:
        if not isinstance(root, str) or not root.strip():
            continue
        try:
            path = Path(os.path.expandvars(os.path.expanduser(root)))
            if not path.is_absolute() or str(path).startswith("\\\\"):
                continue
            path = path.resolve()
        except (OSError, ValueError):
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _snapshot(roots) -> tuple[list[Path], str]:
    selected = _roots(roots)
    key = tuple(str(path) for path in selected)
    root_state = []
    for path in selected:
        stat = _stat(path)
        root_state.append((str(path), getattr(stat, "st_mtime_ns", None),
                           getattr(stat, "st_file_attributes", 0)))
    now = time.monotonic()
    with _LOCK:
        cached = _SCANS.get(key)
        if cached and cached[1] == root_state and now - cached[0] < _SCAN_TTL:
            _SCANS.move_to_end(key)
            return list(cached[2]), cached[3]
    files, seen = [], set()
    digest = hashlib.sha256(repr(root_state).encode("utf-8"))
    directories = 0
    for root in selected:
        if not _available(_stat(root)) or not root.is_dir():
            continue
        for current, dirs, names in os.walk(root, followlinks=False):
            directories += 1
            directory = Path(current)
            try:
                depth = len(directory.relative_to(root).parts)
            except ValueError:
                dirs[:] = []
                continue
            allowed = []
            if depth < _MAX_DEPTH and directories < _MAX_DIRS:
                for name in sorted(dirs):
                    child = directory / name
                    if (not child.is_symlink() and not getattr(child, "is_junction", lambda: False)()
                            and _available(_stat(child))):
                        allowed.append(name)
            dirs[:] = allowed
            for name in sorted(names):
                path = directory / name
                if path.suffix.casefold() not in _EXTENSIONS or path.is_symlink():
                    continue
                stat = _stat(path)
                if stat is not None:
                    digest.update((_fingerprint(path, stat) + ":" + str(getattr(stat, "st_file_attributes", 0))).encode("utf-8"))
                if not _available(stat):
                    continue
                # ReparsePoint alone is normal for locally hydrated cloud files.
                try:
                    resolved = path.resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                identity = os.path.normcase(str(resolved))
                if identity not in seen:
                    seen.add(identity)
                    files.append(path)
                if len(files) >= _MAX_FILES:
                    dirs[:] = []
                    break
            if len(files) >= _MAX_FILES or directories >= _MAX_DIRS:
                break
        if len(files) >= _MAX_FILES or directories >= _MAX_DIRS:
            break
    signature = digest.hexdigest()
    with _LOCK:
        _SCANS[key] = (now, root_state, tuple(files), signature)
        _SCANS.move_to_end(key)
        while len(_SCANS) > 8:
            _SCANS.popitem(last=False)
    return files, signature


def directory_signature(roots: list[str]) -> str:
    """Metadata signature of selected roots; recursive scans are cached for 15s."""
    return _snapshot(roots)[1]


def _open_audio(path: Path):
    import mutagen
    return mutagen.File(path, easy=False)


@dataclass(frozen=True)
class _Record:
    titles: tuple[str, ...]
    artists: tuple[str, ...]
    duration: float
    payload: dict | None
    signature: str


def _record(path: Path) -> _Record | None:
    stat = _stat(path)
    if not _available(stat):
        return None
    signature = _fingerprint(path, stat)
    with _LOCK:
        if signature in _RECORDS:
            _RECORDS.move_to_end(signature)
            return _RECORDS[signature]
    try:
        audio = _open_audio(path)
        if audio is None:
            record = None
        else:
            tags = getattr(audio, "tags", None)
            duration = float(getattr(getattr(audio, "info", None), "length", 0) or 0)
            record = _Record(tuple(_values(tags, {"title", "tit2", "©nam"})),
                             tuple(_values(tags, {"artist", "tpe1", "©art"})),
                             duration if math.isfinite(duration) and duration > 0 else 0.0,
                             extract_lyrics(audio), signature)
    except Exception:  # Invalid/unsupported local tags must not expose their contents.
        record = None
    # Keep a tag-only record for matching this scan, but retry files that had no
    # readable lyrics. A sync client can update what is readable without a new
    # size or modification time.
    if record is not None and record.payload is not None:
        with _LOCK:
            _RECORDS[signature] = record
            _RECORDS.move_to_end(signature)
            while len(_RECORDS) > 4096:
                _RECORDS.popitem(last=False)
    return record


def _normal(value: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKC", value).casefold() if character.isalnum())


def _artists(values) -> set[str]:
    return {_normal(part) for value in values for part in _ARTIST_SPLIT.split(value) if _normal(part)}


def _score(media: dict, path: Path, record: _Record) -> tuple | None:
    title = _normal(str(media.get("title") or ""))
    wanted_artists = _artists([str(media.get("artist") or "")])
    titles = {_normal(value) for value in record.titles}
    artists = _artists(record.artists)
    stem = _normal(re.sub(r"^\d{1,3}[.\s_-]+", "", path.stem))
    tagged_title = title in titles
    full_artist = _normal(str(media.get("artist") or ""))
    filename_artist = bool(full_artist) and stem in (full_artist + title, title + full_artist)
    filename_title = stem == title or filename_artist
    if not tagged_title and (titles or not filename_title):
        return None
    if wanted_artists:
        artist_match = wanted_artists <= artists
        if not artist_match and (artists or not filename_artist):
            return None
    try:
        duration = float(media.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    delta = abs(duration - record.duration) if duration > 0 and record.duration > 0 else None
    if delta is not None and delta > 5.0:
        return None
    return (int(tagged_title), int(bool(wanted_artists) and wanted_artists <= artists), int(delta is not None), -(delta or 0))


def _explicit_path(value) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.lower().startswith("file:"):
        parsed = urlsplit(value)
        if (parsed.netloc and parsed.netloc.lower() != "localhost") or parsed.query or parsed.fragment:
            return None  # Never discover/open a remote UNC path from metadata.
        value = url2pathname(parsed.path)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute() or str(path).startswith("\\\\"):
            return None
        return path
    except (OSError, ValueError):
        return None


def _result(path: Path, record: _Record, signature: str) -> dict | None:
    if record.payload is None:
        return None
    return {"lines": [list(line) for line in record.payload["lines"]],
            "plain_text": record.payload["plain_text"],
            "source_path": str(path), "directory_signature": signature,
            "file_signature": hashlib.sha256(record.signature.encode("utf-8")).hexdigest()}


def find_lyrics(media: dict, roots: list[str]) -> dict | None:
    """Find embedded lyrics without guessing across ambiguous local recordings.

    An existing explicit file is authoritative, including an untimed/no-lyrics
    result. Otherwise require exact title/artist metadata or a matching filename;
    known durations must be within five seconds. Salt's Store session can retain
    the previous duration after changing tracks, so a unique exact tag match
    can recover when no duration-compatible candidate exists. Equally strong matches with
    different lyric payloads are ambiguous and return None. Only selected roots
    are searched (at most 10,000 files/2,000 directories/12 levels).
    """
    for field in ("file_path", "local_path"):
        path = _explicit_path(media.get(field))
        if path is not None and path.suffix.casefold() in _EXTENSIONS and path.is_file():
            record = _record(path)
            return _result(path, record, "explicit") if record is not None else None
    if not str(media.get("title") or "").strip() or not roots:
        return None
    paths, signature = _snapshot(roots)
    salt_session = (media.get("source") == "smtc"
                    and str(media.get("app_id") or "").casefold() == _SALT_STORE_SESSION)
    title = _normal(str(media.get("title") or ""))
    artists = _artists([str(media.get("artist") or "")])
    exact_salt_matches = []
    duration_match_found = False
    matches = []
    for path in paths:
        record = _record(path)
        if record is None:
            continue
        score = _score(media, path, record)
        if score is not None:
            duration_match_found = True
            if record.payload is not None:
                matches.append((score, path, record))
        if (salt_session and title and artists and title in {_normal(t) for t in record.titles}
                and artists == _artists(record.artists)):
            exact_salt_matches.append((path, record))
    if not matches:
        # Count files without lyrics too: they may be the actual recording.
        if not duration_match_found and len(exact_salt_matches) == 1:
            path, record = exact_salt_matches[0]
            return _result(path, record, signature)
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score, best_path, best_record = matches[0]
    for score, _, record in matches[1:]:
        # Sub-second duration variation often reflects container rounding.
        comparable = score[:3] == best_score[:3] and abs(score[3] - best_score[3]) < 1.0
        if comparable and record.payload != best_record.payload:
            return None
    return _result(best_path, best_record, signature)
