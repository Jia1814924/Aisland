"""Shutdown cleanup regressions: background pool and service-list wiring.

bg.py is loaded with a stubbed Qt module so no PySide6 enters this process.
Structural checks keep start/shutdown lists from drifting apart again.
"""
from __future__ import annotations

import ast
import gc
import importlib.util
import sys
import subprocess
import threading
import time
import unittest
import weakref
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def load_bg():
    qt = ModuleType("PySide6")
    qt.QtCore = SimpleNamespace(
        QObject=object,
        Signal=lambda *_args: object(),
        QCoreApplication=SimpleNamespace(instance=lambda: None),
        Qt=SimpleNamespace(ConnectionType=SimpleNamespace(QueuedConnection=2)),
    )
    spec = importlib.util.spec_from_file_location(
        "aisland.core._bg_shutdown_test", PROJECT / "aisland/core/bg.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"PySide6": qt}):
        spec.loader.exec_module(module)
    return module


def find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


class BgPoolTests(unittest.TestCase):
    def test_shutdown_cancels_queued_jobs_without_blocking(self):
        bg = load_bg()
        started, release = threading.Event(), threading.Event()
        pool = bg._BackgroundPool(max_workers=1)
        with patch.object(bg, "_pool", pool):
            busy = pool.submit(lambda: (started.set(), release.wait(5)))
            self.assertTrue(started.wait(2))
            queued = pool.submit(lambda: "queued")
            began = time.monotonic()
            bg.shutdown_pool(timeout=0.1)
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 1.0)
            self.assertTrue(queued.cancelled())
            release.set()
            busy.result(timeout=5)
            bg.shutdown_pool()  # idempotent

    def test_late_background_job_is_dropped_after_shutdown(self):
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        with patch.object(bg, "_pool", pool):
            bg.shutdown_pool()
            self.assertIsNone(bg.run_bg(lambda: "late"))
        pool.shutdown(wait=True)

    def test_shutdown_joins_inflight_workers_bounded(self):
        """正在跑的任务（UIA 采集最长 8s）退出前有界收敛，不无限等也不丢回调序。"""
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        with patch.object(bg, "_pool", pool):
            started, release = threading.Event(), threading.Event()
            pool.submit(lambda: (started.set(), release.wait(5)))
            self.assertTrue(started.wait(2))
            began = time.monotonic()
            bg.shutdown_pool(timeout=0.3)      # 工人卡着：最多等 0.3s
            self.assertLess(time.monotonic() - began, 1.5)
            release.set()
            deadline = time.monotonic() + 5
            while any(t.name.startswith("aisland") for t in threading.enumerate()):
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            self.assertFalse(any(t.name.startswith("aisland") for t in threading.enumerate()))
        pool.shutdown(wait=True)

    def test_ui_after_shutdown_is_dropped_without_raising(self):
        """退出后迟到的回调直接丢弃，不能落到拆到一半的窗口/配置上。"""
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        with patch.object(bg, "_pool", pool):
            bg.shutdown_pool()
            ran = []
            bg.ui(lambda: ran.append(1))
            self.assertEqual(ran, [])
        pool.shutdown(wait=True)

    def test_queued_callback_is_gated_by_the_stopped_flag(self):
        """已 emit 进事件队列的回调，退出开始后才执行也必须被丢弃。"""
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        with patch.object(bg, "_pool", pool):
            ran = []
            obj = object()
            bg._Invoker._dispatch(obj, lambda: ran.append(1))
            self.assertEqual(ran, [1])
            bg.shutdown_pool()
            bg._Invoker._dispatch(obj, lambda: ran.append(2))
            self.assertEqual(ran, [1])
        pool.shutdown(wait=True)

    def test_shutdown_never_waits_for_reader_threads(self):
        """join 范围只限线程池工人；Codex/原生采集读线程不能拖住退出。"""
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        release = threading.Event()
        reader = threading.Thread(target=lambda: release.wait(5),
                                  name="aisland_999", daemon=True)
        reader.start()
        with patch.object(bg, "_pool", pool):
            began = time.monotonic()
            bg.shutdown_pool(timeout=5.0)      # 若误等 reader，这里会吃满 5 秒
            self.assertLess(time.monotonic() - began, 1.0)
        release.set()
        reader.join(5)
        pool.shutdown(wait=True)


class BackgroundPoolTests(unittest.TestCase):
    def test_idle_worker_releases_completed_task_arguments(self):
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)

        class Payload:
            pass

        value = Payload()
        retained = weakref.ref(value)
        try:
            pool.submit(lambda argument: None, value).result(timeout=2)
            del value
            deadline = time.monotonic() + 2
            while retained() is not None and time.monotonic() < deadline:
                gc.collect()
                time.sleep(0.005)
            self.assertIsNone(retained(), "An idle worker retained its finished task")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def test_worker_limit_and_all_queued_jobs_complete(self):
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=2)
        release = threading.Event()
        both_started = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def job(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_started.set()
            release.wait(3)
            with lock:
                active -= 1
            return value * 2

        try:
            futures = [pool.submit(job, value) for value in range(8)]
            self.assertTrue(both_started.wait(2))
            self.assertEqual(sum(future.running() for future in futures), 2)
            release.set()
            self.assertEqual([future.result(timeout=3) for future in futures], list(range(0, 16, 2)))
            self.assertEqual(peak, 2)
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)

    def test_cancelled_job_never_runs_and_failure_does_not_lose_a_worker(self):
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        entered, release = threading.Event(), threading.Event()
        seen = []
        try:
            running = pool.submit(lambda: (entered.set(), release.wait(3)))
            self.assertTrue(entered.wait(2))
            cancelled = pool.submit(lambda: seen.append("cancelled"))
            self.assertTrue(cancelled.cancel())
            failed = pool.submit(lambda: 1 / 0)
            following = pool.submit(lambda: "still works")
            release.set()
            running.result(timeout=3)
            with self.assertRaises(ZeroDivisionError):
                failed.result(timeout=3)
            self.assertEqual(following.result(timeout=3), "still works")
            self.assertEqual(seen, [])
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)
        with self.assertRaises(RuntimeError):
            pool.submit(lambda: None)

    def test_shutdown_from_a_worker_does_not_join_itself(self):
        bg = load_bg()
        pool = bg._BackgroundPool(max_workers=1)
        future = pool.submit(lambda: pool.shutdown(wait=True))
        self.assertIsNone(future.result(timeout=2))
        pool.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in pool._threads))

    def test_a_blocked_job_cannot_hold_interpreter_exit(self):
        # A function-level timing test misses ThreadPoolExecutor's implicit
        # interpreter-exit join. The child must exit with its job still blocked.
        code = """
import sys, threading
sys.path.insert(0, sys.argv[1])
from test_shutdown_cleanup import load_bg
bg = load_bg()
started = threading.Event()
release = threading.Event()
bg.run_bg(lambda: (started.set(), release.wait(30)))
assert started.wait(2)
bg.shutdown_pool(timeout=0.1)
print("bounded-exit", flush=True)
"""
        began = time.monotonic()
        result = subprocess.run([sys.executable, "-B", "-c", code, str(PROJECT / "tests")],
                                cwd=PROJECT, capture_output=True, text=True,
                                encoding="utf-8", timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bounded-exit", result.stdout)
        self.assertLess(time.monotonic() - began, 4)


class ShutdownWiringTests(unittest.TestCase):
    def test_app_uses_one_services_list_for_start_and_shutdown(self):
        tree = ast.parse((PROJECT / "aisland/app.py").read_text(encoding="utf-8"))
        main = find_function(tree, "main")
        self.assertIsNotNone(main)
        assignments = [n for n in ast.walk(main)
                       if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "services" for t in n.targets)]
        self.assertEqual(len(assignments), 1)
        values = assignments[0].value
        self.assertIsInstance(values, ast.Tuple)
        names = {e.attr for e in values.elts if isinstance(e, ast.Attribute)}
        self.assertEqual(names, {"media", "audio", "brightness", "radio", "windows", "notify",
                                 "weather", "clash", "translate", "clipboard", "mailcheck", "learn"})
        loops = [n for n in ast.walk(main)
                 if isinstance(n, ast.For) and isinstance(n.iter, ast.Name)
                 and n.iter.id == "services"]
        self.assertEqual(len(loops), 2)

    def test_shutdown_closes_background_pool(self):
        tree = ast.parse((PROJECT / "aisland/app.py").read_text(encoding="utf-8"))
        shutdown = find_function(tree, "shutdown")
        self.assertIsNotNone(shutdown)
        calls = [n for n in ast.walk(shutdown)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "shutdown_pool"]
        self.assertEqual(len(calls), 1)
        operations = [n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                      for n in ast.walk(shutdown) if isinstance(n, ast.Call)
                      and isinstance(n.func, (ast.Name, ast.Attribute))]
        self.assertLess(operations.index("shutdown_native_readers"), operations.index("shutdown_pool"))

    def test_save_failed_is_surfaced_on_the_island(self):
        """配置读不出来时界面修改不落盘：必须经 notice 通道让用户看见。"""
        tree = ast.parse((PROJECT / "aisland/app.py").read_text(encoding="utf-8"))
        connects = [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "connect" and isinstance(n.func.value, ast.Attribute)
                    and n.func.value.attr == "save_failed"]
        self.assertEqual(len(connects), 1)

    def test_pipeline_waits_only_for_recognition_workers(self):
        tree = ast.parse((PROJECT / "aisland/live/pipeline.py").read_text(encoding="utf-8"))
        method = find_function(tree, "_shutdown_pools")
        self.assertIsNotNone(method)
        waits = [kw.value.value for n in ast.walk(method)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "shutdown"
                 for kw in n.keywords if kw.arg == "wait"]
        self.assertEqual(waits, [False, True])


if __name__ == "__main__":
    unittest.main()
