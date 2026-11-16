# -*- coding: utf-8 -*-
"""源码与安装版共用入口；维护命令不会启动后台服务。"""
import argparse
import json
import os
import sys
import traceback

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if "--adopt-translation-defaults" in sys.argv:
    if sys.stderr is not None:
        print("Public builds do not import bundled translation credentials.", file=sys.stderr)
    sys.exit(2)

if "--native-usage-worker" in sys.argv:
    from aisland.services.native_ai_worker import main as native_usage_worker
    sys.exit(native_usage_worker())

if "--diagnose-access" in sys.argv:
    from aisland.access_diagnostics import main as diagnose_access
    sys.exit(diagnose_access())

if "--self-test" in sys.argv:
    from aisland.diagnostics import main as diagnose
    sys.exit(diagnose())

if "--verify-ui" in sys.argv and not (os.environ.get("AISLAND_USER_HOME", "").strip()
                                      or os.environ.get("WISLAND_USER_HOME", "").strip()):
    if sys.stderr is not None:
        print("--verify-ui requires an isolated AISLAND_USER_HOME", file=sys.stderr)
    sys.exit(2)

from aisland.core.runtime import (DATA_DIR, INSTALL_ROOT, LEGACY_HOME_ENV, PATHS,
                                 adopt_legacy_state, import_legacy_data, rotate_log)

# 安装版第一次用新名字启动时，接管旧品牌留下的用户数据目录（含 config.json）。
adopt_legacy_state(PATHS)

if "--import-from" in sys.argv or "--import-only" in sys.argv:
    parser = argparse.ArgumentParser(description="导入本机旧版 Aisland 配置与数据")
    parser.add_argument("--import-from", required=True)
    parser.add_argument("--import-only", action="store_true")
    args, _remaining = parser.parse_known_args()
    try:
        result = import_legacy_data(args.import_from, PATHS)
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:
        from aisland.maintenance import log_import_failure
        log_import_failure(DATA_DIR)
        sys.exit(1)
    if args.import_only:
        sys.exit(0)

os.chdir(INSTALL_ROOT)

class _Tee:
    """日志始终写入 data/aisland.log；有控制台时同时输出到控制台。"""
    def __init__(self, *streams):
        self.streams = [x for x in streams if x is not None]

    def write(self, s):
        for x in self.streams:
            try:
                x.write(s)
            except Exception:
                pass

    def flush(self):
        for x in self.streams:
            try:
                x.flush()
            except Exception:
                pass


DATA_DIR.mkdir(parents=True, exist_ok=True)
rotate_log(DATA_DIR / "aisland.log")
_log = (DATA_DIR / "aisland.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.stdout, _log)
sys.stderr = _Tee(sys.stderr, _log)

try:
    from aisland.app import main
    sys.exit(main())
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
    raise
