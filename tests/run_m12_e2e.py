#!/usr/bin/env python3
"""Generate the privacy-safe M12 playbook and writing-quality evidence package."""

from __future__ import annotations

import json
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.authoring_inventory import (  # noqa: E402
    build_authoring_inventory,
    render_authoring_inventory_markdown,
)
from csr_writer.conditional_conclusions import (  # noqa: E402
    resolve_conditional_conclusions,
)
from csr_writer.cross_chapter import load_sections_from_manifest  # noqa: E402
from csr_writer.cross_references import REF_PLACEHOLDER_RE, display_reference  # noqa: E402
from csr_writer.datapack import validate_datapack  # noqa: E402
from csr_writer.io_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    sha256_file,
)
from csr_writer.m4_assembly import render_annotated_docx  # noqa: E402
from csr_writer.module_router import route_modules  # noqa: E402
from csr_writer.playbook_qc import audit_complete_playbooks  # noqa: E402
from csr_writer.resolve_placeholders import resolve  # noqa: E402
from csr_writer.section_writer import emit_agent_envelope, ingest_agent_draft  # noqa: E402
from csr_writer.source_lock import verify_source_file_lock  # noqa: E402


OUTPUT = ROOT / "examples" / "output" / "m12"
PLAYBOOK_DIR = ROOT / "skills" / "csr-writer" / "section-playbooks"
FACTS_PATH = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
PROTOCOL_FACTS_PATH = ROOT / "examples" / "demo_protocol_facts.json"
M11_WRITING = ROOT / "examples" / "output" / "m11" / "writing_trial"

REAL_SOURCE_HASHES = {
    "paired_protocol": "3c1cf7c01434405885f562d9e687691185d5250bf4a6aeead564fcdf97aeceae",
    "paired_tfl_shell": "75b6ce46bfda933d75611ce38e0ac564a17116d67faf04a767c1bd81d4bc27c5",
    "reference_protocol": "12ac850d70294e1a074ffa8102e437d98e67f27e81091097bc15fddd63d45ca1",
    "reference_numbering_pdf": "b074ce91810e48e0536cb4464053502e37c5bbe244b7954715ffd41a1936117a",
}

BEFORE_LINE_COUNTS = {
    "s9-randomization-blinding-controls.md": 28,
    "s10-subject-disposition.md": 24,
    "s11-2-demographics-baseline.md": 23,
    "s11-4-primary-efficacy.md": 55,
    "s11-a-dose-escalation.md": 63,
    "s11-clinpharm-special-pop-endpoints.md": 31,
    "s11-topical-pk.md": 34,
    "s12-2-adverse-events.md": 47,
    "s12-exposure.md": 23,
    "s12-route-specific-safety.md": 30,
}

NEW_PLAYBOOKS = {
    "s11-3-treatment-compliance.md": "覆盖 §11.3；治疗依从性会影响偏离、分析集和结果解释。",
    "s12-3-1-deaths-serious-significant-listings.md": "共享覆盖 §12.3.1.1/.2/.3；关键事件集合完整性风险高。",
    "s12-3-2-sae-narratives-boundary.md": "覆盖 §12.3.2；用完整 playbook 固化工具不得代写医学叙述的边界。",
    "s12-3-3-serious-event-analysis.md": "覆盖 §12.3.3；需要把关键事件、暴露和局限有序综合。",
    "s12-4-2-laboratory-evaluation.md": "共享覆盖 §12.4.2.1/.2/.3；防止统计异常被自动解释为临床重要。",
    "s13-discussion-overall-conclusions.md": "覆盖 §13；高价值且必须从已确认正文派生，不能独立选数。",
}

KEYS = {
    "randomized_total": "T14.1.1.1|POP=FAS|ROW=RANDOMIZED|COL=ACTIVE_TOTAL",
    "analysis_total": "T14.1.1.2|POP=FAS|ROW=ANALYSIS_SET|COL=ACTIVE_TOTAL",
    "pk_cmax": "T14.2.4.3.1|POP=PKPS|ROW=CMAX|COL=ACTIVE_TOTAL",
    "pk_auc": "T14.2.4.3.1|POP=PKPS|ROW=AUC0_T|COL=ACTIVE_TOTAL",
    "teae_result": "T14.3.1.2|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL",
    "trae_result": "T14.3.1.2|POP=SS|ROW=ANY_TRAE|COL=ACTIVE_TOTAL",
    "sae_count": "T14.3.1.2|POP=SS|ROW=ANY_SAE|COL=ACTIVE_TOTAL",
    "death_count": "T14.3.1.2|POP=SS|ROW=DEATH|COL=ACTIVE_TOTAL",
    "discontinuation_count": "T14.3.1.2|POP=SS|ROW=AE_DISCONTINUATION|COL=ACTIVE_TOTAL",
    "grade3plus_count": "T14.3.1.2|POP=SS|ROW=GRADE_3_PLUS|COL=ACTIVE_TOTAL",
}


def _resolve_real_sources(real_dir: Path) -> dict[str, Path]:
    matches = {role: [] for role in REAL_SOURCE_HASHES}
    for path in real_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf"}:
            continue
        digest = sha256_file(path)
        for role, expected in REAL_SOURCE_HASHES.items():
            if digest == expected:
                matches[role].append(path)
    invalid = {role: len(paths) for role, paths in matches.items() if len(paths) != 1}
    if invalid:
        raise RuntimeError(f"approved read-only source resolution failed: {invalid}")
    return {role: paths[0] for role, paths in matches.items()}


def _source_audit(sources: Mapping[str, Path]) -> dict[str, Any]:
    records = []
    for role, expected in REAL_SOURCE_HASHES.items():
        observed = sha256_file(sources[role])
        records.append(
            {
                "source_role": role,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "match": observed == expected,
                "path_recorded": False,
                "filename_recorded": False,
            }
        )
    return {
        "schema_version": "1.0",
        "status": "pass" if all(item["match"] for item in records) else "blocked",
        "records": records,
    }


def _request(
    section_id: str,
    variant: str,
    roles: Sequence[str],
    sources: Sequence[tuple[str, str, str, str]],
) -> dict[str, Any]:
    bindings = [
        {
            "role": role,
            "key": KEYS[role],
            "source_id": KEYS[role].split("|", 1)[0],
            "context": "脱敏合成结果；仅用于 M12 行文质量和守卫验收",
        }
        for role in roles
    ]
    return {
        "schema_version": "1.1",
        "section_id": section_id,
        "variant": variant,
        "evidence_status": "demo_only",
        "tfl_bindings": bindings,
        "tfl_references": list(dict.fromkeys(item["source_id"] for item in bindings)),
        "source_fragments": [
            {"source_id": source_id, "locator": locator, "purpose": purpose, "text": text}
            for source_id, locator, purpose, text in sources
        ],
    }


def _requests(*, complete_safety: bool = True) -> dict[str, dict[str, Any]]:
    safety_roles = ["teae_result", "trae_result"]
    if complete_safety:
        safety_roles.extend(
            ["sae_count", "death_count", "discontinuation_count", "grade3plus_count"]
        )
    return {
        "9.4.6": _request(
            "9.4.6",
            "blinding",
            (),
            (
                (
                    "SRC-BLIND-DESIGN",
                    "脱敏方案/盲法设计",
                    "blinding_method",
                    "研究采用双盲设计，受试者、研究者及结果评价人员保持盲态。",
                ),
                (
                    "SRC-BLIND-MAINTENANCE",
                    "脱敏方案/盲法维持",
                    "blinding_method",
                    "研究用制剂在外观、包装和标签方面保持一致；非盲岗位与盲态评价岗位职责分离。",
                ),
                (
                    "SRC-UNBLIND-PROCEDURE",
                    "脱敏方案/应急揭盲",
                    "unblinding_record",
                    "应急揭盲按预设程序启动、记录并报告，非计划揭盲需进入盲态维护台账。",
                ),
                (
                    "SRC-UNBLIND-RECORD",
                    "脱敏盲态维护记录",
                    "unblinding_record",
                    "经审核的盲态维护记录未报告非计划揭盲。",
                ),
            ),
        ),
        "10.1": _request(
            "10.1",
            "subject_disposition",
            ("randomized_total", "analysis_total"),
            (
                (
                    "SRC-DISPOSITION-DEFINITION",
                    "脱敏方案/受试者处置定义",
                    "disposition_definition",
                    "处置按筛选、随机、接受研究治疗、完成和退出状态汇总；退出原因按经审核分类呈现。",
                ),
                (
                    "SRC-ANALYSIS-SET",
                    "脱敏统计分析计划/分析集",
                    "analysis_set",
                    "全分析集的定义和纳入规则以经审核统计分析计划为准。",
                ),
                (
                    "SRC-DISPOSITION-GAP",
                    "脱敏结果证据清单/处置子类",
                    "gap_record",
                    "当前受限证据包未提供完成、退出及退出原因的单元格绑定。",
                ),
            ),
        ),
        "11.5.4": _request(
            "11.5.4",
            "pk_parameters",
            ("pk_cmax", "pk_auc"),
            (
                (
                    "SRC-PK-METHOD",
                    "脱敏统计分析计划/PK 参数",
                    "pk_method",
                    "PK 参数采用经审核的非房室方法计算，参数可计算性取决于可定量浓度数据和采样覆盖。",
                ),
                (
                    "SRC-PK-SET",
                    "脱敏统计分析计划/PK 参数分析集",
                    "analysis_set",
                    "PK 参数分析集按经审核的纳入和排除规则确定。",
                ),
                (
                    "SRC-PK-SAMPLING",
                    "脱敏方案/PK 采样",
                    "sampling_design",
                    "采样覆盖给药前和给药后预设时点，参数解释须结合实际可定量时点。",
                ),
                (
                    "SRC-PK-BLQ",
                    "脱敏统计分析计划/BLQ 处理",
                    "blq_rule",
                    "低于定量下限的数据按预设规则处理，不可定量结果不得改写为无系统暴露。",
                ),
            ),
        ),
        "12.2": _request(
            "12.2",
            "adverse_events_conditioned",
            safety_roles,
            (
                (
                    "SRC-AE-DEFINITION",
                    "脱敏方案/安全性定义",
                    "safety_definition",
                    "治疗期间不良事件按预设观察期、严重程度、严重性和记录类别汇总。",
                ),
                (
                    "SRC-AE-SET",
                    "脱敏统计分析计划/安全性分析集",
                    "analysis_set",
                    "安全性分析采用经审核的安全性分析集，受试者和事件计数口径分开。",
                ),
                (
                    "SRC-AE-CODING",
                    "脱敏数据规范/事件编码",
                    "safety_definition",
                    "事件按经审核的编码字典版本汇总，正文只提炼关键结果，完整分布保留在支持表。",
                ),
            ),
        ),
    }


def _raw_drafts(*, complete_safety: bool = True) -> dict[str, dict[str, Any]]:
    safety_paragraphs = [
        {
            "text": "安全性分析采用经审核的安全性分析集。治疗期间不良事件按预设观察期、严重程度、严重性和记录类别汇总，受试者和事件计数口径分开。事件术语采用经审核的编码字典版本。",
            "sources": ["SRC-AE-DEFINITION", "SRC-AE-SET", "SRC-AE-CODING"],
            "confidence": "derived",
            "needs_human": False,
        },
        {
            "text": "安全性分析集中，TEAE 汇总结果为 {{T14.3.1.2|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL}}，TRAE 汇总结果为 {{T14.3.1.2|POP=SS|ROW=ANY_TRAE|COL=ACTIVE_TOTAL}}。正文在此保留总体口径，完整事件分布详见{{@ref:T14.3.1.2}}。",
            "sources": ["T14.3.1.2", "SRC-AE-CODING"],
            "confidence": "derived",
            "needs_human": False,
        },
    ]
    if complete_safety:
        safety_paragraphs.append(
            {
                "text": "关键事件核对显示，严重不良事件汇总为 {{T14.3.1.2|POP=SS|ROW=ANY_SAE|COL=ACTIVE_TOTAL}}，死亡汇总为 {{T14.3.1.2|POP=SS|ROW=DEATH|COL=ACTIVE_TOTAL}}，导致永久停用研究治疗的事件汇总为 {{T14.3.1.2|POP=SS|ROW=AE_DISCONTINUATION|COL=ACTIVE_TOTAL}}，重度事件汇总为 {{T14.3.1.2|POP=SS|ROW=GRADE_3_PLUS|COL=ACTIVE_TOTAL}}。这些结果在总体评价前单独陈述。",
                "sources": ["T14.3.1.2", "SRC-AE-DEFINITION"],
                "confidence": "derived",
                "needs_human": False,
            }
        )
    safety_paragraphs.append(
        {
            "text": "[待医学负责人确认] {{@conclusion:SAFETY_TOLERABILITY_V1}}",
            "sources": ["T14.3.1.2", "SRC-AE-DEFINITION"],
            "confidence": "inferred",
            "needs_human": True,
        }
    )
    return {
        "9.4.6": {
            "paragraphs": [
                {
                    "text": "本研究采用双盲设计，受试者、研究者及结果评价人员保持盲态。盲法范围和岗位职责按经审核方案执行，不根据设计标签推断实际维持情况。",
                    "sources": ["SRC-BLIND-DESIGN"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "研究用制剂在外观、包装和标签方面保持一致。非盲岗位与盲态评价岗位职责分离，随机编码按受控流程保管。",
                    "sources": ["SRC-BLIND-MAINTENANCE"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "应急揭盲按预设程序启动、记录并报告；非计划揭盲进入盲态维护台账。该流程描述不替代对实际揭盲记录的核查。",
                    "sources": ["SRC-UNBLIND-PROCEDURE"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "经审核的盲态维护记录未报告非计划揭盲。该陈述仅限已提供的记录范围，不外推至未核验资料。",
                    "sources": ["SRC-UNBLIND-RECORD"],
                    "confidence": "derived",
                    "needs_human": False,
                },
            ]
        },
        "10.1": {
            "paragraphs": [
                {
                    "text": "受试者处置按筛选、随机、接受研究治疗、完成和退出状态汇总。退出原因沿用经审核分类，不由写手合并或改写。",
                    "sources": ["SRC-DISPOSITION-DEFINITION"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "已随机受试者汇总为 {{T14.1.1.1|POP=FAS|ROW=RANDOMIZED|COL=ACTIVE_TOTAL}}，纳入全分析集者汇总为 {{T14.1.1.2|POP=FAS|ROW=ANALYSIS_SET|COL=ACTIVE_TOTAL}}。受试者处置和分析集汇总分别详见{{@ref:T14.1.1.1}}和{{@ref:T14.1.1.2}}。",
                    "sources": ["T14.1.1.1", "T14.1.1.2"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "全分析集的定义和纳入规则以经审核统计分析计划为准，并与分析数据集章节保持一致。当前证据包未提供完成、退出及退出原因的结果绑定，因此本节不对这些类别补充数量或原因。",
                    "sources": ["SRC-ANALYSIS-SET", "SRC-DISPOSITION-GAP"],
                    "confidence": "derived",
                    "needs_human": False,
                },
            ]
        },
        "11.5.4": {
            "paragraphs": [
                {
                    "text": "PK 参数采用经审核的非房室方法计算，参数分析集按预设纳入和排除规则确定。参数可计算性取决于可定量浓度数据和实际采样覆盖。",
                    "sources": ["SRC-PK-METHOD", "SRC-PK-SET"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "低于定量下限的数据按预设规则处理。不可定量结果不等同于无系统暴露，参数解释还需同时核对可定量时点、分析方法定量下限和不能计算的具体原因。",
                    "sources": ["SRC-PK-BLQ", "SRC-PK-SAMPLING"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "PK 参数分析集中，峰浓度参数汇总结果为 {{T14.2.4.3.1|POP=PKPS|ROW=CMAX|COL=ACTIVE_TOTAL}}，系统暴露参数汇总结果为 {{T14.2.4.3.1|POP=PKPS|ROW=AUC0_T|COL=ACTIVE_TOTAL}}，详见{{@ref:T14.2.4.3.1}}。这些结果只反映当前绑定的参数汇总。",
                    "sources": ["T14.2.4.3.1"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "[待医学负责人确认] 参数可计算性及系统暴露的临床解释应结合可定量时点、低于定量下限比例、分析方法定量下限和采样局限综合确认；当前汇总不单独支持对暴露水平作判断。",
                    "sources": ["SRC-PK-METHOD", "SRC-PK-BLQ", "SRC-PK-SAMPLING"],
                    "confidence": "inferred",
                    "needs_human": True,
                },
            ]
        },
        "12.2": {"paragraphs": safety_paragraphs},
    }


def _datapack(
    safety_counts: Mapping[str, int],
    *,
    include_critical: bool = True,
) -> dict[str, Any]:
    source_id = "sha256:" + "a" * 64
    displays: dict[str, tuple[str, str, int | None]] = {
        "randomized_total": ("40", "count", 40),
        "analysis_total": ("38", "count", 38),
        "pk_cmax": ("0.86 ng/mL", "statistic", None),
        "pk_auc": ("8.42 h·ng/mL", "statistic", None),
        "teae_result": ("33 (82.5%)", "n_pct", None),
        "trae_result": ("12 (30.0%)", "n_pct", None),
    }
    if include_critical:
        for role in ("sae_count", "death_count", "discontinuation_count", "grade3plus_count"):
            value = int(safety_counts[role])
            displays[role] = (str(value), "count", value)
    cells = {}
    table_ids: dict[str, str] = {}
    for index, (role, (display, value_type, numeric_value)) in enumerate(displays.items(), start=1):
        key = KEYS[role]
        parts = dict(item.split("=", 1) for item in key.split("|")[1:])
        cell = {
            "v": display,
            "display_value": display,
            "type": value_type,
            "row": {"id": parts["ROW"], "label": parts["ROW"]},
            "column": {"id": parts["COL"], "label": parts["COL"]},
            "source_locator": {
                "source_id": source_id,
                "row_index": index,
                "column_index": 2,
            },
            "dimensions": {},
        }
        if numeric_value is not None:
            cell["numeric_value"] = numeric_value
        cells[key] = cell
        table_ids[key.split("|", 1)[0]] = parts["POP"]
    payload = {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "mapping_manifest": {"filename": "m12_synthetic_mapping.json", "sha256": "b" * 64},
        "sources": [{"source_id": source_id}],
        "tables": [
            {
                "table_id": table_id,
                "population": population,
                "source_id": source_id,
                "ingest_strategy": "cell_level",
                "dimensions": {},
            }
            for table_id, population in table_ids.items()
        ],
        "cells": cells,
    }
    validate_datapack(payload)
    return payload


def _readable(text: str) -> str:
    return REF_PLACEHOLDER_RE.sub(
        lambda match: display_reference(match.group("entity")), text
    )


def _run_writing_trial(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    project_dir = output_root / "project"
    requests = _requests()
    raw_drafts = _raw_drafts()
    protocol_facts = json.loads(PROTOCOL_FACTS_PATH.read_text(encoding="utf-8"))
    datapack = _datapack(
        {
            "sae_count": 0,
            "death_count": 0,
            "discontinuation_count": 0,
            "grade3plus_count": 0,
        }
    )
    atomic_write_json(output_root / "synthetic_datapack.json", datapack)
    manifest_entries = []
    records = []
    semantic_categories: Counter[str] = Counter()
    for section_id in ("9.4.6", "10.1", "11.5.4", "12.2"):
        section_root = output_root / section_id.replace(".", "_")
        section_root.mkdir(parents=True, exist_ok=True)
        request = requests[section_id]
        envelope = emit_agent_envelope(request)
        raw = raw_drafts[section_id]
        atomic_write_json(section_root / "request.json", request)
        atomic_write_json(section_root / "envelope.json", envelope)
        atomic_write_json(section_root / "raw_agent_draft_attempt1.json", raw)
        draft, ingest_report = ingest_agent_draft(envelope, raw, project_dir)
        atomic_write_json(section_root / "ingest_guard_attempt1_pass.json", ingest_report)
        conditioned = resolve_conditional_conclusions(draft.markdown, datapack, draft.metadata)
        atomic_write_json(section_root / "conditional_conclusion_guard.json", conditioned.report)
        resolved = resolve(
            conditioned.resolved_text,
            datapack,
            protocol_facts,
            binding_manifest=draft.metadata,
        )
        if resolved.status != "pass" or resolved.resolved_text is None:
            raise RuntimeError(f"M12 resolve failed for {section_id}: {resolved.report}")
        resolved_path = section_root / "resolved.md"
        guard_path = section_root / "resolve_guard.json"
        atomic_write_text(resolved_path, resolved.resolved_text)
        atomic_write_json(guard_path, resolved.report)
        manifest_entries.append(
            {
                "draft": str(draft.markdown_path.relative_to(output_root)),
                "resolved": str(resolved_path.relative_to(output_root)),
                "metadata": str(draft.metadata_path.relative_to(output_root)),
                "guard": str(guard_path.relative_to(output_root)),
            }
        )
        for finding in draft.metadata.get("semantic_findings", []):
            semantic_categories[str(finding["category"])] += 1
        records.append(
            {
                "section_id": section_id,
                "envelope_sha256": envelope["envelope_sha256"],
                "first_attempt_status": "pass",
                "guard_retry_count": 0,
                "conditional_conclusion_status": conditioned.status,
                "conditional_conclusion_branch": (
                    conditioned.report["rules"][0]["branch"]
                    if conditioned.report["rules"]
                    else None
                ),
                "resolve_status": resolved.status,
                "paragraph_count": len(draft.metadata["paragraphs"]),
                "placeholder_count": resolved.report["placeholder_count"],
                "reference_count": resolved.report["reference_count"],
                "manual_polishing_after_guard": False,
            }
        )

    manifest = {"schema_version": "1.0", "sections": manifest_entries}
    manifest_path = output_root / "sections_manifest.json"
    atomic_write_json(manifest_path, manifest)
    sections = load_sections_from_manifest(manifest_path)
    assembly_path = output_root / "M12_writing_trial_annotated.docx"
    assembly_report = render_annotated_docx(
        sections, assembly_path, protocol_id="DEID-M12-WRITING-TRIAL"
    )
    assembly_report["output"] = str(assembly_path.relative_to(ROOT))
    atomic_write_json(output_root / "assembly_report.json", assembly_report)

    sample_lines = [
        "# M12 起草样例",
        "",
        "> 以下正文由本次 Agent 读取受限 envelope 后直接起草，经 ingest、语义护栏、条件化结论、三道数值守卫和确定性 resolve 生成。数据均为脱敏合成验收夹具，未作守卫后的人工润色，不得作为临床证据。",
        "",
    ]
    for section_id in ("9.4.6", "10.1", "11.5.4", "12.2"):
        text = (output_root / section_id.replace(".", "_") / "resolved.md").read_text(
            encoding="utf-8"
        )
        sample_lines.extend([_readable(text).strip(), ""])
    sample_lines.extend(
        [
            "## 守卫拦截记录",
            "",
            "- 首轮被拒节数：0；首轮被拒段落：0。",
            "- 重试次数：0；最终通过：4/4 节。",
            f"- 语义红线强制标记分布：{json.dumps(dict(semantic_categories), ensure_ascii=False, sort_keys=True)}。",
            "- 条件化结论警告不计作本轮主样例拦截；缺关键绑定的专门情形在三情形证据中产生受控警告。",
            "- 守卫通过后未作人工润色。",
            "",
        ]
    )
    atomic_write_text(OUTPUT / "M12起草样例.md", "\n".join(sample_lines))
    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "interface": "Agent emit-envelope -> ingest-draft -> conditioned conclusion -> resolve -> annotated assembly",
        "external_model_api_called": False,
        "agent_itself_was_writer": True,
        "section_count": len(records),
        "first_pass_rejected_section_count": 0,
        "first_pass_rejected_paragraph_count": 0,
        "rejection_reason_distribution": {},
        "semantic_forced_review_distribution": dict(semantic_categories),
        "retry_count": 0,
        "resolved_pass_count": len(records),
        "manual_polishing_after_guard": False,
        "records": records,
        "assembly": assembly_report,
    }
    atomic_write_json(output_root / "writing_trial_summary.json", summary)
    return summary


def _run_conclusion_scenarios(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    protocol_facts = json.loads(PROTOCOL_FACTS_PATH.read_text(encoding="utf-8"))
    main_metadata = json.loads(
        (OUTPUT / "writing_trial" / "project" / "sections" / "12.2.meta.json").read_text(
            encoding="utf-8"
        )
    )
    main_draft = (OUTPUT / "writing_trial" / "project" / "sections" / "12.2.md").read_text(
        encoding="utf-8"
    )
    scenarios: list[dict[str, Any]] = []

    for scenario_id, counts in (
        (
            "no_critical_events",
            {
                "sae_count": 0,
                "death_count": 0,
                "discontinuation_count": 0,
                "grade3plus_count": 0,
            },
        ),
        (
            "important_events_present",
            {
                "sae_count": 1,
                "death_count": 0,
                "discontinuation_count": 2,
                "grade3plus_count": 1,
            },
        ),
    ):
        scenario_root = output_root / scenario_id
        scenario_root.mkdir(parents=True, exist_ok=True)
        datapack = _datapack(counts)
        conditioned = resolve_conditional_conclusions(main_draft, datapack, main_metadata)
        resolved = resolve(
            conditioned.resolved_text,
            datapack,
            protocol_facts,
            binding_manifest=main_metadata,
        )
        if resolved.status != "pass" or resolved.resolved_text is None:
            raise RuntimeError(f"M12 conclusion scenario failed: {scenario_id}")
        atomic_write_json(scenario_root / "synthetic_datapack.json", datapack)
        atomic_write_json(scenario_root / "condition_guard.json", conditioned.report)
        atomic_write_json(scenario_root / "resolve_guard.json", resolved.report)
        atomic_write_text(scenario_root / "resolved.md", resolved.resolved_text)
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "branch": conditioned.report["rules"][0]["branch"],
                "status": conditioned.status,
                "warning_count": len(conditioned.report["warnings"]),
                "resolved": str((scenario_root / "resolved.md").relative_to(ROOT)),
                "text": _readable(resolved.resolved_text).strip(),
            }
        )

    missing_root = output_root / "critical_binding_missing"
    missing_root.mkdir(parents=True, exist_ok=True)
    request = _requests(complete_safety=False)["12.2"]
    raw = _raw_drafts(complete_safety=False)["12.2"]
    envelope = emit_agent_envelope(request)
    draft, ingest_report = ingest_agent_draft(
        envelope, raw, missing_root / "project"
    )
    datapack = _datapack({}, include_critical=False)
    conditioned = resolve_conditional_conclusions(draft.markdown, datapack, draft.metadata)
    resolved = resolve(
        conditioned.resolved_text,
        datapack,
        protocol_facts,
        binding_manifest=draft.metadata,
    )
    if resolved.status != "pass" or resolved.resolved_text is None:
        raise RuntimeError("M12 missing-binding conclusion scenario failed")
    atomic_write_json(missing_root / "request.json", request)
    atomic_write_json(missing_root / "envelope.json", envelope)
    atomic_write_json(missing_root / "raw_agent_draft.json", raw)
    atomic_write_json(missing_root / "ingest_guard.json", ingest_report)
    atomic_write_json(missing_root / "synthetic_datapack.json", datapack)
    atomic_write_json(missing_root / "condition_guard.json", conditioned.report)
    atomic_write_json(missing_root / "resolve_guard.json", resolved.report)
    atomic_write_text(missing_root / "resolved.md", resolved.resolved_text)
    scenarios.append(
        {
            "scenario_id": "critical_binding_missing",
            "branch": conditioned.report["rules"][0]["branch"],
            "status": conditioned.status,
            "warning_count": len(conditioned.report["warnings"]),
            "resolved": str((missing_root / "resolved.md").relative_to(ROOT)),
            "text": _readable(resolved.resolved_text).strip(),
        }
    )

    labels = {
        "no_critical_events": "无 SAE、无死亡、无停药、无重度事件",
        "important_events_present": "有 SAE 或导致停药事件",
        "critical_binding_missing": "关键绑定缺失",
    }
    lines = [
        "# 结论性表述三种情形——实际产出",
        "",
        "> 三组均走相同受控规则；仅脱敏合成 datapack 或绑定完整性不同。",
        "",
    ]
    for item in scenarios:
        lines.extend(
            [
                f"## {labels[item['scenario_id']]}",
                "",
                f"- 规则分支：`{item['branch']}`；状态：`{item['status']}`；警告：{item['warning_count']}。",
                "",
                item["text"],
                "",
            ]
        )
    atomic_write_text(OUTPUT / "结论性表述三情形.md", "\n".join(lines))
    report = {
        "schema_version": "1.0",
        "status": "pass",
        "scenarios": [{key: value for key, value in item.items() if key != "text"} for item in scenarios],
    }
    atomic_write_json(output_root / "scenario_summary.json", report)
    return report


def _comparison() -> dict[str, Any]:
    records = []
    lines = [
        "# M11 vs M12 同节并排对比",
        "",
        "> M11 与 M12 均使用脱敏合成数据。M12 文本为深化 playbook 后 Agent 的首轮原始产出，经守卫后未人工润色。",
        "",
        "| 节 | M11 成品 | M12 成品 | 可见变化 |",
        "|---|---|---|---|",
    ]
    change_notes = {
        "9.4.6": "由方法加记录两段扩展为设计范围、盲法维持、揭盲流程、实际记录四个思考单元。",
        "10.1": "新增分类口径、分析集一致性和未绑定类别的明确缺口，避免只报账户数字。",
        "11.5.4": "补入参数可计算性、BLQ、采样覆盖和外用低暴露解释边界。",
        "12.2": "补入分析口径、关键事件前置和脚本数据条件化结论，不再固定套用安全性良好。",
    }
    for section_id in ("9.4.6", "10.1", "11.5.4", "12.2"):
        slug = section_id.replace(".", "_")
        old_text = _readable((M11_WRITING / slug / "resolved.md").read_text(encoding="utf-8")).strip()
        new_text = _readable(
            (OUTPUT / "writing_trial" / slug / "resolved.md").read_text(encoding="utf-8")
        ).strip()
        old_paragraphs = len([part for part in old_text.split("\n\n") if part.strip()])
        new_paragraphs = len([part for part in new_text.split("\n\n") if part.strip()])
        records.append(
            {
                "section_id": section_id,
                "m11_character_count": len(old_text),
                "m12_character_count": len(new_text),
                "m11_block_count": old_paragraphs,
                "m12_block_count": new_paragraphs,
                "change": change_notes[section_id],
            }
        )
        old_cell = old_text.replace("|", "\\|").replace("\n\n", "<br><br>").replace("\n", "<br>")
        new_cell = new_text.replace("|", "\\|").replace("\n\n", "<br><br>").replace("\n", "<br>")
        lines.append(
            f"| {section_id} | {old_cell} | {new_cell} | {change_notes[section_id]} |"
        )
    lines.append("")
    atomic_write_text(OUTPUT / "M11_vs_M12同节对比.md", "\n".join(lines))
    report = {"schema_version": "1.0", "status": "pass", "records": records}
    atomic_write_json(OUTPUT / "M11_vs_M12同节对比.json", report)
    return report


def _playbook_reports() -> tuple[dict[str, Any], dict[str, Any]]:
    qc = audit_complete_playbooks(PLAYBOOK_DIR)
    if qc["status"] != "pass":
        raise RuntimeError(f"M12 playbook QC failed: {qc}")
    atomic_write_json(OUTPUT / "playbook七要素自查.json", qc)
    qc_lines = [
        "# Playbook 七要素齐全性自查",
        "",
        "| Playbook | 行数 | 权威依据 | 必答问题 | 源映射 | 数值/结论规则 | 段落结构 | 退审问题 | 特有易错点 | 退审问题条数 |",
        "|---|---:|---|---|---|---|---|---|---|---:|",
    ]
    labels = (
        "authority",
        "must_answer",
        "source_mapping",
        "numeric_and_conclusion_rules",
        "paragraph_structure",
        "common_review_issues",
        "section_specific_pitfalls",
    )
    for item in qc["records"]:
        checks = ["✓" if item["seven_elements"][label] else "缺" for label in labels]
        qc_lines.append(
            "| {filename} | {line_count} | {checks} | {review_count} |".format(
                filename=item["filename"],
                line_count=item["line_count"],
                checks=" | ".join(checks),
                review_count=item["review_issue_count"],
            )
        )
    qc_lines.append("")
    atomic_write_text(OUTPUT / "playbook七要素自查.md", "\n".join(qc_lines))

    rows = []
    for item in qc["records"]:
        filename = item["filename"]
        before = BEFORE_LINE_COUNTS.get(filename, 0)
        rows.append(
            {
                "filename": filename,
                "change_type": "deepened" if filename in BEFORE_LINE_COUNTS else "new",
                "before_line_count": before,
                "after_line_count": item["line_count"],
                "delta": item["line_count"] - before,
                "selection_reason": NEW_PLAYBOOKS.get(filename),
            }
        )
    count_report = {
        "schema_version": "1.0",
        "status": "pass",
        "user_request_count_note": "任务正文称现有九份，但显式文件清单含十份；按逐份要求处理十份。",
        "deepened_count": len(BEFORE_LINE_COUNTS),
        "new_count": len(NEW_PLAYBOOKS),
        "records": rows,
    }
    atomic_write_json(OUTPUT / "playbook行数前后对比.json", count_report)
    count_lines = [
        "# Playbook 实际行数",
        "",
        "> 任务正文称“现有 9 份”，但显式列出了 10 个文件；本轮按“逐份”要求深化全部 10 份。",
        "",
        "| Playbook | 类型 | 深化前 | 深化后 | 变化 |",
        "|---|---|---:|---:|---:|",
    ]
    for item in rows:
        count_lines.append(
            f"| {item['filename']} | {item['change_type']} | {item['before_line_count']} | {item['after_line_count']} | +{item['delta']} |"
        )
    count_lines.append("")
    atomic_write_text(OUTPUT / "playbook行数前后对比.md", "\n".join(count_lines))
    return qc, count_report


def _privacy_scan(root: Path) -> dict[str, Any]:
    forbidden = (
        "ZYG",
        "BV25072501",
        "/Users/huanglu",
        "Elidel",
        "研究者姓名",
        "机构名称",
        "SUBJECT-ID-",
    )
    findings = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "privacy_scan.json":
            continue
        if path.suffix.lower() in {".json", ".md", ".txt"}:
            text = path.read_text(encoding="utf-8")
        elif path.suffix.lower() == ".docx":
            chunks = []
            with zipfile.ZipFile(path) as package:
                for name in package.namelist():
                    if name.startswith("word/") and name.endswith(".xml"):
                        chunks.append(package.read(name).decode("utf-8", errors="ignore"))
            text = "\n".join(chunks)
        else:
            continue
        scanned += 1
        for token in forbidden:
            if token in text:
                findings.append({"artifact": path.name, "token": token})
    return {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "files_scanned": scanned,
        "findings": findings,
        "real_source_paths_written": False,
        "real_source_filenames_written": False,
        "subject_level_data_written": False,
    }


def run(real_dir: Path) -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sources = _resolve_real_sources(real_dir)
    real_before = _source_audit(sources)
    spec_before = verify_source_file_lock()
    if real_before["status"] != "pass" or spec_before["status"] != "pass":
        raise RuntimeError("M12 read-only preflight failed")

    playbook_qc, line_counts = _playbook_reports()
    facts = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
    inventory = build_authoring_inventory(route_modules(facts)["structure"]["section_plan"])
    inventory["input_file_sha256"] = {"study_facts": sha256_file(FACTS_PATH)}
    atomic_write_json(OUTPUT / "authoring_inventory.json", inventory)
    atomic_write_text(
        OUTPUT / "章节写作分层清单.md", render_authoring_inventory_markdown(inventory)
    )
    writing = _run_writing_trial(OUTPUT / "writing_trial")
    scenarios = _run_conclusion_scenarios(OUTPUT / "conclusion_scenarios")
    comparison = _comparison()

    delivery = [
        "# M12 交付说明",
        "",
        "## 已完成",
        "",
        f"- 深化现有 playbook {line_counts['deepened_count']} 份，新写 {line_counts['new_count']} 份；共 {playbook_qc['playbook_count']} 份全部达到 150 行并通过七要素门禁。",
        "- 新写六份共享覆盖 M11 清单中原先无 playbook 的全部完整起草单元；其中 SAE 叙述只固化人工边界，第 13 章只允许从已确认正文派生。",
        f"- 完整 playbook 覆盖单元仍为 {inventory['full_playbook_coverage_units']} 个，已有完整参考的单元增至 {inventory['full_units_with_existing_playbook']} 个，无参考单元为 {inventory['full_units_without_existing_playbook']} 个。",
        f"- Agent 首轮起草 {writing['section_count']} 节，首轮拦截 {writing['first_pass_rejected_section_count']} 节，重试 {writing['retry_count']} 次，最终通过 {writing['resolved_pass_count']} 节；守卫后未人工润色。",
        "- 三种安全性结论分支均实跑：无关键事件、有重要事件、关键绑定缺失；缺失分支只给数据提示，不形成判断。",
        "",
        "## 自主设计决定",
        "",
        "- 任务正文写“现有九份”，但显式列出十个文件且要求逐份处理；为避免漏项，按十份执行。",
        "- 六份新 playbook 覆盖十个单元：关键事件列表和实验室评价按同构子节共享，既保持深度，也避免复制后漂移。",
        "- §12.3.2 不以“工具写得越多”为由突破规格红线：工具可写索引和字段骨架，医学叙述内容仍由授权医学人员完成。",
        "- §13 不接入隔离写手注册表；它使用 `derived_from_confirmed_body` 范围，防止独立读取 TFL 后选择新结果。",
        "- 安全性结论 token 在普通取值守卫前由脚本确定性展开；关键值不写入规则日志，仅保留哈希和分支。",
        "- M12 样例采用首轮合规原稿，因此实际拦截为零；另有 rogue/缺绑定回归覆盖失败门槛，没有人为制造失败数字。",
        "",
        "## 未完成项及原因",
        "",
        "- 未批量处理 32 个中等深度和 48 个轻量单元，按本轮边界留给 M13/M14。",
        "- 新增的非派生 playbook 尚未全部建立 executable binding-role contract；本轮先冻结内容标准，避免以不完整绑定接入写手。",
        "- 未接外部模型 API、未自动填写 study_facts、未实现 BE 统计内容。",
        "- 未试图产出终稿质量医学文字；样例用于医学撰写人判断是否比从头写省力。",
        "",
        "## M13 前建议",
        "",
        "- 先由医学撰写人逐段评估 M12 样例的保留/改写比例，并把反馈转成可回归的段落断言。",
        "- 为 §11.3、§12.3.1、§12.3.3 和 §12.4.2 冻结 binding-role contract 后再接入隔离写手；§12.3.2 和 §13 保持特殊工作流。",
        "- 中等深度方法节可按共享证据契约分族推进，轻量事务节优先确定性模板，不把所有内容都交给模型。",
        "",
    ]
    atomic_write_text(OUTPUT / "M12交付说明.md", "\n".join(delivery))

    real_after = _source_audit(sources)
    spec_after = verify_source_file_lock()
    source_audit = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if real_before == real_after
            and spec_before == spec_after
            and real_after["status"] == "pass"
            and spec_after["status"] == "pass"
            else "blocked"
        ),
        "real_sources_before": real_before,
        "real_sources_after": real_after,
        "spec_sources_before": spec_before,
        "spec_sources_after": spec_after,
        "hashes_unchanged": real_before == real_after and spec_before == spec_after,
    }
    atomic_write_json(OUTPUT / "source_hash_audit.json", source_audit)
    privacy = _privacy_scan(OUTPUT)
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    if source_audit["status"] != "pass" or privacy["status"] != "pass":
        raise RuntimeError("M12 source-lock or privacy gate failed")

    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "playbook_qc": playbook_qc["status"],
        "complete_playbook_count": playbook_qc["playbook_count"],
        "deepened_playbook_count": line_counts["deepened_count"],
        "new_playbook_count": line_counts["new_count"],
        "full_units_without_existing_playbook": inventory["full_units_without_existing_playbook"],
        "writing_trial": {
            key: writing[key]
            for key in (
                "section_count",
                "first_pass_rejected_section_count",
                "first_pass_rejected_paragraph_count",
                "rejection_reason_distribution",
                "semantic_forced_review_distribution",
                "retry_count",
                "resolved_pass_count",
                "manual_polishing_after_guard",
            )
        },
        "conclusion_scenario_count": len(scenarios["scenarios"]),
        "comparison_section_count": len(comparison["records"]),
        "source_hash_audit": source_audit["status"],
        "privacy_scan": privacy["status"],
    }
    atomic_write_json(OUTPUT / "m12_e2e_summary.json", summary)
    return summary


if __name__ == "__main__":
    real_source_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/Users/huanglu/Desktop/未命名文件夹"
    )
    print(json.dumps(run(real_source_dir), ensure_ascii=False, indent=2))

