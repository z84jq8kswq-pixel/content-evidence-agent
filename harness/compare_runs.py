"""Compare two evaluation runs side by side.

The comparison logic lives in ``evidence_agent.compare`` so the command line
and the web interface always report the same numbers.

    python harness/compare_runs.py --base runs/final-v1 --compare runs/final-judge-off
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.compare import comparison_for, render_markdown  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比两次评测运行")
    parser.add_argument("--base", required=True, help="基准运行目录")
    parser.add_argument("--compare", required=True, help="对照运行目录")
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--label-base", default=None)
    parser.add_argument("--label-compare", default=None)
    parser.add_argument("--out", default=None, help="对比报告输出路径")
    args = parser.parse_args(argv)

    try:
        comparison = comparison_for(args.base, args.compare, ROOT, args.threshold)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = render_markdown(
        comparison, args.label_base, args.label_compare, args.threshold
    )
    out_path = (
        Path(args.out)
        if args.out
        else ROOT / "runs" / comparison["new"]["run"] / "comparison.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"\n对比报告：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
