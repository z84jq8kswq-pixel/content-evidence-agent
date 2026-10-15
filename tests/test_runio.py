"""运行目录处理的单元测试。

运行编号是命令行输入，会被拼进路径。这里固定的是：不合法编号一律拒绝、
已有产物不允许被静默覆盖。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.runio import plan_run_dir, runs_root  # noqa: E402


class PlanRunDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_accepts_a_normal_run_id(self) -> None:
        planned = plan_run_dir("gap-20261013-101010", self.root)
        self.assertEqual(planned, runs_root(self.root) / "gap-20261013-101010")

    def test_rejects_traversal_and_other_unsafe_names(self) -> None:
        for bad in (
            "..",
            "../evil",
            "a/../b",
            "a/b",
            "C:/tmp/x",
            "/etc/passwd",
            "",
            "   ",
            "x" * 65,
            "trailing.",
            "with space",
        ):
            with self.assertRaises(ValueError, msg=bad):
                plan_run_dir(bad, self.root)

    def test_existing_run_is_not_deleted_without_force(self) -> None:
        run_dir = runs_root(self.root) / "keep"
        run_dir.mkdir(parents=True)
        (run_dir / "results.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(ValueError):
            plan_run_dir("keep", self.root)
        self.assertTrue((run_dir / "results.json").exists())

    def test_force_recreates_the_directory(self) -> None:
        run_dir = runs_root(self.root) / "redo"
        run_dir.mkdir(parents=True)
        (run_dir / "old.txt").write_text("x", encoding="utf-8")

        planned = plan_run_dir("redo", self.root, force=True)
        self.assertEqual(planned, run_dir)
        self.assertFalse(planned.exists())


if __name__ == "__main__":
    unittest.main()
