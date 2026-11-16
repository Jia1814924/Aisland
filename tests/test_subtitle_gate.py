"""Compact subtitle sid-gating regressions (pure stdlib, no Qt)."""
from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def load_gate_module():
    spec = importlib.util.spec_from_file_location(
        "_subtitle_gate_test", PROJECT / "aisland/live/subtitle_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


class SubtitleGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate_module().SubtitleGate()

    def finish_first_utterance(self):
        self.assertTrue(self.gate.begin_if_new("こんにちは"))
        self.assertTrue(self.gate.accept_live("L5"))               # live draft sid
        self.assertFalse(self.gate.begin_if_new(""))                # finalized
        self.gate.add_segment("a1b2c3d4")                           # final uuid sid
        self.assertTrue(self.gate.accept_final("a1b2c3d4"))

    def test_live_and_final_sids_belong_to_same_utterance(self):
        self.finish_first_utterance()
        self.assertTrue(self.gate.accept_live("L5"))
        self.assertTrue(self.gate.accept_final("a1b2c3d4"))

    def test_late_translations_are_dropped_after_next_utterance_starts(self):
        self.finish_first_utterance()
        self.assertTrue(self.gate.begin_if_new("ありがとう"))
        self.assertFalse(self.gate.accept_final("a1b2c3d4"))       # late final
        self.assertFalse(self.gate.accept_live("L5"))               # late live
        self.assertTrue(self.gate.accept_live("L6"))                # new draft
        self.assertFalse(self.gate.accept_live("L5"))

    def test_unknown_final_sid_is_rejected_after_segment(self):
        self.assertTrue(self.gate.begin_if_new("こんにちは"))
        self.gate.add_segment("a1b2c3d4")
        self.assertFalse(self.gate.accept_final("deadbeef"))

    def test_only_first_nonempty_partial_starts_an_utterance(self):
        self.assertTrue(self.gate.begin_if_new("あ"))
        self.assertFalse(self.gate.begin_if_new("あい"))
        self.assertFalse(self.gate.begin_if_new("あいう"))
        self.assertFalse(self.gate.begin_if_new(""))
        self.assertTrue(self.gate.begin_if_new("い"))


class SubtitleWiringTests(unittest.TestCase):
    def test_app_routes_live_and_final_through_the_gate(self):
        tree = ast.parse((PROJECT / "aisland/app.py").read_text(encoding="utf-8"))
        for name, expected in (("on_live_zh", "accept_live"), ("on_final_zh", "accept_final")):
            func = find_function(tree, name)
            self.assertIsNotNone(func)
            calls = [n.func.attr for n in ast.walk(func)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
            self.assertIn(expected, calls)

    def test_tools_on_final_never_creates_a_row_for_unknown_sid(self):
        tree = ast.parse((PROJECT / "aisland/ui/pages/tools.py").read_text(encoding="utf-8"))
        on_final = find_function(tree, "on_final")
        self.assertIsNotNone(on_final)
        calls = [n.func.attr for n in ast.walk(on_final)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertIn("_find", calls)
        self.assertNotIn("_row", calls)


if __name__ == "__main__":
    unittest.main()
