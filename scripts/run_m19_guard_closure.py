#!/usr/bin/env python3
"""Generate the complete M19 split-risk guard evidence package."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from csr_writer.constrained_narration import (  # noqa: E402
    ConstrainedNarrationError,
    _NARRATIVE_GLUE_TERMS,
    audit_whole_value_guard,
    constrained_guard_policy_snapshot,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    write_constrained_artifact,
)
from csr_writer.docx_result import coverage_metric_views  # noqa: E402
from csr_writer.fact_card_adversary import (  # noqa: E402
    enumerate_fact_card_attacks,
    enumerate_whole_value_relation_attacks,
    evaluate_attack_candidates,
)
from csr_writer.io_utils import atomic_write_text_bundle, sha256_file  # noqa: E402
from csr_writer.section_writer_adversary import (  # noqa: E402
    audit_guarded_section_writer_units,
)
from csr_writer.tfl_map import (  # noqa: E402
    migrate_legacy_unspecified_population_candidate,
)


OUTPUT = ROOT / "examples/output/m19"
M16 = ROOT / "examples/output/m16"
METHOD_CARD_DIR = OUTPUT / "fact_cards/method"
SAE_CARD_DIR = M16 / "sae_narratives/fact_cards"
INVENTORY = ROOT / "examples/output/m16/production_method_inventory_120.json"
READ_ONLY_HASHES = {
    ROOT.parent / "ICH-E3章节树.yaml": "87ea56d599120e265c1649881a9d3ad1c08db540a91be13de6a2bd31a6bc0942",
    ROOT.parent
    / "CSR撰写Skill开发规格书.md": "6053ea6fffa79f98d1c92840a812cd2cf12b4a329fb9420d1efcca5c6fb06bdb",
    ROOT.parent
    / "CDE申报要求核查表.md": "21374f376270f1c7d8352e7e3808e7d9385aa5e8d51cd46d12c9f178d682192b",
    ROOT.parent
    / "外部方案评估.md": "12581d0e5fefee3bf0b8c7d4e7e95147a5add099f394a73a89bf3031e4383e0c",
    ROOT
    / "examples/fixtures/synthetic_result_tfl.deidentified.docx": "76338581eb2295e0ed6552a348da369a1f72aaa61a62421fba4fd22174a56c4a",
}


METHOD_DRAFTS: dict[str, list[tuple[str, list[str]]]] = {
    "6": [
        (
            "申办方负责研究总体管理。医学监查人员负责安全性医学审阅。"
            "独立统计团队负责随机化与统计分析。各职能依据经确认的职责分工开展研究管理。",
            ["V01", "V02", "V03", "S01"],
        )
    ],
    "7": [
        (
            "研究适应证为目标皮肤病，研究产品为脱敏研究制剂。"
            "现有治疗仍存在未满足的临床需求。",
            ["V01", "V02", "V03"],
        ),
        (
            "本研究用于评价脱敏研究制剂在目标人群中的临床特征。"
            "研究设计依据现有非临床和早期临床资料确定。",
            ["S01", "S02"],
        ),
    ],
    "9.1": [
        (
            "本研究为随机、双盲、安慰剂对照研究，采用平行组设计。"
            "安慰剂组、低浓度组、中浓度组、高浓度每日两次组和"
            "高浓度每日一次组按等比例分配。",
            ["V01", "V02", "V03", "V04"],
        ),
        (
            "盲法为双盲，对照为安慰剂对照。研究依次经过筛选、随机、给药和随访。"
            "连续给药期后为安全性随访期。",
            ["V05", "V06", "V07", "S01"],
        ),
    ],
    "9.2": [
        (
            "本研究采用安慰剂对照，研究对象为目标适应证成人受试者。"
            "随机平行组设计用于比较各治疗组的观察结果。"
            "安慰剂对照用于区分治疗变化与疾病自然波动。",
            ["V01", "V02", "S01", "S02"],
        )
    ],
    "9.4.1": [
        (
            "各治疗组包括安慰剂组、低浓度组、中浓度组、高浓度每日两次组和"
            "高浓度每日一次组。剂型为乳膏剂，规格为多种浓度规格，按组使用相应浓度。",
            ["V01", "V02", "V03", "V04"],
        ),
        (
            "频次为每日一次或每日两次。途径为皮肤局部外用。疗程为连续治疗期。"
            "研究治疗按分组、浓度和频次实施给药。",
            ["V05", "V06", "V07", "S01"],
        ),
    ],
    "9.4.3": [
        (
            "本研究采用中央随机化，并按等比例分配。分层因素为研究中心和基线严重程度。"
            "随机化清单由独立统计人员生成，授权人员通过系统实施分配。",
            ["V01", "V02", "V03", "S01"],
        ),
        (
            "区组长度由独立统计师保密保存，随机号生成方为独立统计人员。"
            "随机系统为交互式网络应答系统。分配隐藏方法为授权人员登录后按顺序取得分配结果。",
            ["V04", "V05", "V06", "V07"],
        ),
    ],
    "9.4.6": [
        (
            "本研究为双盲设计，设盲对象包括受试者、研究者和疗效评价人员。"
            "研究药物在外观和包装上保持一致，盲底由独立人员保管。",
            ["V01", "V02", "V03", "V04"],
        ),
        (
            "发生需要知晓治疗信息的紧急医疗情形时，授权研究者通过应急揭盲程序提出申请，"
            "仅获授权人员可执行揭盲。研究期间维持盲态，并完整记录应急揭盲。",
            ["V05", "V06", "V07", "S01"],
        ),
    ],
    "9.5.3": [
        (
            "主要终点为目标量表治疗成功率，按方案规定的量表等级变化判定治疗成功。"
            "在方案规定的主要评价访视判定，并按 SAP 规定计算各组治疗成功率。",
            ["V01", "V02", "V03", "V04"],
        ),
        ("分析集为 FAS。本研究未采用正式 estimand 框架。", ["V05", "V06"]),
    ],
}


def _json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _codes(exc: ConstrainedNarrationError) -> list[str]:
    report = exc.report or {}
    codes = [
        str(item["code"])
        for item in report.get("findings", [])
        if isinstance(item, Mapping) and isinstance(item.get("code"), str)
    ]
    return sorted(set(codes)) or ["E-CONSTRAINED-NARRATION-BLOCKED"]


def _method_draft(section_id: str) -> dict[str, Any]:
    return {
        "paragraphs": [
            {"text": text, "fact_ids": fact_ids}
            for text, fact_ids in METHOD_DRAFTS[section_id]
        ]
    }


def _guard_freeze() -> dict[str, Any]:
    frozen = _load(OUTPUT / "guard_policy_frozen.json")
    current = constrained_guard_policy_snapshot()
    if frozen["policy_sha256"] != current["policy_sha256"]:
        raise RuntimeError("guard policy changed after holdout freeze")
    component_findings = []
    for item in frozen["frozen_components"]:
        actual = sha256_file(ROOT / item["path"])
        component_findings.append(
            {**item, "actual_sha256": actual, "unchanged": actual == item["sha256"]}
        )
    if not all(item["unchanged"] for item in component_findings):
        raise RuntimeError("guard implementation/config changed after holdout freeze")
    return {**frozen, "component_checks": component_findings}


def _hash_snapshot() -> dict[str, str]:
    return {str(path): sha256_file(path) for path in READ_ONLY_HASHES}


def _source_hash_audit(before: Mapping[str, str]) -> dict[str, Any]:
    after = _hash_snapshot()
    records = []
    for path, expected in READ_ONLY_HASHES.items():
        key = str(path)
        records.append(
            {
                "path": str(path.relative_to(ROOT.parent)),
                "expected_sha256": expected,
                "before_sha256": before[key],
                "after_sha256": after[key],
                "status": (
                    "pass"
                    if before[key] == after[key] == expected
                    else "failed"
                ),
            }
        )
    report = {
        "schema_version": "1.0",
        "status": "pass"
        if all(item["status"] == "pass" for item in records)
        else "failed",
        "real_clinical_files_accessed": False,
        "records": records,
    }
    _json(OUTPUT / "source_hash_audit.json", report)
    if report["status"] != "pass":
        raise RuntimeError("read-only source hash drift")
    return report


def _holdout(frozen: Mapping[str, Any]) -> dict[str, Any]:
    corpus = _load(OUTPUT / "holdout/legal_rewrites.json")
    if corpus["guard_bundle_sha256_opaque_marker"] != frozen["guard_bundle_sha256"]:
        raise RuntimeError("holdout corpus is not bound to the frozen guard bundle")
    builder_path = ROOT / corpus["construction"]["builder_path"]
    tree = ast.parse(builder_path.read_text(encoding="utf-8"))
    imported_roots = sorted(
        {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        | {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
    )
    if not set(imported_roots).issubset(
        {"__future__", "argparse", "hashlib", "json", "pathlib"}
    ):
        raise RuntimeError("holdout builder imported a guard/runtime module")

    method_cards = {
        card["card_id"]: card
        for card in (_load(path) for path in METHOD_CARD_DIR.glob("*.json"))
    }
    sae_cards = {
        card["card_id"]: card
        for card in (_load(path) for path in SAE_CARD_DIR.glob("*.json"))
    }
    records = []
    for case in corpus["cases"]:
        card = (
            sae_cards[case["card_id"]]
            if case["path"] == "sae_exact_surface"
            else method_cards[case["card_id"]]
        )
        reviewed_surfaces = {
            item["text"] for item in card.get("authorized_surface_forms", [])
        }
        facts_by_id = {fact["fact_id"]: fact for fact in card["facts"]}
        accidental_overlap = [
            paragraph["text"]
            for paragraph in case["paragraphs"]
            if paragraph["text"] in reviewed_surfaces
            and not all(
                facts_by_id[fact_id].get("value") is None
                for fact_id in paragraph["fact_ids"]
            )
        ]
        try:
            ingest_constrained_draft(
                emit_fact_card_envelope(card), {"paragraphs": case["paragraphs"]}
            )
        except ConstrainedNarrationError as exc:
            actual = "blocked"
            codes = _codes(exc)
        else:
            actual = "pass"
            codes = []
        records.append(
            {
                **case,
                "actual": actual,
                "error_codes": codes,
                "accidental_exact_reviewed_surface_overlap": accidental_overlap,
                "official_holdout_eligible": not accidental_overlap,
            }
        )

    paths = {}
    for path in ("method_label_bound", "sae_exact_surface"):
        all_records = [item for item in records if item["path"] == path]
        eligible = [item for item in all_records if item["official_holdout_eligible"]]
        blocked = [item for item in eligible if item["actual"] == "blocked"]
        paths[path] = {
            "constructed_case_count": len(all_records),
            "official_holdout_count": len(eligible),
            "excluded_accidental_surface_collision_count": len(all_records) - len(eligible),
            "pass_count": len(eligible) - len(blocked),
            "blocked_count": len(blocked),
            "false_positive_rate": round(len(blocked) / len(eligible), 6)
            if eligible
            else None,
            "false_positive_percent": round(100 * len(blocked) / len(eligible), 2)
            if eligible
            else None,
            "blocked_case_ids": [item["case_id"] for item in blocked],
            "blocked_sentences": [
                {
                    "case_id": item["case_id"],
                    "paragraphs": [p["text"] for p in item["paragraphs"]],
                    "error_codes": item["error_codes"],
                }
                for item in blocked
            ],
        }
    report = {
        "schema_version": "1.0",
        "guard_bundle_sha256": frozen["guard_bundle_sha256"],
        "independence_proof": {
            **corpus["construction"],
            "builder_import_roots": imported_roots,
            "builder_imports_only_standard_library": True,
            "corpus_authored_after_guard_freeze": corpus["status"]
            == "authored_after_guard_policy_freeze",
            "accidental_surface_collisions_excluded_from_official_denominator": True,
        },
        "paths": paths,
        "records": records,
    }
    _json(OUTPUT / "holdout/false_positive_results.json", report)
    lines = [
        "# M19 独立留出集误伤率",
        "",
        f"- 守卫束哈希：`{frozen['guard_bundle_sha256']}`",
        f"- 构造集：{corpus['case_count']} 条；方法学 {corpus['method_case_count']} 条，SAE {corpus['sae_case_count']} 条。",
        "- 构造器只导入 Python 标准库，只读移除已审句式后的事实卡；不导入守卫，不读连接词表或既有产物。",
        "- 与已审整句意外完全重合的样本仅在评估阶段识别，已从正式误伤率分母排除。",
        "",
        "| 路径 | 正式留出数 | 通过 | 误拦 | 误伤率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for path, label in (
        ("method_label_bound", "方法学有限自由"),
        ("sae_exact_surface", "SAE 逐字预审"),
    ):
        item = paths[path]
        lines.append(
            f"| {label} | {item['official_holdout_count']} | {item['pass_count']} | "
            f"{item['blocked_count']} | {item['false_positive_percent']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "> SAE 路径的高误伤是逐字预审策略的预期代价，不与方法学路径合并。",
        ]
    )
    _write(OUTPUT / "holdout/留出集构造与误伤结果.md", "\n".join(lines))
    return report


def _combined_candidates(card: Mapping[str, Any]) -> list[Any]:
    candidates = [
        *enumerate_fact_card_attacks(card),
        *enumerate_whole_value_relation_attacks(card),
    ]
    unique = {
        (item.strategy, item.text, item.fact_ids): item for item in candidates
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            -item.readability_score,
            item.strategy,
            item.text,
            item.fact_ids,
        ),
    )


def _m19_readability_score(item: Mapping[str, Any]) -> int:
    """Rank readable raw passes without removing any pass from the evidence."""

    text = str(item["text"])
    score = int(item["readability_score"])
    if 8 <= len(text) <= 42:
        score += 3
    if re.search(
        r"^(?:FAS|中央随机化|交互式网络应答系统|安慰剂对照|平行组设计|乳膏剂)"
        r"(?:用于|由|包括|为|是)",
        text,
    ):
        score += 10
    if re.search(r"(?:平行组设计|随机、双盲、安慰剂对照研究)(?:为|是)", text):
        score += 8
    if text.startswith("本研究采用"):
        score += 4
    for awkward in ("由由", "按按", "采用本研究", "采用受试者", "采用研究者"):
        if awkward in text:
            score -= 20
    return score


def _adversary() -> dict[str, Any]:
    path_cards = {
        "method_label_bound": [
            _load(path) for path in sorted(METHOD_CARD_DIR.glob("*.json"))
        ],
        "sae_exact_surface": [
            _load(path) for path in sorted(SAE_CARD_DIR.glob("*.json"))
        ],
    }
    modes = {
        "method_label_bound": "whole_values_label_bound",
        "sae_exact_surface": "whole_declared_values",
    }
    paths: dict[str, Any] = {}
    for path_id, cards in path_cards.items():
        all_passes = []
        card_reports = []
        candidate_total = 0
        strategy_candidates: Counter[str] = Counter()
        strategy_passes: Counter[str] = Counter()
        for card in cards:
            candidates = _combined_candidates(card)
            result = evaluate_attack_candidates(
                card, candidates, coverage_mode=modes[path_id]
            )
            candidate_total += result["candidate_count"]
            strategy_candidates.update(result["strategy_candidate_counts"])
            strategy_passes.update(result["strategy_raw_pass_counts"])
            passes = [
                {
                    "card_id": card["card_id"],
                    "section_id": card["section_id"],
                    **item,
                }
                for item in result["raw_passes"]
            ]
            all_passes.extend(passes)
            card_reports.append(
                {
                    "card_id": card["card_id"],
                    "section_id": card["section_id"],
                    "candidate_count": result["candidate_count"],
                    "raw_pass_count": result["raw_pass_count"],
                    "strategy_candidate_counts": result["strategy_candidate_counts"],
                    "strategy_raw_pass_counts": result["strategy_raw_pass_counts"],
                }
            )
        all_passes.sort(
            key=lambda item: (
                -_m19_readability_score(item),
                -int(item["readability_score"]),
                item["text"],
                item["card_id"],
            )
        )
        path_report = {
            "coverage_mode": modes[path_id],
            "card_count": len(cards),
            "candidate_count": candidate_total,
            "raw_pass_count": len(all_passes),
            "pass_rate": round(len(all_passes) / candidate_total, 8)
            if candidate_total
            else 0.0,
            "pass_percent": round(100 * len(all_passes) / candidate_total, 4)
            if candidate_total
            else 0.0,
            "strategy_candidate_counts": dict(sorted(strategy_candidates.items())),
            "strategy_raw_pass_counts": dict(sorted(strategy_passes.items())),
            "top_10_readable_raw_passes": all_passes[:10],
            "raw_passes_are_unfiltered": True,
            "raw_passes": all_passes,
            "cards": card_reports,
        }
        paths[path_id] = path_report
        _json(OUTPUT / f"adversary/{path_id}_raw_passes.json", path_report)

    unfamiliar = next(
        card
        for card in path_cards["method_label_bound"]
        if card["card_id"] == "M19-METHOD-6-UNFAMILIAR"
    )
    unfamiliar_candidates = _combined_candidates(unfamiliar)
    unfamiliar_result = evaluate_attack_candidates(
        unfamiliar,
        unfamiliar_candidates,
        coverage_mode="whole_values_label_bound",
    )
    n7 = {
        "schema_version": "1.0",
        "card_id": unfamiliar["card_id"],
        "section_id": unfamiliar["section_id"],
        "not_one_of_m18_nine_cards": True,
        "candidate_count": unfamiliar_result["candidate_count"],
        "raw_pass_count": unfamiliar_result["raw_pass_count"],
        "raw_passes": unfamiliar_result["raw_passes"],
        "boundary": "bounded adversary; not a proof over all Chinese semantics",
    }
    _json(OUTPUT / "adversary/N7_unfamiliar_card.json", n7)
    report = {
        "schema_version": "1.0",
        "measurement": (
            "paragraph-level positive-coverage authorization; candidates can be "
            "inserted into an otherwise complete draft, so missing-card-field failures "
            "do not inflate the blocking rate"
        ),
        "threshold_selected_by_implementation": False,
        "paths": paths,
        "n7": n7,
    }
    _json(OUTPUT / "adversary/automatic_adversary_summary.json", report)
    return report


def _method_outputs() -> dict[str, Any]:
    records = []
    combined = ["# M19 方法学有限自由生成样例", ""]
    target = OUTPUT / "method_actual_prose"
    for path in sorted(METHOD_CARD_DIR.glob("*.json")):
        card = _load(path)
        section_id = str(card["section_id"])
        draft = _method_draft(section_id)
        envelope = emit_fact_card_envelope(card)
        artifact = ingest_constrained_draft(envelope, draft)
        markdown_path = target / f"{section_id}.md"
        metadata_path = target / f"{section_id}.meta.json"
        write_constrained_artifact(artifact, markdown_path, metadata_path)
        _json(target / f"{section_id}.draft.json", draft)
        prose = "\n\n".join(item["text"] for item in draft["paragraphs"])
        combined.extend([f"## {card['title']}", "", prose, ""])
        records.append(
            {
                "section_id": section_id,
                "card_id": card["card_id"],
                "authorized_surface_form_count": len(
                    card.get("authorized_surface_forms", [])
                ),
                "paragraph_count": len(draft["paragraphs"]),
                "status": artifact.metadata["status"],
                "guard_profile": artifact.metadata["forward_coverage"][
                    "guard_profile"
                ],
                "markdown": str(markdown_path.relative_to(ROOT)),
                "metadata": str(metadata_path.relative_to(ROOT)),
            }
        )
    _write(OUTPUT / "方法学有限自由成文样例.md", "\n".join(combined))

    original = _load(M16 / "method_pilot/fact_cards/9.4.3.json")
    natural = _method_draft("9.4.3")
    old_exact = audit_whole_value_guard(emit_fact_card_envelope(original), natural)
    current = ingest_constrained_draft(emit_fact_card_envelope(original), natural)
    new_connectors = constrained_guard_policy_snapshot()["policy"]["methodology"][
        "connector_terms"
    ]
    comparison = {
        "schema_version": "1.0",
        "example_section": "9.4.3",
        "m18_exact_surface_path": {
            "status": old_exact["status"],
            "sentence": natural["paragraphs"][0]["text"],
        },
        "m19_method_path": {
            "status": current.metadata["status"],
            "sentence": natural["paragraphs"][0]["text"],
            "authorized_surface_forms_used_for_release": False,
        },
        "connector_count_before_general_glue": len(_NARRATIVE_GLUE_TERMS),
        "connector_count_after_method_policy": len(new_connectors),
        "new_structural_connectors": sorted(
            set(new_connectors) - set(_NARRATIVE_GLUE_TERMS)
        ),
    }
    _json(OUTPUT / "method_actual_prose/connector_freedom_comparison.json", comparison)
    _write(
        OUTPUT / "method_actual_prose/连接词放宽前后对照.md",
        "\n".join(
            [
                "# 方法学连接词放宽前后对照",
                "",
                f"- M18 整句预审路径：{comparison['m18_exact_surface_path']['status']}。",
                f"- M19 方法学路径：{comparison['m19_method_path']['status']}。",
                f"- 句子：{comparison['m19_method_path']['sentence']}",
                f"- 方法学专用连接词表新增结构词：{'、'.join(comparison['new_structural_connectors']) or '无'}。",
                "- 自由度恢复的代价不用人工阈值掩盖；完整真值错关系的原始放行项另表报告。",
            ]
        ),
    )
    return {"unit_count": len(records), "records": records, "comparison": comparison}


def _sae_path_proof() -> dict[str, Any]:
    records = []
    for card_path in sorted(SAE_CARD_DIR.glob("*.json")):
        card = _load(card_path)
        draft = _load(
            M16 / f"sae_narratives/raw_drafts/{card_path.stem}.json"
        )
        artifact = ingest_constrained_draft(emit_fact_card_envelope(card), draft)
        tampered = copy.deepcopy(draft)
        tampered["paragraphs"][0]["text"] = tampered["paragraphs"][0][
            "text"
        ].replace("。", "，", 1)
        try:
            ingest_constrained_draft(emit_fact_card_envelope(card), tampered)
        except ConstrainedNarrationError as exc:
            tampered_status = "blocked"
            codes = _codes(exc)
        else:
            tampered_status = "pass"
            codes = []
        records.append(
            {
                "card_id": card["card_id"],
                "reviewed_surface_status": artifact.metadata["status"],
                "punctuation_variant_status": tampered_status,
                "punctuation_variant_error_codes": codes,
            }
        )
    report = {
        "schema_version": "1.0",
        "path": "sae_exact_reviewed_surface",
        "release_requires_authorized_surface_forms": True,
        "records": records,
    }
    _json(OUTPUT / "sae_exact_path_proof.json", report)
    return report


def _n5_migration() -> dict[str, Any]:
    source = ROOT / "examples/output/m8/tfl_map_candidate.deidentified.json"
    before_hash = sha256_file(source)
    candidate = _load(source)
    migration = migrate_legacy_unspecified_population_candidate(candidate)
    after_hash = sha256_file(source)
    report = {
        **migration,
        "source_path": str(source.relative_to(ROOT)),
        "source_sha256_before": before_hash,
        "source_sha256_after": after_hash,
        "source_unchanged": before_hash == after_hash,
        "migration_policy": (
            "retire unresolved entity from active mapping; no guessed POP and no "
            "silent replacement key; reparse and human confirmation required"
        ),
    }
    _json(OUTPUT / "N5_legacy_unspecified_population_migration.json", report)
    lines = [
        "# N5 历史 `POP=UNSPECIFIED` 迁移",
        "",
        f"- 迁移前：{report['before']['unspecified_entity_count']} 个实体，{report['before']['unspecified_key_count']} 个未解析键。",
        f"- 迁移后活跃未解析键：{report['after']['active_unspecified_key_count']}。",
        f"- 待重新解析实体：{report['after']['pending_reparse_entity_count']}。",
        "- 替代键：空；未根据位置或标题猜造 POP。",
        f"- 历史源文件未改动：{'yes' if report['source_unchanged'] else 'no'}。",
    ]
    _write(OUTPUT / "N5迁移前后对照.md", "\n".join(lines))
    return report


def _f8_denominators() -> dict[str, Any]:
    source = _load(ROOT / "examples/output/m15/result_match_report.json")
    metrics = coverage_metric_views(source["entity_reports"])
    report = {
        "schema_version": "1.0",
        "source_report": "examples/output/m15/result_match_report.json",
        "result_side": {
            "matched": metrics["result_side_matched_value_count"],
            "denominator": metrics["result_side_semantic_value_count"],
            "rate": metrics["result_side_match_rate"],
            "question": "结果 TFL 中已识别的语义值有多少能套入冻结映射？",
        },
        "frozen_shell_side": {
            "matched": metrics["frozen_shell_side_matched_cell_count"],
            "denominator": metrics["frozen_shell_side_contract_cell_count"],
            "rate": metrics["frozen_shell_side_match_rate"],
            "question": "壳阶段明确冻结的单元契约有多少已被结果值填满？",
        },
        "supplementary_full_shell_view": {
            "rate": metrics["shell_oriented_match_rate"],
            "not_substituted_for_required_shell_denominator": True,
        },
    }
    _json(OUTPUT / "F8_dual_denominator.json", report)
    _write(
        OUTPUT / "F8双口径说明.md",
        "\n".join(
            [
                "# F8 匹配率双口径",
                "",
                f"- 结果侧：{report['result_side']['matched']}/{report['result_side']['denominator']} = {100 * report['result_side']['rate']:.1f}%。{report['result_side']['question']}",
                f"- 壳侧：{report['frozen_shell_side']['matched']}/{report['frozen_shell_side']['denominator']} = {100 * report['frozen_shell_side']['rate']:.2f}%。{report['frozen_shell_side']['question']}",
                "- 两个分母回答不同问题，不合并，不择优报告。",
            ]
        ),
    )
    return report


def _guarded_writer() -> dict[str, Any]:
    report = audit_guarded_section_writer_units(INVENTORY)
    _json(OUTPUT / "guarded_section_writer/attack_results.json", report)
    sentence = report["sentence_driven"]
    lines = [
        "# 34 个 `guarded_section_writer` 单元攻击结果",
        "",
        f"- 清单单元：{report['unit_count']}；共享基线通过：{report['baseline_pass_count']}。",
        f"- 生成攻击：{report['attack_count']}；拦截：{report['blocked_attack_count']}；原始放行：{report['raw_pass_count']}。",
        f"- 运行时已接线：{report['runtime_wired_count']}；未接线：{report['runtime_unwired_count']}。",
        f"- `sentence_driven` 单元：{', '.join(sentence['section_ids'])}。",
        f"- 零占位符时，附加无数值价值判断仍放行：{', '.join(sentence['arbitrary_prose_passed_section_ids']) or '无'}。",
        f"- 部分使用占位符时重新进入数量/顺序校验并被拦截：{', '.join(sentence['partial_binding_blocked_section_ids'])}。",
        "",
        "> 结论：独立审计提出的跳过分支存在。它不允许部分占位符混入，但允许模型在正确结论标记外附加无数值、无源正文。本轮按要求如实报告，未擅自修复。",
    ]
    _write(OUTPUT / "guarded_section_writer/34单元攻击报告.md", "\n".join(lines))
    return report


def _new_findings(
    holdout: Mapping[str, Any],
    adversary: Mapping[str, Any],
    guarded: Mapping[str, Any],
    n5: Mapping[str, Any],
    f8: Mapping[str, Any],
) -> list[dict[str, Any]]:
    method = adversary["paths"]["method_label_bound"]
    sae_fp = holdout["paths"]["sae_exact_surface"]
    findings = [
        {
            "id": "M19-N1",
            "severity": "requires_sponsor_decision",
            "finding": "完整真值之间可被连接成卡内未声明的关系",
            "evidence": f"{method['raw_pass_count']}/{method['candidate_count']} 对抗候选放行",
            "status": "reported_not_thresholded",
        },
        {
            "id": "M19-N2",
            "severity": "expected_product_tradeoff",
            "finding": "SAE 逐字预审路径对未参与句式库构建的合法改写保持高误伤",
            "evidence": f"{sae_fp['blocked_count']}/{sae_fp['official_holdout_count']} 留出改写被拦",
            "status": "accepted_split_path_cost",
        },
        {
            "id": "M19-N3",
            "severity": "serious",
            "finding": "sentence_driven 零占位符分支可放行附加的无数值无源正文",
            "evidence": guarded["sentence_driven"][
                "arbitrary_prose_passed_section_ids"
            ],
            "status": "open_reported_per_scope",
        },
        {
            "id": "M19-N4",
            "severity": "serious",
            "finding": "34 个清单单元中有 8 个没有运行时 playbook/variant 接线",
            "evidence": guarded["runtime_unwired_section_ids"],
            "status": "open_reported_per_scope",
        },
        {
            "id": "M19-N5",
            "severity": "migration_block",
            "finding": "历史未解析 POP 已从活跃映射退役，但在重新解析前无替代键",
            "evidence": n5["retired_entities"],
            "status": "blocked_pending_reparse",
        },
        {
            "id": "M19-N6",
            "severity": "known_coverage_limit",
            "finding": "冻结壳显式契约的填值覆盖率仅为壳侧口径",
            "evidence": f"{f8['frozen_shell_side']['matched']}/{f8['frozen_shell_side']['denominator']}",
            "status": "reported_with_correct_denominator",
        },
        {
            "id": "M19-N7",
            "severity": "measurement_note",
            "finding": "留出构造集中有少数样本与 SAE 已审整句意外相同",
            "evidence": sae_fp["excluded_accidental_surface_collision_count"],
            "status": "excluded_before_rate_calculation",
        },
    ]
    _json(OUTPUT / "M19_new_findings.json", findings)
    lines = [
        "# M19 新发现问题清单（未筛选）",
        "",
        "| ID | 严重度 | 发现 | 状态 |",
        "|---|---|---|---|",
    ]
    for item in findings:
        lines.append(
            f"| {item['id']} | {item['severity']} | {item['finding']} | {item['status']} |"
        )
    _write(OUTPUT / "M19新发现问题清单.md", "\n".join(lines))
    return findings


def _summary(
    frozen: Mapping[str, Any],
    holdout: Mapping[str, Any],
    adversary: Mapping[str, Any],
    methods: Mapping[str, Any],
    sae: Mapping[str, Any],
    guarded: Mapping[str, Any],
    n5: Mapping[str, Any],
    f8: Mapping[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    method_adv = adversary["paths"]["method_label_bound"]
    sae_adv = adversary["paths"]["sae_exact_surface"]
    method_fp = holdout["paths"]["method_label_bound"]
    sae_fp = holdout["paths"]["sae_exact_surface"]
    payload = {
        "schema_version": "1.0",
        "status": "implemented_with_open_findings_reported",
        "guard_bundle_sha256": frozen["guard_bundle_sha256"],
        "split_paths": {
            "sae": {
                "section_ids": ["12.3.2"],
                "mode": "exact reviewed authorized_surface_forms",
            },
            "methodology": {
                "section_ids": frozen["policy"]["methodology"]["section_ids"],
                "mode": "whole atomic values plus optional own-label binding",
            },
        },
        "method_actual_prose_unit_count": methods["unit_count"],
        "sae_exact_path_card_count": len(sae["records"]),
        "adversary": {
            "method": {
                "passes": method_adv["raw_pass_count"],
                "candidates": method_adv["candidate_count"],
                "pass_percent": method_adv["pass_percent"],
            },
            "sae": {
                "passes": sae_adv["raw_pass_count"],
                "candidates": sae_adv["candidate_count"],
                "pass_percent": sae_adv["pass_percent"],
            },
        },
        "holdout": {
            "method": {
                "blocked": method_fp["blocked_count"],
                "denominator": method_fp["official_holdout_count"],
                "false_positive_percent": method_fp["false_positive_percent"],
            },
            "sae": {
                "blocked": sae_fp["blocked_count"],
                "denominator": sae_fp["official_holdout_count"],
                "false_positive_percent": sae_fp["false_positive_percent"],
            },
        },
        "guarded_section_writer": {
            "units": guarded["unit_count"],
            "attacks": guarded["attack_count"],
            "raw_passes": guarded["raw_pass_count"],
            "sentence_driven_open_finding": guarded["sentence_driven"][
                "arbitrary_prose_passed_section_ids"
            ],
            "runtime_unwired": guarded["runtime_unwired_section_ids"],
        },
        "n5": {
            "retired_unspecified_keys": n5["before"]["unspecified_key_count"],
            "active_unspecified_keys": n5["after"]["active_unspecified_key_count"],
            "release_eligible": n5["release_eligible"],
        },
        "f8": {
            "result_side": f8["result_side"],
            "frozen_shell_side": f8["frozen_shell_side"],
        },
        "new_finding_count": len(findings),
        "threshold_selected_by_implementation": False,
    }
    _json(OUTPUT / "M19_summary.json", payload)

    methodology_ids = frozen["policy"]["methodology"]["section_ids"]
    _write(
        OUTPUT / "分而治之机制与适用单元.md",
        "\n".join(
            [
                "# 分而治之机制与适用单元",
                "",
                "## SAE 逐字预审路径",
                "",
                "- 适用单元：`12.3.2`及其 SAE 个例叙述产物。",
                "- 模式：每段必须等于已确认 `authorized_surface_forms` 中的完整句式；因果词、缺口句和医学负责人留槽继续严格控制。",
                "- 人工边界：逐例审核句式；守卫只证明未被改写，不代替医学正确性判断。",
                "",
                "## 方法学有限自由路径",
                "",
                "- 模式：完整字段值不可切分；标签可省略，但一旦出现必须与自己 `fact_id` 的值相邻；每段声明的事实必须在本段出现完整值。",
                "- 不使用 `authorized_surface_forms`，不回退到 M17 子串守卫。",
                "- 适用单元："
                + "、".join(f"`{item}`" for item in methodology_ids)
                + "。",
                "- 已实际成文验证：`6`、`7`、`9.1`、`9.2`、`9.4.1`、`9.4.3`、`9.4.6`、`9.5.3`。",
            ]
        ),
    )

    top = method_adv["top_10_readable_raw_passes"]
    lines = [
        "# M19 守卫线收口报告",
        "",
        "## 分而治之",
        "",
        "- SAE `12.3.2` 保持逐字预审句，连标点改动也拦截。",
        f"- 方法学 {len(frozen['policy']['methodology']['section_ids'])} 个单元改用整值 + 标签归属守卫，不依赖已审整句。",
        f"- 已生成可直接阅读的方法学正文 {methods['unit_count']} 节，包含 9.4.3 / 9.4.6 / 9.5.3。",
        "",
        "## 两个数字（分路径）",
        "",
        "| 路径 | 对抗放行率 | 独立留出误伤率 |",
        "|---|---:|---:|",
        f"| 方法学 | {method_adv['raw_pass_count']}/{method_adv['candidate_count']} = {method_adv['pass_percent']:.4f}% | {method_fp['blocked_count']}/{method_fp['official_holdout_count']} = {method_fp['false_positive_percent']:.2f}% |",
        f"| SAE | {sae_adv['raw_pass_count']}/{sae_adv['candidate_count']} = {sae_adv['pass_percent']:.4f}% | {sae_fp['blocked_count']}/{sae_fp['official_holdout_count']} = {sae_fp['false_positive_percent']:.2f}% |",
        "",
        "> 未设置或暗用可接受阈值。方法学的原始放行项全部保留，由申办方判断。",
        "",
        "## 方法学路径可读性最高的前 10 条放行项",
        "",
    ]
    if top:
        for index, item in enumerate(top, start=1):
            lines.append(
                f"{index}. `{item['section_id']}` {item['text']}（{item['strategy']}）"
            )
    else:
        lines.append("无放行项。")
    lines.extend(
        [
            "",
            "## `guarded_section_writer`",
            "",
            f"- 34 个单元共执行 {guarded['attack_count']} 项攻击，原始放行 {guarded['raw_pass_count']} 项。",
            f"- `sentence_driven` 新洞影响：{', '.join(guarded['sentence_driven']['arbitrary_prose_passed_section_ids'])}。",
            f"- 清单中未接线单元：{', '.join(guarded['runtime_unwired_section_ids'])}。",
            "",
            "## N5 / F8 / N7",
            "",
            f"- N5：历史 {n5['before']['unspecified_key_count']} 个 `POP=UNSPECIFIED` 键已从活跃映射退役；无猜造替换键，待重新解析。",
            f"- F8：结果侧 {f8['result_side']['matched']}/{f8['result_side']['denominator']} = {100 * f8['result_side']['rate']:.1f}%；壳侧 {f8['frozen_shell_side']['matched']}/{f8['frozen_shell_side']['denominator']} = {100 * f8['frozen_shell_side']['rate']:.2f}%。",
            f"- N7：陌生事实卡生成 {adversary['n7']['candidate_count']} 个候选，放行 {adversary['n7']['raw_pass_count']} 个；不声称完备。",
        ]
    )
    _write(OUTPUT / "M19守卫收口报告.md", "\n".join(lines))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    before_hashes = _hash_snapshot()
    frozen = _guard_freeze()
    holdout = _holdout(frozen)
    adversary = _adversary()
    methods = _method_outputs()
    sae = _sae_path_proof()
    guarded = _guarded_writer()
    n5 = _n5_migration()
    f8 = _f8_denominators()
    findings = _new_findings(holdout, adversary, guarded, n5, f8)
    _source_hash_audit(before_hashes)
    summary = _summary(
        frozen, holdout, adversary, methods, sae, guarded, n5, f8, findings
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
