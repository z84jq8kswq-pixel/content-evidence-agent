"""缺口任务评测：跑 v3 Agent，按过程和结果一起打分，并给出 v2 上界。

用法：
    python harness/run_agent_eval.py
    python harness/run_agent_eval.py --run-id gap-baseline --json

不需要 API Key：默认走受控离线语料、规则抽取与规则判定，结果可复现。
接真实模型时用 --adapter deepseek，脚本会额外调用模型判定规则判不了的主张。

主张由系统自己拆（--extract rule|model），不读标注：拿标注主张当输入等于把答案抄进
题面，指标会虚高。拆出来的主张按文本相似度与标注对齐（见 gap_eval.score_case）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.envfile import load_env_files  # noqa: E402
from evidence_agent.gap_eval import (  # noqa: E402
    aggregate,
    content_summary,
    load_gap_cases,
    score_case,
    v2_ceiling,
)
from evidence_agent.runio import plan_run_dir  # noqa: E402
from evidence_agent.runtime import extract_claim_texts, make_agent  # noqa: E402
from evidence_agent.tools import LocalCorpus  # noqa: E402
from evidence_agent.trace import TraceWriter, now_iso  # noqa: E402

METRIC_LABELS = [
    ("claim_recall", "主张召回率", "标注里的主张被拆出来的比例", True),
    ("claim_precision", "主张精确率", "拆出来的主张里命中标注的比例", True),
    (
        "routing_accuracy",
        "分诊一致率",
        "该交给人的有没有交到人手里（按处置档位，不看原始状态）",
        True,
    ),
    (
        "state_accuracy",
        "状态一致率",
        "JUDGED／ESCALATED／TOOL_FAILED 三态逐字一致（比上一行更细）",
        True,
    ),
    ("verdict_accuracy", "判定准确率", "期望有结论的主张里，结论是否正确", True),
    ("tool_selection_accuracy", "工具选择准确率", "实际调用的工具是否覆盖标注的最优工具集", True),
    ("reason_match_rate", "理由命中率", "给出结论或转人工的原因是否正确", True),
    ("tier_accuracy", "档位准确率", "建议的处置档位是否与标注一致", True),
    ("evidence_hit_rate", "关键证据进结论率", "关键证据是否出现在承载结论的来源里", True),
    ("flag_recall", "标志位召回", "过期／冲突这类标志有没有漏", True),
    ("flag_precision", "标志位查准", "有没有误报标志位", True),
    ("avg_tool_calls_per_claim", "平均调用次数", "单条主张消耗的工具调用", False),
    ("retry_rate", "重试率", "需要换策略的主张占比", True),
    ("escalation_rate", "转人工率", "交回给人的主张占比", True),
]


def load_run_config() -> dict:
    """缺口评测的模型设置：与 harness/run_eval.py 共用 config/default.json。

    必须共用同一份：否则"规则判定 vs 模型判定"这一步会同时改了两个变量（模型设置和
    判定方式），数字不可比。
    """

    return json.loads(
        (ROOT / "config" / "default.json").read_text(encoding="utf-8")
    )


def decoding_settings(config: dict) -> dict:
    """写进 run_meta 与报告的解码参数快照。"""

    return {
        "model_id": config.get("model_id", ""),
        "temperature": config.get("temperature", 0),
        "max_tokens": config.get("max_tokens", 0),
        "reasoning_effort": config.get("reasoning_effort"),
        "thinking": config.get("thinking"),
        "steps": config.get("steps", {}),
    }


def build_adapter(name: str):
    if name == "stub":
        from evidence_agent.adapters.stub import StubAdapter

        return StubAdapter(model_id="stub")
    if name == "deepseek":
        from evidence_agent.adapters.deepseek import DeepSeekAdapter

        config = load_run_config()
        return DeepSeekAdapter(
            model_id=config["model_id"],
            base_url=config["base_url"],
            api_key_env=config["api_key_env"],
            timeout_s=config["timeout_s"],
            max_retries=config["max_retries"],
            temperature=config["temperature"],
            max_tokens=config.get("max_tokens", 3000),
            json_mode=bool(config.get("json_mode", True)),
            reasoning_effort=config.get("reasoning_effort") or None,
            thinking=config.get("thinking") or None,
        )
    return None


#: 谁负责拆主张。两种都不读标注。
EXTRACTORS = ("rule", "model")


def _fmt_rate(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def extract_with_model(
    adapter,
    content: str,
    step_settings: dict | None = None,
    usage_sink: list[dict] | None = None,
) -> list[str]:
    """让模型拆主张：与 v2 抽取阶段共用同一套提示词与同一份节点设置。"""

    from evidence_agent.pipeline import extract_claims, parse_json_any
    from evidence_agent.prompts import extract_messages

    overrides = (step_settings or {}).get("extract", {})
    response = adapter.complete(
        extract_messages(content, ()),
        thinking=overrides.get("thinking"),
        reasoning_effort=overrides.get("reasoning_effort"),
    )
    if usage_sink is not None:
        # 抽取这一步在运行时之外，token 不会进 ClaimOutcome；不在这里记账，
        # 模型抽取的成本就会被少算（以前只统计了判定那一次）。
        usage_sink.append(
            {
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            }
        )
    if not response.ok:
        return []
    try:
        payload = parse_json_any(response.text)
        claims, _violations = extract_claims(payload)
    except (ValueError, json.JSONDecodeError):
        return []
    return [claim.text for claim in claims if claim.text]


def model_usage(results: list[dict], extra_calls: list[dict] = ()) -> dict:
    """把各主张上的模型调用汇总成代价数字（次数、输入／输出 token、估算金额）。"""

    from evidence_agent.config import DEFAULTS, resolve_pricing

    calls = len(extra_calls)
    prompt_tokens = sum(int(call.get("prompt_tokens") or 0) for call in extra_calls)
    completion_tokens = sum(int(call.get("completion_tokens") or 0) for call in extra_calls)
    for case in results:
        for claim in case.get("claims") or []:
            for call in claim.get("model_calls") or []:
                calls += 1
                prompt_tokens += int(call.get("prompt_tokens") or 0)
                completion_tokens += int(call.get("completion_tokens") or 0)
    prompt_rate, completion_rate, _mode = resolve_pricing(DEFAULTS["pricing"])
    cost = prompt_tokens / 1_000_000 * prompt_rate + completion_tokens / 1_000_000 * completion_rate
    return {
        "model_calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_yuan": round(cost, 4),
    }


def extract_for_case(
    case,
    adapter,
    mode: str,
    step_settings: dict | None = None,
    usage_sink: list[dict] | None = None,
) -> list[str]:
    if mode == "model" and adapter is not None:
        return extract_with_model(adapter, case.content, step_settings, usage_sink)
    return extract_claim_texts(case.content)


def render_report(
    metrics: dict,
    rows: list[dict],
    contents: list[dict],
    ceiling: dict,
    scores: list,
    run_id: str,
    adapter_name: str,
    extractor: str,
    settings: dict | None = None,
    usage: dict | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# 缺口任务评测报告：{run_id}")
    lines.append("")
    lines.append(f"- 生成时间：{now_iso()}")
    lines.append(f"- 模型：{adapter_name}（rule-only 表示只用规则判定，未调用模型）")
    lines.append(f"- 主张来源：{extractor}（rule=规则抽取，model=模型抽取；都不读标注）")
    if settings:
        lines.append(
            f"- 解码参数：{settings['model_id']}｜temperature={settings['temperature']}"
            f"｜max_tokens={settings['max_tokens']}"
            f"｜reasoning_effort={settings['reasoning_effort']}"
            f"｜steps={settings['steps']}"
        )
    if usage:
        lines.append(
            f"- 模型调用：{usage['model_calls']} 次｜输入 {usage['prompt_tokens']} token"
            f"｜输出 {usage['completion_tokens']} token｜估算 {usage['cost_yuan']} 元"
        )
    lines.append("- 任务集：data/gap_tasks.json")
    lines.append("- 语料：data/corpus/（受控离线语料，离线可复现）")
    lines.append("")

    lines.append("## 一、总体指标")
    lines.append("")
    lines.append("| 指标 | 值 | 说明 |")
    lines.append("|---|---:|---|")
    for key, label, desc, is_pct in METRIC_LABELS:
        value = metrics.get(key)
        if value is None:
            shown = "—"
        elif is_pct:
            shown = f"{value:.0%}"
        else:
            shown = f"{value}"
        lines.append(f"| {label} | {shown} | {desc} |")
    lines.append("")
    lines.append(
        "分诊结果：{judged} 条给出结论、{escalated} 条转人工、{failed} 条工具不可用。".format(
            judged=metrics['judged_count'],
            escalated=metrics['escalated_count'],
            failed=metrics['tool_failed_count'],
        )
    )
    lines.append("")

    lines.append("## 二、拆主张这一步")
    lines.append("")
    lines.append(
        "主张由系统自己拆，不读标注：拿标注当输入等于把答案抄进题面。"
        "召回率的分母是标注主张数，精确率的分母是系统拆出来的条数。"
    )
    lines.append("")
    lines.append("| 内容 | 标注主张 | 拆出 | 漏拆 | 多拆（标注里没有） |")
    lines.append("|---|---:|---:|---|---|")
    for row in contents:
        lines.append(
            "| {cid} | {expected} | {extracted} | {missing} | {spurious} |".format(
                cid=row['case_id'],
                expected=row['claims'],
                extracted=row['extracted'],
                missing="、".join(row.get('missing') or []) or "—",
                spurious="、".join(row.get('spurious_claims') or []) or "—",
            )
        )
    lines.append("")
    lines.append(
        "- 主张召回率：{rate}（{matched}/{claims}）".format(
            rate=_fmt_rate(metrics.get('claim_recall')),
            matched=metrics['matched_count'],
            claims=metrics['claims'],
        )
    )
    lines.append(
        "- 主张精确率：{rate}（拆出 {extracted} 条，其中 {spurious} 条不在标注里）".format(
            rate=_fmt_rate(metrics.get('claim_precision')),
            extracted=metrics['extracted_claim_count'],
            spurious=metrics['spurious_claim_count'],
        )
    )
    lines.append("")

    lines.append("## 三、v2 的理论上界")
    lines.append("")
    lines.append("v2 的资料由用户提供，所以在缺口任务上它有一个先验天花板：")
    lines.append("")
    lines.append(f"- 期望能给出结论的主张：{ceiling['judged_expected']} 条")
    lines.append(f"- 其中关键证据本来就在用户资料里的：{ceiling['reachable']} 条")
    ceiling_value = ceiling['ceiling']
    lines.append(
        f"- **v2 上界：{'—' if ceiling_value is None else f'{ceiling_value:.0%}'}**"
        f"（v3 实际给出结论的是 {metrics['judged_count']} / {metrics['claims']}）"
    )
    lines.append("")
    lines.append(f"> {ceiling['note']}")
    lines.append("")
    if ceiling['unreachable_cases']:
        lines.append("够不着的案例：" + "、".join(ceiling['unreachable_cases']))
        lines.append("")

    lines.append("## 四、逐案例结果")
    lines.append("")
    lines.append("| 案例 | 类别 | 拆出 | 状态 | 主张 | 期望 | 实际 | 档位 | 工具 | 调用数 | 理由 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---:|---|")
    for score in [s for s in scores if not s.spurious]:
        tier = score.predicted_tier or "—"
        if score.tier_ok is True:
            tier += " ✅"
        elif score.tier_ok is False:
            tier += f" ❌(应 {score.expected_tier})"
        lines.append(
            "| {case} | {cat} | {matched} | {state} | {claim} | {exp} | {got} | {tier} | {tools} | {calls} | {reason} |".format(
                case=score.case_id,
                cat=score.category,
                matched="✓" if score.matched else "无",
                state="✅" if score.state_ok else "❌",
                claim=score.claim_text,
                exp=score.expected_state,
                got=score.predicted_state,
                tier=tier,
                tools="/".join(score.predicted_tools) or "—",
                calls=score.tool_calls,
                reason=score.reason[:40],
            )
        )
    lines.append("")

    lines.append("## 五、内容条级口径")
    lines.append("")
    lines.append("| 内容 | 类别 | 标注主张 | 拆出 | 多拆 | 已可放行 | 需人工 | 工具调用 | 全部状态正确 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for row in contents:
        lines.append(
            "| {cid} | {cat} | {claims} | {extracted} | {spurious} | {judged} | {needs} | {calls} | {ok} |".format(
                cid=row['case_id'],
                cat=row['category'],
                claims=row['claims'],
                extracted=row['extracted'],
                spurious=row['spurious'],
                judged=row['judged'],
                needs=row['needs_human'],
                calls=row['tool_calls'],
                ok="是" if row['state_ok'] else "否",
            )
        )
    lines.append("")

    expected_rows = [s for s in scores if not s.spurious]
    spurious_rows = [s for s in scores if s.spurious]
    problems = [
        score
        for score in expected_rows
        if not score.matched
        or not score.state_ok
        or score.reason_ok is False
        or score.spurious_flags
    ]
    lines.append("## 六、失败明细")
    lines.append("")
    if not problems and not spurious_rows:
        lines.append("本次没有漏拆、状态错误、理由不匹配或标志位误报。")
    else:
        for score in problems:
            bits = []
            if not score.matched:
                bits.append("标注主张没有被拆出来")
            if not score.state_ok:
                bits.append(f"状态应为 {score.expected_state}，实际 {score.predicted_state}")
            if score.reason_ok is False:
                bits.append(f"理由不匹配（实际：{score.reason}）")
            if score.spurious_flags:
                bits.append("误报标志位：" + "/".join(score.spurious_flags))
            if score.missing_flags:
                bits.append("漏标标志位：" + "/".join(score.missing_flags))
            joined = "；".join(bits)
            lines.append(f"- **{score.case_id}** {score.claim_text}：{joined}")
        for score in spurious_rows:
            lines.append(
                f"- **{score.case_id}** {score.claim_text}：多拆出来的主张，标注里没有这一条"
                f"（状态 {score.predicted_state}）"
            )
    lines.append("")
    lines.append("> 失败明细不是免责声明，而是下一轮迭代的输入：每一条都对应一个具体的设计缺口，")
    lines.append("> 修完之后重跑同一套任务集，看指标是否真的动了。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="缺口任务评测")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--corpus", default=str(ROOT / "data" / "corpus"))
    parser.add_argument("--tasks", default=str(ROOT / "data" / "gap_tasks.json"))
    parser.add_argument(
        "--adapter", default="rule-only", choices=["rule-only", "stub", "deepseek"]
    )
    parser.add_argument(
        "--extract",
        default="rule",
        choices=list(EXTRACTORS),
        help="谁负责拆主张：rule 规则抽取（默认，离线可复现），model 模型抽取",
    )
    parser.add_argument("--force", action="store_true", help="运行目录已存在时覆盖")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    load_env_files(ROOT)
    tasks_raw, cases = load_gap_cases(args.tasks)
    corpus = LocalCorpus.from_dir(args.corpus)

    config = load_run_config()
    adapter = None
    if args.adapter == "deepseek":
        adapter = build_adapter("deepseek")
    if args.extract == "model" and adapter is None:
        print(
            "--extract model 需要同时给出 --adapter deepseek：模型抽取要调用模型。",
            file=sys.stderr,
        )
        return 2

    run_id = args.run_id or f"gap-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        run_dir = plan_run_dir(run_id, ROOT, force=args.force)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    agent = make_agent(corpus, adapter=adapter, step_settings=config.get("steps", {}))
    scores: list = []
    results: list[dict] = []
    extract_usage: list[dict] = []

    with TraceWriter(run_dir) as trace:
        for case in cases:
            claim_texts = extract_for_case(
                case, adapter, args.extract, config.get("steps", {}), extract_usage
            )
            outcome = (
                agent.run_content(case.id, claim_texts, content=case.content, trace=trace)
                if claim_texts
                else None
            )
            scores.extend(score_case(case, outcome))
            results.append(
                {
                    "case_id": case.id,
                    "category": case.category,
                    "difficulty": case.difficulty,
                    "content": case.content,
                    "given_sources": list(case.given_sources),
                    "what_it_tests": case.what_it_tests,
                    "extracted_claims": claim_texts,
                    "summary": outcome.summary() if outcome else None,
                    "claims": [claim.to_dict() for claim in outcome.claims] if outcome else [],
                }
            )

    metrics = aggregate(scores)
    contents = content_summary(cases, scores)
    ceiling = v2_ceiling(cases)
    usage = model_usage(results, extract_usage)

    report = render_report(
        metrics,
        results,
        contents,
        ceiling,
        scores,
        run_id,
        args.adapter,
        args.extract,
        decoding_settings(config) if adapter is not None else None,
        usage if usage["model_calls"] else None,
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "adapter": args.adapter,
                "extractor": args.extract,
                "metrics": metrics,
                "v2_ceiling": ceiling,
                "contents": contents,
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "run_meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": now_iso(),
                "adapter": args.adapter,
                "extractor": args.extract,
                "corpus_version": corpus.version,
                "gap_tasks_version": str(
                    tasks_raw.get("gap_version", "")
                ),
                **(decoding_settings(config) if adapter is not None else {}),
                "cases": len(cases),
                "claims": metrics['claims'],
                "extracted_claims": metrics['extracted_claim_count'],
                "missing_claims": len(metrics['missing_claims']),
                "spurious_claims": metrics['spurious_claim_count'],
                **usage,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.json:
        print(
            json.dumps(
                {"metrics": metrics, "v2_ceiling": ceiling, "usage": usage},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(report)
    print(f"\n产物：runs/{run_id}/report.md")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
