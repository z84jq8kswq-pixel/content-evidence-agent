"""Load the evaluation task set and validate its shape."""

from __future__ import annotations

import json
from pathlib import Path

from .models import TaskCase


class DatasetError(RuntimeError):
    pass


def load_cases(path: str | Path) -> tuple[dict, list[TaskCase]]:
    dataset_path = Path(path)
    if not dataset_path.exists():
        raise DatasetError(f"任务集不存在：{dataset_path}")
    raw = json.loads(dataset_path.read_text(encoding="utf-8"))

    if "cases" not in raw or not isinstance(raw["cases"], list):
        raise DatasetError("任务集缺少 cases 数组")

    cases: list[TaskCase] = []
    seen: set[str] = set()
    for item in raw["cases"]:
        case = TaskCase.from_dict(item)
        if case.id in seen:
            raise DatasetError(f"任务 id 重复：{case.id}")
        seen.add(case.id)
        if not case.sources:
            raise DatasetError(f"{case.id} 没有资料")
        if not case.expected_claims:
            raise DatasetError(f"{case.id} 没有标注主张")
        for claim in case.expected_claims:
            if claim.verdict not in ("SUPPORTED", "PARTIAL", "CONTRADICTED", "INSUFFICIENT"):
                raise DatasetError(f"{case.id} 存在非法判定：{claim.verdict}")
            for source_id in claim.evidence:
                if source_id not in case.source_map():
                    raise DatasetError(
                        f"{case.id} 的标注引用了不存在的资料：{source_id}"
                    )
        cases.append(case)

    return raw, cases
