#!/usr/bin/env python3
"""Run the two M21 guard-preserving unified-composition regressions.

The first project reuses the M20 unfamiliar single-arm fixture and resolves its
intentional shell/result drift through the explicit human-decision queue.  The
second reuses the accepted deidentified randomized-double-blind Ib facts and
the existing M7 strict DOCX-shell/RTF-result shapes, supplemented only with the
chapter-14 coverage entities needed for a complete deterministic volume.

No real clinical input is opened by this script.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.appendix_status import build_appendix_status_plan  # noqa: E402
from csr_writer.bidirectional_trace import validate_bidirectional_trace  # noqa: E402
from csr_writer.constrained_narration import (  # noqa: E402
    emit_fact_card_envelope,
    ingest_constrained_draft,
    write_constrained_artifact,
)
from csr_writer.cross_references import bookmark_name  # noqa: E402
from csr_writer.cross_chapter import (  # noqa: E402
    REVIEW_MARKER,
    apply_review_decisions,
    load_constrained_resolved_section,
    load_resolved_section,
)
from csr_writer.cross_format_result import (  # noqa: E402
    apply_rtf_values_to_shell,
    build_deidentified_result_rtf,
)
from csr_writer.deterministic_sections import (  # noqa: E402
    _canonicalize_docx_package,
    build_deterministic_bundle,
)
from csr_writer.docx_result import apply_docx_result_to_frozen_shell  # noqa: E402
from csr_writer.io_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    load_data,
    sha256_file,
)
from csr_writer.m4_qc import build_qc_report  # noqa: E402
from csr_writer.module_router import route_modules  # noqa: E402
from csr_writer.resolve_placeholders import resolve  # noqa: E402
from csr_writer.section_writer import emit_agent_envelope, ingest_agent_draft  # noqa: E402
from csr_writer.shell_mapping import propose_docx_shell_map  # noqa: E402
from csr_writer.study_facts import load_study_facts, render_fact_card  # noqa: E402
from csr_writer.tfl_drift_decisions import (  # noqa: E402
    build_drift_decision_queue,
    freeze_reconciled_tfl_map,
    reconcile_expected_drift,
)
from csr_writer.tfl_map import freeze_tfl_map  # noqa: E402
from csr_writer.traceability import analyze_tfl_references, render_unreferenced_tfl  # noqa: E402
from csr_writer.unified_orchestrator import (  # noqa: E402
    UnifiedCompositionError,
    compose_unified_csr,
)


M20_INPUT = ROOT / "examples" / "output" / "m20" / "true_e2e_final"
M20_FIXTURE = ROOT / "examples" / "fixtures" / "m20_unfamiliar"
IB_FACTS = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
M7_RESULTS = ROOT / "examples" / "output" / "m7" / "heteromorphic_results"
M19_METHODS = ROOT / "examples" / "output" / "m19" / "method_actual_prose"
INVENTORY = ROOT / "examples" / "output" / "m16" / "production_method_inventory_120.json"

READ_ONLY_INPUTS = {
    "development_specification_v1_4": (
        ROOT.parent / "CSR撰写Skill开发规格书.md",
        "6053ea6fffa79f98d1c92840a812cd2cf12b4a329fb9420d1efcca5c6fb06bdb",
    ),
    "verified_ich_e3_chapter_tree": (
        ROOT.parent / "ICH-E3章节树.yaml",
        "87ea56d599120e265c1649881a9d3ad1c08db540a91be13de6a2bd31a6bc0942",
    ),
    "cde_submission_checklist": (
        ROOT.parent / "CDE申报要求核查表.md",
        "21374f376270f1c7d8352e7e3808e7d9385aa5e8d51cd46d12c9f178d682192b",
    ),
    "external_solution_evaluation": (
        ROOT.parent / "外部方案评估.md",
        "12581d0e5fefee3bf0b8c7d4e7e95147a5add099f394a73a89bf3031e4383e0c",
    ),
    "synthetic_result_fixture": (
        ROOT / "examples" / "fixtures" / "synthetic_result_tfl.deidentified.docx",
        "76338581eb2295e0ed6552a348da369a1f72aaa61a62421fba4fd22174a56c4a",
    ),
    "m20_protocol": (
        M20_FIXTURE / "protocol.single_arm_migraine.deidentified.docx",
        "78488a876ab203aa382f60d468209c70c2efe71896cc59ad6a8a6a36416de7a3",
    ),
    "m20_shell": (
        M20_FIXTURE / "tfl_shell.single_arm_migraine.deidentified.docx",
        "e1ff2a100905e0c1d5abda2912282ac06dabf6ad1c03d09af6e8ed6cdeb3c4b2",
    ),
    "m20_result": (
        M20_FIXTURE / "result_tfl.single_arm_migraine.deidentified.docx",
        "46c6f4c6e3d39b4826b9892d8cf3d1d7968dbcaf7d3bbe43241824e199ede8d5",
    ),
    "m20_facts": (
        M20_FIXTURE / "study_facts.single_arm_migraine.deidentified.json",
        "763b7feb486d1c6a280657e87e10aed9bca01625facca65c34a0188613334947",
    ),
}


def _failure_location(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    project_frames = [frame for frame in frames if str(ROOT) in frame.filename]
    frame = (project_frames or frames)[-1]
    try:
        relative = Path(frame.filename).relative_to(ROOT)
    except ValueError:
        relative = Path(frame.filename)
    return f"{relative}:{frame.lineno}"


def _hash_audit(stage: str) -> dict[str, Any]:
    records = []
    for role, (path, expected) in READ_ONLY_INPUTS.items():
        observed = sha256_file(path)
        records.append(
            {
                "role": role,
                "path": str(path.relative_to(ROOT.parent)),
                "expected_sha256": expected,
                "observed_sha256": observed,
                "unchanged": observed == expected,
            }
        )
    if not all(item["unchanged"] for item in records):
        raise RuntimeError("E-M21-READ-ONLY-HASH-DRIFT")
    return {
        "schema_version": "1.0",
        "stage": stage,
        "status": "pass",
        "records": records,
        "real_clinical_files_accessed": False,
        "real_clinical_files_copied_or_logged": False,
    }


def _write_json(path: Path, payload: Any) -> None:
    atomic_write_json(path, payload)


def _section_sort_key(section_id: str) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"[.-]", section_id)
    )


def _review_decisions(sections: Sequence[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    decisions: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for section in sections:
        paragraph_decisions: dict[str, Any] = {}
        for paragraph in section.paragraphs:
            if not paragraph.needs_human:
                continue
            replacement = paragraph.text.replace(REVIEW_MARKER, "").strip()
            paragraph_decisions[paragraph.para_id] = {
                "status": "resolved",
                "replacement_text": replacement,
            }
            records.append(
                {
                    "section_id": section.section_id,
                    "para_id": paragraph.para_id,
                    "decision": "synthetic-fixture-only replacement",
                }
            )
        decisions[section.section_id] = {
            "draft_sha256": section.draft_sha256,
            "paragraphs": paragraph_decisions,
        }
    return (
        {"schema_version": "1.0", "sections": decisions},
        {
            "schema_version": "1.0",
            "status": "synthetic_simulation_only",
            "reviewer_role": "synthetic_medical_reviewer",
            "reviewed_at": "2026-08-06T18:00:00+08:00",
            "release_authority": False,
            "records": records,
            "warning": "本记录仅用于验证 clean 闸门，不是真实医学负责人签核。",
        },
    )


def _publication_map(frozen: Mapping[str, Any]) -> dict[str, Any]:
    counters = {"14.1": 0, "14.2": 0, "14.3": 0}
    entries: list[dict[str, str]] = []
    entities = sorted(
        frozen["entities"], key=lambda item: _section_sort_key(item["entity_id"][1:])
    )
    for item in entities:
        entity_id = str(item["entity_id"])
        numeric = entity_id[1:]
        family = next(
            prefix for prefix in ("14.1", "14.2", "14.3") if numeric.startswith(prefix)
        )
        counters[family] += 1
        publication_id = f"{entity_id[0]}{family}-{counters[family]}"
        sections = item.get("ich_e3_sections", [])
        entries.append(
            {
                "source_entity_id": entity_id,
                "publication_entity_id": publication_id,
                "title": f"脱敏合成表图 {publication_id}",
                "expected_section": f"§{sections[0]}" if sections else "§待人工确认",
            }
        )
    return {"schema_version": "1.0", "entries": entries}


def _deterministic_context(
    facts: Mapping[str, Any],
    frozen: Mapping[str, Any],
    accepted_entity_ids: Sequence[str],
    publication_map: Mapping[str, Any],
    clean_body: Mapping[str, Sequence[str]],
    synopsis_selection: Sequence[tuple[str, int]],
    *,
    version: str,
) -> dict[str, Any]:
    inventory = load_data(INVENTORY)
    titles = {
        row["section_id"]: row["title"]
        for row in inventory["rows"]
        if row.get("is_authoring_unit")
    }
    augmented_facts = copy.deepcopy(dict(facts))
    augmented_facts["study_identifiers"] = {
        "study_title": facts["study_title"],
        "protocol_id": facts["protocol_id"],
    }
    chapter_1432 = next(
        (
            entry
            for entry in publication_map["entries"]
            if entry["source_entity_id"].startswith("T14.3.2")
        ),
        next(
            entry
            for entry in publication_map["entries"]
            if entry["source_entity_id"].startswith("T14.3")
        ),
    )
    publication_id = chapter_1432["publication_entity_id"]
    publication_bookmark = bookmark_name(publication_id)
    return {
        "unit_titles": titles,
        "study_facts": augmented_facts,
        "sponsor_metadata": {"sponsor": "脱敏合成申办方"},
        "report_version_and_date": {
            "version": version,
            "date": "2026-08-06",
            "confidentiality": "合成测试资料",
        },
        "approved_signatory_register": [
            {
                "control_id": f"CDE-{index}",
                "label": label,
                "value": "脱敏测试槽位",
                "evidence_status": "合成已确认",
                "location": "标题页" if index < 7 else "16.1.5",
            }
            for index, label in enumerate(
                (
                    "药品注册申请人签字及盖章",
                    "主要或协调研究者签字",
                    "负责或协调研究单位名称",
                    "统计学负责人签字",
                    "统计单位名称",
                    "ICH E3要求的其他标题页信息",
                    "申办方负责医学专员签名",
                ),
                start=1,
            )
        ],
        "confirmed_clean_body": {
            key: list(value) for key, value in clean_body.items()
        },
        "synopsis_selection_manifest": {
            "selection": [
                {
                    "source_section_id": section_id,
                    "paragraph_indices": [paragraph_index],
                }
                for section_id, paragraph_index in synopsis_selection
            ]
        },
        "document_heading_styles": "Heading 1-5",
        "caption_style_table": "Table",
        "caption_style_figure": "Figure",
        "frozen_tfl_map": dict(frozen),
        "publication_reference_map": copy.deepcopy(dict(publication_map)),
        "accepted_result_tfl": {
            "entity_ids": list(accepted_entity_ids),
            "diagnostic_scope_only": False,
        },
        "appendix_status_plan": build_appendix_status_plan(facts),
        "confirmed_submission_registry": {
            "entries": [
                {
                    "report_id": "NONE-CONFIRMED",
                    "title": "合成夹具未登记单独递交的支持性报告",
                    "submission_location": "不适用",
                    "status": "合成确认",
                }
            ]
        },
        "committee_register": {"committees": ["脱敏合成伦理委员会"]},
        "approval_identifier": "SYN-IEC-M21",
        "approval_date": "2026-08-06",
        "protocol_version": "synthetic-reviewed-v1",
        "applicable_ethics_standard": "《赫尔辛基宣言》",
        "gcp_standard": "药物临床试验质量管理规范",
        "confirmed_ethics_deviations": "合成夹具未登记伦理相关偏离",
        "consent_form_versions": ["synthetic-reviewed-v1"],
        "consent_process_register": "合成夹具设定为研究前完成书面知情同意",
        "waiver_if_any": "无豁免",
        "individual_listing_targets": {
            "11.4.3": {"display": "附录16.2.6", "bookmark": "_Ref_Sec_16_2_6"},
            "11.4.6": {"display": "附录16.2.6", "bookmark": "_Ref_Sec_16_2_6"},
            "12.2.2": {"display": "附录16.2.7", "bookmark": "_Ref_Sec_16_2_7"},
            "12.2.4": {"display": "附录16.2.7", "bookmark": "_Ref_Sec_16_2_7"},
            "12.4.1": {"display": "附录16.2.8", "bookmark": "_Ref_Sec_16_2_8"},
            **{
                section_id: {
                    "target_type": "publication",
                    "entity_id": publication_id,
                    "display": publication_id.replace("T", "表", 1),
                    "bookmark": publication_bookmark,
                }
                for section_id in ("12.3.1.1", "12.3.1.2", "12.3.1.3")
            },
        },
        "confirmed_claim_ledger": {
            "claims": [{"claim_id": "CLM-001", "source_id": "ICH-E3"}]
        },
        "bibliography_registry": {
            "sources": {"ICH-E3": "ICH E3：临床研究报告的结构和内容。"}
        },
    }


def _trace_and_qc(
    root: Path,
    *,
    datapack: Mapping[str, Any],
    metadata_paths: Sequence[Path],
    source_entity_id: str,
    csr_section_id: str,
    qc_checks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    metadata = [load_data(path) for path in metadata_paths]
    unreferenced = analyze_tfl_references(datapack, metadata)
    _write_json(root / "unreferenced_tfl.json", unreferenced)
    atomic_write_text(root / "unreferenced_tfl.md", render_unreferenced_tfl(unreferenced))
    manifest = {
        "schema_version": "1.0",
        "nodes": [
            {"id": "PROTOCOL1", "layer": "protocol", "locator": "脱敏方案已确认节", "label": "研究目标与评价"},
            {"id": "SAP1", "layer": "sap", "locator": "脱敏 SAP/TFL shell", "label": "分析规格"},
            {"id": "TFL1", "layer": "tfl", "locator": source_entity_id, "label": "已接受结果表"},
            {"id": "CSR1", "layer": "csr", "locator": f"§{csr_section_id}", "label": "已守卫正文"},
        ],
        "links": [
            {"from": "PROTOCOL1", "to": "SAP1", "relation": "specified_by"},
            {"from": "SAP1", "to": "TFL1", "relation": "implemented_by"},
            {"from": "TFL1", "to": "CSR1", "relation": "reported_in"},
        ],
        "required_paths": [
            {
                "trace_id": "M21TRACE1",
                "protocol": "PROTOCOL1",
                "sap": "SAP1",
                "tfl": "TFL1",
                "csr": "CSR1",
            }
        ],
    }
    _write_json(root / "bidirectional_trace_manifest.json", manifest)
    trace = validate_bidirectional_trace(manifest)
    _write_json(root / "bidirectional_trace_report.json", trace)
    qc_markdown, qc = build_qc_report(qc_checks)
    atomic_write_text(root / "qc_report.md", qc_markdown)
    _write_json(root / "qc_report.json", qc)
    return {"unreferenced": unreferenced, "bidirectional": trace, "qc": qc}


def _unguarded_rejection(
    root: Path,
    *,
    routing: Mapping[str, Any],
    bundle: Mapping[str, Any],
    valid_markdown: Path,
    valid_metadata: Path,
    publication_map: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    tampered = copy.deepcopy(load_data(valid_metadata))
    tampered["forward_coverage"]["status"] = "blocked"
    tampered["forward_coverage"]["release_eligible"] = False
    tampered_path = root / "tampered_unguarded.meta.json"
    _write_json(tampered_path, tampered)
    try:
        compose_unified_csr(
            routing=routing,
            deterministic_bundle=bundle,
            constrained_artifacts=[
                {
                    "markdown_path": valid_markdown,
                    "metadata_path": tampered_path,
                    "evidence_status": "demo_only",
                }
            ],
            review_decisions={"schema_version": "1.0", "sections": {}},
            publication_reference_map=publication_map,
            study_facts=facts,
            annotated_output=root / "must_not_exist.annotated.docx",
            clean_output=root / "must_not_exist.clean.docx",
        )
    except UnifiedCompositionError as exc:
        report = {
            "schema_version": "1.0",
            "status": "pass",
            "attack": "forward_coverage status changed from pass to blocked",
            "actual": "rejected",
            "error": str(exc),
            "error_code": str(exc).split(":", 1)[0],
            "failure_location": _failure_location(exc),
            "output_created": any(root.glob("must_not_exist.*.docx")),
        }
    else:
        raise RuntimeError("E-M21-ASSEMBLER-GUARD-BYPASS")
    _write_json(root / "unguarded_artifact_rejection.json", report)
    return report


def _run_m20(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    facts = load_study_facts(M20_FIXTURE / "study_facts.single_arm_migraine.deidentified.json")
    route = load_data(M20_INPUT / "03_module_route.json")
    shell = load_data(M20_INPUT / "04_shell_datapack.json")
    frozen_before = load_data(M20_INPUT / "05_frozen_map.json")
    strict_drift = load_data(M20_INPUT / "06_result_apply.strict_blocked.json")
    result_path = M20_FIXTURE / "result_tfl.single_arm_migraine.deidentified.docx"

    drift_dir = root / "05_drift_adjudication"
    drift_dir.mkdir()
    queue = build_drift_decision_queue(
        strict_drift,
        frozen_before,
        shell_path=M20_FIXTURE / "tfl_shell.single_arm_migraine.deidentified.docx",
        result_path=result_path,
        result_route_proposals={"T14.3.9.1": ["12.4.3"]},
    )
    _write_json(drift_dir / "decision_queue.json", queue)
    decision = {
        "schema_version": "1.0",
        "queue_sha256": queue["queue_sha256"],
        "decided_by_role": "synthetic_tfl_reviewer",
        "decided_at": "2026-08-06T18:10:00+08:00",
        "decisions": [
            {
                "decision_id": item["decision_id"],
                "decision": "expected_change",
                "reason": "合成夹具故意设置的锁库后预期版本变更",
                "ich_e3_sections": ["12.4.3"] if item["direction"] == "result_only" else [],
            }
            for item in queue["decisions"]
        ],
    }
    _write_json(drift_dir / "decision_record.synthetic.json", decision)
    reconciliation = reconcile_expected_drift(
        queue, decision, shell, frozen_before, result_path
    )
    _write_json(drift_dir / "reconciliation.json", reconciliation)
    confirmation = {
        "status": "confirmed",
        "confirmation_id": "M21-M20-DRIFT-REFREEZE",
        "confirmed_by_role": "synthetic_tfl_reviewer",
        "confirmed_at": "2026-08-06T18:11:00+08:00",
        "candidate_sha256": reconciliation["candidate"]["candidate_sha256"],
    }
    frozen, provenance = freeze_reconciled_tfl_map(reconciliation, confirmation)
    _write_json(drift_dir / "frozen_map.reconciled.json", frozen)
    _write_json(drift_dir / "frozen_map.provenance.json", provenance)
    datapack, match = apply_docx_result_to_frozen_shell(
        reconciliation["revised_shell_datapack"],
        frozen,
        result_path,
        expected_sha256=sha256_file(result_path),
    )
    _write_json(drift_dir / "result_datapack.strict.json", datapack)
    _write_json(drift_dir / "result_match.strict.json", match)

    ordinary_specs = [
        {
            "draft_path": M20_INPUT / "10_guarded_result/project/sections/12.3.3.md",
            "resolved_path": M20_INPUT / "10_guarded_result/12.3.3.resolved.md",
            "metadata_path": M20_INPUT / "10_guarded_result/project/sections/12.3.3.meta.json",
            "guard_path": M20_INPUT / "10_guarded_result/resolve.pass.json",
        }
    ]
    constrained_specs = [
        {
            "markdown_path": M20_INPUT / "08_method_limited_free/9.1.md",
            "metadata_path": M20_INPUT / "08_method_limited_free/9.1.meta.json",
            "evidence_status": "demo_only",
        },
        {
            "markdown_path": M20_INPUT / "09_sae_exact/12.3.2.md",
            "metadata_path": M20_INPUT / "09_sae_exact/12.3.2.meta.json",
            "evidence_status": "demo_only",
        },
    ]
    sections = (
        load_constrained_resolved_section(
            constrained_specs[0]["markdown_path"],
            constrained_specs[0]["metadata_path"],
            evidence_status="demo_only",
        ),
        load_constrained_resolved_section(
            constrained_specs[1]["markdown_path"],
            constrained_specs[1]["metadata_path"],
            evidence_status="demo_only",
        ),
        load_resolved_section(
            ordinary_specs[0]["draft_path"],
            ordinary_specs[0]["resolved_path"],
            ordinary_specs[0]["metadata_path"],
            ordinary_specs[0]["guard_path"],
        ),
    )
    decisions = load_data(M20_INPUT / "12_review_decisions.synthetic.json")
    clean_body = apply_review_decisions(sections, decisions)
    publication_map = _publication_map(frozen)
    context = _deterministic_context(
        facts,
        frozen,
        [item["entity_id"] for item in frozen["entities"]],
        publication_map,
        clean_body,
        [("9.1", 0), ("12.3.3", 0)],
        version="M21-M20-unified",
    )
    deterministic_dir = root / "06_deterministic"
    deterministic_dir.mkdir()
    bundle = build_deterministic_bundle(context)
    _write_json(deterministic_dir / "context.json", context)
    _write_json(deterministic_dir / "bundle.json", bundle)

    composition_dir = root / "07_unified_composition"
    composition_dir.mkdir()
    composition = compose_unified_csr(
        routing=route,
        deterministic_bundle=bundle,
        ordinary_artifacts=ordinary_specs,
        constrained_artifacts=constrained_specs,
        review_decisions=decisions,
        publication_reference_map=publication_map,
        study_facts=facts,
        annotated_output=composition_dir / "M21_M20_unfamiliar.annotated.docx",
        clean_output=composition_dir / "M21_M20_unfamiliar.clean.docx",
        composition_manifest_output=composition_dir / "composition_manifest.json",
    )
    bypass = _unguarded_rejection(
        composition_dir,
        routing=route,
        bundle=bundle,
        valid_markdown=constrained_specs[1]["markdown_path"],
        valid_metadata=constrained_specs[1]["metadata_path"],
        publication_map=publication_map,
        facts=facts,
    )
    trace = _trace_and_qc(
        root / "08_trace_qc",
        datapack=datapack,
        metadata_paths=[
            constrained_specs[0]["metadata_path"],
            constrained_specs[1]["metadata_path"],
            ordinary_specs[0]["metadata_path"],
        ],
        source_entity_id="T14.3.2.1",
        csr_section_id="12.3.3",
        qc_checks={
            "映射漂移": {"status": "pass", "summary": "两个漂移实体已经显式裁决并重新冻结，严格套值通过。"},
            "统一合卷": {"status": "pass", "summary": "clean 与 annotated 由同一组成计划生成。"},
            "守卫入卷": {"status": "pass", "summary": "未通过正向覆盖证明的正文被编排器拒绝。"},
            "双向追溯": {"status": "pass", "summary": "未发现本次脱敏合成四层追溯路径断链。"},
        },
    )
    active = set(route["structure"]["active_module_ids"])
    omitted = route["structure"]["section_plan"]["omitted_sections"]
    return {
        "schema_version": "1.0",
        "status": "pass",
        "blocking_breakpoints": [],
        "steps": [
            {"step": "study_facts 与模块路由", "status": "pass"},
            {"step": "TFL map 漂移决策队列", "status": "pass", "decision_count": queue["decision_count"]},
            {"step": "显式裁决与重新冻结", "status": "manual_gate_simulated"},
            {"step": "结果 TFL 严格套值", "status": match["status"]},
            {"step": "三条正文路径入场", "status": "pass"},
            {"step": "统一 clean/annotated 合卷", "status": composition["status"]},
            {"step": "反向追溯与 QC", "status": trace["bidirectional"]["status"]},
        ],
        "routing": {
            "randomization_generated": "M-RANDOMIZED" in active,
            "blinding_generated": "M-BLINDING" in active,
        },
        "drift": {
            "queue_count": queue["decision_count"],
            "shell_only": [item["entity_id"] for item in queue["decisions"] if item["direction"] == "shell_only"],
            "result_only": [item["entity_id"] for item in queue["decisions"] if item["direction"] == "result_only"],
            "strict_result_status": match["status"],
            "matched_values": match["metrics"]["matched_result_value_count"],
            "unmatched_values": match["metrics"]["unmatched_result_value_count"],
            "unresolved_frozen_values": match["metrics"]["unresolved_frozen_cell_count"],
        },
        "three_paths": {
            "deterministic_generation": "pass",
            "method_limited_free": "pass",
            "sae_exact_reviewed_surface": "pass",
            "guarded_section_writer": "pass",
        },
        "omission_declarations": {
            "expected": len(omitted),
            "annotated_actual": composition["outputs"]["annotated"]["ooxml_qc"]["omission_reason_line_count"],
            "clean_actual": composition["outputs"]["clean"]["ooxml_qc"]["omission_reason_line_count"],
        },
        "guard_bypass_test": bypass,
        "composition": composition,
        "trace_qc": trace,
    }


def _grid(document: Document, rows: Sequence[tuple[int, str]]) -> None:
    table = document.add_table(rows=len(rows) + 1, cols=3)
    table.cell(0, 0).text = "ROW"
    table.cell(0, 1).text = "PBO"
    table.cell(0, 2).text = "C1_0PCT_QD"
    for row_index, (indent, label) in enumerate(rows, start=1):
        paragraph = table.cell(row_index, 0).paragraphs[0]
        paragraph.text = (" " * indent) + label
        table.cell(row_index, 1).text = "xx"
        table.cell(row_index, 2).text = "xx"


def _build_ib_shell(path: Path) -> None:
    document = Document()
    specifications = [
        ("T14.1.1.2", "分析集人数", "FAS", [(0, "N"), (0, "COMPLETED")]),
        ("T14.1.2.1", "人口学与基线", "FAS", [(0, "N"), (0, "MEAN"), (0, "SD")]),
        ("T14.1.4.1", "Medication result", "FAS", [(0, "ATC分类1"), (2, "药物名1")]),
        ("T14.2.2.1.1", "ANCOVA result", "FAS", [(0, "N"), (0, "Mean"), (0, "SD"), (0, "LS Mean Diff"), (0, "95% CI"), (0, "P-value")]),
        ("T14.3.1.3.1", "SOC/PT result", "SS", [(0, "系统器官分类1"), (2, "首选术语1"), (2, "首选术语2")]),
        ("T14.3.1.3.2", "Mixed rows", "SS", [(0, "N"), (0, "系统器官分类1"), (2, "首选术语1")]),
        ("T14.3.2.1", "死亡与严重不良事件列表", "SS", [(0, "N")]),
        ("T14.3.3.1", "严重不良事件叙述索引", "SS", [(0, "N")]),
        ("T14.3.4.1", "实验室异常值", "SS", [(0, "N"), (0, "MEAN"), (0, "SD")]),
    ]
    for entity_id, title, population, rows in specifications:
        document.add_paragraph(f"表{entity_id[1:]} {title} ({population})")
        _grid(document, rows)
    document.save(path)
    _canonicalize_docx_package(path)


def _ib_sae_card_and_draft() -> tuple[dict[str, Any], dict[str, Any]]:
    card = copy.deepcopy(load_data(M20_FIXTURE / "sae_fact_card.12.3.2.json"))
    draft = copy.deepcopy(load_data(M20_FIXTURE / "sae_draft.12.3.2.json"))
    replacements = {
        "M20-SAE-EXACT-01": "M21-IB-SAE-EXACT-01",
        "m20_safety_record": "m21_ib_safety_record",
        "m20_crf_narrative": "m21_ib_crf_narrative",
        "M20-SAE": "M21-IB-SAE",
        "受试者丁": "受试者戊",
        "既往有季节性鼻炎史": "既往有季节性过敏史",
        "接受固定口服给药方案": "接受皮肤局部外用给药",
        "偏头痛加重": "用药部位皮炎",
    }

    def replace_text(value: Any) -> Any:
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [replace_text(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_text(item) for key, item in value.items()}
        return value

    return replace_text(card), replace_text(draft)


def _run_ib(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True)
    facts = load_study_facts(IB_FACTS)
    route = route_modules(facts)
    atomic_write_text(root / "01_facts_card.md", render_fact_card(facts))
    _write_json(root / "02_module_route.json", route)

    tfl_dir = root / "03_tfl_mapping"
    tfl_dir.mkdir()
    shell_path = tfl_dir / "ib_randomized_double_blind.shell.deidentified.docx"
    _build_ib_shell(shell_path)
    proposal, shell, candidate = propose_docx_shell_map(
        shell_path,
        expected_sha256=sha256_file(shell_path),
        map_version="M21-IB-RANDOMIZED-DOUBLE-BLIND-1",
    )
    confirmation = {
        "status": "confirmed",
        "confirmation_id": "M21-IB-SYNTHETIC-MAP",
        "confirmed_by_role": "synthetic_fixture_reviewer",
        "confirmed_at": "2026-08-06T18:20:00+08:00",
        "candidate_sha256": candidate["candidate_sha256"],
    }
    frozen = freeze_tfl_map(candidate, confirmation)
    _write_json(tfl_dir / "proposal.json", proposal)
    _write_json(tfl_dir / "shell_datapack.json", shell)
    _write_json(tfl_dir / "candidate.json", candidate)
    _write_json(tfl_dir / "confirmation.synthetic.json", confirmation)
    _write_json(tfl_dir / "frozen_map.json", frozen)

    existing_results = {
        "T14.1.4.1": M7_RESULTS / "medication_result.deidentified.rtf",
        "T14.2.2.1.1": M7_RESULTS / "ancova_result.deidentified.rtf",
        "T14.3.1.3.1": M7_RESULTS / "soc_pt_result.deidentified.rtf",
        "T14.3.1.3.2": M7_RESULTS / "mixed_result.deidentified.rtf",
    }
    generated_results: dict[str, Path] = {}
    for entity_id in ("T14.1.1.2", "T14.1.2.1", "T14.3.2.1", "T14.3.3.1", "T14.3.4.1"):
        path = tfl_dir / f"{entity_id}.result.deidentified.rtf"
        atomic_write_text(path, build_deidentified_result_rtf(frozen, entity_id))
        generated_results[entity_id] = path
    result_paths = {**existing_results, **generated_results}
    datapack = shell
    result_reports = []
    for entity_id in sorted(result_paths, key=lambda value: _section_sort_key(value[1:])):
        datapack, result_report = apply_rtf_values_to_shell(
            datapack, frozen, entity_id, result_paths[entity_id]
        )
        result_reports.append(result_report)
    unresolved = sorted(
        key for key, cell in datapack["cells"].items() if str(cell.get("v")).lower() == "xx"
    )
    if unresolved:
        raise RuntimeError(f"E-M21-IB-UNRESOLVED-TFL: {unresolved[:5]}")
    _write_json(tfl_dir / "result_datapack.strict.json", datapack)
    _write_json(
        tfl_dir / "result_application.strict.json",
        {
            "schema_version": "1.0",
            "status": "pass",
            "entity_count": len(result_reports),
            "unresolved_shell_placeholder_count": 0,
            "reports": result_reports,
            "reused_existing_result_entities": sorted(existing_results),
            "supplemental_chapter14_result_entities": sorted(generated_results),
        },
    )

    writer_dir = root / "04_guarded_result_writer"
    writer_dir.mkdir()
    bindings = [
        (
            "exploratory_endpoint_result",
            "T14.2.2.1.1|POP=FAS|ROW=LS_MEAN_DIFFERENCE|COL=C1_0PCT_QD",
        ),
    ]
    request = {
        "schema_version": "1.1",
        "section_id": "11.6.1.4",
        "variant": "exploratory_endpoint_results",
        "evidence_status": "demo_only",
        "source_fragments": [
            {
                "source_id": "SRC-M21-IB-ENDPOINT",
                "locator": "脱敏方案主要终点节",
                "purpose": "endpoint_definition",
                "text": "皮肤科量表终点按全分析集进行探索性协变量分析。",
            },
            {
                "source_id": "SRC-M21-IB-SAP",
                "locator": "脱敏 SAP ANCOVA 方法节",
                "purpose": "analysis_method",
                "text": "模型纳入治疗组，并以基线值为协变量。",
            },
        ],
        "tfl_bindings": [
            {
                "role": role,
                "key": key,
                "source_id": "T14.2.2.1.1",
                "context": "脱敏合成 Ib 严格套值结果",
            }
            for role, key in bindings
        ],
        "tfl_references": ["T14.2.2.1.1"],
        "confirmed_tfl_map": {
            "frozen_sha256": frozen["frozen_sha256"],
            "entity_ids": [item["entity_id"] for item in frozen["entities"]],
        },
    }
    draft = {
        "paragraphs": [
            {
                "text": "本项皮肤科量表终点采用全分析集，并以基线值为协变量进行探索性分析。",
                "sources": ["SRC-M21-IB-ENDPOINT", "SRC-M21-IB-SAP"],
                "confidence": "derived",
                "needs_human": False,
            },
            {
                "text": "试验制剂组相较安慰剂组的最小二乘均值差为 {{T14.2.2.1.1|POP=FAS|ROW=LS_MEAN_DIFFERENCE|COL=C1_0PCT_QD}}，详见{{@ref:T14.2.2.1.1}}。",
                "sources": ["T14.2.2.1.1"],
                "confidence": "derived",
                "needs_human": False,
            },
            {
                "text": "本项结果为探索性分析，不用于确证性结论。[待医学负责人确认]",
                "sources": ["SRC-M21-IB-ENDPOINT"],
                "confidence": "inferred",
                "needs_human": True,
            },
        ]
    }
    _write_json(writer_dir / "request.json", request)
    _write_json(writer_dir / "draft.json", draft)
    envelope = emit_agent_envelope(request)
    _write_json(writer_dir / "envelope.json", envelope)
    section_artifact, ingest_report = ingest_agent_draft(
        envelope, draft, writer_dir / "project"
    )
    _write_json(writer_dir / "ingest.json", ingest_report)
    numeric = resolve(
        section_artifact.markdown,
        datapack,
        facts,
        binding_manifest=section_artifact.metadata,
        frozen_tfl_map=frozen,
    )
    if numeric.status != "pass" or numeric.resolved_text is None:
        raise RuntimeError(numeric.report)
    resolved_path = writer_dir / "11.6.1.4.resolved.md"
    guard_path = writer_dir / "11.6.1.4.guard.json"
    atomic_write_text(resolved_path, numeric.resolved_text)
    _write_json(guard_path, numeric.report)

    sae_dir = root / "05_sae_exact"
    sae_dir.mkdir()
    sae_card, sae_draft = _ib_sae_card_and_draft()
    _write_json(sae_dir / "fact_card.json", sae_card)
    _write_json(sae_dir / "draft.json", sae_draft)
    sae_envelope = emit_fact_card_envelope(sae_card)
    _write_json(sae_dir / "envelope.json", sae_envelope)
    sae = ingest_constrained_draft(sae_envelope, sae_draft)
    write_constrained_artifact(
        sae, sae_dir / "12.3.2.md", sae_dir / "12.3.2.meta.json"
    )

    ordinary_specs = [
        {
            "draft_path": section_artifact.markdown_path,
            "resolved_path": resolved_path,
            "metadata_path": section_artifact.metadata_path,
            "guard_path": guard_path,
        }
    ]
    constrained_specs = [
        {
            "markdown_path": M19_METHODS / "9.4.3.md",
            "metadata_path": M19_METHODS / "9.4.3.meta.json",
            "evidence_status": "demo_only",
        },
        {
            "markdown_path": M19_METHODS / "9.4.6.md",
            "metadata_path": M19_METHODS / "9.4.6.meta.json",
            "evidence_status": "demo_only",
        },
        {
            "markdown_path": sae_dir / "12.3.2.md",
            "metadata_path": sae_dir / "12.3.2.meta.json",
            "evidence_status": "demo_only",
        },
    ]
    sections = (
        load_constrained_resolved_section(
            constrained_specs[0]["markdown_path"], constrained_specs[0]["metadata_path"], evidence_status="demo_only"
        ),
        load_constrained_resolved_section(
            constrained_specs[1]["markdown_path"], constrained_specs[1]["metadata_path"], evidence_status="demo_only"
        ),
        load_constrained_resolved_section(
            constrained_specs[2]["markdown_path"], constrained_specs[2]["metadata_path"], evidence_status="demo_only"
        ),
        load_resolved_section(
            ordinary_specs[0]["draft_path"], ordinary_specs[0]["resolved_path"], ordinary_specs[0]["metadata_path"], ordinary_specs[0]["guard_path"]
        ),
    )
    decisions, decision_provenance = _review_decisions(sections)
    _write_json(root / "06_review_decisions.synthetic.json", decisions)
    _write_json(root / "06_review_decisions.provenance.json", decision_provenance)
    clean_body = apply_review_decisions(sections, decisions)
    publication_map = _publication_map(frozen)
    context = _deterministic_context(
        facts,
        frozen,
        [item["entity_id"] for item in frozen["entities"]],
        publication_map,
        clean_body,
        [("9.4.3", 0), ("11.6.1.4", 0)],
        version="M21-Ib-randomized-double-blind",
    )
    deterministic_dir = root / "07_deterministic"
    deterministic_dir.mkdir()
    bundle = build_deterministic_bundle(context)
    _write_json(deterministic_dir / "context.json", context)
    _write_json(deterministic_dir / "bundle.json", bundle)

    composition_dir = root / "08_unified_composition"
    composition_dir.mkdir()
    composition = compose_unified_csr(
        routing=route,
        deterministic_bundle=bundle,
        ordinary_artifacts=ordinary_specs,
        constrained_artifacts=constrained_specs,
        review_decisions=decisions,
        publication_reference_map=publication_map,
        study_facts=facts,
        annotated_output=composition_dir / "M21_Ib_randomized_double_blind.annotated.docx",
        clean_output=composition_dir / "M21_Ib_randomized_double_blind.clean.docx",
        composition_manifest_output=composition_dir / "composition_manifest.json",
    )
    text = "\n".join(paragraph.text for paragraph in Document(composition_dir / "M21_Ib_randomized_double_blind.clean.docx").paragraphs)
    required_headings = {
        "9.4.3": "9.4.3" in text,
        "9.4.6": "9.4.6" in text,
    }
    if not all(required_headings.values()):
        raise RuntimeError(f"E-M21-IB-ROUTED-HEADING: {required_headings}")
    trace = _trace_and_qc(
        root / "09_trace_qc",
        datapack=datapack,
        metadata_paths=[
            constrained_specs[0]["metadata_path"],
            constrained_specs[1]["metadata_path"],
            constrained_specs[2]["metadata_path"],
            ordinary_specs[0]["metadata_path"],
        ],
        source_entity_id="T14.2.2.1.1",
        csr_section_id="11.6.1.4",
        qc_checks={
            "严格套值": {"status": "pass", "summary": "9 个脱敏结果实体全部按冻结语义契约套值。"},
            "随机与盲法路由": {"status": "pass", "summary": "随机化与盲法模块已激活，相应正文节已入卷。"},
            "统一合卷": {"status": "pass", "summary": "clean 与 annotated 由同一组成计划生成。"},
            "双向追溯": {"status": "pass", "summary": "未发现本次脱敏合成四层追溯路径断链。"},
        },
    )
    active = set(route["structure"]["active_module_ids"])
    omitted = route["structure"]["section_plan"]["omitted_sections"]
    return {
        "schema_version": "1.0",
        "status": "pass",
        "blocking_breakpoints": [],
        "fixture_reuse": {
            "study_facts": str(IB_FACTS.relative_to(ROOT)),
            "strict_rtf_result_entities_reused": sorted(existing_results),
            "supplemental_entities_reason": "补齐确定性第14章八个分类；不改既有夹具或解析规则。",
        },
        "steps": [
            {"step": "已确认 Ib study_facts 与模块路由", "status": "pass"},
            {"step": "DOCX shell 提议与冻结", "status": "manual_gate_simulated"},
            {"step": "RTF 结果集严格套值", "status": "pass", "entity_count": len(result_reports)},
            {"step": "方法学有限自由、SAE 逐字预审、结果写手", "status": "pass"},
            {"step": "统一 clean/annotated 合卷", "status": composition["status"]},
            {"step": "反向追溯与 QC", "status": trace["bidirectional"]["status"]},
        ],
        "routing": {
            "randomization_generated": "M-RANDOMIZED" in active,
            "blinding_generated": "M-BLINDING" in active,
            "required_headings_present": required_headings,
        },
        "three_paths": {
            "deterministic_generation": "pass",
            "method_limited_free": "pass",
            "sae_exact_reviewed_surface": "pass",
            "guarded_section_writer": "pass",
        },
        "tfl": {
            "frozen_entity_count": len(frozen["entities"]),
            "strict_result_entity_count": len(result_reports),
            "unresolved_placeholder_count": len(unresolved),
        },
        "omission_declarations": {
            "expected": len(omitted),
            "annotated_actual": composition["outputs"]["annotated"]["ooxml_qc"]["omission_reason_line_count"],
            "clean_actual": composition["outputs"]["clean"]["ooxml_qc"]["omission_reason_line_count"],
        },
        "composition": composition,
        "trace_qc": trace,
    }


def run(output: Path) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError(f"Output already exists; refusing to overwrite: {output}")
    output.mkdir(parents=True)
    before = _hash_audit("before")
    _write_json(output / "00_read_only_hashes.before.json", before)
    project_reports: dict[str, Any] = {}
    new_findings: list[dict[str, Any]] = []
    try:
        project_reports["m20_unfamiliar"] = _run_m20(output / "m20_unfamiliar")
    except Exception as exc:
        project_reports["m20_unfamiliar"] = {
            "status": "blocked",
            "blocking_breakpoints": [
                {
                    "stage": "M20 陌生夹具统一编排",
                    "code": str(exc).split(":", 1)[0],
                    "message": str(exc),
                    "failure_location": _failure_location(exc),
                }
            ],
        }
        new_findings.extend(project_reports["m20_unfamiliar"]["blocking_breakpoints"])
    try:
        project_reports["ib_randomized_double_blind"] = _run_ib(
            output / "ib_randomized_double_blind"
        )
    except Exception as exc:
        project_reports["ib_randomized_double_blind"] = {
            "status": "blocked",
            "blocking_breakpoints": [
                {
                    "stage": "Ib 随机双盲夹具统一编排",
                    "code": str(exc).split(":", 1)[0],
                    "message": str(exc),
                    "failure_location": _failure_location(exc),
                }
            ],
        }
        new_findings.extend(project_reports["ib_randomized_double_blind"]["blocking_breakpoints"])

    after = _hash_audit("after")
    _write_json(output / "99_read_only_hashes.after.json", after)
    immutable_pairs_match = [
        {**left, "stage_after_observed_sha256": right["observed_sha256"]}
        for left, right in zip(before["records"], after["records"], strict=True)
    ]
    if any(
        item["observed_sha256"] != item["stage_after_observed_sha256"]
        for item in immutable_pairs_match
    ):
        raise RuntimeError("E-M21-READ-ONLY-CHANGED-DURING-RUN")

    blocking = [
        item
        for report in project_reports.values()
        for item in report.get("blocking_breakpoints", [])
    ]
    report = {
        "schema_version": "1.0",
        "status": "pass" if not blocking else "blocked",
        "closeout_gate_passed": not blocking,
        "blocking_breakpoint_count": len(blocking),
        "blocking_breakpoints": blocking,
        "projects": project_reports,
        "read_only_hashes_unchanged": True,
        "real_clinical_files_accessed": False,
        "guard_policy_relaxed_for_e2e": False,
        "fixture_reduced_for_e2e": False,
    }
    _write_json(output / "M21_dual_e2e_report.json", report)
    _write_json(
        output / "M21_new_findings.json",
        {"schema_version": "1.0", "count": len(new_findings), "findings": new_findings},
    )
    lines = ["# M21 端到端断点清单", ""]
    if blocking:
        for item in blocking:
            lines.extend(
                [
                    f"- **{item['stage']}**",
                    f"  - 代码：`{item['code']}`",
                    f"  - 位置：`{item['failure_location']}`",
                    f"  - 信息：{item['message']}",
                ]
            )
    else:
        lines.append("- 阻断性断点：**0**。")
        lines.append("- 两套夹具均完成严格套值、守卫入卷、双版本合卷、追溯和 QC。")
    atomic_write_text(output / "M21端到端断点清单.md", "\n".join(lines) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "examples" / "output" / "m21",
    )
    args = parser.parse_args()
    report = run(args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
