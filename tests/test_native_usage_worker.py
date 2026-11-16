"""Real pipes/processes exercise hangs, recovery, GUI streams and shutdown."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aisland.services import native_ai_worker as worker_module

CHILD = Path(__file__).with_name("_native_usage_worker_child.py")


class NativeUsageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aisland-native-reader-")
        self.addCleanup(self.temporary.cleanup)
        self.area = Path(self.temporary.name)
        self.processes = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.spawn_patch = patch.object(worker_module.subprocess, "Popen", side_effect=spawn)
        self.spawn_patch.start()
        self.addCleanup(self.spawn_patch.stop)

    def reader(self, mode="ok", provider="workbuddy", *, windowed=False):
        marker = self.area / f"{provider}-{mode}"
        command = [sys.executable, "-B", str(CHILD), mode, str(marker)]
        if windowed:
            command.append("--windowed")
        reader = worker_module.NativeUsageWorker(provider, command=command, timeout=1.5)
        self.addCleanup(reader.close)
        return reader, marker

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(condition())

    def test_hung_process_is_killed_and_next_read_recovers(self):
        reader, marker = self.reader("hang-first")
        started = time.monotonic()
        self.assertIsNone(reader.read())
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(marker.exists(), "The fixture never entered its hung read")
        self.assertIsNotNone(self.processes[0].poll())
        self.assertEqual(reader.read()[0].display, "628")
        self.assertEqual(len(self.processes), 2)

    def test_successful_reads_reuse_one_process_and_keep_metric_fields(self):
        reader, _ = self.reader()
        for _ in range(3):
            metrics = reader.read()
            self.assertEqual((metrics[0].label, metrics[0].display, metrics[0].percent, metrics[0].detail),
                             ("奖励积分", "628", None, "剩余积分"))
        self.assertEqual(len(self.processes), 1)

    def test_cold_start_does_not_consume_the_read_budget(self):
        reader, _ = self.reader("slow-start")
        reader.timeout = 0.3
        reader.startup_timeout = 4
        started = time.monotonic()
        self.assertEqual(reader.read()[0].display, "628")
        self.assertGreater(time.monotonic() - started, reader.timeout)
        self.assertEqual(reader.read()[0].display, "628")
        self.assertEqual(len(self.processes), 1)

    def test_startup_timeout_is_bounded_and_next_attempt_recovers(self):
        reader, marker = self.reader("startup-hang-first")
        reader.startup_timeout = 0.8
        started = time.monotonic()
        self.assertIsNone(reader.read())
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertTrue(marker.exists(), "The fixture never entered its startup")
        self.assertIsNotNone(self.processes[0].poll())
        self.assertEqual(reader.read()[0].display, "628")
        self.assertEqual(len(self.processes), 2)

    def test_close_interrupts_startup_without_waiting_for_its_deadline(self):
        reader, marker = self.reader("startup-hang-first")
        reader.startup_timeout = 20
        results = []
        thread = threading.Thread(target=lambda: results.append(reader.read()))
        thread.start()
        try:
            self.wait_for(marker.exists)
            started = time.monotonic()
            reader.close()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(results, [None])
            self.assertIsNotNone(self.processes[0].poll())
        finally:
            reader.close()
            thread.join(2)

    def test_windowed_exe_missing_python_streams_still_uses_pipes(self):
        reader, _ = self.reader(windowed=True)
        self.assertEqual(reader.read()[0].display, "628")

    def test_close_interrupts_a_hung_read_without_waiting_for_deadline(self):
        reader, marker = self.reader("hang-first")
        reader.timeout = 10
        results = []
        thread = threading.Thread(target=lambda: results.append(reader.read()))
        thread.start()
        try:
            self.wait_for(marker.exists)
            started = time.monotonic()
            reader.close()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(results, [None])
            self.assertIsNotNone(self.processes[0].poll())
            self.assertIsNone(reader.read())
        finally:
            reader.close()
            thread.join(2)

    def test_one_hung_provider_does_not_block_another(self):
        first, marker = self.reader("hang-first")
        second, _ = self.reader(provider="deepseek_harness")
        results = []
        first.timeout = 10
        thread = threading.Thread(target=lambda: results.append(first.read()))
        thread.start()
        try:
            self.wait_for(marker.exists)
            self.assertEqual(second.read()[0].display, "628")
            self.assertTrue(thread.is_alive())
        finally:
            first.close()
            thread.join(2)
        self.assertEqual(results, [None])

    def test_reader_exit_is_a_failed_sample_and_next_attempt_restarts(self):
        reader, _ = self.reader("exit")
        self.assertIsNone(reader.read())
        reader.command = [sys.executable, "-B", str(CHILD), "ok", str(self.area / "recovered")]
        self.assertEqual(reader.read()[0].display, "628")

    def test_no_visible_page_is_a_normal_miss_without_restarting(self):
        reader, _ = self.reader("none")
        self.assertIsNone(reader.read())
        self.assertIsNone(reader.read())
        self.assertEqual(len(self.processes), 1)

    def test_controller_shutdown_closes_workers_and_rejects_late_jobs(self):
        reader, _ = self.reader()
        with (patch.object(worker_module, "_workers", {"workbuddy": reader}),
              patch.object(worker_module, "_stopped", False)):
            self.assertEqual(worker_module.read_isolated_usage("workbuddy")[0].display, "628")
            worker_module.shutdown_native_readers()
            self.assertIsNone(worker_module.read_isolated_usage("workbuddy"))
            self.assertEqual(worker_module._workers, {})
        self.assertIsNotNone(self.processes[0].poll())

    def test_parent_death_also_stops_a_hung_helper(self):
        import psutil

        marker = self.area / "parent-death"
        host = subprocess.Popen([sys.executable, "-B", str(CHILD), "host", str(marker)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        child = None
        try:
            self.wait_for(marker.exists)
            pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
            child = psutil.Process(pid)
            host.kill()
            host.wait(timeout=2)
            child.wait(timeout=2)
        finally:
            if host.poll() is None:
                host.kill()
                host.wait(timeout=2)
            if child is not None and child.is_running():
                # This is a fixture-owned helper, never an application process.
                self.assertIn(str(marker), child.cmdline())
                child.kill()
                child.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
