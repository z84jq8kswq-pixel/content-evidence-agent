"""Run configuration loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "adapter": "stub",
    "model_id": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
    "json_mode": True,
    "reasoning_effort": "low",
    "thinking": None,
    "steps": {
        "extract": {"thinking": "disabled"},
        "judge": {"thinking": "disabled"},
    },
    "temperature": 0.0,
    "max_tokens": 3000,
    "timeout_s": 60,
    "max_retries": 2,
    "backoff_s": 1.5,
    "pipeline": "v2",
    "match_threshold": 0.3,
    "pricing": {
        "mode": "off_peak",
        "off_peak": {"prompt_per_mtok": 1.0, "completion_per_mtok": 4.0},
        "peak": {"prompt_per_mtok": 2.0, "completion_per_mtok": 8.0},
    },
}


def resolve_pricing(pricing: dict) -> tuple[float, float, str]:
    """Return (prompt_rate, completion_rate, mode) per million tokens."""

    pricing = pricing or {}
    mode = str(pricing.get("mode", "off_peak"))
    block = pricing.get(mode)
    if isinstance(block, dict) and "prompt_per_mtok" in block:
        return (
            float(block.get("prompt_per_mtok", 0.0)),
            float(block.get("completion_per_mtok", 0.0)),
            mode,
        )
    return (
        float(pricing.get("prompt_per_mtok", 0.0)),
        float(pricing.get("completion_per_mtok", 0.0)),
        mode,
    )


def load_config(path: str | Path | None) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULTS))
    if path is None:
        return config
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    for key, value in raw.items():
        if key == "pricing" and isinstance(value, dict):
            config["pricing"] = {**config["pricing"], **value}
        else:
            config[key] = value
    return config
