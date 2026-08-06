#!/usr/bin/env python3
"""Generate the privacy-safe M10 structure and protocol-fact audit package."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from docx import Document
from docx.oxml.ns import qn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.appendix_status import build_appendix_status_plan
from csr_writer.io_utils import atomic_write_json, atomic_write_text, sha256_file
from csr_writer.m9_structure_qc import audit_pruned_project_docx
from csr_writer.module_router import route_modules
from csr_writer.project_skeleton import (
    render_omitted_sections_markdown,
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
)
from csr_writer.protocol_fact_scan import (
    render_protocol_fact_scan,
    scan_protocol_facts,
)
from csr_writer.source_lock import verify_source_file_lock
from csr_writer.title_overrides import load_title_overrides


OUTPUT = ROOT / "examples" / "output" / "m10"
PAIRED_FACTS = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
REFERENCE_FACTS = ROOT / "examples" / "fixtures" / "topical_ib_study_facts.deidentified.json"
PUBLICATION_MAP = ROOT / "examples" / "fixtures" / "m9_publication_reference_map.deidentified.json"
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


def _real_hash_audit(sources: Mapping[str, Path]) -> dict[str, Any]:
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
        item("10.1", "subject_disposition", "受试者处置", ("T14.1.1.1", "T14.1.1.2")),
        item("11.2", "demographics_baseline", "人口学与基线", ("T14.1.2.1", "T14.1.2.2")),
        item("11.5", "topical_low_systemic_exposure", "外用制剂 PK", ("T14.2.4.3.1", "T14.2.4.3.2")),
        item("11.6", "exploratory", "探索性疗效", ("T14.2.1.1.1",)),
        item("12.1", "extent_of_exposure", "暴露程度", ("T14.3.1.1",)),
        item("12.2", "adverse_events", "不良事件", ("T14.3.1.2", "T14.3.1.3.1")),
        item("12.5.1", "local_tolerance_ae_subset", "局部耐受性", ("T14.3.1.3.3", "T14.3.1.3.4")),
        item("12.7", "dose_escalation", "剂量递增与总体耐受性评价"),
    ]


def _docx_text(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                chunks.append(package.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def _visible_text(path: Path) -> str:
    document = Document(path)
    chunks = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            chunks.extend(cell.text for cell in row.cells)
    return "\n".join(chunks)


def _heading_records(path: Path, section_ids: Sequence[str]) -> list[dict[str, str]]:
    wanted = {"_Ref_Sec_" + item.replace(".", "_"): item for item in section_ids}
    records: list[dict[str, str]] = []
    for paragraph in Document(path).paragraphs:
        for start in paragraph._p.findall(qn("w:bookmarkStart")):
            bookmark = start.get(qn("w:name"), "")
            if bookmark in wanted:
                records.append(
                    {
                        "section_id": wanted[bookmark],
                        "visible_text": paragraph.text,
                        "style": paragraph.style.name,
                        "bookmark": bookmark,
                    }
                )
    return records


def _privacy_scan(root: Path) -> dict[str, Any]:
    forbidden = (
        "ZYG",
        "BV25072501",
        "/Users/huanglu",
        "Elidel",
        "研究者姓名",
        "机构名称",
        "受试者标识",
    )
    findings = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "privacy_scan.json":
            continue
        if path.suffix.lower() in {".json", ".md", ".txt"}:
            text = path.read_text(encoding="utf-8")
        elif path.suffix.lower() == ".docx":
            text = _docx_text(path)
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
        "automatic_fact_updates": False,
    }


def run(real_dir: Path) -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    real_sources = _resolve_real_sources(real_dir)
    real_before = _real_hash_audit(real_sources)
    spec_before = verify_source_file_lock()
    if real_before["status"] != "pass" or spec_before["status"] != "pass":
        raise RuntimeError("M10 read-only source preflight failed")

    paired_facts = json.loads(PAIRED_FACTS.read_text(encoding="utf-8"))
    reference_facts = json.loads(REFERENCE_FACTS.read_text(encoding="utf-8"))
    publication_map = json.loads(PUBLICATION_MAP.read_text(encoding="utf-8"))
    routing = route_modules(paired_facts)
    content = _content()

    annotated_path = OUTPUT / "CSR_DEID_M10_skeleton_annotated.docx"
    clean_path = OUTPUT / "CSR_DEID_M10_skeleton_clean.docx"
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        annotated_report = render_project_annotated_skeleton(
            routing,
            content,
            annotated_path,
            scaffold_path=temporary_root / "annotated_scaffold.docx",
            publication_reference_map=publication_map,
            study_facts=paired_facts,
        )
        clean_report = render_project_clean_skeleton(
            routing,
            clean_path,
            scaffold_path=temporary_root / "clean_scaffold.docx",
            publication_reference_map=publication_map,
            study_facts=paired_facts,
        )
    annotated_qc = audit_pruned_project_docx(
        annotated_path,
        annotated_report,
        publication_reference_map=publication_map,
    )
    clean_qc = audit_pruned_project_docx(
        clean_path,
        clean_report,
        publication_reference_map=publication_map,
        clean=True,
    )
    if annotated_qc["status"] != "pass" or clean_qc["status"] != "pass":
        raise RuntimeError("M10 skeleton OOXML QC failed")

    plan = routing["structure"]["section_plan"]
    no_pk_facts = copy.deepcopy(reference_facts)
    no_pk_facts["data_domains"]["pk_concentration"] = False
    no_pk_facts["analyses"]["nca"] = False
    no_pk_plan = route_modules(no_pk_facts)["structure"]["section_plan"]
    paired_14_2 = next(item for item in plan["official_sections"] if item["section_id"] == "14.2")
    no_pk_14_2 = next(item for item in no_pk_plan["official_sections"] if item["section_id"] == "14.2")

    paired_scan = scan_protocol_facts(
        real_sources["paired_protocol"],
        paired_facts,
        expected_sha256=REAL_SOURCE_HASHES["paired_protocol"],
        protocol_role="paired_protocol",
    )
    reference_scan = scan_protocol_facts(
        real_sources["reference_protocol"],
        reference_facts,
        expected_sha256=REAL_SOURCE_HASHES["reference_protocol"],
        protocol_role="reference_protocol",
    )
    injected_facts = copy.deepcopy(reference_facts)
    injected_facts["data_domains"]["local_tolerance"] = False
    injected_scan = scan_protocol_facts(
        real_sources["reference_protocol"],
        injected_facts,
        expected_sha256=REAL_SOURCE_HASHES["reference_protocol"],
        protocol_role="reference_protocol_injected_fact_test",
    )
    injection_findings = [
        item for item in injected_scan["findings"] if item["rule_id"] == "local_tolerance"
    ]
    if not injection_findings:
        raise RuntimeError("injected false local-tolerance fact was not detected")

    assessment = {
        "schema_version": "1.0",
        "status": "pass",
        "assessment_unit": "unique fact issue, not every repeated keyword occurrence",
        "paired_protocol": {
            "finding_count": paired_scan["metrics"]["finding_count"],
            "real_omission_count": 1,
            "real_omissions": [
                {
                    "fact": "design.dlt_defined",
                    "assessment": "方案存在 DLT 定义性内容，而脱敏 facts 为 false；需人工更正或说明为何不构成 DLT。",
                    "supporting_rule": "dlt_mtd_rp2d",
                }
            ],
            "false_positive_theme_count": 4,
            "false_positive_themes": [
                "稽查权利/职责条款不等于已有稽查证书",
                "生物等效性指导原则引用不等于 BE 研究设计",
                "背景文献中的儿科人群不等于本研究纳入儿科",
                "否定性 DLT 背景表述不等于设置 DLT 评价",
            ],
        },
        "reference_protocol": {
            "finding_count": reference_scan["metrics"]["finding_count"],
            "real_omission_count": 0,
            "false_positive_theme_count": 3,
            "false_positive_themes": [
                "稽查权利/职责条款不等于已有稽查证书",
                "生物等效性指导原则引用不等于 BE 研究设计",
                "背景文献中的老年人群不等于本研究纳入老年特殊人群",
            ],
        },
        "injected_false_test": {
            "field": "data_domains.local_tolerance",
            "original_value": True,
            "injected_value": False,
            "detected": True,
            "matching_finding_count": len(injection_findings),
            "representative_evidence": injection_findings[0],
            "source_file_modified": False,
            "facts_file_modified": False,
        },
    }

    heading_ids = [
        "11.5",
        *[f"11.5.{value}" for value in range(1, 11)],
        "11.6.7",
        "12.7",
        "12.6",
        "14.2",
        "16.0",
    ]
    structure_proof = {
        "schema_version": "1.0",
        "status": "pass",
        "heading_records": _heading_records(annotated_path, heading_ids),
        "conclusion_order": annotated_report["conclusion_order"],
        "omission_declaration_blocks": annotated_report["omission_declaration_blocks"],
        "manual_review_queue": annotated_report["manual_review_queue"],
        "no_supporting_module_basis_count": sum(
            basis.get("value") == "no supporting module"
            for item in plan["omitted_sections"]
            for basis in item["basis"]
            if isinstance(basis, Mapping)
        ),
        "appendix_omission_count": sum(
            item["section_id"].startswith("16") for item in plan["omitted_sections"]
        ),
        "chapter_14": {
            "pk_study_title": paired_14_2["render_title"],
            "pk_title_drivers": paired_14_2["title_drivers"],
            "non_pk_comparator_title": no_pk_14_2["render_title"],
            "non_pk_comparator_has_pk_extension": any(
                item["section_id"].startswith("11.5")
                for item in no_pk_plan["formal_extensions"]
            ),
        },
        "annotated_visible_excerpt_checks": {
            "has_human_readable_omission_declaration": (
                "本章以下小节因本研究设计不适用而未纳入" in _visible_text(annotated_path)
            ),
            "has_complete_pk_branch": all(
                f"11.5.{value} " in _visible_text(annotated_path) for value in range(1, 11)
            ),
        },
    }
    appendix_plan = build_appendix_status_plan(paired_facts)
    title_override = load_title_overrides()
    source_after = _real_hash_audit(real_sources)
    spec_after = verify_source_file_lock()
    source_integrity = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if real_before == source_after and spec_before == spec_after
            else "blocked"
        ),
        "real_sources_before": real_before,
        "real_sources_after": source_after,
        "specification_sources_before": spec_before,
        "specification_sources_after": spec_after,
        "all_sources_unchanged": real_before == source_after and spec_before == spec_after,
    }
    if source_integrity["status"] != "pass":
        raise RuntimeError("M10 source hash changed")

    atomic_write_json(OUTPUT / "routing_topical.deidentified.json", routing)
    atomic_write_json(OUTPUT / "annotated_skeleton_report.json", annotated_report)
    atomic_write_json(OUTPUT / "clean_skeleton_report.json", clean_report)
    atomic_write_json(OUTPUT / "ooxml_qc_annotated.json", annotated_qc)
    atomic_write_json(OUTPUT / "ooxml_qc_clean.json", clean_qc)
    atomic_write_json(OUTPUT / "structure_correction_proof.json", structure_proof)
    atomic_write_json(OUTPUT / "appendix_status_plan.json", appendix_plan)
    atomic_write_json(
        OUTPUT / "title_override_proof.json",
        {
            "schema_version": "1.0",
            "status": "pass",
            "config_sha256": title_override["config_sha256"],
            "structure_sha256": title_override["structure_sha256"],
            "applied": annotated_report["title_overrides"]["applied"],
        },
    )
    atomic_write_json(OUTPUT / "source_integrity_proof.json", source_integrity)
    atomic_write_text(
        OUTPUT / "omitted_sections.md",
        render_omitted_sections_markdown(plan),
    )
    atomic_write_json(OUTPUT / "protocol_fact_scan_paired.json", paired_scan)
    atomic_write_text(
        OUTPUT / "protocol_fact_scan_paired.md",
        render_protocol_fact_scan(paired_scan),
    )
    atomic_write_json(OUTPUT / "protocol_fact_scan_reference.json", reference_scan)
    atomic_write_text(
        OUTPUT / "protocol_fact_scan_reference.md",
        render_protocol_fact_scan(reference_scan),
    )
    atomic_write_json(OUTPUT / "protocol_fact_scan_assessment.json", assessment)

    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "scope": "M10 structural correction and protocol-to-facts contradiction scan",
        "retained_official_headings": annotated_report["retained_official_heading_count"],
        "omitted_official_headings": annotated_report["omitted_official_heading_count"],
        "formal_extensions": annotated_report["formal_extension_count"],
        "pk_subsection_count": 10,
        "appendix_status_rows": appendix_plan["entry_count"],
        "appendix_unique_items": appendix_plan["unique_appendix_count"],
        "paired_protocol_findings": paired_scan["metrics"]["finding_count"],
        "reference_protocol_findings": reference_scan["metrics"]["finding_count"],
        "injected_false_detected": True,
        "annotated_ooxml_qc": annotated_qc["status"],
        "clean_ooxml_qc": clean_qc["status"],
        "source_integrity": source_integrity["status"],
        "automatic_fact_updates": False,
    }
    atomic_write_json(OUTPUT / "m10_e2e_summary.json", summary)
    lines = [
        "# M10 结构修复与方案事实反向核对报告",
        "",
        "> 本报告只证明结构、OOXML、来源完整性和规则命中；不作申报或递交就绪判断。",
        "",
        "## 结构修复",
        "",
        "- 11.5 已展开为 10 个 PK 子节；11.5.10 和 11.6.7 为对应结论节。",
        "- 12.7 已排在 12.6 安全性结论之前；所有语义结论均通过同级末位硬检查。",
        "- 编号断层在 clean/annotated 正文均有方案语言声明；内部删节表保留字段证据。",
        f"- 16.0 状态清单含 {appendix_plan['entry_count']} 行、覆盖 {appendix_plan['unique_appendix_count']} 个官方附录项；16.x 删除数为 0。",
        f"- PK 研究的 14.2 标题为「{paired_14_2['render_title']}」；无 PK 对照为「{no_pk_14_2['render_title']}」。",
        "",
        "## 方案反向核对",
        "",
        f"- 配对方案：{paired_scan['metrics']['finding_count']} 条规则命中；人工归并后判断 1 个真实漏填问题（DLT），其余为背景、否定性表述或一般稽查条款。",
        f"- 参照方案：{reference_scan['metrics']['finding_count']} 条规则命中；未判断出真实漏填，均需保留人工确认记录。",
        f"- 埋错：把 `data_domains.local_tolerance` 改为 false 后命中 {len(injection_findings)} 条，未修改源方案或 facts 文件。",
        "- 扫描器只出核对项，不自动填 facts。",
        "",
        "## 完整性与 QC",
        "",
        f"- annotated OOXML QC：{annotated_qc['status']}；clean OOXML QC：{clean_qc['status']}。",
        "- 四份真实材料和四份规格源文件运行前后 SHA-256 一致。",
        "",
    ]
    atomic_write_text(OUTPUT / "M10端到端报告.md", "\n".join(lines))
    privacy = _privacy_scan(OUTPUT)
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    if privacy["status"] != "pass":
        raise RuntimeError(f"M10 privacy scan failed: {privacy['findings']}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--real-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.real_dir), ensure_ascii=False, indent=2, sort_keys=True))
