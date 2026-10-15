"""Probe the configured model with one tiny call.

Run this before a full evaluation to confirm that the API key, base URL and
model id are all correct. It costs one very small request.

    python harness/probe_model.py
    python harness/probe_model.py --model-id deepseek-flash
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent.adapters import ADAPTERS, AdapterConfigError  # noqa: E402
from evidence_agent.config import load_config  # noqa: E402
from evidence_agent.dataset import load_cases  # noqa: E402
from evidence_agent.envfile import load_env_file  # noqa: E402
from evidence_agent.prompts import extract_messages  # noqa: E402

PROBE_SYSTEM = "你是一个连通性探测程序。只输出 JSON，不要输出其他文字。"
PROBE_USER = '请输出 {"ok": true, "echo": "probe"}'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探测模型接口连通性")
    parser.add_argument("--config", default=str(ROOT / "config" / "default.json"))
    parser.add_argument("--adapter", default="deepseek", choices=sorted(ADAPTERS))
    parser.add_argument("--model-id")
    parser.add_argument("--base-url")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument(
        "--case",
        help="用某个评测案例的真实抽取提示词探测，例如 EV-001",
    )
    parser.add_argument(
        "--no-json", action="store_true", help="关闭 response_format json_object"
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        help="推理型模型的思考强度",
    )
    parser.add_argument(
        "--thinking",
        choices=["enabled", "disabled"],
        help="DeepSeek 思考模式开关",
    )
    args = parser.parse_args(argv)

    load_env_file(ROOT / ".env.local")

    config = load_config(args.config)
    model_id = args.model_id or config["model_id"]
    base_url = args.base_url or config.get("base_url", "")

    messages = [
        {"role": "system", "content": PROBE_SYSTEM},
        {"role": "user", "content": PROBE_USER},
    ]
    max_tokens = args.max_tokens or 64

    if args.case:
        _, cases = load_cases(ROOT / "data" / "eval_tasks.json")
        case = next((c for c in cases if c.id == args.case), None)
        if case is None:
            print(f"没有找到案例 {args.case}", file=sys.stderr)
            return 2
        messages = extract_messages(case.content, case.sources)
        max_tokens = args.max_tokens or int(config.get("max_tokens", 3000))
        print(f"使用 {case.id} 的真实抽取提示词：{case.content[:40]}…")

    adapter_cls = ADAPTERS[args.adapter]
    kwargs = dict(
        model_id=model_id,
        timeout_s=float(args.timeout or config.get("timeout_s", 60)),
        max_retries=0,
        backoff_s=0.0,
        temperature=0.0,
        max_tokens=max_tokens,
    )
    if adapter_cls.name == "deepseek":
        kwargs.update(
            base_url=base_url,
            api_key_env=config.get("api_key_env", "DEEPSEEK_API_KEY"),
            json_mode=bool(config.get("json_mode", True)) and not args.no_json,
            reasoning_effort=(
                args.reasoning_effort or config.get("reasoning_effort") or None
            ),
            thinking=args.thinking or config.get("thinking") or None,
        )

    try:
        adapter = adapter_cls(**kwargs)
    except AdapterConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if adapter_cls.name == "deepseek":
        print(f"接口地址：{adapter.endpoint}")
    print(f"模型 id：{adapter.model_id}")
    print(f"max_tokens：{max_tokens}｜json_mode：{not args.no_json}")
    print("发送探测请求…")

    response = adapter.complete(messages)

    print("-" * 60)
    if not response.ok:
        print(f"失败：{response.error_code}")
        print(response.error)
        if response.raw_body:
            print("")
            print("接口原始返回：")
            print(response.raw_body)
        print("")
        print("排查顺序：")
        print("1. 环境变量 DEEPSEEK_API_KEY 是否设置且没有多余空格")
        print("2. 模型 id 是否与账号可用的模型名完全一致")
        print("3. 接口地址是否需要带 /v1 前缀")
        return 1

    print(f"成功：{response.latency_ms} ms，重试 {response.attempts - 1} 次")
    print(f"token：输入 {response.prompt_tokens}，输出 {response.completion_tokens}")
    print(f"finish_reason：{response.finish_reason}")
    print("返回内容：")
    print(response.text.strip()[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
