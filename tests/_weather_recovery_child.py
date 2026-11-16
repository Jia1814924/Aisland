"""No network or real credentials; mock only HTTP/clock/background boundaries."""
from __future__ import annotations

from pathlib import Path
import json
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PySide6 import QtCore
from aisland.services import weather

app = QtCore.QCoreApplication([])
CITY = {"name": "合肥", "lat": 31.82, "lon": 117.23, "country": "CN"}
OTHER = {"name": "上海", "lat": 31.23, "lon": 121.47, "country": "CN"}
OPTIONS = {"provider": "open-meteo", "api_key": ""}
PAYLOAD = {"utc_offset_seconds": 28800,
           "current": {"temperature_2m": 21, "weather_code": 0, "is_day": 1}}



def response(status=200, *, headers=None):
    return httpx.Response(status, json=PAYLOAD, headers=headers,
                          request=httpx.Request("GET", "https://weather.invalid/fixture"))


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []
        self.stopped = False

    def wait(self, delay):
        self.waits.append(delay)
        self.now += delay
        return self.stopped

    def is_set(self):
        return self.stopped


class _WeatherTests(unittest.TestCase):
    def setUp(self):
        self.service = weather.WeatherService()
        real_client = self.service._client
        self.addCleanup(real_client.close)
        self.addCleanup(self.service.stop)
        self.service._client = Mock()
        self.service.cities = lambda: [dict(CITY), dict(OTHER)]

    def fake_clock(self):
        clock = _Clock()
        self.service._stopped = clock
        # stop() uses a real Event, so restore it before the cleanup registered above.
        import threading
        self.addCleanup(setattr, self.service, "_stopped", threading.Event())
        patcher = patch.object(weather.time, "monotonic", side_effect=lambda: clock.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        return clock


class WeatherPublicDefaultsTests(_WeatherTests):
    def test_source_and_example_use_free_weather_without_translation_credentials(self):
        from aisland.core.config import DEFAULTS
        example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        for config in (DEFAULTS, example):
            with self.subTest(config="defaults" if config is DEFAULTS else "example"):
                self.assertEqual(config["weather"]["provider"], "open-meteo")
                self.assertEqual(config["weather"]["api_key"], "")
                self.assertEqual(config["translate"]["api_key"], "")
                self.assertEqual(config["translate"]["import_from"], "")
                self.assertFalse(config["translate"]["use_bundled_defaults"])

    def test_default_weather_request_needs_no_credentials(self):
        self.service._client.get.return_value = response()
        result = self.service._fetch_one(OPTIONS, CITY)
        self.assertTrue(result["ok"])
        self.assertEqual(result["temp"], 21)
        call = self.service._client.get.call_args
        self.assertEqual(call.args[0], weather.OM_API)
        self.assertNotIn("appid", call.kwargs["params"])
        self.assertNotIn("apikey", call.kwargs["params"])
        self.assertEqual(call.kwargs["params"]["wind_speed_unit"], "ms")

    def test_explicit_free_provider_ignores_stale_key_for_weather_and_city_search(self):
        options = {"provider": "open-meteo", "api_key": "FICTIONAL-UNUSED-KEY"}
        self.service._client.get.return_value = response()
        self.service._fetch_one(options, CITY)
        self.assertEqual(self.service._client.get.call_args.args[0], weather.OM_API)
        self.service._client.get.reset_mock()
        with (patch.object(weather.cfg, "get", return_value=options),
              patch.object(weather, "run_bg", return_value=object()) as run,
              patch.object(self.service, "_geocode", return_value=CITY) as geocode):
            self.service.add_city("Shanghai")
            self.assertEqual(run.call_args.args[2](), CITY)
        geocode.assert_called_once_with("Shanghai")
        self.service._client.get.assert_not_called()

    def test_optional_openweather_still_requires_user_supplied_key(self):
        self.assertEqual(weather._openweather_key({"provider": "openweathermap", "api_key": "FICTIONAL-KEY"}), "FICTIONAL-KEY")
        self.assertEqual(weather._openweather_key({"provider": "openweathermap"}), "")


class WeatherTransportTests(_WeatherTests):
    def test_proxy_and_remote_disconnect_retry_once(self):
        self.fake_clock()
        for error in (httpx.ProxyError("proxy unavailable"), httpx.RemoteProtocolError("peer closed")):
            with self.subTest(error=type(error).__name__):
                self.service._client.get.reset_mock()
                self.service._client.get.side_effect = [error, response()]
                self.assertTrue(self.service._fetch_one(OPTIONS, CITY)["ok"])
                self.assertEqual(self.service._client.get.call_count, 2)

    def test_repeated_timeout_stops_after_two_attempts_and_hides_url(self):
        self.fake_clock()
        self.service._client.get.side_effect = httpx.ConnectTimeout("SECRET URL")
        with self.assertRaises(weather._WeatherNetworkError) as caught:
            self.service._fetch_one(OPTIONS, CITY)
        self.assertEqual(self.service._client.get.call_count, 2)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(weather._transient(caught.exception))

    def test_http_auth_rate_limit_and_bad_request_never_retry(self):
        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                self.service._client.get.reset_mock()
                self.service._client.get.return_value = response(status)
                with self.assertRaises(httpx.HTTPStatusError) as caught:
                    self.service._fetch_one(OPTIONS, CITY)
                self.assertEqual(self.service._client.get.call_count, 1)
                self.assertFalse(weather._transient(caught.exception))

    def test_geocoding_transport_retry_once(self):
        self.fake_clock()
        self.service._client.get.side_effect = [httpx.ConnectError("offline"), response()]
        self.assertEqual(self.service._get("https://weather.invalid", stage="geocoding").status_code, 200)
        self.assertEqual(self.service._client.get.call_count, 2)
        self.service._client.get.reset_mock()
        self.service._client.get.side_effect = httpx.ReadTimeout("raw URL")
        with self.assertRaises(weather._WeatherNetworkError):
            self.service._geocode("Shanghai")
        self.assertEqual(self.service._client.get.call_count, 2)

    def test_stop_during_transport_retry_prevents_next_request(self):
        clock = self.fake_clock()
        def stop_in_wait(delay):
            clock.stopped = True
            return True
        clock.wait = stop_in_wait
        self.service._client.get.side_effect = httpx.ReadTimeout("offline")
        with self.assertRaises(weather._WeatherNetworkError):
            self.service._fetch_one(OPTIONS, CITY)
        self.assertEqual(self.service._client.get.call_count, 1)

    def test_stopped_service_never_starts_request(self):
        self.service.stop()
        with self.assertRaises(RuntimeError):
            self.service._fetch_one(OPTIONS, CITY)
        self.service._client.get.assert_not_called()


class WeatherRecoveryLifecycleTests(_WeatherTests):
    @staticmethod
    def failed(name="合肥", retryable=True):
        return {"name": name, "ok": False, "error": "天气请求超时", "_retryable": retryable}

    def test_auto_recovery_has_three_backoff_intervals_and_stops(self):
        for delay in (15_000, 30_000, 60_000):
            self.service._done([self.failed()])
            self.assertTrue(self.service._retry_timer.isActive())
            self.assertEqual(self.service._retry_timer.interval(), delay)
        self.service._done([self.failed()])
        self.assertFalse(self.service._retry_timer.isActive())
        self.assertEqual(self.service._retry_attempt, 3)

    def test_auth_config_and_rate_limit_failures_do_not_schedule_recovery(self):
        self.service._done([self.failed(retryable=False)])
        self.assertFalse(self.service._retry_timer.isActive())
        self.assertEqual(self.service._retry_attempt, 0)

    def test_success_and_manual_refresh_reset_recovery_budget(self):
        self.service._done([self.failed()])
        self.service._done([{"name": "合肥", "ok": True, "temp": 20}])
        self.assertEqual(self.service._retry_attempt, 0)
        self.assertFalse(self.service._retry_timer.isActive())
        self.service._done([self.failed()])
        with patch.object(weather, "run_bg", return_value=object()):
            self.service.refresh()
        self.assertEqual(self.service._retry_attempt, 0)
        self.assertFalse(self.service._retry_timer.isActive())
        self.assertTrue(self.service._busy)

    def test_retry_timeout_submits_only_current_failed_city_targets(self):
        self.service._done([self.failed(), {"name": "上海", "ok": True, "temp": 22}])
        with patch.object(weather, "run_bg", return_value=object()) as run:
            self.service._retry_timer.timeout.emit()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[4], frozenset({"合肥"}))

    def test_rejected_submission_releases_busy_for_later_manual_attempt(self):
        with patch.object(weather, "run_bg", return_value=None) as run:
            self.service.refresh()
            self.assertFalse(self.service._busy)
            self.assertIsNone(self.service._request_token)
            self.service.refresh()
        self.assertEqual(run.call_count, 2)

    def test_stop_cancels_timer_and_rejects_late_callbacks_and_refresh(self):
        with patch.object(weather, "run_bg", return_value=object()) as run:
            self.service.refresh()
            callbacks = run.call_args.kwargs
            self.service._schedule_recovery({"合肥"})
            self.service.stop()
            callbacks["on_done"]([{"name": "合肥", "ok": True, "temp": 25}])
            callbacks["on_error"](httpx.ReadTimeout("late"))
            self.service._retry_timer.timeout.emit()
            self.service.refresh()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.service._retry_timer.isActive())
        self.assertFalse(self.service._busy)
        self.assertEqual(self.service.cities_data, [])

    def test_old_callback_cannot_complete_a_new_request_after_restart(self):
        with patch.object(weather, "run_bg", return_value=object()) as run:
            self.service.refresh()
            old_done = run.call_args.kwargs["on_done"]
            self.service.stop()
            self.service.start()
            current_done = run.call_args.kwargs["on_done"]
            old_done([{"name": "合肥", "ok": True, "temp": 5}])
            self.assertTrue(self.service._busy)
            self.assertEqual(self.service.cities_data, [])
            current_done([{"name": "合肥", "ok": True, "temp": 25}])
        self.assertEqual(self.service.cities_data[0]["temp"], 25)

    def test_old_city_lookup_callback_is_ignored_after_restart(self):
        events = []
        self.service.city_added.connect(lambda *args: events.append(args))
        with (patch.object(weather, "run_bg", return_value=object()) as run,
              patch.object(weather.cfg, "set") as save):
            self.service.add_city("北京")
            lookup = run.call_args.kwargs
            self.service.stop()
            self.service.start()
            lookup["on_done"]({"lat": 39.9, "lon": 116.4, "country": "CN"})
            lookup["on_error"](httpx.ReadTimeout("late"))
        self.assertEqual(events, [])
        save.assert_not_called()

    def test_inflight_old_generation_never_retries_or_continues_after_restart(self):
        old_entered, release_old, second_request = threading.Event(), threading.Event(), threading.Event()
        cities = [dict(CITY), dict(OTHER)]
        self.service.cities = lambda: list(cities)
        started = []
        threads, pending = [], []

        def get(*args, **kwargs):
            started.append(time.monotonic())
            if len(started) == 1:
                old_entered.set()
                if not release_old.wait(5):
                    raise RuntimeError("Old fixture was never released")
                raise httpx.ReadTimeout("old request finished after restart")
            second_request.set()
            return httpx.Response(200, json={**PAYLOAD, "current": {"temperature_2m": 25, "weather_code": 0, "is_day": 1}},
                                  request=httpx.Request("GET", "https://weather.invalid/fixture"))

        def run(fn, *args, on_done=None, on_error=None):
            def work():
                try:
                    data = fn(*args)
                except Exception as error:
                    pending.append((on_error, error))
                else:
                    pending.append((on_done, data))
            thread = threading.Thread(target=work)
            threads.append(thread)
            thread.start()
            return object()

        self.service._client.get.side_effect = get
        try:
            with (patch.object(weather, "run_bg", side_effect=run),
                  patch.object(weather.cfg, "get", side_effect=lambda key, default=None:
                               OPTIONS if key == "weather" else default)):
                self.service.refresh()
                self.assertTrue(old_entered.wait(2))
                old_generation = self.service._stopped
                self.service.stop()
                cities[:] = [dict(CITY)]
                self.service.start()
                self.assertTrue(old_generation.is_set())
                self.assertIsNot(self.service._stopped, old_generation)
                self.assertTrue(second_request.wait(2), "New generation did not complete independently")
                release_old.set()
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(len(started), 2, "Old batch retried or continued to its second city")
                # Actual bg.run_bg dispatches these completions to the Qt thread.
                for callback, data in pending:
                    callback(data)
            self.assertEqual(self.service.cities_data[0]["temp"], 25)
            self.assertFalse(self.service._busy)
            self.assertFalse(self.service._retry_timer.isActive())
        finally:
            release_old.set()
            for thread in threads:
                thread.join(6)


class WeatherPartialFailureTests(_WeatherTests):
    def test_failed_city_keeps_old_value_and_other_city_gets_new_value(self):
        self.service.cities_data = [{"name": "合肥", "ok": True, "temp": 20},
                                    {"name": "上海", "ok": True, "temp": 21}]
        self.service._fetch_one = Mock(side_effect=[httpx.ReadTimeout("offline"),
                                                    {"name": "上海", "ok": True, "temp": 23}])
        data = self.service._fetch_all({}, [CITY, OTHER])
        self.service._done(data)
        self.assertEqual([d["temp"] for d in self.service.cities_data], [20, 23])
        self.assertTrue(self.service.cities_data[0]["ok"])
        self.assertEqual(self.service.cities_data[0]["error"], "天气请求超时")
        self.assertEqual(self.service._retry_targets, frozenset({"合肥"}))

    def test_recovery_batch_does_not_repeat_auth_error_or_successful_city(self):
        previous = [{"name": "合肥", "ok": False, "error": "凭据无效", "_retryable": False},
                    {"name": "上海", "ok": False, "error": "天气请求超时", "_retryable": True}]
        self.service._fetch_one = Mock(return_value={"name": "上海", "ok": True, "temp": 22})
        data = self.service._fetch_all({}, [CITY, OTHER], previous, frozenset({"上海"}))
        self.service._fetch_one.assert_called_once_with({}, OTHER)
        self.service._done(data)
        self.assertEqual(data[0]["error"], "凭据无效")
        self.assertFalse(self.service._retry_timer.isActive())

    def test_exhausted_transport_error_is_retryable_but_bad_configuration_is_not(self):
        self.service._fetch_one = Mock(side_effect=[weather._WeatherNetworkError("Open-Meteo", httpx.ReadTimeout("offline")),
                                                    ValueError("请配置凭据")])
        data = self.service._fetch_all({}, [CITY, OTHER])
        self.assertEqual([d["_retryable"] for d in data], [True, False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
