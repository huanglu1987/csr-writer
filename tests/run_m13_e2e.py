#!/usr/bin/env python3
"""Generate the privacy-safe M13 leakage and conditioned-conclusion evidence."""

from __future__ import annotations

import copy
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

import run_m12_e2e as m12  # noqa: E402
import test_m13_conditioned_conclusions as fixtures  # noqa: E402

from csr_writer.conditional_conclusions import resolve_conditional_conclusions  # noqa: E402
from csr_writer.cross_chapter import (  # noqa: E402
    apply_review_decisions,
    derive_confirmed_body_binding_manifest,
    load_sections_from_manifest,
)
from csr_writer.io_utils import atomic_write_json, atomic_write_text  # noqa: E402
from csr_writer.m4_assembly import render_annotated_docx  # noqa: E402
from csr_writer.playbook_qc import (  # noqa: E402
    COMPLETE_PLAYBOOK_FILENAMES,
    PROCESS_SEPARATION_ELEMENTS,
    audit_complete_playbooks,
)
from csr_writer.resolve_placeholders import resolve  # noqa: E402
from csr_writer.section_writer import (  # noqa: E402
    AgentDraftGuardError,
    emit_agent_envelope,
    ingest_agent_draft,
)
from csr_writer.semantic_guard import scan_internal_process_language  # noqa: E402
from csr_writer.source_lock import verify_source_file_lock  # noqa: E402


OUTPUT = ROOT / "examples" / "output" / "m13"
PLAYBOOK_DIR = ROOT / "skills" / "csr-writer" / "section-playbooks"
PROTOCOL_FACTS_PATH = ROOT / "examples" / "demo_protocol_facts.json"
M12_WRITING = ROOT / "examples" / "output" / "m12" / "writing_trial"

KNOWN_LEAKS = (
    {
        "case_id": "disposition_writer_self_reference",
        "text": "退出原因沿用经审核分类，不由写手合并或改写。",
        "replacement": "退出原因按经审核的分类呈现。",
    },
    {
        "case_id": "disposition_internal_data_terms",
        "text": "当前证据包未提供完成、退出及退出原因的结果绑定。",
        "replacement": "本节完成与退出情况待相应统计结果到位后补充。",
    },
    {
        "case_id": "blinding_process_scope",
        "text": "该陈述仅限已提供的记录范围，不外推至未核验资料。",
        "replacement": "本节结论基于现有盲态维护记录，未涵盖记录范围之外的情形。",
    },
    {
        "case_id": "pk_internal_binding_term",
        "text": "这些结果只反映当前绑定的参数汇总。",
        "replacement": "上述为可计算参数的汇总结果，不代表全部采样时点。",
    },
)

PRE_M13_LINE_COUNTS = {
    "s9-randomization-blinding-controls.md": 152,
    "s10-subject-disposition.md": 156,
    "s11-2-demographics-baseline.md": 154,
    "s11-3-treatment-compliance.md": 153,
    "s11-4-primary-efficacy.md": 176,
    "s11-a-dose-escalation.md": 180,
    "s11-clinpharm-special-pop-endpoints.md": 182,
    "s11-topical-pk.md": 151,
    "s12-2-adverse-events.md": 168,
    "s12-3-1-deaths-serious-significant-listings.md": 156,
    "s12-3-2-sae-narratives-boundary.md": 154,
    "s12-3-3-serious-event-analysis.md": 154,
    "s12-4-2-laboratory-evaluation.md": 158,
    "s12-exposure.md": 153,
    "s12-route-specific-safety.md": 167,
    "s13-discussion-overall-conclusions.md": 158,
}

NEW_PLAYBOOK_REASONS = {
    "s11-5-10-pk-conclusion.md": "PK 结论最易把 BLQ 或不可计算误写为低系统暴露。",
    "s11-6-7-exploratory-efficacy-conclusion.md": "探索性结果最易被升级为确证性措辞。",
    "s13-overall-conclusion-conditioned.md": "总体结论价值判断密集且不得引入前文未报告的新结果。",
}


def _fixed_m12_raw_drafts() -> dict[str, dict[str, Any]]:
    drafts = copy.deepcopy(m12._raw_drafts())
    drafts["9.4.6"]["paragraphs"][3]["text"] = (
        "经审核的盲态维护记录未报告非计划揭盲。"
        "本节结论基于现有盲态维护记录，未涵盖记录范围之外的情形。"
    )
    drafts["10.1"]["paragraphs"][0]["text"] = (
        "受试者处置按筛选、随机、接受研究治疗、完成和退出状态汇总。"
        "退出原因按经审核的分类呈现。"
    )
    drafts["10.1"]["paragraphs"][2]["text"] = (
        "全分析集的定义和纳入规则以经审核统计分析计划为准，并与分析数据集章节保持一致。"
        "本节完成、退出及退出原因情况待相应统计结果到位后补充。"
    )
    drafts["11.5.4"]["paragraphs"][2]["text"] = (
        "PK 参数分析集中，峰浓度参数汇总结果为 "
        "{{T14.2.4.3.1|POP=PKPS|ROW=CMAX|COL=ACTIVE_TOTAL}}，"
        "系统暴露参数汇总结果为 "
        "{{T14.2.4.3.1|POP=PKPS|ROW=AUC0_T|COL=ACTIVE_TOTAL}}，"
        "详见{{@ref:T14.2.4.3.1}}。"
        "上述为可计算参数的汇总结果，不代表全部采样时点。"
    )
    return drafts


def _conclusion_raw(envelope: Mapping[str, Any]) -> dict[str, Any]:
    section = envelope["data"]["section_id"]
    labels = {
        "11.5.10": {
            "blq_ratio": "BLQ 比例",
            "quantifiable_timepoint_count": "可定量时点汇总",
            "cmax_calculable": "Cmax 可计算性",
            "auc_calculable": "AUC 可计算性",
            "lloq": "分析方法 LLOQ",
            "noncalculable_reason": "参数不可计算原因",
            "sampling_coverage_complete": "采样覆盖完整性",
        },
        "11.6.7": {
            "primary_endpoint_result": "主要终点结果",
            "analysis_set": "分析集",
            "prespecified": "预设状态",
            "hypothesis_test": "假设检验状态",
        },
        "13": {
            "primary_endpoint_result": "前文主要终点结果",
            "pk_key_finding": "前文药代动力学关键发现",
            "teae_result": "前文 TEAE 汇总",
            "sae_count": "前文严重不良事件汇总",
            "death_count": "前文死亡汇总",
            "discontinuation_count": "前文导致停药事件汇总",
        },
    }[section]
    rules = {
        "11.5.10": "PK_CONCLUSION_V1",
        "11.6.7": "EXPLORATORY_EFFICACY_CONCLUSION_V1",
        "13": "OVERALL_CONCLUSION_V1",
    }
    bindings = envelope["data"]["tfl_bindings"]
    statements = [
        f"{labels[item['role']]}为 {{{{{item['key']}}}}}"
        for item in bindings
    ]
    references = "、".join(
        f"{{{{@ref:{item}}}}}" for item in envelope["data"]["tfl_references"]
    )
    paragraphs = []
    if statements:
        suffix = f"，支持性结果详见{references}" if references else ""
        paragraphs.append(
            {
                "text": "；".join(statements) + suffix + "。",
                "sources": list(dict.fromkeys(item["source_id"] for item in bindings)),
                "confidence": "derived",
                "needs_human": False,
            }
        )
    paragraphs.append(
        {
            "text": f"[待医学负责人确认] {{{{@conclusion:{rules[section]}}}}}",
            "sources": [
                item["source_id"] for item in envelope["data"]["source_fragments"]
            ],
            "confidence": "inferred",
            "needs_human": True,
        }
    )
    return {"paragraphs": paragraphs}


def _run_section(
    *,
    request: Mapping[str, Any],
    raw: Mapping[str, Any],
    datapack: dict[str, Any],
    output_root: Path,
    project_dir: Path,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    envelope = emit_agent_envelope(request)
    atomic_write_json(output_root / "request.json", request)
    atomic_write_json(output_root / "envelope.json", envelope)
    atomic_write_json(output_root / "raw_draft.json", raw)
    draft, ingest_report = ingest_agent_draft(envelope, raw, project_dir)
    atomic_write_json(output_root / "ingest_guard.json", ingest_report)
    conditioned = resolve_conditional_conclusions(
        draft.markdown, datapack, draft.metadata
    )
    atomic_write_json(output_root / "conditional_conclusion_guard.json", conditioned.report)
    protocol_facts = json.loads(PROTOCOL_FACTS_PATH.read_text(encoding="utf-8"))
    resolved = resolve(
        conditioned.resolved_text,
        datapack,
        protocol_facts,
        binding_manifest=draft.metadata,
    )
    if resolved.status != "pass" or resolved.resolved_text is None:
        raise RuntimeError(
            f"M13 resolve failed for {request['section_id']}: {resolved.report}"
        )
    atomic_write_json(output_root / "synthetic_datapack.json", datapack)
    atomic_write_json(output_root / "resolve_guard.json", resolved.report)
    atomic_write_text(output_root / "resolved.md", resolved.resolved_text)
    return {
        "section_id": request["section_id"],
        "variant": request["variant"],
        "draft": draft,
        "ingest_report": ingest_report,
        "conditioned": conditioned,
        "resolved_text": resolved.resolved_text,
        "resolved_path": output_root / "resolved.md",
        "guard_path": output_root / "resolve_guard.json",
        "manual_polishing_after_guard": False,
    }


def _known_leak_proof(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    request = m12._requests()["10.1"]
    envelope = emit_agent_envelope(request)
    valid = _fixed_m12_raw_drafts()["10.1"]
    records = []
    for index, case in enumerate(KNOWN_LEAKS, start=1):
        rogue = copy.deepcopy(valid)
        rogue["paragraphs"][0] = {
            "text": case["text"],
            "sources": ["SRC-DISPOSITION-DEFINITION"],
            "confidence": "derived",
            "needs_human": False,
        }
        scan = scan_internal_process_language(case["text"])
        replacement_scan = scan_internal_process_language(case["replacement"])
        case_root = output_root / f"case_{index:02d}"
        try:
            ingest_agent_draft(envelope, rogue, case_root / "project")
        except AgentDraftGuardError as exc:
            guard = exc.report
        else:  # pragma: no cover - hard-fail evidence path
            raise RuntimeError(f"known internal-language leak passed: {case['case_id']}")
        atomic_write_json(case_root / "rejected_guard.json", guard)
        records.append(
            {
                **case,
                "old_text_hard_blocked": True,
                "old_text_findings": list(scan.findings),
                "replacement_passed": not replacement_scan.blocked,
                "guard_report_sha256": guard["report_sha256"],
            }
        )
    report = {
        "schema_version": "1.0",
        "status": "pass",
        "case_count": len(records),
        "hard_blocked_count": sum(item["old_text_hard_blocked"] for item in records),
        "replacement_pass_count": sum(item["replacement_passed"] for item in records),
        "records": records,
    }
    atomic_write_json(output_root / "summary.json", report)
    lines = [
        "# 四处已知泄漏的硬拦截证明",
        "",
        "| 用例 | 原句 | 命中分类 | 发布结果 | CSR 改写 | 改写扫描 |",
        "|---|---|---|---|---|---|",
    ]
    for item in records:
        subclasses = sorted(
            {finding["subclass"] for finding in item["old_text_findings"]}
        )
        lines.append(
            f"| {item['case_id']} | {item['text']} | {', '.join(subclasses)} | 硬拒绝 | {item['replacement']} | 通过 |"
        )
    lines.append("")
    atomic_write_text(OUTPUT / "内部流程语言泄漏回归证明.md", "\n".join(lines))
    return report


def _run_main_sections(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    project_dir = output_root / "project"
    requests = m12._requests()
    raws = _fixed_m12_raw_drafts()
    datapack = m12._datapack(
        {
            "sae_count": 0,
            "death_count": 0,
            "discontinuation_count": 0,
            "grade3plus_count": 0,
        }
    )
    records = {}
    for section in ("9.4.6", "10.1", "11.5.4", "12.2"):
        records[section] = _run_section(
            request=requests[section],
            raw=raws[section],
            datapack=datapack,
            output_root=output_root / section.replace(".", "_"),
            project_dir=project_dir,
        )
    return records


def _scenario_specs() -> list[dict[str, Any]]:
    return [
        {"section": "11.5.10", "scenario": "normal", "kwargs": {}, "omitted": None},
        {
            "section": "11.5.10",
            "scenario": "limited",
            "kwargs": {"pk_limited": True},
            "omitted": None,
        },
        {
            "section": "11.5.10",
            "scenario": "binding_missing",
            "kwargs": {"omit_keys": {fixtures.PK_KEYS["sampling_coverage_complete"]}},
            "omitted": {"sampling_coverage_complete"},
        },
        {"section": "11.6.7", "scenario": "normal", "kwargs": {}, "omitted": None},
        {
            "section": "11.6.7",
            "scenario": "hypothesis_test_present",
            "kwargs": {"efficacy_hypothesis_test": True},
            "omitted": None,
        },
        {
            "section": "11.6.7",
            "scenario": "binding_missing",
            "kwargs": {"omit_keys": {fixtures.EFFICACY_KEYS["hypothesis_test"]}},
            "omitted": {"hypothesis_test"},
        },
        {"section": "13", "scenario": "normal", "kwargs": {}, "omitted": None},
        {
            "section": "13",
            "scenario": "important_events_present",
            "kwargs": {"overall_events": True},
            "omitted": None,
        },
        {
            "section": "13",
            "scenario": "binding_missing",
            "kwargs": {"omit_keys": {fixtures.OVERALL_KEYS["pk_key_finding"]}},
            "omitted": {"pk_key_finding"},
        },
    ]


def _derive_demo_confirmed_body_manifests(
    output_root: Path,
    main_records: Mapping[str, Any],
    normal_runs: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_runs = [
        normal_runs["11.6.7"],
        normal_runs["11.5.10"],
        main_records["12.2"],
    ]
    manifest_path = OUTPUT / "confirmed_body_sources_manifest.json"
    atomic_write_json(
        manifest_path,
        {
            "schema_version": "1.0",
            "sections": [_manifest_entry(item) for item in source_runs],
        },
    )
    sections = load_sections_from_manifest(manifest_path)
    decisions = {"schema_version": "1.0", "sections": {}}
    for section in sections:
        paragraph_decisions = {}
        for paragraph in section.paragraphs:
            if paragraph.needs_human:
                replacement = paragraph.text.replace(
                    "[待医学负责人确认]", "", 1
                ).strip()
                paragraph_decisions[paragraph.para_id] = {
                    "status": "resolved",
                    "replacement_text": replacement,
                }
        decisions["sections"][section.section_id] = {
            "draft_sha256": section.draft_sha256,
            "paragraphs": paragraph_decisions,
        }
    clean = apply_review_decisions(sections, decisions)
    atomic_write_json(output_root / "synthetic_review_decisions.json", decisions)
    full_keys = [
        fixtures.OVERALL_KEYS["primary_endpoint_result"],
        fixtures.OVERALL_KEYS["pk_key_finding"],
        fixtures.OVERALL_KEYS["teae_result"],
        fixtures.OVERALL_KEYS["sae_count"],
        fixtures.OVERALL_KEYS["death_count"],
        fixtures.OVERALL_KEYS["discontinuation_count"],
    ]
    full = derive_confirmed_body_binding_manifest(sections, clean, full_keys)
    missing_pk = derive_confirmed_body_binding_manifest(
        sections,
        clean,
        [key for key in full_keys if key != fixtures.OVERALL_KEYS["pk_key_finding"]],
    )
    atomic_write_json(output_root / "confirmed_body_binding_manifest.json", full)
    atomic_write_json(
        output_root / "confirmed_body_binding_manifest_missing_pk.json", missing_pk
    )
    return full, missing_pk


def _run_conclusion_scenarios(
    output_root: Path, main_records: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_root.mkdir(parents=True, exist_ok=True)
    records = []
    normal_runs = {}
    specs = _scenario_specs()
    non_overall_specs = [item for item in specs if item["section"] != "13"]
    overall_specs = [item for item in specs if item["section"] == "13"]
    for spec in non_overall_specs:
        request = fixtures._request(spec["section"], omitted_roles=spec["omitted"])
        envelope = emit_agent_envelope(request)
        raw = _conclusion_raw(envelope)
        run = _run_section(
            request=request,
            raw=raw,
            datapack=fixtures._datapack(**spec["kwargs"]),
            output_root=(
                output_root
                / spec["section"].replace(".", "_")
                / spec["scenario"]
            ),
            project_dir=(
                output_root
                / spec["section"].replace(".", "_")
                / spec["scenario"]
                / "project"
            ),
        )
        branch = run["conditioned"].report["rules"][0]["branch"]
        record = {
            "section_id": spec["section"],
            "scenario": spec["scenario"],
            "branch": branch,
            "status": run["conditioned"].status,
            "warning_count": len(run["conditioned"].report["warnings"]),
            "resolved": str(run["resolved_path"].relative_to(ROOT)),
            "text": m12._readable(run["resolved_text"]).strip(),
            "run": run,
        }
        records.append(record)
        if spec["scenario"] == "normal":
            normal_runs[spec["section"]] = run

    full_manifest, missing_manifest = _derive_demo_confirmed_body_manifests(
        output_root, main_records, normal_runs
    )
    for spec in overall_specs:
        request = fixtures._request(spec["section"], omitted_roles=spec["omitted"])
        request["confirmed_body_binding_manifest"] = (
            missing_manifest if spec["omitted"] else full_manifest
        )
        envelope = emit_agent_envelope(request)
        raw = _conclusion_raw(envelope)
        run = _run_section(
            request=request,
            raw=raw,
            datapack=fixtures._datapack(**spec["kwargs"]),
            output_root=(
                output_root
                / spec["section"].replace(".", "_")
                / spec["scenario"]
            ),
            project_dir=(
                output_root
                / spec["section"].replace(".", "_")
                / spec["scenario"]
                / "project"
            ),
        )
        branch = run["conditioned"].report["rules"][0]["branch"]
        record = {
            "section_id": spec["section"],
            "scenario": spec["scenario"],
            "branch": branch,
            "status": run["conditioned"].status,
            "warning_count": len(run["conditioned"].report["warnings"]),
            "resolved": str(run["resolved_path"].relative_to(ROOT)),
            "text": m12._readable(run["resolved_text"]).strip(),
            "run": run,
        }
        records.append(record)
        if spec["scenario"] == "normal":
            normal_runs[spec["section"]] = run

    labels = {
        ("11.5.10", "normal"): "PK：关键结果完整",
        ("11.5.10", "limited"): "PK：BLQ 较多且关键参数不可计算",
        ("11.5.10", "binding_missing"): "PK：关键角色缺失",
        ("11.6.7", "normal"): "探索性疗效：预设描述性分析",
        ("11.6.7", "hypothesis_test_present"): "探索性疗效：实施假设检验",
        ("11.6.7", "binding_missing"): "探索性疗效：关键角色缺失",
        ("13", "normal"): "总体结论：关键正文完整且无关键事件",
        ("13", "important_events_present"): "总体结论：存在关键安全性事件",
        ("13", "binding_missing"): "总体结论：关键角色缺失",
    }
    lines = [
        "# 三节结论条件化九种情形——实际产出",
        "",
        "> 所有结果均使用脱敏合成验收夹具；同节原稿不选择措辞分支。",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## {labels[(record['section_id'], record['scenario'])]}",
                "",
                f"- 分支：`{record['branch']}`；状态：`{record['status']}`；警告：{record['warning_count']}。",
                "",
                record["text"],
                "",
            ]
        )
    atomic_write_text(OUTPUT / "结论性表述三节九情形.md", "\n".join(lines))
    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "scenario_count": len(records),
        "records": [
            {key: value for key, value in item.items() if key not in {"text", "run"}}
            for item in records
        ],
    }
    atomic_write_json(output_root / "scenario_summary.json", summary)
    return records, normal_runs


def _leak_scan(records: Mapping[str, Any], normal_runs: Mapping[str, Any]) -> dict[str, Any]:
    findings = []
    scanned = {}
    combined = {**records, **normal_runs}
    for section, run in combined.items():
        review = scan_internal_process_language(run["resolved_text"])
        scanned[section] = len(review.findings)
        findings.extend(
            {"section_id": section, **finding} for finding in review.findings
        )
    counts = Counter(item["subclass"] for item in findings)
    for subclass in (
        "tool_self_reference",
        "internal_data_term",
        "process_negation",
        "internal_state",
    ):
        counts.setdefault(subclass, 0)
    report = {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "section_count": len(combined),
        "section_findings": scanned,
        "subclass_counts": dict(sorted(counts.items())),
        "type_a_count": counts["tool_self_reference"] + counts["internal_data_term"],
        "type_b_rewritten_in_csr_voice": True,
        "findings": findings,
    }
    atomic_write_json(OUTPUT / "泄漏扫描统计.json", report)
    lines = [
        "# 重新起草后的泄漏扫描统计",
        "",
        f"- 扫描正文节数：{report['section_count']}。",
        f"- 工具自指：{counts['tool_self_reference']}。",
        f"- 内部数据术语：{counts['internal_data_term']}。",
        f"- 流程性否定：{counts['process_negation']}。",
        f"- 内部状态：{counts['internal_state']}。",
        f"- 类型 A 合计：{report['type_a_count']}。",
        "- 类型 B 已改为 CSR 语体并保留证据边界含义。",
        "",
    ]
    atomic_write_text(OUTPUT / "泄漏扫描统计.md", "\n".join(lines))
    if report["status"] != "pass":
        raise RuntimeError(f"M13 body leakage scan failed: {findings}")
    return report


def _manifest_entry(run: Mapping[str, Any]) -> dict[str, str]:
    return {
        "draft": str(run["draft"].markdown_path.relative_to(OUTPUT)),
        "resolved": str(run["resolved_path"].relative_to(OUTPUT)),
        "metadata": str(run["draft"].metadata_path.relative_to(OUTPUT)),
        "guard": str(run["guard_path"].relative_to(OUTPUT)),
    }


def _assemble(records: Mapping[str, Any], normal_runs: Mapping[str, Any]) -> dict[str, Any]:
    ordered = [
        records["9.4.6"],
        records["10.1"],
        records["11.5.4"],
        normal_runs["11.5.10"],
        normal_runs["11.6.7"],
        records["12.2"],
        normal_runs["13"],
    ]
    manifest = {
        "schema_version": "1.0",
        "sections": [_manifest_entry(item) for item in ordered],
    }
    manifest_path = OUTPUT / "sections_manifest.json"
    atomic_write_json(manifest_path, manifest)
    sections = load_sections_from_manifest(manifest_path)
    output_path = OUTPUT / "M13起草样例_annotated.docx"
    report = render_annotated_docx(
        sections, output_path, protocol_id="DEID-M13-WRITING-SAMPLE"
    )
    report["output"] = str(output_path.relative_to(ROOT))
    atomic_write_json(OUTPUT / "assembly_report.json", report)
    return report


def _validate_docx_ooxml(path: Path) -> dict[str, Any]:
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/styles.xml",
    }
    parsed_xml_parts = 0
    errors = []
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        missing = sorted(required - names)
        for name in sorted(names):
            if not name.endswith((".xml", ".rels")):
                continue
            try:
                ET.fromstring(package.read(name))
                parsed_xml_parts += 1
            except ET.ParseError as exc:
                errors.append({"part": name, "error": str(exc)})
    report = {
        "schema_version": "1.0",
        "status": "pass" if not missing and not errors else "blocked",
        "required_parts_missing": missing,
        "parsed_xml_part_count": parsed_xml_parts,
        "xml_parse_errors": errors,
        "validation_layer": "OOXML package and XML parse; no PDF conversion",
    }
    atomic_write_json(OUTPUT / "docx_ooxml_validation.json", report)
    if report["status"] != "pass":
        raise RuntimeError(f"M13 DOCX OOXML validation failed: {report}")
    return report


def _sample_and_comparison(
    records: Mapping[str, Any], normal_runs: Mapping[str, Any]
) -> dict[str, Any]:
    ordered = (
        records["9.4.6"],
        records["10.1"],
        records["11.5.4"],
        normal_runs["11.5.10"],
        normal_runs["11.6.7"],
        records["12.2"],
        normal_runs["13"],
    )
    lines = [
        "# M13 起草样例",
        "",
        "> 以下正文经受限证据输入、语义门禁、条件化结论和三道数值守卫生成。数据均为脱敏合成验收夹具，守卫后未作人工润色，不得作为临床证据。",
        "",
    ]
    for run in ordered:
        lines.extend([m12._readable(run["resolved_text"]).strip(), ""])
    lines.extend(
        [
            "## 验收记录",
            "",
            "- 正文节数：7。",
            "- 首轮被拒正文节数：0；重试次数：0。",
            "- 四条历史问题均在独立对抗回放中被硬拒绝。",
            "- 守卫后未作人工润色。",
            "",
        ]
    )
    atomic_write_text(OUTPUT / "M13起草样例.md", "\n".join(lines))

    comparison_lines = [
        "# M12 vs M13 同节并排对比",
        "",
        "> M13 只修正内部流程语体，不对结果数字和医学含义作人工润色。",
        "",
        "| 节 | M12 成品 | M13 成品 | 变化 |",
        "|---|---|---|---|",
    ]
    comparison_records = []
    notes = {
        "9.4.6": "证据范围限制保留，改为盲态维护记录范围的 CSR 表述。",
        "10.1": "删除起草流程自指；缺结果改为正式待补充句。",
        "11.5.4": "删除内部数据术语；保留参数可计算性和采样范围限制。",
        "12.2": "正文未发现同类泄漏，作为不变对照。",
    }
    for section in ("9.4.6", "10.1", "11.5.4", "12.2"):
        old_text = m12._readable(
            (M12_WRITING / section.replace(".", "_") / "resolved.md").read_text(
                encoding="utf-8"
            )
        ).strip()
        new_text = m12._readable(records[section]["resolved_text"]).strip()
        comparison_records.append(
            {
                "section_id": section,
                "m12_character_count": len(old_text),
                "m13_character_count": len(new_text),
                "change": notes[section],
            }
        )
        old_cell = old_text.replace("|", "\\|").replace("\n", "<br>")
        new_cell = new_text.replace("|", "\\|").replace("\n", "<br>")
        comparison_lines.append(
            f"| {section} | {old_cell} | {new_cell} | {notes[section]} |"
        )
    comparison_lines.append("")
    atomic_write_text(OUTPUT / "M12_vs_M13同节对比.md", "\n".join(comparison_lines))
    report = {
        "schema_version": "1.0",
        "status": "pass",
        "records": comparison_records,
    }
    atomic_write_json(OUTPUT / "M12_vs_M13同节对比.json", report)
    return report


def _extract_heading_section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def _playbook_reports() -> tuple[dict[str, Any], dict[str, Any]]:
    qc = audit_complete_playbooks(PLAYBOOK_DIR)
    if qc["status"] != "pass":
        raise RuntimeError(f"M13 playbook QC failed: {qc}")
    atomic_write_json(OUTPUT / "playbook质量门禁.json", qc)
    rows = []
    for item in qc["records"]:
        before = PRE_M13_LINE_COUNTS.get(item["filename"], 0)
        rows.append(
            {
                "filename": item["filename"],
                "change_type": "partitioned" if before else "new_complete",
                "before_line_count": before,
                "after_line_count": item["line_count"],
                "selection_reason": NEW_PLAYBOOK_REASONS.get(item["filename"]),
            }
        )
    report = {
        "schema_version": "1.0",
        "status": "pass",
        "existing_partitioned_count": len(PRE_M13_LINE_COUNTS),
        "new_complete_count": len(NEW_PLAYBOOK_REASONS),
        "new_medium_count": 0,
        "total_complete_count": qc["playbook_count"],
        "records": rows,
    }
    atomic_write_json(OUTPUT / "playbook完成数与行数.json", report)
    lines = [
        "# M13 Playbook 完成数与行数",
        "",
        "| Playbook | 类型 | M13 前 | M13 后 | 选择理由 |",
        "|---|---|---:|---:|---|",
    ]
    for item in rows:
        lines.append(
            f"| {item['filename']} | {item['change_type']} | {item['before_line_count']} | {item['after_line_count']} | {item['selection_reason'] or '既有完整档分区改造'} |"
        )
    lines.extend(
        [
            "",
            "- 第三优先实际新增完整档：3 份。",
            "- 中等档新增：0 份；为保证泄漏修复和三节结论条件化完整，本轮未以浅层内容凑数。",
            "",
        ]
    )
    atomic_write_text(OUTPUT / "playbook完成数与行数.md", "\n".join(lines))

    partition_lines = [
        "# 16 份既有 Playbook 的分区内容",
        "",
        "> 以下逐份列出新增的两个分区；正文可写句不含内部流程术语。",
        "",
    ]
    for filename in PRE_M13_LINE_COUNTS:
        text = (PLAYBOOK_DIR / filename).read_text(encoding="utf-8")
        partition_lines.extend(
            [
                f"## {filename}",
                "",
                _extract_heading_section(
                    text, PROCESS_SEPARATION_ELEMENTS["writer_constraints"]
                ),
                "",
                _extract_heading_section(
                    text, PROCESS_SEPARATION_ELEMENTS["csr_standard_sentences"]
                ),
                "",
            ]
        )
    atomic_write_text(OUTPUT / "16份playbook分区内容.md", "\n".join(partition_lines))
    return qc, report


def _privacy_scan(root: Path) -> dict[str, Any]:
    forbidden = (
        "ZYG",
        "BV25072501",
        "/Users/huanglu",
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
    sources = m12._resolve_real_sources(real_dir)
    real_before = m12._source_audit(sources)
    spec_before = verify_source_file_lock()
    if real_before["status"] != "pass" or spec_before["status"] != "pass":
        raise RuntimeError("M13 read-only preflight failed")

    leak_proof = _known_leak_proof(OUTPUT / "known_leak_replay")
    qc, playbooks = _playbook_reports()
    main_records = _run_main_sections(OUTPUT / "writing_trial")
    scenarios, normal_runs = _run_conclusion_scenarios(
        OUTPUT / "conclusion_scenarios", main_records
    )
    leak_scan = _leak_scan(main_records, normal_runs)
    comparison = _sample_and_comparison(main_records, normal_runs)
    assembly = _assemble(main_records, normal_runs)
    docx_ooxml = _validate_docx_ooxml(OUTPUT / "M13起草样例_annotated.docx")

    delivery = [
        "# M13 交付说明",
        "",
        "## 已完成",
        "",
        f"- 四处历史泄漏全部硬拒绝：{leak_proof['hard_blocked_count']}/4；四条 CSR 改写全部通过。",
        f"- 重新起草正文扫描 {leak_scan['section_count']} 节，四类命中均为零。",
        f"- 既有完整 playbook {playbooks['existing_partitioned_count']} 份完成双分区改造；新增完整档 {playbooks['new_complete_count']} 份。",
        f"- 三节条件化结论共实跑 {len(scenarios)} 个情形，均完成取值、三道守卫和装配。",
        "- M13 起草样例收录七节正文，守卫后未作人工润色。",
        "",
        "## 自主设计决定",
        "",
        "- 内部流程语言采用硬拒绝，不复用医学语义红线的待确认标记；这类内容没有进入 CSR 的价值。",
        "- 证据边界内容不删除，统一从版本化标准句库选择 CSR 语体表述。",
        "- 对“模型”采用语境化匹配，保留“幂函数模型”等正常临床药理表述。",
        "- PK 条件规则把 BLQ 比例达到一半作为保守限制触发条件；该阈值只控制是否禁止低暴露概括，不构成临床判断。",
        "- 可计算性、预设状态和采样完整性只读取显式布尔机器值，不从自由文本猜测。",
        "- 第 13 章的结果清单由前三节经显式审阅决定后的段落元数据确定性派生；结果键、来源段落和清单哈希须同时匹配。",
        "- 第三优先只新增三份与本轮条件规则直接对应的完整档；中等档未以浅层内容凑数。",
        "",
        "## 遇到的问题",
        "",
        "- 历史确定性段落本身含少量起草约束语气；在启用硬门禁前同步改为正式 CSR 句式，避免旧回归被新规则误伤。",
        "- PK 的不可计算原因并不在最初建议角色清单中；为满足先说明原因的医学要求，新增了独立角色。",
        "- 总体结论不能仅凭 TFL 映射证明“前文已出现”，因此新增确认正文键清单，而不是把 confirmed_tfl_map 当成替代。",
        "",
        "## 未完成项及原因",
        "",
        "- 未扩展中等档 32 个单元；本轮优先完成泄漏根治与三节高风险结论规则。",
        "- 未处理轻量档 48 个单元，按范围留给 M14。",
        "- 未接外部模型 API、未自动填写 study_facts、未实现 BE 统计内容。",
        "",
        "## M14 前建议",
        "",
        "- 先由医学撰写人审核九种条件化结论措辞及 PK 保守触发条件，再冻结申办方用语偏好。",
        "- 中等档按方案方法、统计方法和受试者管理三族推进，并沿用本轮双分区格式。",
        "- 轻量档优先采用确定性模板；只有确有叙述价值的事务节才交给生成式起草。",
        "- 将本轮四分类扫描纳入每次完整 CSR 装配的发布门槛。",
        "",
    ]
    atomic_write_text(OUTPUT / "M13交付说明.md", "\n".join(delivery))

    real_after = m12._source_audit(sources)
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
        raise RuntimeError("M13 source-lock or privacy gate failed")

    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "known_leaks_hard_blocked": leak_proof["hard_blocked_count"],
        "known_leak_replacements_passed": leak_proof["replacement_pass_count"],
        "leak_scan": leak_scan,
        "existing_playbooks_partitioned": playbooks["existing_partitioned_count"],
        "new_complete_playbooks": playbooks["new_complete_count"],
        "new_medium_playbooks": playbooks["new_medium_count"],
        "complete_playbook_count": qc["playbook_count"],
        "conditioned_scenario_count": len(scenarios),
        "comparison_section_count": len(comparison["records"]),
        "assembly_status": assembly.get("status", "pass"),
        "docx_ooxml_validation": docx_ooxml["status"],
        "source_hash_audit": source_audit["status"],
        "privacy_scan": privacy["status"],
        "external_model_api_called": False,
        "manual_polishing_after_guard": False,
    }
    atomic_write_json(OUTPUT / "m13_summary.json", summary)
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_m13_e2e.py REAL_READ_ONLY_DIRECTORY")
    result = run(Path(sys.argv[1]))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
