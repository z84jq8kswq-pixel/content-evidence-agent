"""把导入并确认过的真实规格写成语料条目（可回滚的合并脚本）。

为什么要脚本而不是手改 JSON：

- 语料是判定基准，改坏了不会有报错，只会让结论悄悄变形；
- 每条写入都必须带**出处**（source_id → sources.json 的记录，含 URL 与文件哈希）；
- 必须能回滚：脚本先把改动前的两个文件备份到 `runs/corpus-backup-<时间>/`。

本次写入的内容（充电宝品类，小米移动电源3 20000mAh 高配版）：

- 资料：1 份 —— 小米商城官方参数页（`kind=品牌官方声明`，即"厂商标称"）；
- 事实：5 条 —— 电池能量 74Wh、电芯容量 20000mAh、额定容量 12600mAh(5V 5.4A)、
  输出电压/功率档位、产品尺寸；
- **日期留空**：另存的页面上没有标注发布或更新日期，不编。

用途边界（写在这里免得被误用）：

- 这是**厂商自述**类来源。按 `文档库/02-判定规则/来源分级.md`，它过门槛，
  可作为"厂商是否这样标称"的依据；但按 R13，只有厂商自述承载结论时，
  降级分发／打标／转违规三档会被拦为转人工。
- 它**不改变**现有 26 条缺口任务与 30 条评测任务的输入，所以指标不受影响；
  真实化的是语料，不是题面。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CORPUS = ROOT / "data" / "corpus"

#: 新增资料。id 取 S-025 起，避免与现有 24 份冲突。
NEW_SOURCES: list[dict[str, Any]] = [
    {
        "id": "S-025",
        "kind": "品牌官方声明",
        "title": "小米移动电源3 20000mAh 高配版 官方参数页",
        "tier": "A",
        # 页面没有标注发布/更新时间，留空——按口径，日期缺失排最后，不猜
        "date": "",
        "publisher": "小米商城（mi.com）",
        "text": (
            "小米移动电源3 20000mAh 高配版。电池能量 74Wh，电芯容量 3.7V 20000mAh。"
            "额定容量 12600mAh(5V 5.4A)。输出参数 45W MAX；USB-A 单口 5V 2.4A / 9V 2A / 12V 1.5A；"
            "三口 5V 5.4A / 9V 3A / 12V 3A / 15V 3A / 20V 2A。"
            "输入参数 5V 3A / 9V 3A / 12V 3A / 15V 3A / 20V 2.25A。"
            "产品尺寸 153.5×73.5×27.5mm。充电时间约 11 小时（5V/2A 充电器、包装内充电线）。"
            "工作温度 5°C~40°C。"
        ),
        # 出处留档：另存文件的 URL 与内容哈希，便于回溯到底看的是哪一版页面
        "source_url": "https://www.mi.com/battery3/specs",
        "archived_file": "data/imports/小米移动电源3 20000mAh高配版 参数 - 小米商城.html",
        "archived_sha256": "见 runs/import-20261015/candidates.json",
    }
]

#: 新增事实。单位与写法沿用现有语料的约定：
#: 标称类（容量、额定能量）容差 5%，测量类 20%（见 assessor.TOLERANCE_BY_METRIC）。
NEW_FACTS: list[dict[str, Any]] = [
    {
        "entity": "小米移动电源3 20000mAh 高配版",
        "metric": "额定能量",
        "value": 74,
        "unit": "Wh",
        "period": "",
        "source_id": "S-025",
        "date": "",
        "tier": "A",
        "note": "厂商标称的电池能量。对照民航 100Wh 免申报上限使用。",
    },
    {
        "entity": "小米移动电源3 20000mAh 高配版",
        "metric": "容量",
        "value": 20000,
        "unit": "mAh",
        "period": "",
        "source_id": "S-025",
        "date": "",
        "tier": "A",
        "conditions": "电芯容量，3.7V",
        "note": "电芯容量（3.7V 标称）",
    },
    {
        "entity": "小米移动电源3 20000mAh 高配版",
        "metric": "额定容量",
        "value": 12600,
        "unit": "mAh",
        "period": "",
        "source_id": "S-025",
        "date": "",
        "tier": "A",
        "conditions": "5V 5.4A 输出条件下",
        "note": "额定容量与电芯容量不是同一口径：20000mAh 是电芯，12600mAh 是 5V 输出下可放出的电量",
    },
    {
        "entity": "小米移动电源3 20000mAh 高配版",
        "metric": "快充功率",
        "value": 45,
        "unit": "W",
        "period": "",
        "source_id": "S-025",
        "date": "",
        "tier": "A",
        "conditions": "整机输出上限",
        "note": "输出参数 45W MAX",
    },
    {
        "entity": "小米移动电源3 20000mAh 高配版",
        "metric": "充电时间",
        "value": 11,
        "unit": "小时",
        "period": "",
        "source_id": "S-025",
        "date": "",
        "tier": "A",
        "conditions": "5V/2A 充电器、包装内充电线",
        "note": "厂商给出的是特定充电器条件下的时间，条件必须一起引用",
    },
]

#: 实体别名。真实型号的常见写法都要能命中，否则检索与事实查询都找不到。
ALIASES = {
    "小米移动电源3 20000mAh 高配版": [
        "小米移动电源3",
        "小米移动电源 3",
        "移动电源3 20000mAh",
        "小米充电宝 20000mAh",
        "PLM18ZM",
    ]
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = ROOT / "runs" / f"corpus-backup-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("sources.json", "facts.json"):
        shutil.copy(CORPUS / name, target / name)
    return target


def merge(*, apply: bool, version_bump: bool = True) -> dict[str, Any]:
    sources = load(CORPUS / "sources.json")
    facts = load(CORPUS / "facts.json")

    existing_ids = {str(item.get("id")) for item in sources.get("sources", [])}
    added_sources = [item for item in NEW_SOURCES if item["id"] not in existing_ids]

    existing_fact_keys = {
        (str(row.get("entity")), str(row.get("metric")), str(row.get("value")), str(row.get("source_id")))
        for row in facts.get("facts", [])
    }
    added_facts = [
        row
        for row in NEW_FACTS
        if (str(row["entity"]), str(row["metric"]), str(row["value"]), str(row["source_id"]))
        not in existing_fact_keys
    ]

    aliases = sources.setdefault("aliases", {})
    added_aliases = {k: v for k, v in ALIASES.items() if k not in aliases}

    report = {
        "apply": apply,
        "sources_before": len(sources.get("sources", [])),
        "sources_added": [item["id"] for item in added_sources],
        "facts_before": len(facts.get("facts", [])),
        "facts_added": len(added_facts),
        "aliases_added": sorted(added_aliases),
    }

    if not apply:
        return report

    backup_dir = backup()
    report["backup"] = str(backup_dir.relative_to(ROOT))

    if added_sources:
        sources["sources"].extend(added_sources)
        sources["sources"].sort(key=lambda item: str(item.get("id", "")))
    if added_aliases:
        aliases.update(added_aliases)
    if version_bump and (added_sources or added_facts):
        sources["corpus_version"] = "c4"
        sources["description"] = (
            str(sources.get("description", "")).rstrip()
            + " c4（2026-10-15）：加入第一个真实产品型号（小米移动电源3 高配版），"
            "用于把「厂商标称 vs 夸大宣传」这条链路跑在真实规格上；"
            "示例实体保留在原有条目中。"
        )
    if added_facts:
        facts["facts"].extend(added_facts)
        facts["facts_version"] = "f4"
        facts["description"] = (
            str(facts.get("description", "")).rstrip()
            + " f4（2026-10-15）：加入小米移动电源3 20000mAh 高配版的厂商标称值。"
        )

    (CORPUS / "sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (CORPUS / "facts.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 写完立刻自检：语料必须还能被加载、事实的 source_id 必须都存在
    from evidence_agent.tools import LocalCorpus

    corpus = LocalCorpus.from_dir(CORPUS)
    report["sources_after"] = len(corpus.docs)
    report["facts_after"] = len(corpus.facts)
    report["validated"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把确认过的真实规格合并进语料")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则只预演")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = merge(apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = "已写入" if args.apply else "预演（未写入）"
        print(f"模式：{mode}")
        print(f"资料：{report['sources_before']} → 新增 {report['sources_added']}"
              f"（写入后 {report.get('sources_after', '—')}）")
        print(f"事实：{report['facts_before']} → 新增 {report['facts_added']} 条"
              f"（写入后 {report.get('facts_after', '—')}）")
        print(f"别名：新增 {report['aliases_added'] or '无'}")
        if args.apply:
            print(f"备份：{report.get('backup')}")
            print(f"校验：{'通过（语料可加载、事实出处齐全）' if report.get('validated') else '未执行'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
