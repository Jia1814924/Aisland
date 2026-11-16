"""Regressions for the dead live-launcher removal and wordbook listener cleanup."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

_MISSING = object()


class swap_modules:
    """只替换列出的模块名，退出时也只还原这几项。

    以前用 patch.dict(sys.modules, ...) 整表回滚：被测模块执行期间导入的
    numpy 子模块会被一起删掉，进程里再 import numpy 就报
    "cannot load module more than once per process"，把后面的测试连带弄坏。
    """

    def __init__(self, mapping):
        self._mapping = mapping
        self._saved = {}

    def __enter__(self):
        for name, module in self._mapping.items():
            self._saved[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = module
        return self

    def __exit__(self, *_exc):
        for name, old in self._saved.items():
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        return False

TRANSLATE = (PROJECT / "aisland/services/translate.py").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_wordbook_class():
    config = ModuleType("aisland.core.config")
    config.DATA_DIR = PROJECT / "build" / "wordbook-listener-tests"   # never created
    spec = importlib.util.spec_from_file_location(
        "aisland.learn._wordbook_test", PROJECT / "aisland/learn/wordbook.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks up cls.__module__ in sys.modules while decorating Card;
    # the module must be registered (and numpy must only be imported once).
    with swap_modules({"aisland.core.config": config, spec.name: module}):
        spec.loader.exec_module(module)
    return module.Wordbook


class WordbookListenerTests(unittest.TestCase):
    def make_book(self):
        cls = load_wordbook_class()
        book = cls.__new__(cls)          # avoid opening the SQLite database
        book._listeners = []
        return book

    def test_off_change_removes_every_registration(self):
        book = self.make_book()
        called = []
        listener = lambda: called.append(1)
        book.on_change(listener)
        book.on_change(listener)
        book.off_change(listener)
        book._changed()
        self.assertEqual(called, [])
        self.assertEqual(book._listeners, [])

    def test_off_change_keeps_other_listeners_and_is_idempotent(self):
        book = self.make_book()
        calls = []
        first = lambda: calls.append("first")
        second = lambda: calls.append("second")
        book.on_change(first)
        book.on_change(second)
        book.off_change(first)
        book.off_change(first)           # never registered anymore: no error
        book._changed()
        self.assertEqual(calls, ["second"])


class DeadCodeAndWiringTests(unittest.TestCase):
    def test_dead_live_launcher_is_gone(self):
        for token in ("launch_live", "jpsub.main", "translate.live_app",
                      "import subprocess", "bg import launch"):
            self.assertNotIn(token, TRANSLATE)

if __name__ == "__main__":
    unittest.main()
