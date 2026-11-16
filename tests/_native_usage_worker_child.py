"""Controlled child for process-boundary tests; never reads actual client windows."""
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aisland.services import native_ai_usage, native_ai_worker
from aisland.services.ai_usage import Metric

mode, marker = sys.argv[1], Path(sys.argv[2])
if mode == "host":
    reader = native_ai_worker.NativeUsageWorker(
        "workbuddy", timeout=30,
        command=[sys.executable, "-B", __file__, "hang-first", str(marker)])
    reader.read()
    raise SystemExit(0)
if "--windowed" in sys.argv:
    sys.stdin = sys.stdout = None


def read(provider):
    if mode == "hang-first" and not marker.exists():
        marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        threading.Event().wait()   # Must be terminated by the parent deadline.
    if mode == "exit":
        os._exit(3)
    if mode == "none":
        return None
    return [Metric("奖励积分", "628", None, "剩余积分")]


native_ai_usage._read_native_usage_local = read


def prepare():
    # Controlled startup only: fixtures never import COM or inspect client windows.
    if mode == "slow-start":
        time.sleep(1.0)
    if mode == "startup-hang-first" and not marker.exists():
        marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        threading.Event().wait()


native_ai_worker._prepare_native_runtime = prepare
raise SystemExit(native_ai_worker.main())
