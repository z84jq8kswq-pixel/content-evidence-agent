"""运行 v3 自主检索 Agent 的演示：一条内容，端到端，含 Trace。

用法：
    python harness/run_agent_demo.py

不需要 API Key——默认走本地语料与规则判定，可复现。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.runio import plan_run_dir  # noqa: E402
from evidence_agent.runtime import make_agent  # noqa: E402
from evidence_agent.tools import LocalCorpus  # noqa: E402
from evidence_agent.trace import TraceWriter  # noqa: E402

DEMO_CONTENT = (
    "云石 P20 充电宝用了两周，20000mAh 容量，一次能给手机充 5 次，"
    "额定能量 74Wh，坐飞机直接带上机。另外青禾 X1 电动牙刷标称续航 21 天，"
    "还获得过口腔护理协会认证，通过了欧盟 CE 认证。"
)

DEMO_CLAIMS = [
    "云石 P20 容量 20000mAh",
    "云石 P20 一次能给手机充 5 次",
    "云石 P20 额定能量 74Wh",
    "青禾 X1 标称续航 21 天",
    "获得口腔护理协会认证",
    "通过了欧盟 CE 认证",
]

STATE_LABEL = {
    "JUDGED": "已判定",
    "ESCALATED": "转人工",
    "TOOL_FAILED": "工具不可用",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 v3 Agent 演示")
    parser.add_argument("--run-id", default="demo-agent-v3")
    parser.add_argument("--corpus", default=str(ROOT / "data" / "corpus"))
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--force", action="store_true", help="运行目录已存在时覆盖")
    args = parser.parse_args()

    corpus = LocalCorpus.from_dir(args.corpus)
    agent = make_agent(corpus)
    try:
        run_dir = plan_run_dir(args.run_id, ROOT, force=args.force)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with TraceWriter(run_dir) as trace:
        outcome = agent.run_content(
            "DEMO-001", DEMO_CLAIMS, content=DEMO_CONTENT, trace=trace
        )

    if args.json:
        print(
            json.dumps(
                {
                    "summary": outcome.summary(),
                    "claims": [claim.to_dict() for claim in outcome.claims],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("待核查内容：", DEMO_CONTENT)
    print()
    for claim in outcome.claims:
        label = STATE_LABEL.get(claim.state, claim.state)
        flags = "/".join(claim.flags) if claim.flags else "无"
        print(f"[{label}] {claim.claim_text}")
        print(f"    判定={claim.verdict}　标志={flags}")
        print(f"    理由={claim.reason}")
        for step in claim.attempts:
            print(
                "      · {tool:<12} {strategy:<9} {status:<8} {reason}".format(
                    tool=step.plan.tool,
                    strategy=step.plan.strategy,
                    status=step.result.status,
                    reason=step.plan.reason,
                )
            )
        print()

    summary = outcome.summary()
    print("汇总：", json.dumps(summary, ensure_ascii=False))
    print(
        "分诊结果：{judged} 条可直接放行，{escalated} 条需要人工，{failed} 条因工具不可用无法核查".format(
            judged=summary["judged"],
            escalated=summary["escalated"],
            failed=summary["tool_failed"],
        )
    )
    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    print(f"Trace：{run_dir.relative_to(ROOT)}/trace.jsonl（{len(trace_lines)} 条事件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
