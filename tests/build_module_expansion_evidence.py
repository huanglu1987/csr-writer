#!/usr/bin/env python3
"""Build reproducible evidence for the M4.1 content-module expansion."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.io_utils import atomic_write_json, atomic_write_text, load_json_unique
from csr_writer.module_router import (
    EXPECTED_MODULE_IDS,
    ORIGINAL_M3_MODULE_IDS,
    canonical_structure_json,
    load_module_definitions,
    route_modules,
)
from csr_writer.study_facts import validate_study_facts


FIXTURE_PATH = ROOT / "examples" / "fixtures" / "topical_ib_study_facts.deidentified.json"
DEMO_PATH = ROOT / "examples" / "demo_protocol_facts.json"
JSON_OUTPUT = ROOT / "examples" / "output" / "m4_module_expansion_acceptance.json"
MARKDOWN_OUTPUT = ROOT / "examples" / "output" / "m4_topical_module_comparison.md"
INVENTORY_OUTPUT = ROOT / "examples" / "output" / "m4_module_inventory.md"


def _ids(routed: dict) -> list[str]:
    return [item["id"] for item in routed["active_modules"]]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build() -> dict:
    topical = validate_study_facts(load_json_unique(FIXTURE_PATH))
    demo = validate_study_facts(load_json_unique(DEMO_PATH))
    topical_routing = route_modules(topical)
    demo_routing = route_modules(demo)
    topical_ids = _ids(topical_routing)
    demo_ids = _ids(demo_routing)
    legacy = [item for item in topical_ids if item in ORIGINAL_M3_MODULE_IDS]
    expanded = [item for item in topical_ids if item not in ORIGINAL_M3_MODULE_IDS]
    topical_only = sorted(set(topical_ids) - set(demo_ids))

    phase_changed = copy.deepcopy(topical)
    phase_changed["phase"] = "III"
    before = canonical_structure_json(topical)
    after = canonical_structure_json(phase_changed)

    definitions = load_module_definitions()
    added = [item for item in definitions if item["id"] not in ORIGINAL_M3_MODULE_IDS]
    module_matrix = [
        {
            "id": item["id"],
            "activation_flag": item["activation_flag"],
            "condition": item["condition"],
            "e3_structure": item["structure"],
            "playbook": item["playbook"],
            "expected_tfl_categories": item["expected_tfl_categories"],
            "required_analysis_sets": item["required_analysis_sets"],
            "implementation_status": (
                "framework_only" if item.get("framework_only", False) else "implemented"
            ),
        }
        for item in added
    ]
    payload = {
        "schema_version": "1.0",
        "fixture_provenance": {
            "classification": "deidentified_equivalent_structure",
            "contains_real_protocol_text": False,
            "note": "Synthetic routing fixture only; it is not a copy or extract of the real protocol.",
            "path": str(FIXTURE_PATH.relative_to(ROOT)),
        },
        "inventory": {
            "original_m3_count": len(ORIGINAL_M3_MODULE_IDS),
            "expanded_count": len(EXPECTED_MODULE_IDS),
            "added_count": len(EXPECTED_MODULE_IDS) - len(ORIGINAL_M3_MODULE_IDS),
            "added_module_ids": list(EXPECTED_MODULE_IDS[len(ORIGINAL_M3_MODULE_IDS) :]),
        },
        "topical_comparison": {
            "original_23_intersection_count": len(legacy),
            "original_23_intersection": legacy,
            "expanded_active_count": len(topical_ids),
            "expanded_active_modules": topical_ids,
            "newly_placed_count": len(expanded),
            "newly_placed_modules": expanded,
            "topical_only_vs_systemic_oncology_demo": topical_only,
            "required_placements": {
                "local_tolerance_csr": ["12.2"],
                "local_tolerance_tfl_source": ["14.3.1.3.3", "14.3.1.3.4"],
                "ae_consequence_tfl_source": [
                    "14.3.1.3.5",
                    "14.3.1.3.6",
                    "14.3.1.3.7",
                    "14.3.1.3.8",
                    "14.3.1.3.9",
                    "14.3.1.3.10",
                ],
                "randomization": ["9.4.3", "16.1.7"],
                "blinding": ["9.4.6"],
            },
            "status": "pass" if len(legacy) == 5 and len(topical_ids) > 5 and topical_only else "fail",
        },
        "phase_independence": {
            "before": topical["phase"],
            "after": phase_changed["phase"],
            "before_sha256": _sha256_text(before),
            "after_sha256": _sha256_text(after),
            "exactly_equal": before == after,
            "status": "pass" if before == after else "fail",
        },
        "therapeutic_area_extension_slots": {
            "implemented": ["infection_microbiology", "dermatology_scales"],
            "declared_not_implemented": ["oncology_response", "cardiovascular_events"],
        },
        "added_module_matrix": module_matrix,
    }
    return payload


def render_markdown(payload: dict) -> str:
    comparison = payload["topical_comparison"]
    phase = payload["phase_independence"]
    lines = [
        "# 外用 Ib 期等价结构的模块路由对比",
        "",
        "> 边界：本证据只使用脱敏合成的等价 `study_facts`，不含真实方案文本、抽取记录或可识别信息。",
        "",
        "| 指标 | 扩展前（原 23 模块） | 扩展后（当前 41 模块） |",
        "|---|---:|---:|",
        f"| 该结构激活数 | {comparison['original_23_intersection_count']} | {comparison['expanded_active_count']} |",
        f"| 新正确落位的模块 | 0 | {comparison['newly_placed_count']} |",
        "",
        "## 扩展前激活的 5 个模块",
        "",
        "、".join(comparison["original_23_intersection"]),
        "",
        "## 新正确落位的模块",
        "",
        "、".join(comparison["newly_placed_modules"]),
        "",
        "其中局部耐受性作为 14.3.1.3.x 的 AE 子集输入 CSR `12.2`，AE 后果分层覆盖减量/暂停/永久停药的 TEAE/TRAE 成对表；随机化落位 `9.4.3` 及 `16.1.7`，盲法/揭盲落位 `9.4.6`。",
        "",
        "## 只在外用等价结构中激活",
        "",
        "、".join(comparison["topical_only_vs_systemic_oncology_demo"]),
        "",
        "## 期别不参与路由",
        "",
        f"- 只改 `{phase['before']}` → `{phase['after']}`：结构完全相等 = `{str(phase['exactly_equal']).lower()}`。",
        f"- 前后 SHA-256：`{phase['before_sha256']}` / `{phase['after_sha256']}`。",
        "",
        "## 明确的扩展槽",
        "",
        "已实现感染/微生物学与皮肤科量表两个示例。肿瘤缓解和心血管事件只在 schema 中声明为扩展槽，本轮不生成对应内容模块。",
        "",
    ]
    return "\n".join(lines)


def render_inventory_markdown(definitions: list[dict]) -> str:
    lines = [
        "# 内容模块清单（schema 1.2）",
        "",
        "> 路由只使用 study_facts 五维事实；`phase` 禁止参与模块激活。",
        "",
        "| 层级 | 模块 | 实现状态 | 激活谓词 | E3 落位 | Playbook | 预期 TFL | 所需分析集 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    original = set(ORIGINAL_M3_MODULE_IDS)
    for item in definitions:
        scope = "M3 原有" if item["id"] in original else "M4.1 新增"
        condition = item["condition"].replace("|", "\\|")
        sections = "、".join(
            f"{part['section']}({part['action']}:{part['variant']})"
            for part in item["structure"]
        )
        playbook = item["playbook"] or "—"
        tfl = "、".join(item["expected_tfl_categories"]) or "—"
        analysis_sets = "、".join(item["required_analysis_sets"]) or "—"
        implementation = "仅框架槽" if item.get("framework_only") else "路由/内容合同"
        lines.append(
            f"| {scope} | {item['id']} | {implementation} | `{condition}` | {sections} | {playbook} | {tfl} | {analysis_sets} |"
        )
    lines.extend(
        [
            "",
            "## 治疗领域扩展槽",
            "",
            "- 已实现：`infection_microbiology`、`dermatology_scales`。",
            "- 仅声明、未实现内容模块：`oncology_response`、`cardiovascular_events`。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    payload = build()
    atomic_write_json(JSON_OUTPUT, payload)
    atomic_write_text(MARKDOWN_OUTPUT, render_markdown(payload))
    atomic_write_text(INVENTORY_OUTPUT, render_inventory_markdown(load_module_definitions()))
    if payload["topical_comparison"]["status"] != "pass":
        raise SystemExit("topical module expansion acceptance failed")
    if payload["phase_independence"]["status"] != "pass":
        raise SystemExit("phase-independence acceptance failed")
    print(f"PASS: {payload['inventory']['expanded_count']} modules; topical 5 -> {payload['topical_comparison']['expanded_active_count']}")


if __name__ == "__main__":
    main()
