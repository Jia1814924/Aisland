# -*- coding: utf-8 -*-
"""天气：默认使用 Open-Meteo（无需 Key），可选 OpenWeatherMap。

信号 updated(dict):
  {ok, error, city, temp, feels_like, desc, icon('10d'), humidity, wind, sunrise, sunset(epoch),
   hourly: [{time, temp, icon, pop}] 接下来 8 个 3 小时步长,
   daily:  [{date('YYYY-MM-DD'), min, max, icon}] 接下来最多 5 天（按城市当地日期聚合）,
   time(epoch 获取时间)}

图标：
  fluent_icon(code)   -> Segoe Fluent Icons 字形。Fluent 字体里只有 3 个天气相关字形（已在本机字体中逐一
                         渲染核对，官方列表中没有雨/雪/雷等）：E706 Brightness(太阳) / E708 QuietHours(月亮) /
                         E753 Cloud(云)。所以只能区分 晴(日)/晴(夜)/其它=云。
  weather_glyph(code) -> (字符, 字体族) 更细的映射：雨、雪、雷、雾等用 "Segoe UI Symbol" 的单色天气符号
                         （U+2600 ☀、U+2601 ☁、U+1F324 🌤、U+1F327 🌧、U+1F326 🌦、U+1F329 🌩、U+2744 ❄、U+1F32B 🌫，
                         均在本机 Segoe UI Symbol 中以单色渲染核对过）；晴夜用 Fluent 的月亮 E708
                         （Segoe UI Symbol 无单色月亮，会回落到彩色 emoji）。
"""
from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from PySide6 import QtCore

from aisland.core.bg import run_bg, ui
from aisland.core.config import cfg

API = "https://api.openweathermap.org/data/2.5"
OM_API = "https://api.open-meteo.com/v1/forecast"
OM_GEO = "https://geocoding-api.open-meteo.com/v1/search"
RECOVERY_DELAYS_MS = (15_000, 30_000, 60_000)
_TRANSIENT_NETWORK = (httpx.TimeoutException, httpx.NetworkError,
                      httpx.ProxyError, httpx.RemoteProtocolError)


class _WeatherNetworkError(RuntimeError):
    """Safe stage-only message: credential-bearing request URLs never escape."""

    def __init__(self, stage: str, error: Exception):
        super().__init__(stage + ("请求超时" if isinstance(error, httpx.TimeoutException)
                                  else "暂时无法连接"))


def _transient(error: Exception) -> bool:
    return isinstance(error, (*_TRANSIENT_NETWORK, _WeatherNetworkError))


def _openweather_key(options: dict) -> str:
    """An explicit free provider must ignore credentials left by another provider."""
    provider = str(options.get("provider") or "").strip().lower()
    if provider not in ("", "openweather", "openweathermap"):
        return ""
    return str(options.get("api_key") or "").strip()


# WMO 天气代码 -> (中文描述, OpenWeather 图标前缀)；图标前缀复用现有的字形映射
_WMO = {
    0: ("晴", "01"), 1: ("晴间多云", "02"), 2: ("多云", "03"), 3: ("阴", "04"),
    45: ("雾", "50"), 48: ("雾凇", "50"),
    51: ("小毛毛雨", "09"), 53: ("毛毛雨", "09"), 55: ("大毛毛雨", "09"), 56: ("冻毛毛雨", "09"), 57: ("冻毛毛雨", "09"),
    61: ("小雨", "10"), 63: ("中雨", "10"), 65: ("大雨", "10"), 66: ("冻雨", "13"), 67: ("冻雨", "13"),
    71: ("小雪", "13"), 73: ("中雪", "13"), 75: ("大雪", "13"), 77: ("雪粒", "13"),
    80: ("阵雨", "09"), 81: ("阵雨", "09"), 82: ("强阵雨", "09"), 85: ("阵雪", "13"), 86: ("强阵雪", "13"),
    95: ("雷暴", "11"), 96: ("雷暴伴冰雹", "11"), 99: ("雷暴伴冰雹", "11"),
}


def _wmo(code, day=True) -> tuple[str, str]:
    desc, base = _WMO.get(int(code or 0), ("", "03"))
    return desc, base + ("d" if day else "n")


def _open_meteo_daily(daily: dict, tz_off: int) -> list[dict]:
    tz = timezone(timedelta(seconds=tz_off))
    days = []
    for stamp, low, high, code in zip(
        daily.get("time") or [], daily.get("temperature_2m_min") or [],
        daily.get("temperature_2m_max") or [], daily.get("weather_code") or [],
    ):
        if stamp is None or low is None or high is None:
            continue
        _, icon = _wmo(code)
        days.append({"date": datetime.fromtimestamp(int(stamp), tz).date().isoformat(),
                     "min": low, "max": high, "icon": icon})
    return days[:5]


def parse_open_meteo(j: dict) -> dict:
    """Open-Meteo /v1/forecast（timeformat=unixtime）-> 与 parse() 相同结构。"""
    cur = j.get("current") or {}
    daily = j.get("daily") or {}
    hourly = j.get("hourly") or {}
    tz_off = int(j.get("utc_offset_seconds") or 0)
    desc, icon = _wmo(cur.get("weather_code"), bool(cur.get("is_day", 1)))
    d0 = lambda k: (daily.get(k) or [None])[0]  # noqa: E731
    out = {
        "ok": True, "error": "", "city": "",
        "temp": cur.get("temperature_2m"), "feels_like": cur.get("apparent_temperature"),
        "desc": desc, "icon": icon, "humidity": cur.get("relative_humidity_2m"),
        "wind": cur.get("wind_speed_10m"), "wind_deg": cur.get("wind_direction_10m"),
        "sunrise": d0("sunrise"), "sunset": d0("sunset"),
        "pressure": cur.get("pressure_msl"), "visibility": cur.get("visibility"),
        "clouds": cur.get("cloud_cover"),
        "temp_min": d0("temperature_2m_min"), "temp_max": d0("temperature_2m_max"),
        "tz_offset": tz_off, "hourly": [], "daily": _open_meteo_daily(daily, tz_off), "time": time.time(),
    }
    now = time.time()
    times = hourly.get("time") or []
    start = next((i for i, t in enumerate(times) if t >= now - 1800), 0)
    for i in range(start + 1, min(len(times), start + 1 + 24), 3):        # 从下一个整点起，3 小时一格
        hd, hicon = _wmo((hourly.get("weather_code") or [0])[i], bool((hourly.get("is_day") or [1] * len(times))[i]))
        pop = (hourly.get("precipitation_probability") or [0] * len(times))[i] or 0
        out["hourly"].append({"time": times[i], "temp": (hourly.get("temperature_2m") or [None])[i],
                              "icon": hicon, "pop": float(pop) / 100.0})
    return out

FLUENT_FONT = "Segoe Fluent Icons"
SYMBOL_FONT = "Segoe UI Symbol"
G_SUN = "\uE706"     # Brightness
G_MOON = "\uE708"    # QuietHours（月牙）
G_CLOUD = "\uE753"   # Cloud


def fluent_icon(owm_code: str) -> str:
    """OWM 图标代码（如 '10d'）-> Segoe Fluent Icons 字形（只有 日/月/云 三种）。"""
    c = str(owm_code or "")
    if c.startswith("01"):
        return G_MOON if c.endswith("n") else G_SUN
    return G_CLOUD


_SYMBOL = {
    "01d": ("\u2600", SYMBOL_FONT),        # ☀
    "01n": (G_MOON, FLUENT_FONT),
    "02d": ("\U0001F324", SYMBOL_FONT),    # 🌤 少云
    "02n": ("\u2601", SYMBOL_FONT),
    "03": ("\u2601", SYMBOL_FONT),         # ☁
    "04": ("\u2601", SYMBOL_FONT),
    "09": ("\U0001F327", SYMBOL_FONT),     # 🌧 阵雨
    "10d": ("\U0001F326", SYMBOL_FONT),    # 🌦 晴间雨
    "10n": ("\U0001F327", SYMBOL_FONT),
    "11": ("\U0001F329", SYMBOL_FONT),     # 🌩 雷
    "13": ("\u2744", SYMBOL_FONT),         # ❄
    "50": ("\U0001F32B", SYMBOL_FONT),     # 🌫 雾/霾
}


def weather_glyph(owm_code: str) -> tuple[str, str]:
    """OWM 图标代码 -> (字符, 字体族)。"""
    c = str(owm_code or "")
    return _SYMBOL.get(c) or _SYMBOL.get(c[:2]) or (G_CLOUD, FLUENT_FONT)


def _icon_rank(icon: str) -> int:
    """聚合一天的图标时，优先显示"更坏"的天气。"""
    order = {"11": 7, "13": 6, "09": 5, "10": 5, "50": 3, "04": 2, "03": 2, "02": 1, "01": 0}
    return order.get(str(icon)[:2], 0)


def parse(cur: dict, fc: dict | None) -> dict:
    """把 /weather 与 /forecast 的 JSON 解析成 updated 信号的载荷。"""
    w = (cur.get("weather") or [{}])[0]
    main = cur.get("main") or {}
    sys_ = cur.get("sys") or {}
    out = {
        "ok": True, "error": "",
        "city": cur.get("name") or "",
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "desc": w.get("description") or w.get("main") or "",
        "icon": w.get("icon") or "",
        "humidity": main.get("humidity"),
        "wind": (cur.get("wind") or {}).get("speed"),
        "sunrise": sys_.get("sunrise"),
        "sunset": sys_.get("sunset"),
        "pressure": main.get("pressure"),
        "visibility": cur.get("visibility"),
        "clouds": (cur.get("clouds") or {}).get("all"),
        "wind_deg": (cur.get("wind") or {}).get("deg"),
        "temp_min": main.get("temp_min"),
        "temp_max": main.get("temp_max"),
        "tz_offset": int(cur.get("timezone") or 0),
        "hourly": [], "daily": [],
        "time": time.time(),
    }
    if not fc:
        return out
    tz_off = int((fc.get("city") or {}).get("timezone", cur.get("timezone", 0)) or 0)
    tz = timezone(timedelta(seconds=tz_off))
    items = fc.get("list") or []
    for it in items[:8]:
        out["hourly"].append({
            "time": it.get("dt"),
            "temp": (it.get("main") or {}).get("temp"),
            "icon": ((it.get("weather") or [{}])[0]).get("icon", ""),
            "pop": float(it.get("pop") or 0.0),
        })
    days: dict[str, dict] = defaultdict(lambda: {"min": None, "max": None, "icons": []})
    order: list[str] = []
    for it in items:
        dt = it.get("dt")
        if dt is None:
            continue
        local = datetime.fromtimestamp(int(dt), tz)
        key = local.strftime("%Y-%m-%d")
        if key not in days:
            order.append(key)
        d = days[key]
        m = it.get("main") or {}
        lo, hi = m.get("temp_min", m.get("temp")), m.get("temp_max", m.get("temp"))
        if lo is not None:
            d["min"] = lo if d["min"] is None else min(d["min"], lo)
        if hi is not None:
            d["max"] = hi if d["max"] is None else max(d["max"], hi)
        icon = ((it.get("weather") or [{}])[0]).get("icon", "")
        if icon and 6 <= local.hour <= 21:  # 白天时段决定当天图标
            d["icons"].append(icon)
        elif icon and not d["icons"]:
            d["icons"].append(icon)
    for key in order[:5]:
        d = days[key]
        icons = d["icons"]
        icon = ""
        if icons:
            cnt = Counter(i[:2] for i in icons)
            # 最常见的；若有降水/雷暴出现 2 次以上，则显示更坏的那种
            common = cnt.most_common(1)[0][0]
            worst = max(cnt, key=lambda k: (_icon_rank(k), cnt[k]))
            pick = worst if _icon_rank(worst) >= 5 and cnt[worst] >= 2 else common
            icon = pick + "d"
        out["daily"].append({"date": key, "min": d["min"], "max": d["max"], "icon": icon})
    # 当前天气补充当地时区偏移（UI 显示日出日落可用）
    out["tz_offset"] = tz_off
    return out


class WeatherService(QtCore.QObject):
    """多城市天气。cities_updated(list) 每个城市一份 parse() 结果（含 name / index）；
    updated(dict) 只发第一个城市（灵动岛收起态显示它）。"""
    updated = QtCore.Signal(dict)
    cities_updated = QtCore.Signal(list)
    city_added = QtCore.Signal(bool, str)      # 成功?, 提示

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)
        self._client = httpx.Client(timeout=httpx.Timeout(12.0, connect=6.0), follow_redirects=True)
        self._busy = False
        self._again = False
        self._stopped = threading.Event()
        self._request_local = threading.local()
        self._request_token = None
        self._retry_attempt = 0
        self._retry_targets: frozenset[str] = frozenset()
        self._retry_timer = QtCore.QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._refresh_retry)
        self.last: dict = {}
        self.cities_data: list[dict] = []
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)

    # ---------- 城市列表 ----------
    def cities(self) -> list[dict]:
        cs = cfg.get("weather.cities")
        if isinstance(cs, list) and cs:
            return [dict(c) for c in cs if isinstance(c, dict)]
        lat, lon = cfg.get("weather.lat"), cfg.get("weather.lon")
        c = {"name": cfg.get("weather.city") or "Shanghai"}
        if lat not in (None, "") and lon not in (None, ""):
            c.update(lat=lat, lon=lon)
        return [c]

    def _save(self, cs: list[dict]):
        cfg.set("weather.cities", cs)

    def remove_city(self, i: int):
        cs = self.cities()
        if 0 <= i < len(cs) and len(cs) > 1:
            cs.pop(i)
            self._save(cs)
            if 0 <= i < len(self.cities_data):
                self.cities_data.pop(i)
            self._publish()

    def make_default(self, i: int):
        cs = self.cities()
        if 0 < i < len(cs):
            cs.insert(0, cs.pop(i))
            self._save(cs)
            if i < len(self.cities_data):
                self.cities_data.insert(0, self.cities_data.pop(i))
            self._publish()

    def add_city(self, query: str):
        stopped = self._stopped
        if stopped.is_set():
            return
        query = (query or "").strip()
        key = _openweather_key(cfg.get("weather") or {})
        if not query:
            self.city_added.emit(False, "请输入城市名")
            return

        def geo():
            if not key:
                return self._geocode(query)
            r = self._get("https://api.openweathermap.org/geo/1.0/direct", stage="城市定位",
                          params={"q": query, "limit": 1, "appid": key})
            r.raise_for_status()
            js = r.json()
            return js[0] if js else None

        def done(g):
            if self._stopped is not stopped or stopped.is_set():
                return
            if not g:
                self.city_added.emit(False, f"找不到城市：{query}（可试试英文名）")
                return
            cs = self.cities()
            cs.append({"name": query, "lat": round(g["lat"], 4), "lon": round(g["lon"], 4),
                       "country": g.get("country", "")})
            self._save(cs)
            self.city_added.emit(True, f"已添加 {query}（{g.get('country', '')}）")
            self.refresh()

        def fail(e):
            if self._stopped is not stopped or stopped.is_set():
                return
            self.city_added.emit(False, "查询失败：" + self._friendly(e))

        run_bg(self._in_request, stopped, geo, on_done=done, on_error=fail)

    # ---------- 刷新 ----------
    def start(self):
        if self._stopped.is_set():
            # Keep the old generation cancelled even if its request is still in flight.
            self._stopped = threading.Event()
        try:
            mins = max(5.0, float(cfg.get("weather.interval_min") or 15))
        except (TypeError, ValueError, OverflowError):
            mins = 15.0
        self._timer.start(int(min(mins * 60_000, 2_147_483_647)))   # Qt 使用 32 位整数毫秒
        self.refresh()

    def stop(self):
        self._stopped.set()
        self._timer.stop()
        self._retry_timer.stop()
        self._retry_targets = frozenset()
        self._request_token = None
        self._busy = self._again = False

    def refresh(self):
        """A user refresh or normal polling interval starts a fresh recovery budget."""
        if self._stopped.is_set():
            return
        self._retry_timer.stop()
        self._retry_attempt = 0
        self._retry_targets = frozenset()
        self._submit()

    def _refresh_retry(self):
        if not self._stopped.is_set() and self._retry_targets:
            self._submit(self._retry_targets)

    def _submit(self, retry_only: frozenset[str] | None = None):
        if self._stopped.is_set():
            return
        if self._busy:
            if retry_only is None:
                self._again = True
            return
        self._busy = True
        token = object()
        self._request_token = token

        def done(data):
            if self._request_token is token and not self._stopped.is_set():
                self._request_token = None
                self._done(data)

        def fail(error):
            if self._request_token is token and not self._stopped.is_set():
                self._request_token = None
                self._fail(error)

        job = run_bg(self._fetch_all, cfg.get("weather") or {}, self.cities(),
                     list(self.cities_data), retry_only, self._stopped,
                     on_done=done, on_error=fail)
        if job is None and self._request_token is token:
            self._request_token = None
            self._busy = self._again = False

    def _schedule_recovery(self, targets):
        self._retry_timer.stop()
        self._retry_targets = frozenset(targets)
        if self._stopped.is_set():
            return
        if not self._retry_targets:
            self._retry_attempt = 0
            return
        if self._retry_attempt < len(RECOVERY_DELAYS_MS):
            delay = RECOVERY_DELAYS_MS[self._retry_attempt]
            self._retry_attempt += 1
            self._retry_timer.start(delay)

    def _request_stop(self):
        return getattr(self._request_local, "stopped", self._stopped)

    def _in_request(self, stopped, fn, *args):
        """Bind a batch to its original cancellation event on reused pool workers."""
        previous = getattr(self._request_local, "stopped", None)
        self._request_local.stopped = stopped
        try:
            return fn(*args)
        finally:
            if previous is None:
                del self._request_local.stopped
            else:
                self._request_local.stopped = previous

    def _get(self, url: str, *, stage: str, **kwargs):
        """Retry one transient transport miss; HTTP/auth errors are never retried here."""
        stopped = self._request_stop()
        for attempt in range(2):
            if stopped.is_set():
                raise RuntimeError("天气服务已停止")
            try:
                return self._client.get(url, **kwargs)
            except _TRANSIENT_NETWORK as error:
                if attempt or stopped.wait(0.3):
                    raise _WeatherNetworkError(stage, error) from None

    def _params(self, key: str, c: dict) -> dict:
        p = {"appid": key, "units": cfg.get("weather.units") or "metric", "lang": cfg.get("weather.lang") or "zh_cn"}
        if c.get("lat") not in (None, "") and c.get("lon") not in (None, ""):
            p["lat"], p["lon"] = c["lat"], c["lon"]
        else:
            p["q"] = c.get("q") or c.get("name")
        return p

    def _geocode(self, name: str) -> dict | None:
        """Open-Meteo 地理编码（支持中文城市名）-> {lat, lon, country} 或 None。"""
        r = self._get(OM_GEO, stage="城市定位",
                      params={"name": name, "count": 1, "language": "zh", "format": "json"})
        r.raise_for_status()
        res = (r.json().get("results") or [])
        if not res:
            return None
        g = res[0]
        return {"lat": g["latitude"], "lon": g["longitude"], "country": g.get("country_code", "")}

    def _fetch_open_meteo(self, c: dict) -> dict:
        lat, lon = c.get("lat"), c.get("lon")
        if lat in (None, "") or lon in (None, ""):
            g = self._geocode(c.get("q") or c.get("name") or "Shanghai")
            if not g:
                raise ValueError(f"找不到城市：{c.get('name')}")
            lat, lon = g["lat"], g["lon"]
        r = self._get(OM_API, stage="Open-Meteo 天气", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "timeformat": "unixtime",
            "wind_speed_unit": "ms", "forecast_days": 6,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,weather_code,"
                       "cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,visibility",
            "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset",
        })
        r.raise_for_status()
        d = parse_open_meteo(r.json())
        d["source"] = "Open-Meteo"
        d["name"] = d["city"] = c.get("name") or ""
        return d

    def _fetch_one(self, options: dict, c: dict) -> dict:
        key = _openweather_key(options)
        if not key:
            return self._fetch_open_meteo(c)
        p = self._params(key, c)
        r = self._get(API + "/weather", stage="OpenWeather 天气", params=p)
        r.raise_for_status()
        cur = r.json()
        try:
            r2 = self._get(API + "/forecast", stage="OpenWeather 预报", params=p)
            r2.raise_for_status()
            fc = r2.json()
        except Exception:  # noqa: BLE001
            fc = None
        d = parse(cur, fc)
        d["source"] = "OpenWeatherMap"
        d["name"] = c.get("name") or d.get("city")
        return d

    def _fetch_all(self, options: dict, cities: list[dict], previous: list[dict] | None = None,
                   retry_only: frozenset[str] | None = None, stopped=None) -> list[dict]:
        return self._in_request(stopped or self._request_stop(), self._fetch_cities,
                                options, cities, previous, retry_only)

    def _fetch_cities(self, options, cities, previous, retry_only):
        stopped = self._request_stop()
        out = []
        cached = {d.get("name"): d for d in previous or []}
        for i, c in enumerate(cities):
            if stopped.is_set():
                break
            name = c.get("name", "?")
            if retry_only is not None and name not in retry_only and name in cached:
                d = dict(cached[name])
            else:
                try:
                    d = self._fetch_one(options, c)
                except Exception as e:  # noqa: BLE001
                    d = {"ok": False, "error": self._friendly(e), "name": name,
                         "_retryable": _transient(e)}
            d["index"] = i
            out.append(d)
        return out

    def _done(self, data: list[dict]):
        if self._stopped.is_set():
            return
        self._busy = False
        old = {d.get("name"): d for d in self.cities_data if d.get("ok")}
        merged = []
        for d in data:
            if not d.get("ok") and d.get("name") in old:      # 失败的城市保留上次数据
                keep = dict(old[d["name"]])
                keep.update(error=d.get("error"), index=d.get("index"))
                merged.append(keep)
            else:
                merged.append(d)
        self.cities_data = merged
        self._publish()
        if self._again:
            self._again = False
            self.refresh()
        else:
            self._schedule_recovery(d.get("name") for d in data
                                    if not d.get("ok") and d.get("_retryable"))

    def _publish(self):
        for i, d in enumerate(self.cities_data):
            d["index"] = i
        self.cities_updated.emit(list(self.cities_data))
        if self.cities_data:
            self._emit_first(self.cities_data[0])

    def _fail(self, e: Exception):
        if self._stopped.is_set():
            return
        self._busy = False
        d = dict(self.last) if self.last.get("ok") else {"ok": False}
        d["error"] = self._friendly(e)
        self._emit_first(d)
        if self._again:
            self._again = False
            self.refresh()
        else:
            self._schedule_recovery((c.get("name", "?") for c in self.cities()) if _transient(e) else ())

    @staticmethod
    def _friendly(e: Exception) -> str:
        if isinstance(e, _WeatherNetworkError):
            return str(e)
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            unauthorized = ("OpenWeather API Key 无效（新 Key 需等待最多 2 小时生效）"
                            if e.request.url.host == "api.openweathermap.org" else "天气服务拒绝访问")
            return {401: unauthorized,
                    404: "找不到该城市", 429: "请求过于频繁"}.get(code, f"天气服务错误 HTTP {code}")
        if isinstance(e, httpx.TimeoutException):
            return "天气请求超时"
        if isinstance(e, httpx.TransportError):
            return "无法连接天气服务"
        if isinstance(e, ValueError) and str(e):
            return str(e)
        return f"天气解析失败：{str(e)[:60]}"

    def _emit_first(self, d: dict):
        if d.get("ok"):
            self.last = d
        self.updated.emit(d)
