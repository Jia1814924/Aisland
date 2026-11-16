"""Backdrop 快照原子发布：主线程不得读到「新背景图 + 旧主色」的错配一帧。

玻璃截取在后台线程写 state/rgb，主线程（island.py 的 feed_backdrop）同帧读两个；
只有「单一元组、一次赋值」的快照在 GIL 下是原子的。这里用 AST 钉住结构：
state 与 rgb 必须是只读 property，二者只允许从同一个 _snapshot 派生。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TREE = ast.parse((PROJECT / "aisland" / "ui" / "glass.py").read_text(encoding="utf-8"))


def _written_attrs(tree):
    """所有被直写的 self.<attr>（含带注解的赋值 self.<attr>: tuple = ...）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute):
            yield node.targets[0].attr
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            yield node.target.attr


class BackdropSnapshotTests(unittest.TestCase):
    def test_state_and_rgb_are_never_assigned_directly(self):
        writers = [attr for attr in _written_attrs(TREE) if attr in ("state", "rgb")]
        self.assertEqual(writers, [])

    def test_both_derive_from_one_snapshot_assignment(self):
        snapshots = [attr for attr in _written_attrs(TREE) if attr == "_snapshot"]
        # 初始快照 + _capture 里的整组发布，一共两处；没有第三种写法。
        self.assertEqual(snapshots.count("_snapshot"), 2)
        capture = next(n for n in ast.walk(TREE)
                       if isinstance(n, ast.FunctionDef) and n.name == "_capture")
        published = [attr for attr in _written_attrs(capture) if attr == "_snapshot"]
        self.assertEqual(published, ["_snapshot"])
        for name in ("state", "rgb"):
            func = next(n for n in ast.walk(TREE)
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            decorators = [d.id for d in func.decorator_list if isinstance(d, ast.Name)]
            self.assertIn("property", decorators, f"{name} 必须是只读 property")

    def test_consumers_keep_reading_the_same_attributes(self):
        island = (PROJECT / "aisland" / "ui" / "island.py").read_text(encoding="utf-8")
        self.assertIn("self.backdrop.luminance", island)
        self.assertIn("self.backdrop.rgb", island)
        self.assertIn("self.backdrop.state", island)


if __name__ == "__main__":
    unittest.main()
