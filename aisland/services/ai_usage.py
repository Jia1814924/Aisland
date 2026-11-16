"""Discover AI notification sources and read supported usage metrics."""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..core.runtime import DATA_DIR

AI_NAMES = {"codex": "Codex", "workbuddy": "WorkBuddy", "deepseek_harness": "DeepSeek Harness"}
NATIVE_USAGE_GUIDES = {"workbuddy": "打开「套餐与积分」",
                       "deepseek_harness": "打开「账号与余额」"}
USAGE_PROVIDERS = frozenset(("codex", *NATIVE_USAGE_GUIDES))
CACHE_PATH = DATA_DIR / "ai_usage_cache.json"
PROVIDER_KEY = "notify.ai_provider_last"
QUOTA_SOURCE_ENV = "AISLAND_WORKBUDDY_QUOTA_SOURCE"
DEFAULT_QUOTA_SOURCE = DATA_DIR / "workbuddy_quota.json"
STALE_AFTER = 120.0
RETRY_DELAYS = (2.0, 3.0, 5.0, 10.0)


def retry_interval(base: float, failures: int) -> float:
    """Retry a missed read after 2, 3, 5, then 10 seconds.

    A native billing panel appears and disappears as the user switches pages;
    retry quickly after a miss so a just-opened page is picked up at once, but
    stop hammering UI Automation while the panel stays closed. Only visible
    panels use this schedule; hidden panels retain their normal cadence.
    """
    if failures <= 0:
        return base
    return RETRY_DELAYS[min(failures - 1, len(RETRY_DELAYS) - 1)]

BASE_CREDIT_KEYS = ("basecredit", "base_credit", "base", "packagebasecredit",
                    "packagecredit", "packagebase", "套餐基础积分", "基础积分")
BONUS_CREDIT_KEYS = ("bonuscredit", "bonus_credit", "bonus", "rewardcredit",
                     "reward_credit", "reward", "奖励积分", "赠送积分")


@dataclass(frozen=True)
class Metric:
    """One card of the AI panel: percentage quota or a plain credit number."""
    label: str
    display: str
    percent: float | None
    detail: str = ""
    observed_at: float | None = None
    resets_at: float | None = None


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: float
    minutes: int | None
    resets_at: float | None
    observed_at: float | None = None


def find_codex_cli() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    desktop = local / "OpenAI" / "Codex" / "bin"
    try:
        candidates = sorted(desktop.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        candidates = []
    if candidates:
        return candidates[0]
    for part in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(part.strip('"')) / "codex.exe"
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    return None


def installed_ai() -> tuple[str, ...]:
    """Discover desktop providers from their local runtime or data directory."""
    providers = []
    if find_codex_cli() is not None:
        providers.append("codex")
    if (Path.home() / ".workbuddy" / "workbuddy.db").is_file():
        providers.append("workbuddy")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    if (local / "Programs/DeepSeek Harness/DeepSeek Harness.exe").is_file():
        providers.append("deepseek_harness")
    return tuple(providers)


def _window(raw: object, observed_at: float | None = None) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    # App Server 用 camelCase，rollout 日志里是 snake_case，两种都要认。
    used = raw.get("usedPercent", raw.get("used_percent"))
    if type(used) not in (int, float) or (type(used) is float and not math.isfinite(used)):
        return None
    minutes = raw.get("windowDurationMins", raw.get("window_minutes"))
    reset = raw.get("resetsAt", raw.get("resets_at"))
    return QuotaWindow(
        float(max(0, min(100, used))),
        minutes if type(minutes) is int and minutes > 0 else None,
        float(reset) if type(reset) in (int, float) and 946684800 <= reset < 4102444800 else None,
        observed_at,
    )


def parse_codex_limits(result: dict) -> tuple[QuotaWindow | None, QuotaWindow | None]:
    buckets = result.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        bucket = result.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        return None, None
    return _window(bucket.get("primary")), _window(bucket.get("secondary"))


def read_codex_limits() -> tuple[QuotaWindow | None, QuotaWindow | None]:
    executable = find_codex_cli()
    if executable is None:
        return None, None
    messages = queue.Queue()
    deadline = time.monotonic() + 12

    def receive(request_id):
        while True:
            message = messages.get(timeout=max(0, deadline - time.monotonic()))
            if message is None:
                return {}
            if message.get("id") == request_id:
                result = message.get("result")
                return result if isinstance(result, dict) else {}

    try:
        with subprocess.Popen(
            [str(executable), "app-server", "--listen", "stdio://"], cwd=executable.parent,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        ) as process:
            def read_replies():
                try:
                    for line in process.stdout:
                        try:
                            message = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(message, dict) and message.get("id") in (1, 2):
                            messages.put(message)
                except (OSError, ValueError):
                    pass
                finally:
                    messages.put(None)

            def send(message):
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()

            threading.Thread(target=read_replies, name="aisland-codex-quota", daemon=True).start()
            try:
                send({"id": 1, "method": "initialize",
                      "params": {"clientInfo": {"name": "aisland", "version": "1.0"}}})
                if not receive(1):
                    return None, None
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read"})
                return parse_codex_limits(receive(2))
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
    except (OSError, ValueError, queue.Empty):
        return None, None


# ------------------------------------------------- 无进程读取：Codex 日志 / DSH 账本
CODEX_TAIL_BYTES = 1_500_000
# 目录全量重扫的间隔：两次扫描之间只 stat 候选文件、只解析新增字节。
# 新开的会话文件最迟在这个间隔内被发现（追加读数不受影响）。
CODEX_SCAN_TTL = 5.0
# 重扫后保留的候选文件数上限；额度读数只取最新的几条。
CODEX_CANDIDATES = 32


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else Path.home() / ".codex"


def _log_stamp(record: dict, path: Path) -> float | None:
    stamp = record.get("timestamp")
    if isinstance(stamp, str):
        try:
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            if 0 < observed < 4102444800:
                return observed
        except (ValueError, OSError, OverflowError):
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return None


@dataclass
class _CodexFileState:
    """一个 rollout 日志的增量解析状态：只往前消费完整行。"""
    mtime_ns: int
    size: int
    offset: int = 0          # 已消费到的字节位置（最后一条完整行的末尾）
    limits: tuple = (None, None)
    identity: tuple = ()
    tail: bytes = b""          # bounded append-boundary validation, including a partial last line
    quota_anchor: tuple[int, bytes] | None = None


_codex_lock = threading.Lock()
_codex_scan: dict[str, tuple[float, list[tuple[float, Path]]]] = {}
_codex_parse: dict[Path, _CodexFileState] = {}


def _rate_limits_from_record(record, path: Path):
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return None
    bucket = payload.get("rate_limits")
    if not isinstance(bucket, dict) or bucket.get("limit_id") not in (None, "codex"):
        return None
    stamp = _log_stamp(record, path)
    if stamp is None:
        return None
    limits = (_window(bucket.get("primary"), stamp), _window(bucket.get("secondary"), stamp))
    return limits if any(item is not None for item in limits) else None


def _consume_codex_chunk(chunk: bytes, state: _CodexFileState, path: Path) -> None:
    """扫描一段字节里的完整行，只记住最后一条有效 rate_limits；半行留给下次。"""
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    position = state.offset
    for raw in chunk[: end + 1].splitlines(keepends=True):
        line_offset = position
        position += len(raw)
        if b"rate_limits" not in raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        limits = _rate_limits_from_record(record, path)
        if limits is not None:
            state.limits = limits
            state.quota_anchor = (line_offset, raw)
    state.offset += end + 1


def _seed_codex_state(path: Path, stat) -> _CodexFileState | None:
    """首次接触一个日志：只从尾部一段开始，几 MB 的旧内容不必整份解析。"""
    try:
        with path.open("rb") as handle:
            size = stat.st_size
            if size > CODEX_TAIL_BYTES:
                handle.seek(size - CODEX_TAIL_BYTES)
                handle.readline()          # 丢掉被截断的半行
            start = handle.tell()
            data = handle.read()
    except OSError:
        return None
    state = _CodexFileState(mtime_ns=stat.st_mtime_ns, size=start + len(data), offset=start,
                            identity=(stat.st_dev, stat.st_ino), tail=data[-4096:])
    _consume_codex_chunk(data, state, path)
    return state


def _advance_codex_state(path: Path, state: _CodexFileState, stat) -> _CodexFileState | None:
    """日志增长时只读上次偏移之后的新字节；被截断或原地重写则从头重新播种。"""
    if stat.st_size <= state.size or state.identity != (stat.st_dev, stat.st_ino):
        return _seed_codex_state(path, stat)
    try:
        with path.open("rb") as handle:
            # Size growth alone does not prove an append: editors and sync
            # tools also replace/rewrite logs with a longer file. Validate the
            # previous boundary and the record that supplied our saved quota.
            # True appends read only these bounded anchors plus new bytes.
            anchors = [(state.size - len(state.tail), state.tail)]
            if state.quota_anchor is not None:
                anchors.append(state.quota_anchor)
            for position, expected in anchors:
                handle.seek(position)
                if handle.read(len(expected)) != expected:
                    return _seed_codex_state(path, stat)
            handle.seek(state.offset)
            chunk = handle.read()
            size = state.offset + len(chunk)
            handle.seek(max(0, size - 4096))
            tail = handle.read(min(size, 4096))
    except OSError:
        return None
    fresh = replace(state, mtime_ns=stat.st_mtime_ns, size=size, tail=tail)
    _consume_codex_chunk(chunk, fresh, path)
    return fresh


def _scan_codex_files(root: Path) -> list[tuple[float, Path]]:
    candidates: list[tuple[float, Path]] = []
    try:
        for path in root.rglob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                # 单个日志被占用/刚消失只跳过它自己；整批排序不能被一个坏文件清空。
                continue
    except OSError:
        pass
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[:CODEX_CANDIDATES]


def read_codex_log_limits(limit: int = 6, home: Path | None = None) -> tuple[QuotaWindow | None, QuotaWindow | None]:
    """每个 rollout 日志里的 ``payload.rate_limits`` 就是一次额度读数，取最新的那条。

    这条路子只读磁盘：不用起 codex 进程，也不要求客户端面板可见。目录
    扫描最多每 ``CODEX_SCAN_TTL`` 秒一次；两次扫描之间只 stat 候选文件，
    且只解析新增字节——Codex 没在跑时，这里一次文件读都不做。
    """
    root = (home or _codex_home()) / "sessions"
    now = time.monotonic()
    with _codex_lock:
        scan = _codex_scan.get(str(root))
        if scan is None or now - scan[0] >= CODEX_SCAN_TTL:
            scan = (now, _scan_codex_files(root))
            _codex_scan[str(root)] = scan
            alive = {path for _mtime, path in scan[1]}
            for stale in [p for p in _codex_parse if p not in alive]:
                del _codex_parse[stale]
        for _mtime, path in scan[1][:limit]:
            try:
                stat = path.stat()
            except OSError:
                _codex_parse.pop(path, None)
                continue
            state = _codex_parse.get(path)
            if state is None:
                state = _seed_codex_state(path, stat)
            elif (state.mtime_ns != stat.st_mtime_ns or state.size != stat.st_size
                  or state.identity != (stat.st_dev, stat.st_ino)):
                state = _advance_codex_state(path, state, stat)
            if state is None:
                _codex_parse.pop(path, None)
                continue
            _codex_parse[path] = state
            if any(state.limits):
                return state.limits
    return None, None


def _dsh_home() -> Path:
    raw = os.environ.get("DSH_HOME")
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else Path.home() / ".dsh"


def read_deepseek_ledger(home: Path | None = None) -> list[Metric] | None:
    """读 DSH 鲸鱼挂件维护的账本：余额与今日差额都在里面，不必再打接口。

    挂件按「日初余额 − 当前余额」记账，所以 dayStart = lastBalance + todayUsage。
    """
    path = (home or _dsh_home()) / ".dshw-usage.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    balance = raw.get("lastBalance")
    if type(balance) not in (int, float) or not math.isfinite(balance):
        return None
    # 账本里的 todayUsage 只属于它那一天。挂件隔天没跑时，这条记录仍是昨天的消耗，
    # 直接当「今日已用」显示会给出一个错数，所以按 date 字段核对当天。
    stamp = str(raw.get("date") or "").strip()
    fresh = not stamp or stamp == time.strftime("%Y-%m-%d")
    today = raw.get("todayUsage")
    today = float(today) if fresh and type(today) in (int, float) and math.isfinite(today) else None
    currency = raw.get("currency")
    symbol = {"CNY": "¥", "RMB": "¥", "USD": "$"}.get(
        str(currency or "CNY").upper(), f"{currency} " if currency else "¥")
    if today is None:
        note = "今日暂无消耗" if fresh else f"账本停在 {stamp}"
        return [Metric("充值余额", f"{symbol}{balance:.2f}", None, note),
                Metric("今日已用", "—", None, f"日初 {symbol}{balance:.2f}" if fresh else "挂件今天没跑")]
    return [Metric("充值余额", f"{symbol}{balance:.2f}", None, f"今日已用 {symbol}{today:.3f}"),
            Metric("今日已用", f"{symbol}{today:.3f}", None, f"日初 {symbol}{balance + today:.2f}")]


# ---------------------------------------------------------------- usage cache
_CACHE_LOCK = threading.RLock()
_cache_path = ""
_cache_stamp = -1
_cache_data: dict = {}


def usage_stamp(metrics: list[Metric]) -> float:
    """When a reading was observed: the producer's stamp, else the read time."""
    stamp = next((m.observed_at for m in metrics if m.observed_at is not None), None)
    return time.time() if stamp is None else stamp


def _read_cache_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_usage_cache() -> dict:
    """Read the cache file once per on-disk change.

    The panel polls every 2 s and each redraw consults the cache two or three
    times; remember the parsed content keyed by the file's mtime so an
    unchanged file is never read twice.
    """
    global _cache_path, _cache_stamp, _cache_data
    path = CACHE_PATH
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(path)
    with _CACHE_LOCK:
        if key == _cache_path and stamp == _cache_stamp:
            return _cache_data
    data = _read_cache_file(path)
    with _CACHE_LOCK:
        _cache_path, _cache_stamp, _cache_data = key, stamp, data
        return data


def save_usage_cache(provider: str, metrics: list[Metric]) -> float:
    """Persist the last successful reading so it survives app/provider restarts.

    Native readings carry no ``observed_at``; identical native polls do not
    rewrite the file. A producer's newer explicit observation time is retained
    even when the numbers stay the same. Runs via bg.run_bg.
    """
    global _cache_path, _cache_stamp, _cache_data
    payload = [{"label": m.label, "display": m.display,
                "percent": m.percent, "detail": m.detail,
                "resets_at": m.resets_at} for m in metrics]
    stamp = usage_stamp(metrics)
    with _CACHE_LOCK:
        data = copy.deepcopy(load_usage_cache())
        entry = data.get(provider)
        if isinstance(entry, dict) and entry.get("metrics") == payload:
            previous_stamp = entry.get("updated")
            explicit_stamp = next((m.observed_at for m in metrics if m.observed_at is not None), None)
            if (explicit_stamp is None or (type(previous_stamp) in (int, float)
                                          and previous_stamp >= explicit_stamp)):
                return float(previous_stamp or stamp)
        data[provider] = {"updated": stamp, "metrics": payload}
        tmp = CACHE_PATH.with_name(f"{CACHE_PATH.name}.{time.time_ns()}.tmp")
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass
        else:
            # Publish the file we just wrote so the next reader skips the disk.
            try:
                stamp_ns = CACHE_PATH.stat().st_mtime_ns
            except OSError:
                _cache_path, _cache_stamp, _cache_data = "", -1, {}
            else:
                _cache_path, _cache_stamp, _cache_data = str(CACHE_PATH), stamp_ns, data
        finally:
            tmp.unlink(missing_ok=True)
    return stamp


def cached_metrics(provider: str) -> list[Metric] | None:
    entry = load_usage_cache().get(provider)
    if not isinstance(entry, dict) or not isinstance(entry.get("metrics"), list):
        return None
    out = []
    for raw in entry["metrics"]:
        if not isinstance(raw, dict):
            return None
        percent = raw.get("percent")
        if type(percent) not in (int, float) or not math.isfinite(percent):
            percent = None
        reset = raw.get("resets_at")
        if type(reset) not in (int, float) or not 946684800 <= reset < 4102444800:
            reset = None
        out.append(Metric(str(raw.get("label", "")), str(raw.get("display", "—")),
                          percent, str(raw.get("detail", "")), resets_at=reset))
    return out or None


def cached_updated(provider: str) -> float | None:
    entry = load_usage_cache().get(provider)
    stamp = entry.get("updated") if isinstance(entry, dict) else None
    if type(stamp) in (int, float) and 0 < stamp < 4102444800:
        return float(stamp)
    return None


# ---------------------------------------------------------------- WorkBuddy credits
def _as_number(value) -> float | None:
    if type(value) is bool:
        return None
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _find_credit(obj, names: tuple[str, ...]) -> float | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if normalized in names:
                number = _as_number(value)
                if number is not None:
                    return number
            found = _find_credit(value, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_credit(item, names)
            if found is not None:
                return found
    return None


def resolve_workbuddy_source(configured: str = "", data_dir: Path | None = None) -> str:
    """Pick an active quota source without touching any WorkBuddy setting.

    Priority: explicit Aisland config, then an environment override, then a
    drop-in JSON file next to Aisland's own data.  A producer only has to write
    that file; nothing about the WorkBuddy client needs to change.
    """
    for candidate in ((configured or "").strip(),
                      (os.environ.get(QUOTA_SOURCE_ENV) or "").strip()):
        if candidate:
            return candidate
    default = (data_dir or DATA_DIR) / DEFAULT_QUOTA_SOURCE.name
    return str(default) if default.is_file() else ""


def source_stamp(source: str) -> float | None:
    """Modification time of a local JSON source; loopback endpoints have none."""
    source = (source or "").strip()
    if not source or source.lower().startswith(("http://", "https://")):
        return None
    try:
        return Path(os.path.expandvars(os.path.expanduser(source))).stat().st_mtime
    except OSError:
        return None


def _read_source(source: str):
    """Read a local JSON file or a loopback HTTP endpoint (no remote URLs)."""
    if source.lower().startswith(("http://", "https://")):
        from urllib.parse import urlsplit
        import httpx
        if urlsplit(source).hostname not in ("127.0.0.1", "localhost", "::1"):
            return None
        response = httpx.get(source, timeout=3.0, follow_redirects=False)
        response.raise_for_status()
        return response.json()
    path = Path(os.path.expandvars(os.path.expanduser(source)))
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _format_number(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def read_workbuddy_credits(source: str = "") -> list[Metric] | None:
    """Parse package base / bonus credits from a configured local source."""
    source = (source or "").strip()
    if not source:
        return None
    try:
        raw = _read_source(source)
    except Exception:  # noqa: BLE001 - a failed read must fall back to the cache
        return None
    base = _find_credit(raw, BASE_CREDIT_KEYS)
    bonus = _find_credit(raw, BONUS_CREDIT_KEYS)
    if base is None or bonus is None:
        return None
    # A file source carries the producer's own timestamp, so the panel shows
    # when the numbers were produced rather than when Aisland happened to read.
    stamp = source_stamp(source)
    return [Metric("套餐基础积分", _format_number(base), None, "积分", observed_at=stamp),
            Metric("奖励积分", _format_number(bonus), None, "积分", observed_at=stamp)]


# ---------------------------------------------------------------- shared reading
def quota_label(window: QuotaWindow | None, fallback: str) -> str:
    if window is None or not window.minutes:
        return fallback
    if window.minutes == 7 * 24 * 60:
        return "周用量"
    if window.minutes % 60 == 0:
        return f"{window.minutes // 60} 小时"
    return f"{window.minutes} 分钟"


def quota_metrics(limits) -> list[Metric]:
    out = []
    for window, fallback in zip(limits, ("5 小时额度", "周额度")):
        label = quota_label(window, fallback)
        if window is None:
            out.append(Metric(label, "—", None, "暂无数据"))
            continue
        detail = "已用"
        if window.resets_at is not None:
            stamp = time.localtime(window.resets_at)
            fmt = "%H:%M" if stamp[:3] == time.localtime()[:3] else "%m/%d %H:%M"
            detail = time.strftime(fmt, stamp) + " 重置"
        out.append(Metric(label, f"{window.used_percent:.0f}%", window.used_percent, detail,
                          observed_at=window.observed_at, resets_at=window.resets_at))
    return out


def read_ai_usage(provider: str, source: str = "") -> list[Metric] | None:
    if provider == "codex":
        now = time.time()
        limits = read_codex_log_limits()
        if not any(limits) or any(w and w.resets_at is not None and w.resets_at <= now for w in limits):
            limits = read_codex_limits()
        active = tuple(w if w and (w.resets_at is None or w.resets_at > now) else None for w in limits)
        return quota_metrics(active) if any(active) else None
    if provider == "deepseek_harness":
        ledger = read_deepseek_ledger()           # 挂件账本比 UI 自动化稳，也不要求客户端在跑
        if ledger is not None:
            return ledger
    if provider in NATIVE_USAGE_GUIDES:
        from .native_ai_usage import read_native_usage

        if provider == "workbuddy" and source:
            configured = read_workbuddy_credits(source)
            if configured is not None:
                return configured
        return read_native_usage(provider)
    return None


def _newest_cached_provider(max_age: float = 3600.0) -> str | None:
    """The reading the user looked at last, but only if it is still recent."""
    newest, name = time.time() - max_age, None
    for provider, entry in load_usage_cache().items():
        if provider not in AI_NAMES or not isinstance(entry, dict):
            continue
        stamp = entry.get("updated")
        if type(stamp) in (int, float) and stamp > newest:
            newest, name = float(stamp), provider
    return name


def resolve_provider(selected: str | None, latest: str | None,
                     last: str | None, active: str | None = None) -> str | None:
    """Prefer the observed foreground client in auto mode, preserving fixed choices.

    A restart has no notifications, so without ``last`` (or the cached reading)
    the panel would jump to whichever client happens to be installed first even
    though the reading the user was watching is still on disk.
    """
    for candidate in (selected, active, latest, last):
        if candidate in AI_NAMES:
            return candidate
    return _newest_cached_provider() or next(iter(installed_ai()), None)


def _codex_remaining(metric: Metric) -> Metric:
    label = metric.label.removesuffix("用量").removesuffix("额度") + "剩余"
    if metric.percent is None:
        return replace(metric, label=label, display="—")
    percent = 100.0 - min(100.0, max(0.0, metric.percent))
    detail = "" if metric.detail == "已用" else metric.detail
    return replace(metric, label=label, display=f"{percent:.0f}%", percent=percent, detail=detail)


def display_metrics(provider: str, metrics: list[Metric] | None = None,
                    stamp: float | None = None, now: float | None = None) -> list[Metric]:
    """Resolve visible cards, falling back to the last persisted reading.

    A reading older than two minutes is labelled with its time instead of being
    cleared, so closing the provider (or restarting Aisland) keeps it visible.
    """
    metrics = metrics or cached_metrics(provider)
    if not metrics:
        labels = {"workbuddy": ("套餐基础积分", "奖励积分"),
                  "deepseek_harness": ("充值余额",)}.get(provider, ("5 小时剩余", "周剩余"))
        detail = NATIVE_USAGE_GUIDES.get(provider, "暂无数据")
        return [Metric(label, "—", None, detail) for label in labels]
    if stamp is None:
        stamp = next((m.observed_at for m in metrics if m.observed_at is not None), None)
        if stamp is None:
            stamp = cached_updated(provider)
    if provider == "codex":
        # Keep Codex's saved readings in used-percent form for existing caches.
        metrics = [_codex_remaining(metric) for metric in metrics]
    now = time.time() if now is None else now
    if stamp:
        when = time.strftime("%m/%d %H:%M", time.localtime(stamp))
        return [replace(m, detail=f"上次更新 {when}")
                if m.display != "—" and (now - stamp > STALE_AFTER
                                         or (m.resets_at is not None and m.resets_at <= now)) else m
                for m in metrics]
    return metrics
