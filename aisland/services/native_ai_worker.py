"""Isolate blocking UI Automation in restartable, stdio-connected processes."""
from __future__ import annotations

import atexit
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .ai_usage import Metric, NATIVE_USAGE_GUIDES

READ_TIMEOUT = 8.0
STARTUP_TIMEOUT = 30.0
MAX_MESSAGE = 16_384
_log = logging.getLogger(__name__)


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--native-usage-worker"]
    return [sys.executable, "-B", str(Path(__file__).resolve().parents[2] / "run.pyw"),
            "--native-usage-worker"]


def _receive(process, responses):
    """The reader owns stdout so shutdown never waits on its buffered-read lock."""
    try:
        while line := process.stdout.readline(MAX_MESSAGE + 1):
            if len(line) > MAX_MESSAGE:
                break
            try:
                response = json.loads(line)
            except ValueError:
                break
            responses.put_nowait(response)
    except (OSError, ValueError, queue.Full):
        pass
    finally:
        try:
            responses.put_nowait(None)
        except queue.Full:
            pass
        process.stdout.close()


def _terminate(process):
    if process is None:
        return
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass


class NativeUsageWorker:
    """One sequential reader per provider; other providers never wait on its lock."""

    def __init__(self, provider: str, *, command=None, timeout=READ_TIMEOUT,
                 startup_timeout=STARTUP_TIMEOUT):
        self.provider = provider
        self.command = list(command) if command is not None else _command()
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process = None
        self._responses = None
        self._ready_process = None
        self._closed = False
        self._serial = 0

    def _start(self):
        with self._state_lock:
            if self._closed:
                return None, None
            if self._process is None:
                process = subprocess.Popen(
                    self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                responses = queue.Queue(maxsize=2)
                self._process, self._responses = process, responses
                threading.Thread(target=_receive, args=(process, responses),
                                 name=f"aisland-quota-{self.provider}", daemon=True).start()
            return self._process, self._responses

    def _discard(self, process):
        with self._state_lock:
            if self._process is process:
                self._process = self._responses = None
                self._ready_process = None
        _terminate(process)

    def read(self) -> list[Metric] | None:
        # The UI already coalesces requests. A direct concurrent caller should
        # miss one sample rather than occupy another shared background thread.
        if not self._request_lock.acquire(blocking=False):
            return None
        process = None
        phase = "startup"
        try:
            process, responses = self._start()
            if process is None:
                return None
            if self._ready_process is not process:
                response = responses.get(timeout=self.startup_timeout)
                if response != {"ready": 1}:
                    self._discard(process)
                    return None
                with self._state_lock:
                    if self._closed or self._process is not process:
                        return None
                    self._ready_process = process
            # A real readiness message separates frozen startup and dependency
            # imports from the window-discovery/UIA budget. Popen returning only
            # means that Windows created the process, not that Python is ready.
            phase = "read"
            deadline = time.monotonic() + self.timeout
            self._serial += 1
            serial = self._serial
            process.stdin.write(json.dumps({"id": serial, "provider": self.provider}) + "\n")
            process.stdin.flush()
            response = responses.get(timeout=max(0.0, deadline - time.monotonic()))
            if not isinstance(response, dict) or response.get("id") != serial:
                self._discard(process)
                return None
            raw = response.get("metrics")
            if raw is None:
                return None
            if not isinstance(raw, list) or len(raw) > 8:
                raise ValueError("Invalid native quota response")
            return [Metric(**item) for item in raw] or None
        except queue.Empty:
            _log.warning("Native quota %s timed out (%s); discarding its process", phase, self.provider)
            self._discard(process)
            return None
        except (OSError, ValueError, TypeError):
            self._discard(process)
            return None
        finally:
            self._request_lock.release()

    def close(self):
        # Do not take _request_lock: its owner may be waiting for a hung UIA
        # call. Terminating the process and waking its waiter bounds shutdown.
        with self._state_lock:
            self._closed = True
            process, responses = self._process, self._responses
            self._process = self._responses = None
            self._ready_process = None
        if responses is not None:
            try:
                responses.put_nowait(None)
            except queue.Full:
                pass
        _terminate(process)


_workers: dict[str, NativeUsageWorker] = {}
_workers_lock = threading.Lock()
_stopped = False


def read_isolated_usage(provider: str) -> list[Metric] | None:
    if provider not in NATIVE_USAGE_GUIDES:
        return None
    with _workers_lock:
        if _stopped:
            return None
        worker = _workers.get(provider)
        if worker is None:
            worker = _workers[provider] = NativeUsageWorker(provider)
    return worker.read()


def shutdown_native_readers():
    global _stopped
    with _workers_lock:
        _stopped = True
        workers = list(_workers.values())
        _workers.clear()
    for worker in workers:
        worker.close()


atexit.register(shutdown_native_readers)


def _pipe_stream(reading: bool):
    """Windowed frozen EXEs have sys.stdin/stdout=None, even with inherited pipes."""
    stream = sys.stdin if reading else sys.stdout
    if stream is not None:
        return stream
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as W
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes, kernel.GetStdHandle.restype = [W.DWORD], W.HANDLE
        kernel.GetCurrentProcess.restype = W.HANDLE
        kernel.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, ctypes.POINTER(W.HANDLE),
                                          W.DWORD, W.BOOL, W.DWORD]
        kernel.DuplicateHandle.restype = W.BOOL
        kernel.CloseHandle.argtypes = [W.HANDLE]
        handle = kernel.GetStdHandle(-10 if reading else -11)
        current, duplicate = kernel.GetCurrentProcess(), W.HANDLE()
        if not kernel.DuplicateHandle(current, handle, current, ctypes.byref(duplicate), 0, False, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(duplicate.value, os.O_BINARY | (os.O_RDONLY if reading else os.O_WRONLY))
        except OSError:
            kernel.CloseHandle(duplicate)
            raise
    else:
        fd = os.dup(0 if reading else 1)
    return os.fdopen(fd, "r" if reading else "w", encoding="utf-8", buffering=1)


def _prepare_native_runtime():
    """Load native bindings before announcing readiness, without probing windows."""
    from . import system  # noqa: F401 - imported by every native read
    import comtypes.client

    comtypes.client.GetModule(str(Path(os.environ["SystemRoot"]) / "System32/UIAutomationCore.dll"))


def main() -> int:
    incoming, outgoing = _pipe_stream(True), _pipe_stream(False)
    # Keep imports and third-party diagnostics out of the JSON protocol.
    sys.stdout = sys.stderr = sys.stderr or open(os.devnull, "w", encoding="utf-8")
    from .native_ai_usage import _read_native_usage_local

    requests = queue.Queue(maxsize=1)

    def receive_requests():
        try:
            while line := incoming.readline(MAX_MESSAGE + 1):
                if len(line) > MAX_MESSAGE:
                    break
                requests.put(json.loads(line))
        except (OSError, ValueError):
            pass
        # Parent death closes stdin. Exit even if the main thread is stuck
        # inside UIA, so an abandoned helper never survives its application.
        os._exit(0)

    threading.Thread(target=receive_requests, name="aisland-quota-input", daemon=True).start()
    # Parent death remains observable even while an import or COM type-library
    # preparation is stuck. The controller separately bounds this startup phase.
    _prepare_native_runtime()
    outgoing.write(json.dumps({"ready": 1}) + "\n")
    outgoing.flush()
    while True:
        request = requests.get()
        if not isinstance(request, dict) or request.get("provider") not in NATIVE_USAGE_GUIDES:
            return 2
        try:
            metrics = _read_native_usage_local(request["provider"])
        except Exception:
            metrics = None
        outgoing.write(json.dumps({"id": request.get("id"),
                                   "metrics": [asdict(metric) for metric in metrics] if metrics else None}) + "\n")
        outgoing.flush()
