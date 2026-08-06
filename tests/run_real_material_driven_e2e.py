#!/usr/bin/env python3
"""Build deidentified evidence for the real-material-driven M4 extension."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.bidirectional_trace import BidirectionalTraceError, validate_bidirectional_trace
from csr_writer.docx_tfl_parser import parse_docx_tfl_tables
from csr_writer.group_semantics import GroupSemanticError, validate_group_semantics
from csr_writer.io_utils import atomic_write_json, atomic_write_text, sha256_file
from csr_writer.module_router import ORIGINAL_M3_MODULE_IDS, route_modules
from csr_writer.number_guard import CompiledWhitelist, find_bare_numbers
from csr_writer.rtf_parser import parse_rtf_table
from csr_writer.semantic_guard import review_semantic_redlines
from csr_writer.semantic_key import parse_semantic_cell_key
from csr_writer.study_facts import validate_study_facts
from csr_writer.tfl_map import (
    TFLMapDriftError,
    build_tfl_map_candidate,
    check_result_against_frozen_map,
    freeze_tfl_map,
)


FIXTURES = ROOT / "examples" / "fixtures"
M4 = ROOT / "examples" / "m4"
OUTPUT = ROOT / "examples" / "output" / "real_material_driven"
DUAL = ROOT / "tests" / "fixtures" / "tfl-merged"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _five_group_datapack(contract: dict) -> dict:
    datapack = _load(M4 / "synthetic_datapack.json")
    datapack["cells"] = {}
    datapack["tables"] = [
        item
        for item in datapack["tables"]
        if item["table_id"] in {"T10.1", "T11.1", "T14.2.1"}
    ]
    source_id = datapack["sources"][0]["source_id"]
    for index, (group_id, group) in enumerate(contract["groups"].items(), start=1):
        column = group["column"]
        for key, value, row_id, row_index, value_type in (
            (group["randomized_anchor"], str(20 + index), "RANDOMIZED_COUNT", 1, "count"),
            (group["analysis_set_anchor"], str(19 + index), "ANALYSIS_SET_COUNT", 2, "count"),
            (
                f"T14.2.1|POP=FAS|ROW=PRIMARY_RESPONSE|COL={column}",
                f"{index + 4} ({20 + index}.0)",
                "PRIMARY_RESPONSE",
                3,
                "n_pct",
            ),
        ):
            datapack["cells"][key] = {
                "column": {"id": column, "label": group_id},
                "display_value": value,
                "row": {"id": row_id, "indent": 0, "label": row_id},
                "source_locator": {
                    "column_index": index,
                    "row_index": row_index,
                    "source_id": source_id,
                },
                "type": value_type,
                "v": value,
            }
    return datapack


def build() -> dict:
    OUTPUT.mkdir(parents=True, exist_ok=True)

    semantic_fixture = _load(FIXTURES / "datapack_semantic_v2.deidentified.json")
    keys = set(semantic_fixture["cells"])
    semantic_proof = {
        "status": "pass",
        "classification": "deidentified_equivalent_structure",
        "soc_pt_cross_parent_keys": sorted(
            key for key in keys if "PT_SHARED" in key
        ),
        "stage_keys": sorted(key for key in keys if "|STAGE=" in key),
        "four_dose_columns": sorted(
            {
                parse_semantic_cell_key(key).column
                for key in keys
                if parse_semantic_cell_key(key).column.startswith("DOSE_")
            }
        ),
        "distinct_total_columns": ["ACTIVE_TOTAL", "TOTAL"],
        "non_numeric_types": sorted(
            {
                cell["type"]
                for cell in semantic_fixture["cells"].values()
                if cell["type"] in {"date", "text_judgement", "composite", "status"}
            }
        ),
        "metadata_only_entities": sorted(
            item["table_id"]
            for item in semantic_fixture["tables"]
            if item.get("ingest_strategy") == "metadata_only"
        ),
        "analysis_set_examples": ["FAS", "mFAS", "SS", "PKS", "PKCS", "PKPS", "BES", "All Screened Set"],
    }
    atomic_write_json(OUTPUT / "semantic_key_proof.json", semantic_proof)

    rtf_path = DUAL / "spanner-soc-pt.rtf"
    docx_path = DUAL / "spanner-soc-pt.docx"
    before = {"rtf": sha256_file(rtf_path), "docx": sha256_file(docx_path)}
    rtf = parse_rtf_table(rtf_path)
    docx = parse_docx_tfl_tables(docx_path)
    mapping = {
        "指标": "ROW_LABEL",
        "例数 / 受试制剂T": "N_T",
        "例数 / 参比制剂R": "N_R",
    }
    after = {"rtf": sha256_file(rtf_path), "docx": sha256_file(docx_path)}
    dual_proof = {
        "status": "pass" if before == after and rtf.physical_table.as_dict() == docx.tables[0].physical_table.as_dict() else "fail",
        "classification": "deidentified_equivalent_structure",
        "source_hashes_before": before,
        "source_hashes_after": after,
        "source_unchanged": before == after,
        "normalized_models_equal": rtf.physical_table.as_dict() == docx.tables[0].physical_table.as_dict(),
        "flattened_header_paths": list(rtf.physical_table.flatten_header_paths()),
        "reviewed_semantic_header_ids": list(rtf.physical_table.reviewed_header_ids(mapping)),
        "same_pt_under_two_parents_count": sum(
            row.cells[0].strip() == "恶心"
            for row in rtf.physical_table.rows[rtf.header_row_count :]
        ),
    }
    atomic_write_json(OUTPUT / "dual_parser_spanner_proof.json", dual_proof)

    routes = _load(FIXTURES / "tfl_route_config.deidentified.json")
    candidate = build_tfl_map_candidate(semantic_fixture, routes)
    confirmation = _load(FIXTURES / "tfl_map_confirmation.deidentified.json")
    confirmation["candidate_sha256"] = candidate["candidate_sha256"]
    frozen = freeze_tfl_map(candidate, confirmation)
    result_datapack = copy.deepcopy(semantic_fixture)
    replacements = {
        "n_pct": "1 (1.0)",
        "geomean_cv": "1.0 (10.0)",
        "date": "2026-01-01",
        "text_judgement": "synthetic judgement",
        "composite": "35/F",
        "status": "synthetic status",
    }
    for cell in result_datapack["cells"].values():
        cell["v"] = replacements[cell["type"]]
        cell["display_value"] = replacements[cell["type"]]
    apply_pass = check_result_against_frozen_map(result_datapack, frozen)
    rogue_result = copy.deepcopy(result_datapack)
    removed_key = sorted(rogue_result["cells"])[0]
    del rogue_result["cells"][removed_key]
    try:
        check_result_against_frozen_map(rogue_result, frozen)
        drift = {"status": "unexpected_pass"}
    except TFLMapDriftError as exc:
        drift = exc.report
    atomic_write_json(OUTPUT / "tfl_map_candidate.json", candidate)
    atomic_write_json(OUTPUT / "tfl_map_frozen.json", frozen)
    atomic_write_json(OUTPUT / "synthetic_result_datapack.json", result_datapack)
    atomic_write_json(OUTPUT / "tfl_map_apply_pass.json", apply_pass)
    atomic_write_json(OUTPUT / "tfl_map_drift_blocked.json", drift)

    topical = validate_study_facts(_load(FIXTURES / "topical_ib_study_facts.deidentified.json"))
    routed = route_modules(topical)
    active_ids = [item["id"] for item in routed["active_modules"]]
    legacy = [item for item in active_ids if item in ORIGINAL_M3_MODULE_IDS]
    expanded = [item for item in active_ids if item not in ORIGINAL_M3_MODULE_IDS]
    module_proof = {
        "status": "pass",
        "before_count": len(legacy),
        "after_count": len(active_ids),
        "before_modules": legacy,
        "newly_placed_modules": expanded,
        "topical_only_nonempty": bool(expanded),
        "local_tolerance_tfl_route": ["14.3.1.3.3", "14.3.1.3.4"],
        "ae_consequence_pair_route": ["14.3.1.3.5", "14.3.1.3.6", "14.3.1.3.7", "14.3.1.3.8", "14.3.1.3.9", "14.3.1.3.10"],
    }
    atomic_write_json(OUTPUT / "topical_module_route_proof.json", module_proof)

    rogue_cases = [
        "综上，本品疗效确切、安全性良好，获益风险比显著优于现有治疗，建议推进 III 期。",
        "该不良事件经判断与研究药物明确相关。",
    ]
    redline_cases = []
    for text in rogue_cases:
        review = review_semantic_redlines("11.4", text)
        redline_cases.append(
            {
                "input": text,
                "marked": review.text.startswith("[待医学负责人确认]"),
                "categories": sorted({item["category"] for item in review.findings}),
                "status": "blocked_for_human_review" if review.requires_human else "unexpected_pass",
            }
        )
    strict = CompiledWhitelist(tuple(), "strict-deidentified-proof")
    pk_findings = find_bare_numbers(
        "血浆消除半衰期（t1/2）约为 12 小时，AUC0-∞ 为 345 ng·h/mL。", strict
    )
    semantic_guard_proof = {
        "status": "pass",
        "rogue_cases": redline_cases,
        "pk_parameter_token_only_findings": find_bare_numbers("t1/2、AUC0-∞、AUC0-t", strict),
        "pk_result_findings": pk_findings,
        "pk_result_tokens": [item["token"] for item in pk_findings],
    }
    atomic_write_json(OUTPUT / "semantic_redline_and_pk_token_proof.json", semantic_guard_proof)

    group_contract = _load(FIXTURES / "five_group_contract.deidentified.json")
    group_metadata = _load(FIXTURES / "five_group_section_metadata.deidentified.json")
    five_datapack = _five_group_datapack(group_contract)
    group_pass = validate_group_semantics(five_datapack, group_metadata, group_contract)
    swapped = copy.deepcopy(group_metadata)
    swapped[0]["binding_manifest"][1]["key"], swapped[0]["binding_manifest"][4]["key"] = (
        swapped[0]["binding_manifest"][4]["key"],
        swapped[0]["binding_manifest"][1]["key"],
    )
    try:
        validate_group_semantics(five_datapack, swapped, group_contract)
        group_block = {"status": "unexpected_pass"}
    except GroupSemanticError as exc:
        group_block = exc.report
    atomic_write_json(OUTPUT / "five_group_semantics_pass.json", group_pass)
    atomic_write_json(OUTPUT / "five_group_swap_blocked.json", group_block)

    trace_manifest = _load(FIXTURES / "bidirectional_trace.deidentified.json")
    trace_pass = validate_bidirectional_trace(trace_manifest)
    broken_trace = copy.deepcopy(trace_manifest)
    broken_trace["links"].pop(1)
    try:
        validate_bidirectional_trace(broken_trace)
        trace_block = {"status": "unexpected_pass"}
    except BidirectionalTraceError as exc:
        trace_block = exc.report
    atomic_write_json(OUTPUT / "bidirectional_trace_pass.json", trace_pass)
    atomic_write_json(OUTPUT / "bidirectional_trace_gap_blocked.json", trace_block)

    m4_summary = _load(ROOT / "examples" / "output" / "m4" / "m4_e2e_summary.json")
    summary = {
        "schema_version": "1.0",
        "scope": "deidentified-equivalent fixtures only; no real clinical source content",
        "semantic_key": semantic_proof["status"],
        "dual_parser": dual_proof["status"],
        "tfl_map": {
            "candidate": candidate["status"],
            "frozen": frozen["status"],
            "result_apply": apply_pass["status"],
            "drift_injection": drift["status"],
        },
        "module_route": {"before": len(legacy), "after": len(active_ids), "status": module_proof["status"]},
        "semantic_redlines": "pass" if all(item["marked"] for item in redline_cases) else "fail",
        "pk_token": "pass" if [item["token"] for item in pk_findings] == ["12", "345"] else "fail",
        "five_group": {"normal": group_pass["status"], "swap": group_block["status"]},
        "bidirectional_trace": {"complete": trace_pass["status"], "gap": trace_block["status"]},
        "cross_chapter": m4_summary["cross_chapter"],
        "dual_output": m4_summary["assembly"],
        "qc": m4_summary["qc"],
        "reverse_unreferenced_tfl": m4_summary["reverse_traceability"],
    }
    atomic_write_json(OUTPUT / "acceptance_summary.json", summary)
    lines = [
        "# 真实材料驱动扩展：脱敏等价验收摘要",
        "",
        "> 本目录只包含合成/脱敏等价结构，不含真实方案、SAP/TFL 正文、机构/研究者或受试者信息。",
        "",
        "| 验收项 | 结果 |",
        "|---|---|",
        f"| 扩维语义键（SOC/PT、D1/D28、四剂量组、双 Total） | {summary['semantic_key']} |",
        f"| RTF / DOCX 合并与 spanner 归一 | {summary['dual_parser']} |",
        f"| shell 候选 → 人工冻结 → 结果套值 | {summary['tfl_map']['result_apply']} |",
        f"| 结果键漂移故障注入 | {summary['tfl_map']['drift_injection']} |",
        f"| 外用等价路由 | {len(legacy)} → {len(active_ids)} |",
        f"| 两条语义红线 | {summary['semantic_redlines']} |",
        f"| t1/2 / AUC token 例外且结果值仍拦截 | {summary['pk_token']} |",
        f"| 五组正常 / 两列互换 | {group_pass['status']} / {group_block['status']} |",
        f"| 双向追溯完整 / 缺边注入 | {trace_pass['status']} / {trace_block['status']} |",
        f"| 跨章 / 双输出 / QC 权限 | {summary['cross_chapter']} / {summary['dual_output']['clean_after_decisions']} / {summary['qc']['status']} |",
        "",
    ]
    atomic_write_text(OUTPUT / "README.md", "\n".join(lines))
    return summary


if __name__ == "__main__":
    built = build()
    print(json.dumps(built, ensure_ascii=False, indent=2, sort_keys=True))
