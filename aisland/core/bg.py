# -*- coding: utf-8 -*-
"""后台执行小工具：线程池跑阻塞任务，结果通过 Qt 信号回到主线程。"""
from __future__ import annotations

import subprocess
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Executor, Future
from typing import Callable

from PySide6 import QtCore

class _BackgroundPool(Executor):
    """A bounded pool whose blocked jobs cannot keep the application alive.

    ThreadPoolExecutor registers its workers with Python's interpreter-exit
    hook, which joins them even after shutdown(wait=False). These workers are
    daemon threads owned only by this pool instead. Normal shutdown still
    gives in-flight work a short chance to finish; queued jobs can be cancelled
    and all results use the standard Future interface.
    """

    def __init__(self, max_workers=8):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._max_workers = max_workers
        self._condition = threading.Condition()
        self._pending = deque()
        self._threads = []
        self._shutdown = False

    def submit(self, fn, /, *args, **kwargs):
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Background pool is shutting down")
            future = Future()
            item = (future, fn, args, kwargs)
            self._pending.append(item)
            if len(self._threads) < self._max_workers:
                thread = threading.Thread(target=self._work,
                                          name=f"aisland_{len(self._threads)}", daemon=True)
                self._threads.append(thread)
                try:
                    thread.start()
                except RuntimeError:
                    self._threads.remove(thread)
                    self._pending.remove(item)
                    raise
            self._condition.notify()
            return future

    def _work(self):
        while True:
            with self._condition:
                while not self._pending:
                    if self._shutdown:
                        return
                    self._condition.wait()
                item = self._pending.popleft()
                # Claiming a job and cancelling queued work use the same lock:
                # shutdown cannot miss a job between dequeue and its start.
                if not item[0].set_running_or_notify_cancel():
                    del item
                    continue
            try:
                self._execute(*item)
            finally:
                # An idle worker must not retain the last task's callbacks,
                # arguments or result (which can own a discarded island).
                del item

    @staticmethod
    def _execute(future, fn, args, kwargs):
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._condition:
            self._shutdown = True
            cancelled = list(self._pending) if cancel_futures else []
            if cancel_futures:
                self._pending.clear()
            self._condition.notify_all()
        # Future cancellation can run callers' callbacks; never hold the pool
        # lock while invoking them.
        for future, _fn, _args, _kwargs in cancelled:
            future.cancel()
        if wait:
            self.join()

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            threads = tuple(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)


_pool = _BackgroundPool(max_workers=8)
_stopped = False

CREATE_NO_WINDOW = 0x08000000


class _Invoker(QtCore.QObject):
    call = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._dispatch, QtCore.Qt.ConnectionType.QueuedConnection)

    def _dispatch(self, fn):
        # Checked here as well as in ui(): a callback already queued in the Qt
        # event loop before shutdown must not run while the windows and the
        # config are being torn down.
        if not _stopped:
            fn()


_invoker: _Invoker | None = None


def init() -> None:
    """必须在主线程、QApplication 创建之后调用一次（让调度对象归属主线程）。"""
    global _invoker
    if _invoker is None:
        _invoker = _Invoker()
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            _invoker.moveToThread(app.thread())


def ui(fn: Callable[[], None]) -> None:
    """在主线程执行 fn（可在任意线程调用）。"""
    if _stopped:
        # 已经在退出：迟到的回调不允许落到拆到一半的窗口/配置上。
        return
    if _invoker is None:
        init()
    _invoker.call.emit(fn)


def run_bg(fn: Callable, *args, on_done: Callable | None = None, on_error: Callable | None = None):
    """后台执行 fn(*args)；完成后在主线程调用 on_done(result) / on_error(exc)。"""
    if _stopped:
        return None
    def task():
        try:
            res = fn(*args)
        except Exception as e:  # noqa: BLE001
            if on_error:
                # 调用方自己处理错误（例如后端没启动、网络超时）：不再打印整段堆栈
                ui(lambda e=e: on_error(e))
            else:
                traceback.print_exc()
            return
        if on_done:
            ui(lambda: on_done(res))
    try:
        return _pool.submit(task)
    except RuntimeError:
        # Application is shutting down; dropping a late job is safer than
        # raising into a Qt callback that is already tearing down.
        return None


def shutdown_pool(timeout: float = 3.0) -> None:
    """Stop accepting jobs and bound how long teardown waits for in-flight work.

    Both late UI callbacks and callbacks already queued in the event loop are
    dropped. Join only our own workers for the shared budget; a stuck task is
    left on a daemon worker and cannot extend interpreter exit. Services stop
    their own resources before this drain, and queued tasks are cancelled.
    """
    global _stopped
    _stopped = True
    _pool.shutdown(wait=False, cancel_futures=True)
    _pool.join(timeout)


def run_hidden(args, timeout: float = 10.0, **kw) -> subprocess.CompletedProcess:
    """无窗口执行命令行（netsh 等），输出按系统编码解码。"""
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, **kw)


def launch(path: str, args: list[str] | None = None) -> bool:
    """启动外部程序（不等待）。"""
    import os
    try:
        if path.lower().endswith((".exe", ".bat", ".cmd")):
            subprocess.Popen([path, *(args or [])], cwd=os.path.dirname(path) or None,
                             creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP
        else:
            os.startfile(path)  # noqa: S606
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False
