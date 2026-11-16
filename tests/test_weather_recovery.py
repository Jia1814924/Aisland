"""Weather requests and recovery are exercised in isolated Qt processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WeatherRecoveryTests(unittest.TestCase):
    def _run(self, group):
        with tempfile.TemporaryDirectory(prefix="aisland-weather-") as state:
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).with_name("_weather_recovery_child.py")), group],
                env=dict(os.environ, AISLAND_USER_HOME=state, QT_QPA_PLATFORM="offscreen"),
                capture_output=True, encoding="utf-8", errors="replace", timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_public_defaults_and_keyless_provider_routes(self):
        self._run("WeatherPublicDefaultsTests")

    def test_transport_retry_limits_and_cancellation(self):
        self._run("WeatherTransportTests")

    def test_recovery_budget_and_shutdown(self):
        self._run("WeatherRecoveryLifecycleTests")

    def test_city_failures_preserve_other_weather(self):
        self._run("WeatherPartialFailureTests")


if __name__ == "__main__":
    unittest.main()
