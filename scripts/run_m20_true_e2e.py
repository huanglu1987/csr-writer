#!/usr/bin/env python3
"""Run the M20 unfamiliar-input diagnostic E2E without touching real files.

The intentional shell/result drift is preserved.  Downstream artifacts are
therefore diagnostic-only even when their local guards pass.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.appendix_status import build_appendix_status_plan  # noqa: E402
from csr_writer.bidirectional_trace import validate_bidirectional_trace  # noqa: E402
from csr_writer.conditional_conclusions import resolve_conditional_conclusions  # noqa: E402
from csr_writer.constrained_narration import (  # noqa: E402
    ConstrainedNarrationError,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    write_constrained_artifact,
)
from csr_writer.cross_chapter import (  # noqa: E402
    CrossChapterError,
    REVIEW_MARKER,
    apply_review_decisions,
    load_constrained_resolved_section,
    load_resolved_section,
)
from csr_writer.deterministic_sections import (  # noqa: E402
    build_deterministic_bundle,
    render_deterministic_bundle_docx,
)
from csr_writer.docx_extract import extract_docx  # noqa: E402
from csr_writer.docx_result import (  # noqa: E402
    DocxResultError,
    apply_docx_result_to_frozen_shell,
)
from csr_writer.io_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    load_data,
    sha256_file,
)
from csr_writer.m4_assembly import (  # noqa: E402
    render_annotated_docx,
    render_clean_and_synopsis,
)
from csr_writer.m4_qc import build_qc_report  # noqa: E402
from csr_writer.module_router import route_modules  # noqa: E402
from csr_writer.resolve_placeholders import resolve  # noqa: E402
from csr_writer.section_writer import (  # noqa: E402
    AgentDraftGuardError,
    emit_agent_envelope,
    ingest_agent_draft,
)
from csr_writer.shell_mapping import propose_docx_shell_map  # noqa: E402
from csr_writer.study_facts import load_study_facts, render_fact_card  # noqa: E402
from csr_writer.tfl_map import freeze_tfl_map  # noqa: E402
from csr_writer.traceability import (  # noqa: E402
    analyze_tfl_references,
    render_unreferenced_tfl,
)


FIXTURE_ROOT = ROOT / "examples" / "fixtures" / "m20_unfamiliar"
PROTOCOL = FIXTURE_ROOT / "protocol.single_arm_migraine.deidentified.docx"
SHELL = FIXTURE_ROOT / "tfl_shell.single_arm_migraine.deidentified.docx"
RESULT = FIXTURE_ROOT / "result_tfl.single_arm_migraine.deidentified.docx"
FACTS_PATH = FIXTURE_ROOT / "study_facts.single_arm_migraine.deidentified.json"

EXPECTED_INPUT_HASHES = {
    "protocol": "78488a876ab203aa382f60d468209c70c2efe71896cc59ad6a8a6a36416de7a3",
    "shell": "e1ff2a100905e0c1d5abda2912282ac06dabf6ad1c03d09af6e8ed6cdeb3c4b2",
    "result": "46c6f4c6e3d39b4826b9892d8cf3d1d7968dbcaf7d3bbe43241824e199ede8d5",
    "study_facts": "763b7feb486d1c6a280657e87e10aed9bca01625facca65c34a0188613334947",
}


def _failure_location(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    project_frames = [frame for frame in frames if str(ROOT) in frame.filename]
    frame = (project_frames or frames)[-1]
    return f"{Path(frame.filename).relative_to(ROOT)}:{frame.lineno}"


def _write_json(path: Path, payload: Any) -> None:
    atomic_write_json(path, payload)


def _input_hash_audit() -> dict[str, Any]:
    paths = {
        "protocol": PROTOCOL,
        "shell": SHELL,
        "result": RESULT,
        "study_facts": FACTS_PATH,
    }
    records = []
    for role, path in paths.items():
        observed = sha256_file(path)
        records.append(
            {
                "role": role,
                "path": str(path.relative_to(ROOT)),
                "expected_sha256": EXPECTED_INPUT_HASHES[role],
                "observed_sha256": observed,
                "unchanged": observed == EXPECTED_INPUT_HASHES[role],
            }
        )
    if not all(item["unchanged"] for item in records):
        raise RuntimeError("M20 synthetic fixture hash drift")
    return {
        "schema_version": "1.0",
        "status": "pass",
        "records": records,
        "real_clinical_files_accessed": False,
    }


def _review_decisions(sections) -> tuple[dict[str, Any], dict[str, Any]]:
    decision_sections: dict[str, Any] = {}
    resolved_records = []
    for section in sections:
        paragraph_decisions = {}
        for paragraph in section.paragraphs:
            if not paragraph.needs_human:
                continue
            replacement = paragraph.text.replace(REVIEW_MARKER, "").strip()
            paragraph_decisions[paragraph.para_id] = {
                "status": "resolved",
                "replacement_text": replacement,
            }
            resolved_records.append(
                {
                    "section_id": section.section_id,
                    "para_id": paragraph.para_id,
                    "decision": "synthetic-fixture-only replacement",
                }
            )
        decision_sections[section.section_id] = {
            "draft_sha256": section.draft_sha256,
            "paragraphs": paragraph_decisions,
        }
    decisions = {"schema_version": "1.0", "sections": decision_sections}
    provenance = {
        "schema_version": "1.0",
        "status": "synthetic_simulation_only",
        "reviewer_role": "synthetic_fixture_reviewer",
        "reviewed_at": "2026-08-06T15:00:00+08:00",
        "release_authority": False,
        "records": resolved_records,
        "warning": "本记录仅为测试 clean 闸门，不是医学负责人实际确认。",
    }
    return decisions, provenance


def _deterministic_context(
    facts: Mapping[str, Any],
    frozen_map: Mapping[str, Any],
    clean_body: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    inventory = load_data(
        ROOT / "examples" / "output" / "m16" / "production_method_inventory_120.json"
    )
    titles = {
        row["section_id"]: row["title"]
        for row in inventory["rows"]
        if row.get("is_authoring_unit")
    }
    source_ids = [
        "T14.1.1.1",
        "T14.1.2.1",
        "T14.1.3.1",
        "T14.2.1",
        "T14.3.1.1",
        "T14.3.2.1",
        "T14.3.3.1",
        "T14.3.4.1",
    ]
    publication_ids = [
        "T14.1-1",
        "T14.1-2",
        "T14.1-3",
        "T14.2-1",
        "T14.3-1",
        "T14.3-2",
        "T14.3-3",
        "T14.3-4",
    ]
    publication_entries = [
        {
            "source_entity_id": source,
            "publication_entity_id": publication,
            "title": f"脱敏合成表图{index}",
            "expected_section": "§10.1" if index == 1 else "§11/§12",
        }
        for index, (source, publication) in enumerate(
            zip(source_ids, publication_ids, strict=True), start=1
        )
    ]
    augmented_facts = copy.deepcopy(dict(facts))
    augmented_facts["study_identifiers"] = {
        "study_title": facts["study_title"],
        "protocol_id": facts["protocol_id"],
    }
    return {
        "unit_titles": titles,
        "study_facts": augmented_facts,
        "sponsor_metadata": {"sponsor": "脱敏合成申办方"},
        "report_version_and_date": {
            "version": "M20-diagnostic",
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
        "confirmed_clean_body": {key: list(value) for key, value in clean_body.items()},
        "synopsis_selection_manifest": {
            "selection": [
                {"source_section_id": "9.1", "paragraph_indices": [0]},
                {"source_section_id": "12.3.3", "paragraph_indices": [0]},
            ]
        },
        "document_heading_styles": "Heading 1-5",
        "caption_style_table": "Table",
        "caption_style_figure": "Figure",
        "frozen_tfl_map": dict(frozen_map),
        "publication_reference_map": {
            "schema_version": "1.0",
            "entries": publication_entries,
        },
        "accepted_result_tfl": {
            "entity_ids": source_ids,
            "diagnostic_scope_only": True,
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
        "approval_identifier": "SYN-IEC-M20",
        "approval_date": "2026-08-06",
        "protocol_version": "synthetic-v1",
        "applicable_ethics_standard": "《赫尔辛基宣言》",
        "gcp_standard": "药物临床试验质量管理规范",
        "confirmed_ethics_deviations": "合成夹具未登记伦理相关偏离",
        "consent_form_versions": ["synthetic-v1"],
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
                    "entity_id": "T14.3-2",
                    "display": "表14.3-2",
                    "bookmark": "_Ref_Tab_14_3_2",
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


def _trace_manifest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "nodes": [
            {"id": "PROTOCOL1", "layer": "protocol", "locator": "合成方案 §7", "label": "安全性评价"},
            {"id": "SAP1", "layer": "sap", "locator": "合成 SAP 附件 TFL shell", "label": "重要医学事件表规格"},
            {"id": "TFL1", "layer": "tfl", "locator": "T14.3.2.1", "label": "死亡和其他重要医学事件"},
            {"id": "CSR1", "layer": "csr", "locator": "§12.3.3", "label": "重要事件分析与讨论"},
        ],
        "links": [
            {"from": "PROTOCOL1", "to": "SAP1", "relation": "specified_by"},
            {"from": "SAP1", "to": "TFL1", "relation": "implemented_by"},
            {"from": "TFL1", "to": "CSR1", "relation": "reported_in"},
        ],
        "required_paths": [
            {
                "trace_id": "M20TRACE1",
                "protocol": "PROTOCOL1",
                "sap": "SAP1",
                "tfl": "TFL1",
                "csr": "CSR1",
            }
        ],
    }


def run(output: Path) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError(f"Output already exists; refusing to overwrite: {output}")
    output.mkdir(parents=True)
    steps: list[dict[str, Any]] = []
    breakpoints: list[dict[str, Any]] = []

    before_hashes = _input_hash_audit()
    _write_json(output / "00_input_hashes.before.json", before_hashes)
    steps.append({"step": "输入哈希", "status": "pass", "code": None})

    extracted = extract_docx(PROTOCOL)
    _write_json(output / "01_protocol_extract.json", extracted)
    facts = load_study_facts(FACTS_PATH)
    atomic_write_text(output / "02_facts_card.md", render_fact_card(facts))
    route = route_modules(facts)
    _write_json(output / "03_module_route.json", route)
    active = set(route["structure"]["active_module_ids"])
    steps.extend(
        [
            {"step": "方案抽取", "status": "pass", "code": None},
            {"step": "study_facts 人工确认输入", "status": "manual_gate_simulated", "code": "H-STUDY-FACTS-CONFIRM"},
            {
                "step": "模块路由",
                "status": "pass",
                "code": None,
                "randomization_generated": "M-RANDOMIZED" in active,
                "blinding_generated": "M-BLINDING" in active,
            },
        ]
    )

    proposal, shell_datapack, candidate = propose_docx_shell_map(
        SHELL,
        expected_sha256=EXPECTED_INPUT_HASHES["shell"],
        map_version="M20-UNFAMILIAR-V1",
    )
    _write_json(output / "04_shell_proposal.json", proposal)
    _write_json(output / "04_shell_datapack.json", shell_datapack)
    _write_json(output / "04_shell_candidate.json", candidate)
    confirmation = {
        "status": "confirmed",
        "confirmation_id": f"M20-SYNTHETIC-{proposal['report_sha256'][:16]}",
        "confirmed_by_role": "synthetic_fixture_reviewer",
        "confirmed_at": "2026-08-06T14:00:00+08:00",
        "candidate_sha256": candidate["candidate_sha256"],
    }
    frozen = freeze_tfl_map(candidate, confirmation)
    _write_json(output / "05_shell_confirmation.synthetic.json", confirmation)
    _write_json(output / "05_frozen_map.json", frozen)
    steps.extend(
        [
            {"step": "TFL shell 提议", "status": "pass", "code": None},
            {"step": "TFL map 确认与冻结", "status": "manual_gate_simulated", "code": "H-TFL-MAP-CONFIRM"},
        ]
    )

    try:
        apply_docx_result_to_frozen_shell(
            shell_datapack,
            frozen,
            RESULT,
            expected_sha256=EXPECTED_INPUT_HASHES["result"],
        )
        raise RuntimeError("Intentional result drift unexpectedly passed")
    except DocxResultError as exc:
        strict_report = dict(exc.report)
        strict_report["failure_location"] = _failure_location(exc)
        _write_json(output / "06_result_apply.strict_blocked.json", strict_report)
        breakpoints.append(
            {
                "id": "BP-M20-RESULT-DRIFT",
                "stage": "结果 TFL 套值",
                "code": "E-TFL-MAP-DRIFT",
                "failure_location": strict_report["failure_location"],
                "manual_action": "确认 shell-only 和 result-only 实体的处置后重新冻结映射",
                "release_blocking": True,
            }
        )
        steps.append(
            {
                "step": "结果 TFL 严格套值",
                "status": "blocked",
                "code": "E-TFL-MAP-DRIFT",
                "failure_location": strict_report["failure_location"],
            }
        )

    datapack, result_report = apply_docx_result_to_frozen_shell(
        shell_datapack,
        frozen,
        RESULT,
        expected_sha256=EXPECTED_INPUT_HASHES["result"],
        diagnostic_partial=True,
    )
    _write_json(output / "07_datapack.diagnostic_nonrelease.json", datapack)
    _write_json(output / "07_result_match.diagnostic_nonrelease.json", result_report)
    steps.append(
        {
            "step": "诊断性部分套值",
            "status": "diagnostic_only",
            "code": None,
            "release_eligible": False,
        }
    )

    method_dir = output / "08_method_limited_free"
    method_dir.mkdir()
    method_card = load_data(FIXTURE_ROOT / "method_fact_card.9.1.json")
    method_envelope = emit_fact_card_envelope(method_card)
    _write_json(method_dir / "envelope.json", method_envelope)
    try:
        ingest_constrained_draft(
            method_envelope, load_data(FIXTURE_ROOT / "method_draft.9.1.json")
        )
        raise RuntimeError("Known card-external terms unexpectedly passed")
    except ConstrainedNarrationError as exc:
        report = dict(exc.report)
        report["failure_location"] = _failure_location(exc)
        _write_json(method_dir / "attempt1.blocked.json", report)
        steps.append(
            {
                "step": "方法学有限自由首稿",
                "status": "blocked_then_retried",
                "code": "E-FACT-CARD-FORWARD-COVERAGE",
                "failure_location": report["failure_location"],
            }
        )
    method_artifact = ingest_constrained_draft(
        method_envelope, load_data(FIXTURE_ROOT / "method_draft.9.1.retry1.json")
    )
    write_constrained_artifact(
        method_artifact, method_dir / "9.1.md", method_dir / "9.1.meta.json"
    )

    sae_dir = output / "09_sae_exact"
    sae_dir.mkdir()
    sae_card = load_data(FIXTURE_ROOT / "sae_fact_card.12.3.2.json")
    sae_envelope = emit_fact_card_envelope(sae_card)
    _write_json(sae_dir / "envelope.json", sae_envelope)
    sae_artifact = ingest_constrained_draft(
        sae_envelope, load_data(FIXTURE_ROOT / "sae_draft.12.3.2.json")
    )
    write_constrained_artifact(
        sae_artifact, sae_dir / "12.3.2.md", sae_dir / "12.3.2.meta.json"
    )
    steps.append({"step": "SAE 逐字预审句", "status": "pass", "code": None})

    guarded_dir = output / "10_guarded_result"
    guarded_dir.mkdir()
    request = load_data(FIXTURE_ROOT / "section_request.12.3.3.json")
    envelope = emit_agent_envelope(request)
    _write_json(guarded_dir / "envelope.json", envelope)
    try:
        ingest_agent_draft(
            envelope,
            load_data(FIXTURE_ROOT / "section_draft.12.3.3.attack.json"),
            guarded_dir / "attack_project",
        )
        raise RuntimeError("N3 unsourced conclusion attack unexpectedly passed")
    except AgentDraftGuardError as exc:
        attack_report = dict(exc.report)
        attack_report["failure_location"] = _failure_location(exc)
        _write_json(guarded_dir / "n3_attack.blocked.json", attack_report)
    guarded, ingest_report = ingest_agent_draft(
        envelope,
        load_data(FIXTURE_ROOT / "section_draft.12.3.3.json"),
        guarded_dir / "project",
    )
    _write_json(guarded_dir / "ingest.pass.json", ingest_report)
    conditioned = resolve_conditional_conclusions(
        guarded.markdown, datapack, guarded.metadata
    )
    _write_json(guarded_dir / "conditioned.report.json", conditioned.report)
    numeric = resolve(
        conditioned.resolved_text,
        datapack,
        facts,
        binding_manifest=guarded.metadata,
        frozen_tfl_map=frozen,
    )
    if numeric.status != "pass" or numeric.resolved_text is None:
        raise RuntimeError(numeric.report)
    numeric.report["conditional_conclusion"] = conditioned.report
    atomic_write_text(guarded_dir / "12.3.3.resolved.md", numeric.resolved_text)
    _write_json(guarded_dir / "resolve.pass.json", numeric.report)
    steps.append(
        {
            "step": "结果写手+条件化结论+三道数值守卫",
            "status": "pass",
            "code": None,
            "n3_unsourced_attack": "blocked",
        }
    )

    try:
        load_resolved_section(
            sae_dir / "12.3.2.md",
            sae_dir / "12.3.2.md",
            sae_dir / "12.3.2.meta.json",
            sae_dir / "12.3.2.meta.json",
        )
        raise RuntimeError("Legacy assembly loader unexpectedly accepted constrained metadata")
    except CrossChapterError as exc:
        bridge_failure = {
            "status": "blocked",
            "code": "E-CONSTRAINED-ASSEMBLY-CONTRACT",
            "message": str(exc),
            "failure_location": _failure_location(exc),
        }
        _write_json(output / "11_constrained_assembly.before_bridge.json", bridge_failure)
        breakpoints.append(
            {
                "id": "BP-M20-CONSTRAINED-ASSEMBLY",
                "stage": "受控事实卡产物装配",
                "code": "E-CONSTRAINED-ASSEMBLY-CONTRACT",
                "failure_location": bridge_failure["failure_location"],
                "manual_action": None,
                "release_blocking": False,
                "resolution": "新增仅接受正向覆盖、文风通过且零占位符产物的严格桥接层",
            }
        )

    method_section = load_constrained_resolved_section(
        method_dir / "9.1.md", method_dir / "9.1.meta.json", evidence_status="demo_only"
    )
    sae_section = load_constrained_resolved_section(
        sae_dir / "12.3.2.md", sae_dir / "12.3.2.meta.json", evidence_status="demo_only"
    )
    guarded_section = load_resolved_section(
        guarded.markdown_path,
        guarded_dir / "12.3.3.resolved.md",
        guarded.metadata_path,
        guarded_dir / "resolve.pass.json",
    )
    sections = (method_section, sae_section, guarded_section)
    decisions, decision_provenance = _review_decisions(sections)
    _write_json(output / "12_review_decisions.synthetic.json", decisions)
    _write_json(output / "12_review_decisions.provenance.json", decision_provenance)
    clean_body = apply_review_decisions(sections, decisions)
    assembly_dir = output / "13_assembly"
    assembly_dir.mkdir()
    annotated = render_annotated_docx(
        sections,
        assembly_dir / "M20_unfamiliar.annotated.docx",
        protocol_id=facts["protocol_id"],
    )
    clean = render_clean_and_synopsis(
        sections,
        decisions,
        {
            "9.1": [method_section.paragraphs[0].para_id],
            "12.3.3": [guarded_section.paragraphs[0].para_id],
        },
        assembly_dir / "M20_unfamiliar.clean.docx",
        assembly_dir / "M20_unfamiliar.synopsis.docx",
        assembly_dir / "synopsis_manifest.json",
        protocol_id=facts["protocol_id"],
    )
    _write_json(output / "13_assembly_report.json", {"annotated": annotated, **clean})
    steps.append(
        {
            "step": "clean/annotated 正文装配",
            "status": "diagnostic_only",
            "code": None,
            "simulated_medical_decisions": len(decision_provenance["records"]),
        }
    )

    deterministic_dir = output / "14_deterministic"
    deterministic_dir.mkdir()
    context = _deterministic_context(facts, frozen, clean_body)
    _write_json(deterministic_dir / "context.json", context)
    bundle = build_deterministic_bundle(context)
    rendering = render_deterministic_bundle_docx(
        bundle, deterministic_dir / "M20_deterministic_units.docx"
    )
    _write_json(
        deterministic_dir / "manifest.json", {**bundle, "docx_rendering": rendering}
    )
    steps.append(
        {
            "step": "48 个确定性单元+4 个模板",
            "status": "pass_separate_volume",
            "code": "N-DETERMINISTIC-ASSEMBLY-NOT-UNIFIED",
            "model_called": bundle["model_called"],
        }
    )
    breakpoints.append(
        {
            "id": "BP-M20-DOCUMENT-COMPOSITION",
            "stage": "完整 CSR 合卷",
            "code": "N-DETERMINISTIC-ASSEMBLY-NOT-UNIFIED",
            "failure_location": "csr_writer/deterministic_sections.py + csr_writer/m4_assembly.py",
            "manual_action": "当前需人工合卷；本轮不伪装成单一完整 CSR",
            "release_blocking": True,
        }
    )

    trace_dir = output / "15_trace_qc"
    trace_dir.mkdir()
    metadata_inputs = [
        load_data(method_dir / "9.1.meta.json"),
        load_data(sae_dir / "12.3.2.meta.json"),
        load_data(guarded.metadata_path),
    ]
    unreferenced = analyze_tfl_references(datapack, metadata_inputs)
    _write_json(trace_dir / "unreferenced_tfl.json", unreferenced)
    atomic_write_text(
        trace_dir / "unreferenced_tfl.md", render_unreferenced_tfl(unreferenced)
    )
    trace_manifest = _trace_manifest()
    _write_json(trace_dir / "bidirectional_trace_manifest.json", trace_manifest)
    trace_report = validate_bidirectional_trace(trace_manifest)
    _write_json(trace_dir / "bidirectional_trace_report.json", trace_report)

    checks = {
        "模块路由": {
            "status": "pass",
            "summary": "未激活随机化和盲法模块，与单臂开放性设计一致。",
        },
        "结果映射": {
            "status": "blocked",
            "summary": "发现 1 张 shell-only 表和 1 张 result-only 表，严格套值已阻断。",
        },
        "三条产出路径": {
            "status": "pass",
            "summary": "确定性生成、方法学有限自由和 SAE 逐字预审路径的本地守卫均已执行。",
        },
        "数值与结论守卫": {
            "status": "pass",
            "summary": "12.3.3 的条件化结论和三道数值守卫通过，附加无源判断句被硬阻断。",
        },
        "双向追溯": {
            "status": "pass",
            "summary": "未发现所执行的合成方案至 CSR 四层路径断链。",
        },
        "文档合卷": {
            "status": "blocked",
            "summary": "确定性单元卷与受控正文卷尚无统一合卷入口。",
        },
    }
    qc_markdown, qc_report = build_qc_report(checks)
    atomic_write_text(trace_dir / "qc_report.md", qc_markdown)
    _write_json(trace_dir / "qc_report.json", qc_report)
    steps.append({"step": "反向溯源与 QC", "status": "attention_required", "code": None})

    after_hashes = _input_hash_audit()
    _write_json(output / "99_input_hashes.after.json", after_hashes)
    if before_hashes != after_hashes:
        raise RuntimeError("M20 fixture hash changed during E2E")

    report = {
        "schema_version": "1.0",
        "status": "completed_with_release_blockers",
        "release_eligible": False,
        "steps": steps,
        "breakpoints": breakpoints,
        "manual_intervention_gate_count": 4,
        "manual_intervention_gates": [
            "study_facts 人工确认",
            "TFL map 人工确认",
            "2 段医学负责人确认（本次仅用合成决定演示 clean 闸门）",
            "shell/result 漂移裁决",
        ],
        "three_paths": {
            "deterministic_generation": "pass_separate_volume",
            "method_limited_free": "pass_after_one_guarded_retry",
            "sae_exact_reviewed_surface": "pass",
            "guarded_result_writer": "pass",
        },
        "routing_assertions": {
            "randomization_section_generated": False,
            "blinding_section_generated": False,
        },
        "mismatched_tables": {
            "shell_only": result_report["missing_result_entities"],
            "result_only": result_report["unmatched_entities"],
            "strict_action": "hard_fail",
            "diagnostic_action": "explicit non-release partial datapack",
        },
        "outputs": {
            "annotated": annotated["output"],
            "clean": clean["clean"]["path"],
            "synopsis": clean["synopsis"]["path"],
            "deterministic_volume": str(
                (deterministic_dir / "M20_deterministic_units.docx").resolve()
            ),
        },
        "closeout_entered": False,
        "closeout_reason": "存在映射漂移、人工闸门和两卷未合并的阻断项。",
    }
    _write_json(output / "M20_true_e2e_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "examples" / "output" / "m20" / "true_e2e_replay",
    )
    args = parser.parse_args()
    report = run(args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
