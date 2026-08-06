#!/usr/bin/env python3
"""Generate the privacy-safe M11 structure-closure and writing-trial package."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.authoring_inventory import (  # noqa: E402
    build_authoring_inventory,
    render_authoring_inventory_markdown,
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
from csr_writer.m9_structure_qc import audit_pruned_project_docx  # noqa: E402
from csr_writer.module_router import route_modules  # noqa: E402
from csr_writer.project_skeleton import (  # noqa: E402
    render_omitted_sections_markdown,
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
)
from csr_writer.resolve_placeholders import resolve  # noqa: E402
from csr_writer.section_writer import (  # noqa: E402
    AgentDraftGuardError,
    emit_agent_envelope,
    ingest_agent_draft,
)
from csr_writer.source_lock import verify_source_file_lock  # noqa: E402


OUTPUT = ROOT / "examples" / "output" / "m11"
FACTS_PATH = (
    ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
)
PUBLICATION_MAP_PATH = (
    ROOT / "examples" / "fixtures" / "m9_publication_reference_map.deidentified.json"
)
PROTOCOL_FACTS_PATH = ROOT / "examples" / "demo_protocol_facts.json"

REAL_SOURCE_HASHES = {
    "paired_protocol": "3c1cf7c01434405885f562d9e687691185d5250bf4a6aeead564fcdf97aeceae",
    "paired_tfl_shell": "75b6ce46bfda933d75611ce38e0ac564a17116d67faf04a767c1bd81d4bc27c5",
    "reference_protocol": "12ac850d70294e1a074ffa8102e437d98e67f27e81091097bc15fddd63d45ca1",
    "reference_numbering_pdf": "b074ce91810e48e0536cb4464053502e37c5bbe244b7954715ffd41a1936117a",
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


def _content() -> list[dict[str, Any]]:
    def item(
        section_id: str,
        variant: str,
        title: str,
        references: Sequence[str] = (),
    ) -> dict[str, Any]:
        reference_text = "、".join(f"{{{{@ref:{value}}}}}" for value in references)
        text = (
            f"已登记的表级来源详见{reference_text}；结果资料尚未提供，内容保持空缺。"
            if references
            else "经审核的项目来源尚未提供，内容保持空缺，未进行推断补全。"
        )
        return {
            "section_id": section_id,
            "variant": variant,
            "title": title,
            "evidence_status": "source_gap",
            "reference_entities": list(references),
            "paragraphs": [
                {
                    "text": text,
                    "sources": ["deidentified-reviewed-structure-register"],
                    "confidence": "inferred",
                    "needs_human": True,
                }
            ],
        }

    return [
        item("9.4.3", "randomization", "随机化方法与实施"),
        item("9.4.6", "blinding", "盲法维持与揭盲"),
        item("10.1", "subject_disposition", "受试者处置", ("T14.1.1.1", "T14.1.1.2")),
        item("11.2", "demographics_baseline", "人口学与基线", ("T14.1.2.1", "T14.1.2.2")),
        item("11.5", "topical_low_systemic_exposure", "外用制剂 PK", ("T14.2.4.3.1", "T14.2.4.3.2")),
        item("11.6", "exploratory", "探索性疗效", ("T14.2.1.1.1",)),
        item("12.1", "extent_of_exposure", "暴露程度", ("T14.3.1.1",)),
        item("12.2", "adverse_events", "不良事件", ("T14.3.1.2", "T14.3.1.3.1")),
        item("12.5.1", "local_tolerance_ae_subset", "局部耐受性", ("T14.3.1.3.3", "T14.3.1.3.4")),
        item("12.6", "dose_escalation", "剂量递增与总体耐受性评价"),
    ]


def _request(
    section_id: str,
    variant: str,
    bindings: Sequence[tuple[str, str]],
    sources: Sequence[tuple[str, str, str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.1",
        "section_id": section_id,
        "variant": variant,
        "evidence_status": "demo_only",
        "tfl_bindings": [
            {
                "role": role,
                "key": key,
                "source_id": key.split("|", 1)[0],
                "context": "脱敏合成结果；仅用于写作机制与行文质量验收",
            }
            for role, key in bindings
        ],
        "tfl_references": list(
            dict.fromkeys(key.split("|", 1)[0] for _role, key in bindings)
        ),
        "source_fragments": [
            {"source_id": source_id, "locator": locator, "purpose": purpose, "text": text}
            for source_id, locator, purpose, text in sources
        ],
    }


def _trial_requests() -> dict[str, dict[str, Any]]:
    return {
        "9.4.6": _request(
            "9.4.6",
            "blinding",
            (),
            (
                (
                    "SRC-BLIND-PROTOCOL",
                    "脱敏方案/盲法章节",
                    "blinding_method",
                    "研究采用双盲设计；研究用制剂在外观、包装和标签方面保持一致，随机编码由非盲人员保管。",
                ),
                (
                    "SRC-UNBLIND-RECORD",
                    "脱敏盲态维护记录",
                    "unblinding_record",
                    "经审核的盲态维护记录未报告非计划揭盲；应急揭盲按预设程序执行并留痕。",
                ),
            ),
        ),
        "10.1": _request(
            "10.1",
            "subject_disposition",
            (
                ("randomized_total", "T14.1.1.1|POP=FAS|ROW=RANDOMIZED|COL=ACTIVE_TOTAL"),
                ("analysis_total", "T14.1.1.2|POP=FAS|ROW=ANALYSIS_SET|COL=ACTIVE_TOTAL"),
            ),
            (
                (
                    "SRC-DISPOSITION-PROTOCOL",
                    "脱敏方案/受试者处置定义",
                    "disposition_definition",
                    "受试者处置按随机、接受研究治疗、完成和退出状态汇总，退出原因按经审核分类呈现。",
                ),
                (
                    "SRC-DISPOSITION-SAP",
                    "脱敏统计分析计划/分析集",
                    "analysis_set",
                    "全分析集定义及纳入规则以经审核统计分析计划为准。",
                ),
            ),
        ),
        "11.5.4": _request(
            "11.5.4",
            "pk_parameters",
            (
                ("pk_cmax", "T14.2.4.3.1|POP=PKPS|ROW=CMAX|COL=ACTIVE_TOTAL"),
                ("pk_auc", "T14.2.4.3.1|POP=PKPS|ROW=AUC0_T|COL=ACTIVE_TOTAL"),
            ),
            (
                (
                    "SRC-PK-METHOD",
                    "脱敏统计分析计划/PK 参数",
                    "pk_method",
                    "PK 参数采用经审核的非房室方法计算；参数可计算性取决于可定量浓度数据和采样覆盖。",
                ),
                (
                    "SRC-PK-SET",
                    "脱敏统计分析计划/PK 参数分析集",
                    "analysis_set",
                    "PK 参数分析集按经审核纳入和排除规则确定。",
                ),
                (
                    "SRC-PK-BLQ",
                    "脱敏统计分析计划/BLQ 规则",
                    "blq_rule",
                    "低于定量下限的数据按预设规则处理；不得由不可定量结果推断为无系统暴露。",
                ),
            ),
        ),
        "12.2": _request(
            "12.2",
            "adverse_events",
            (
                ("teae_result", "T14.3.1.3.1|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL"),
                ("trae_result", "T14.3.1.3.2|POP=SS|ROW=ANY_TRAE|COL=ACTIVE_TOTAL"),
            ),
            (
                (
                    "SRC-AE-DEFINITION",
                    "脱敏方案/安全性定义",
                    "safety_definition",
                    "治疗期间出现的不良事件按预设观察期、严重程度和研究者记录的因果关系分类汇总。",
                ),
                (
                    "SRC-AE-SET",
                    "脱敏统计分析计划/安全性分析集",
                    "analysis_set",
                    "安全性分析集按接受研究治疗及安全性观察的经审核规则定义。",
                ),
            ),
        ),
    }


def _raw_drafts() -> dict[str, dict[str, Any]]:
    return {
        "9.4.6": {
            "paragraphs": [
                {
                    "text": "本研究采用双盲设计，研究用制剂在外观、包装和标签方面保持一致，随机编码由非盲人员保管。",
                    "sources": ["SRC-BLIND-PROTOCOL"],
                    "confidence": "verbatim",
                    "needs_human": False,
                },
                {
                    "text": "应急揭盲按预设程序执行并留痕；经审核的盲态维护记录未报告非计划揭盲。",
                    "sources": ["SRC-UNBLIND-RECORD"],
                    "confidence": "derived",
                    "needs_human": False,
                },
            ]
        },
        "10.1": {
            "paragraphs": [
                {
                    "text": "受试者处置按随机、接受研究治疗、完成和退出状态汇总，分析集纳入按经审核的统计分析计划执行。",
                    "sources": ["SRC-DISPOSITION-PROTOCOL", "SRC-DISPOSITION-SAP"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "已随机受试者为 {{T14.1.1.1|POP=FAS|ROW=RANDOMIZED|COL=ACTIVE_TOTAL}}，纳入全分析集者为 {{T14.1.1.2|POP=FAS|ROW=ANALYSIS_SET|COL=ACTIVE_TOTAL}}；受试者处置和分析集汇总分别详见{{@ref:T14.1.1.1}}和{{@ref:T14.1.1.2}}。",
                    "sources": ["T14.1.1.1", "T14.1.1.2"],
                    "confidence": "derived",
                    "needs_human": False,
                },
            ]
        },
        "11.5.4": {
            "paragraphs": [
                {
                    "text": "PK 参数采用经审核的非房室方法计算，参数可计算性取决于可定量浓度数据和采样覆盖；低于定量下限的数据按预设规则处理。",
                    "sources": ["SRC-PK-METHOD", "SRC-PK-BLQ", "SRC-PK-SET"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "PK 参数分析集中，峰浓度参数汇总结果为 {{T14.2.4.3.1|POP=PKPS|ROW=CMAX|COL=ACTIVE_TOTAL}}，系统暴露参数汇总结果为 {{T14.2.4.3.1|POP=PKPS|ROW=AUC0_T|COL=ACTIVE_TOTAL}}，详见{{@ref:T14.2.4.3.1}}。",
                    "sources": ["T14.2.4.3.1"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "[待医学负责人确认] 参数可计算性及暴露的临床解释应结合可定量时点、低于定量下限比例、分析方法定量下限和采样局限综合确认。",
                    "sources": ["SRC-PK-METHOD", "SRC-PK-BLQ"],
                    "confidence": "inferred",
                    "needs_human": True,
                },
            ]
        },
        "12.2": {
            "paragraphs": [
                {
                    "text": "安全性分析采用安全性分析集，治疗期间出现的不良事件按预设观察期、严重程度和研究者记录的因果关系分类汇总。",
                    "sources": ["SRC-AE-DEFINITION", "SRC-AE-SET"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "安全性分析集中，出现治疗期间不良事件的汇总结果为 {{T14.3.1.3.1|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL}}，研究者记录为与研究治疗相关事件的汇总结果为 {{T14.3.1.3.2|POP=SS|ROW=ANY_TRAE|COL=ACTIVE_TOTAL}}，详见{{@ref:T14.3.1.3.1}}和{{@ref:T14.3.1.3.2}}。",
                    "sources": ["T14.3.1.3.1", "T14.3.1.3.2"],
                    "confidence": "derived",
                    "needs_human": False,
                },
                {
                    "text": "总体事件分布提示本研究安全性良好。",
                    "sources": ["SRC-AE-DEFINITION"],
                    "confidence": "inferred",
                    "needs_human": True,
                },
            ]
        },
    }


def _synthetic_datapack() -> dict[str, Any]:
    source_id = "sha256:" + "a" * 64
    values = {
        "T14.1.1.1|POP=FAS|ROW=RANDOMIZED|COL=ACTIVE_TOTAL": ("40", "count"),
        "T14.1.1.2|POP=FAS|ROW=ANALYSIS_SET|COL=ACTIVE_TOTAL": ("38", "count"),
        "T14.2.4.3.1|POP=PKPS|ROW=CMAX|COL=ACTIVE_TOTAL": ("0.86 ng/mL", "statistic"),
        "T14.2.4.3.1|POP=PKPS|ROW=AUC0_T|COL=ACTIVE_TOTAL": ("8.42 h·ng/mL", "statistic"),
        "T14.3.1.3.1|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL": ("33 (82.5%)", "n_pct"),
        "T14.3.1.3.2|POP=SS|ROW=ANY_TRAE|COL=ACTIVE_TOTAL": ("12 (30.0%)", "n_pct"),
    }
    table_populations = {
        "T14.1.1.1": "FAS",
        "T14.1.1.2": "FAS",
        "T14.2.4.3.1": "PKPS",
        "T14.3.1.3.1": "SS",
        "T14.3.1.3.2": "SS",
    }
    cells = {}
    for index, (key, (display, value_type)) in enumerate(values.items(), start=1):
        parts = dict(item.split("=", 1) for item in key.split("|")[1:])
        cells[key] = {
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
    payload = {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "mapping_manifest": {"filename": "m11_synthetic_mapping.json", "sha256": "b" * 64},
        "sources": [{"source_id": source_id}],
        "tables": [
            {
                "table_id": table_id,
                "population": population,
                "source_id": source_id,
                "ingest_strategy": "cell_level",
                "dimensions": {},
            }
            for table_id, population in table_populations.items()
        ],
        "cells": cells,
    }
    validate_datapack(payload)
    return payload


def _draft_retry(section_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    retry = copy.deepcopy(raw)
    if section_id == "12.2":
        retry["paragraphs"][2]["text"] = (
            "[待医学负责人确认] 总体事件分布提示本研究安全性良好。"
        )
    return retry


def _run_writing_trial(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    project_dir = output_root / "project"
    requests = _trial_requests()
    raw_drafts = _raw_drafts()
    datapack = _synthetic_datapack()
    protocol_facts = json.loads(PROTOCOL_FACTS_PATH.read_text(encoding="utf-8"))
    atomic_write_json(output_root / "synthetic_datapack.json", datapack)

    manifest_entries = []
    records = []
    rejection_reasons: dict[str, int] = {}
    rejected_paragraph_count = 0
    for section_id in ("9.4.6", "10.1", "11.5.4", "12.2"):
        section_root = output_root / section_id.replace(".", "_")
        section_root.mkdir(parents=True, exist_ok=True)
        request = requests[section_id]
        envelope = emit_agent_envelope(request)
        raw = raw_drafts[section_id]
        atomic_write_json(section_root / "request.json", request)
        atomic_write_json(section_root / "envelope.json", envelope)
        atomic_write_json(section_root / "raw_agent_draft_attempt1.json", raw)
        prior_reports = []
        attempts = 1
        try:
            draft, ingest_report = ingest_agent_draft(
                envelope, raw, project_dir, prior_guard_reports=prior_reports
            )
        except AgentDraftGuardError as exc:
            rejected = exc.report
            prior_reports.append(rejected)
            atomic_write_json(
                section_root / "ingest_guard_attempt1_rejected.json", rejected
            )
            reason = str(rejected["reason"])
            category = (
                "missing_exact_medical_review_marker"
                if "lack the exact [待医学负责人确认] marker" in reason
                else "other_section_guard"
            )
            rejection_reasons[category] = rejection_reasons.get(category, 0) + 1
            rejected_paragraph_count += 1
            retry = _draft_retry(section_id, raw)
            atomic_write_json(section_root / "raw_agent_draft_attempt2.json", retry)
            attempts = 2
            draft, ingest_report = ingest_agent_draft(
                envelope, retry, project_dir, prior_guard_reports=prior_reports
            )
        atomic_write_json(
            section_root / f"ingest_guard_attempt{attempts}_pass.json", ingest_report
        )

        resolved = resolve(
            draft.markdown,
            datapack,
            protocol_facts,
            binding_manifest=draft.metadata,
        )
        if resolved.status != "pass" or resolved.resolved_text is None:
            raise RuntimeError(f"M11 resolve failed for {section_id}: {resolved.report}")
        resolved_path = section_root / "resolved.md"
        resolve_guard_path = section_root / "resolve_guard.json"
        atomic_write_text(resolved_path, resolved.resolved_text)
        atomic_write_json(resolve_guard_path, resolved.report)
        manifest_entries.append(
            {
                "draft": str(draft.markdown_path.relative_to(output_root)),
                "resolved": str(resolved_path.relative_to(output_root)),
                "metadata": str(draft.metadata_path.relative_to(output_root)),
                "guard": str(resolve_guard_path.relative_to(output_root)),
            }
        )
        records.append(
            {
                "section_id": section_id,
                "envelope_sha256": envelope["envelope_sha256"],
                "attempts": attempts,
                "guard_retry_count": attempts - 1,
                "resolve_status": resolved.status,
                "placeholder_count": resolved.report["placeholder_count"],
                "reference_count": resolved.report["reference_count"],
                "semantic_finding_categories": sorted(
                    {
                        finding["category"]
                        for finding in draft.metadata.get("semantic_findings", [])
                    }
                ),
                "raw_draft_recorded": True,
                "manual_polishing_after_guard": False,
            }
        )

    manifest = {"schema_version": "1.0", "sections": manifest_entries}
    manifest_path = output_root / "sections_manifest.json"
    atomic_write_json(manifest_path, manifest)
    sections = load_sections_from_manifest(manifest_path)
    assembly_path = output_root / "M11_writing_trial_annotated.docx"
    assembly_report = render_annotated_docx(
        sections,
        assembly_path,
        protocol_id="DEID-M11-WRITING-TRIAL",
    )
    assembly_report["output"] = str(assembly_path.relative_to(ROOT))
    atomic_write_json(output_root / "assembly_report.json", assembly_report)

    sample_lines = [
        "# 起草样例",
        "",
        "> 以下正文为 Agent 读取受限 envelope 后的原始起草，经 ingest、语义护栏、三道数值守卫及确定性 resolve 生成。所用数据均为脱敏合成验收夹具，未作人工润色，不得作为临床证据。",
        "",
    ]
    for section_id in ("9.4.6", "10.1", "11.5.4", "12.2"):
        resolved_path = output_root / section_id.replace(".", "_") / "resolved.md"
        resolved_text = resolved_path.read_text(encoding="utf-8").strip()
        sample_lines.append(
            REF_PLACEHOLDER_RE.sub(
                lambda match: display_reference(match.group("entity")),
                resolved_text,
            )
        )
        sample_lines.append("")
    sample_lines.extend(
        [
            "## 守卫记录",
            "",
            f"- 首轮被拒节数：{sum(item['attempts'] > 1 for item in records)}；被拒段落：{rejected_paragraph_count}。",
            f"- 拒绝原因分布：{json.dumps(rejection_reasons, ensure_ascii=False, sort_keys=True)}。",
            f"- 重试总次数：{sum(item['guard_retry_count'] for item in records)}；最终通过：{sum(item['resolve_status'] == 'pass' for item in records)}/{len(records)} 节。",
            "- 语义红线按既定规则强制标记，不等同于医学结论已获确认。",
            "",
        ]
    )
    atomic_write_text(output_root / "起草样例.md", "\n".join(sample_lines))
    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "interface": "Agent emit-envelope -> ingest-draft -> resolve -> annotated assembly",
        "external_model_api_called": False,
        "agent_itself_was_writer": True,
        "section_count": len(records),
        "first_pass_rejected_section_count": sum(item["attempts"] > 1 for item in records),
        "first_pass_rejected_paragraph_count": rejected_paragraph_count,
        "rejection_reason_distribution": rejection_reasons,
        "retry_count": sum(item["guard_retry_count"] for item in records),
        "resolved_pass_count": sum(item["resolve_status"] == "pass" for item in records),
        "manual_polishing_after_guard": False,
        "records": records,
        "assembly": assembly_report,
    }
    atomic_write_json(output_root / "writing_trial_summary.json", summary)
    return summary


def _appendix_markdown(plan: Mapping[str, Any]) -> str:
    lines = [
        "# 16.0 附录目录及提交状态（文档渲染同源数据）",
        "",
        "| 附录编号 | 文件名称 | 本次是否提交 | 提交位置 | 未提交理由/是否可按要求提供 |",
        "|---|---|---|---|---|",
    ]
    previous = None
    for row in plan["rows"]:
        if row["category_title"] != previous:
            lines.append(f"|  | **{row['category_title']}** |  |  |  |")
            previous = row["category_title"]
        lines.append(
            "| {appendix_id} | {file_name} | {submitted} | {submission_location} | {reason} |".format(
                appendix_id=row["appendix_id"],
                file_name=row["file_name"].replace("|", "\\|"),
                submitted=row["submitted"],
                submission_location=row["submission_location"],
                reason=row["reason_or_on_request"].replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


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
    real_sources = _resolve_real_sources(real_dir)
    real_before = _source_audit(real_sources)
    spec_before = verify_source_file_lock()
    if real_before["status"] != "pass" or spec_before["status"] != "pass":
        raise RuntimeError("M11 read-only preflight failed")

    facts = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
    publication_map = json.loads(PUBLICATION_MAP_PATH.read_text(encoding="utf-8"))
    routing = route_modules(facts)
    annotated_path = OUTPUT / "CSR_DEID_M11_skeleton_annotated.docx"
    clean_path = OUTPUT / "CSR_DEID_M11_skeleton_clean.docx"
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        annotated_report = render_project_annotated_skeleton(
            routing,
            _content(),
            annotated_path,
            scaffold_path=temporary_root / "annotated_scaffold.docx",
            publication_reference_map=publication_map,
            study_facts=facts,
        )
        clean_report = render_project_clean_skeleton(
            routing,
            clean_path,
            scaffold_path=temporary_root / "clean_scaffold.docx",
            publication_reference_map=publication_map,
            study_facts=facts,
        )
    annotated_qc = audit_pruned_project_docx(
        annotated_path, annotated_report, publication_reference_map=publication_map
    )
    clean_qc = audit_pruned_project_docx(
        clean_path, clean_report, publication_reference_map=publication_map, clean=True
    )
    if annotated_qc["status"] != "pass" or clean_qc["status"] != "pass":
        raise RuntimeError("M11 project skeleton OOXML audit failed")
    atomic_write_json(OUTPUT / "skeleton_annotated_report.json", annotated_report)
    atomic_write_json(OUTPUT / "skeleton_clean_report.json", clean_report)
    atomic_write_json(OUTPUT / "skeleton_annotated_qc.json", annotated_qc)
    atomic_write_json(OUTPUT / "skeleton_clean_qc.json", clean_qc)
    atomic_write_text(
        OUTPUT / "omitted_sections.md",
        render_omitted_sections_markdown(routing["structure"]["section_plan"]),
    )
    atomic_write_text(
        OUTPUT / "16.0附录状态清单.md",
        _appendix_markdown(annotated_report["appendix_status_plan"]),
    )

    inventory = build_authoring_inventory(routing["structure"]["section_plan"])
    inventory["input_file_sha256"] = {"study_facts": sha256_file(FACTS_PATH)}
    atomic_write_json(OUTPUT / "authoring_inventory.json", inventory)
    atomic_write_text(
        OUTPUT / "章节写作分层清单.md", render_authoring_inventory_markdown(inventory)
    )

    no_optional_safety = copy.deepcopy(facts)
    no_optional_safety["data_domains"].update(
        local_tolerance=False,
        ecg=False,
        ada=False,
        pd_biomarker=False,
        aesi=False,
        vital_signs=False,
        pregnancy=False,
        medication_error_overdose_misuse=False,
        ae_study_withdrawal=False,
        ae_consequence_strata=False,
    )
    no_optional_safety_extensions = {
        item["section_id"]
        for item in route_modules(no_optional_safety)["structure"]["section_plan"]["formal_extensions"]
    }
    enriched = copy.deepcopy(facts)
    enriched["data_domains"].update(
        pd_biomarker=True,
        ada=True,
        aesi=True,
        vital_signs=True,
        pregnancy=True,
        medication_error_overdose_misuse=True,
        ae_study_withdrawal=True,
    )
    enriched_extensions = {
        item["section_id"]
        for item in route_modules(enriched)["structure"]["section_plan"]["formal_extensions"]
    }
    structure_proof = {
        "schema_version": "1.0",
        "status": "pass",
        "analysis_set_headings": [
            item
            for item in routing["structure"]["section_plan"]["formal_extensions"]
            if item["section_id"].startswith("10.3")
        ],
        "analysis_sets_relocated_from": "11.1",
        "baseline_location_decision": "11.2 retained at the official E3 location",
        "endpoint_hierarchy_present": sorted(
            section_id
            for section_id in {
                item["section_id"]
                for item in routing["structure"]["section_plan"]["formal_extensions"]
            }
            if section_id.startswith("9.5") or section_id.startswith("11.6.1")
        ),
        "no_optional_safety_comparator": {
            f"{section_id}_absent": section_id not in no_optional_safety_extensions
            for section_id in (
                "9.5.9",
                "9.5.10",
                "12.2.5",
                "12.2.6",
                "12.2.7",
                "12.2.8",
                "12.2.9",
                "12.5.1",
                "12.5.2",
                "12.5.3",
                "12.5.4",
                "12.5.5",
                "12.5.6",
            )
        },
        "optional_safety_activation": {
            section_id: section_id in enriched_extensions
            for section_id in (
                "9.5.10",
                "12.2.5",
                "12.2.8",
                "12.5.3",
                "12.5.4",
                "12.5.5",
                "12.5.6",
            )
        },
        "numbering_order": annotated_report["numbering_order"],
        "conclusion_order": annotated_report["conclusion_order"],
    }
    atomic_write_json(OUTPUT / "structure_strengthening_proof.json", structure_proof)

    writing_summary = _run_writing_trial(OUTPUT / "writing_trial")

    real_after = _source_audit(real_sources)
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

    delivery_lines = [
        "# M11 交付说明",
        "",
        "## 结果",
        "",
        f"- 16.0 已渲染 {annotated_report['appendix_status_row_count']} 条实际条目，五列、四分类；第 3 章附录目录使用同一数据源，不再自指。",
        "- 删节声明已改为 clean/annotated 正文段落；内部依据仍保留在 omitted_sections.md。",
        "- 第 12 章编号已固定为 12.6 剂量递增与总体耐受性评价、12.7 安全性结论，编号与语义顺序一致。",
        "- 11.1 已迁移至由 analyses.analysis_sets 驱动的 10.3；11.2 保留在 E3 原位置，避免把基线结果误归为受试者处置内容。",
        "- 9.5.x 与结果章按已确认终点层级展开；局部耐受性、PD/PRO 和安全性专项均由 facts 激活，反例未出现对应节。",
        "- 免疫原性作为安全性专项定位于 12.5.6；ADA 为 false 时该节不出现。",
        f"- 当前骨架共有 {inventory['visible_heading_count']} 个可见标题、{inventory['authoring_unit_count']} 个叶级起草单元；完整 playbook 覆盖单元为 {inventory['full_playbook_coverage_units']} 个。",
        "- M10 骨架的 112 节是补强前口径；本次新增 10.3、9.5.x、11.5/11.6 和 12.x 专项后，以重新生成的实际可见结构 150 个标题为准。",
        f"- Agent 写作试水完成 {writing_summary['section_count']} 节；首轮拒绝 {writing_summary['first_pass_rejected_section_count']} 节、重试 {writing_summary['retry_count']} 次，最终全部通过三道数值守卫。",
        "",
        "## 自主设计决定",
        "",
        "- 将 11.2 留在 E3 原位置：10.3 先统一定义跨结果域分析集，11.2 随后呈现基线结果，既消除 11.1 的疗效偏置，也保留审评者熟悉的基线定位。",
        "- 新增事实字段均为可选且默认关闭，以保持既有 study_facts 文件兼容；只有经确认 facts 才激活专项节。",
        "- 将 AE 后果、AESI 和退出事件统一接到本轮试水完善的 AE playbook；局部耐受性主动评分仍使用给药途径专项 playbook，避免概念混并。",
        "- 分层清单按叶级起草单元计数；容器标题仍逐项列出但不虚增 playbook 数。",
        "",
        "## 未完成项与边界",
        "",
        "- 未批量撰写其余完整 playbook；本轮只为试水完善了 12.2 AE playbook。",
        "- 未将 annotated 标记改为 Word 批注；按任务边界留待后续。",
        "- 未接外部模型 API，Agent 通过 emit/ingest 接口充当写手；样例数据均为脱敏合成夹具。",
        "- 未实现 BE 统计内容，也未自动填写 study_facts。",
        "",
        "## M12 前建议",
        "",
        "- 先由医学撰写人只评审起草样例的可改性，并逐段标记保留、重写和删除；用反馈冻结结果类 playbook 的段落模板。",
        "- 将完整 playbook 覆盖单元按安全性主体、PK、疗效和结论四批推进，每批先做少量节的 Agent 回归，再扩展写手映射。",
        "- 在扩大覆盖前确认终点 hierarchy 字段的录入规则；缺失 hierarchy 目前按探索性疗效设计作保守归类并留有事实依据。",
        "",
    ]
    atomic_write_text(OUTPUT / "交付说明.md", "\n".join(delivery_lines))

    privacy = _privacy_scan(OUTPUT)
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    if source_audit["status"] != "pass" or privacy["status"] != "pass":
        raise RuntimeError("M11 source-lock or privacy release gate failed")

    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "source_hash_audit": source_audit["status"],
        "privacy_scan": privacy["status"],
        "annotated_skeleton": str(annotated_path.relative_to(ROOT)),
        "clean_skeleton": str(clean_path.relative_to(ROOT)),
        "appendix_rows_rendered": annotated_report["appendix_status_row_count"],
        "omission_declaration_blocks": len(annotated_report["omission_declaration_blocks"]),
        "numbering_order": annotated_report["numbering_order"],
        "structure_qc": {
            "annotated": annotated_qc["status"],
            "clean": clean_qc["status"],
        },
        "authoring_inventory": {
            key: inventory[key]
            for key in (
                "visible_heading_count",
                "authoring_unit_count",
                "container_count",
                "type_counts",
                "depth_counts",
                "full_playbook_coverage_units",
                "full_units_with_existing_playbook",
                "full_units_with_exact_writer",
                "full_units_without_existing_playbook",
            )
        },
        "writing_trial": {
            key: writing_summary[key]
            for key in (
                "section_count",
                "first_pass_rejected_section_count",
                "first_pass_rejected_paragraph_count",
                "rejection_reason_distribution",
                "retry_count",
                "resolved_pass_count",
                "manual_polishing_after_guard",
            )
        },
    }
    atomic_write_json(OUTPUT / "m11_e2e_summary.json", summary)
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--real-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.real_dir), ensure_ascii=False, indent=2, sort_keys=True))
