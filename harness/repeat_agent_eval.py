"""同一配置重复运行，用噪声带判断两个配置的差异是否可信。

用法：
    python harness/repeat_agent_eval.py --configs rule --repeats 3
    python harness/repeat_agent_eval.py --configs rule,judge --repeats 3 --json

为什么需要它：同配置重跑的噪声下限是实测出来的（30 条任务集上约 13% 的案例会翻转，见 docs/06 补记），
所以两次运行差几个点说明不了谁更好。这里把同一配置跑 N 次，报出每个指标的中位数与极差（噪声带），
再看配置之间的差异有没有超出噪声带。

注意：它直接调用 harness/run_agent_eval.py 的 main()，产物落在 runs/<run_id>/；
单测不跑它（会写 runs/ 目录），验证方式是直接运行。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HARNESS = ROOT / "harness"
for path in (SRC, HARNESS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import run_agent_eval  # noqa: E402

#: 配置名 -> 传给 run_agent_eval 的额外参数
CONFIGS: dict[str, list[str]] = {
    "rule": [],
    "judge": ["--adapter", "deepseek"],
    "both": ["--adapter", "deepseek", "--extract", "model"],
}

#: 参与噪声带计算的指标（口径中立的计数不进这张表）
METRICS: tuple[str, ...] = (
    "claim_recall",
    "claim_precision",
    "routing_accuracy",
    "state_accuracy",
    "verdict_accuracy",
    "flag_recall",
    "flag_precision",
    "evidence_hit_rate",
    "reason_match_rate",
    "tier_accuracy",
    "escalation_rate",
    "avg_tool_calls_per_claim",
    "spurious_claim_count",
    "insufficient_missed",
)


def run_once(config: str, index: int, prefix: str) -> dict:
    """跑一次，返回这次运行的指标与用量。"""

    run_id = f"{prefix}-{config}-r{index}"
    argv = list(CONFIGS[config]) + ["--run-id", run_id, "--force", "--json"]
    if run_agent_eval.main(argv) != 0:
        raise SystemExit(f"运行失败：{run_id}")
    run_dir = ROOT / "runs" / run_id
    payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    return {
        "run_id": run_id,
        "metrics": payload.get("metrics", {}),
        "model_calls": int(meta.get("model_calls", 0) or 0),
        "cost_yuan": float(meta.get("cost_yuan", 0.0) or 0.0),
    }


def summarize(records: list[dict]) -> dict:
    """每个指标给出中位数与极差；极差就是这次重复运行观察到的噪声带。"""

    out: dict = {}
    for metric in METRICS:
        values = [
            float(r["metrics"][metric])
            for r in records
            if isinstance(r["metrics"].get(metric), (int, float))
        ]
        if values:
            out[metric] = {
                "median": round(statistics.median(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                "band": round(max(values) - min(values), 4),
            }
    out["model_calls"] = sum(r["model_calls"] for r in records)
    out["cost_yuan"] = round(sum(r["cost_yuan"] for r in records), 4)
    out["runs"] = [r["run_id"] for r in records]
    return out


def render(summary: dict[str, dict], names: list[str]) -> str:
    lines: list[str] = []
    lines.append("| 指标 | " + " | ".join(names) + " |")
    lines.append("|---|" + "---|" * len(names))
    for metric in METRICS:
        cells = []
        for name in names:
            row = summary[name].get(metric)
            if row is None:
                cells.append("—")
            else:
                cells.append("%.3f（噪声带 %.3f）" % (row["median"], row["band"]))
        lines.append("| " + metric + " | " + " | ".join(cells) + " |")
    lines.append("")
    for name in names:
        row = summary[name]
        lines.append(
            "%s：模型调用 %d 次，成本 %.4f 元｜运行 %s"
            % (name, row["model_calls"], row["cost_yuan"], "，".join(row["runs"]))
        )
    lines.append("")
    lines.append("读法：两个配置的中位数之差小于各自的噪声带时，说明这次比较没超出运行噪声。")
    return chr(10).join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同一配置重复运行，算噪声带")
    parser.add_argument("--configs", default="rule", help="逗号分隔，可选 " + "|".join(CONFIGS))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--prefix", default="repeat")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    names = [item.strip() for item in args.configs.split(",") if item.strip()]
    unknown = [name for name in names if name not in CONFIGS]
    if unknown:
        print("未知配置：" + ", ".join(unknown), file=sys.stderr)
        return 2

    runs: dict[str, list[dict]] = {}
    for name in names:
        runs[name] = []
        for index in range(1, args.repeats + 1):
            print("跑 %s 第 %d 次…" % (name, index))
            runs[name].append(run_once(name, index, args.prefix))

    summary = {name: summarize(records) for name, records in runs.items()}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print()
    print(render(summary, names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
