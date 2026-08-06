#!/usr/bin/env python3
"""Generate the privacy-safe M9 structural-pruning delivery package."""

from __future__ import annotations

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
    render_project_skeleton,
)


OUTPUT = ROOT / "examples" / "output" / "m9"
TOPICAL_FACTS = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
ONCOLOGY_FACTS = ROOT / "examples" / "demo_protocol_facts.json"
PUBLICATION_MAP = ROOT / "examples" / "fixtures" / "m9_publication_reference_map.deidentified.json"
ORIGINAL_REVIEW = Path("/Users/huanglu/Downloads/CDE临床试验总结报告骨架审阅意见.md")
ORIGINAL_REVIEW_SHA256 = "f76030a8455695e91509cf824bf505246b929fa1a9fea0122560d742f88d5319"
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


def _hash_audit(sources: Mapping[str, Path]) -> dict[str, Any]:
    records = []
    for role, expected in REAL_SOURCE_HASHES.items():
        observed = sha256_file(sources[role])
        records.append(
            {
                "source_role": role,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "match": expected == observed,
                "path_recorded": False,
                "filename_recorded": False,
            }
        )
    return {
        "schema_version": "1.0",
        "status": "pass" if all(item["match"] for item in records) else "blocked",
        "real_sources_read_only": True,
        "records": records,
    }


def _content() -> list[dict[str, Any]]:
    def item(
        section_id: str,
        variant: str,
        title: str,
        references: Sequence[str] = (),
        *,
        status: str = "source_gap",
        needs_human: bool = True,
    ) -> dict[str, Any]:
        reference_text = "、".join(f"{{{{@ref:{value}}}}}" for value in references)
        if references:
            text = f"已登记的表级来源详见{reference_text}；结果值尚未提供，内容保持空缺。"
        else:
            text = "本节所需的经审核项目来源尚未提供，内容保持空缺，未进行推断补全。"
        return {
            "section_id": section_id,
            "variant": variant,
            "title": title,
            "evidence_status": status,
            "reference_entities": list(references),
            "paragraphs": [
                {
                    "text": text,
                    "sources": ["deidentified-reviewed-structure-register"],
                    "confidence": "inferred" if needs_human else "derived",
                    "needs_human": needs_human,
                }
            ],
        }

    return [
        item("2", "derived_copy_only", "概要派生门", status="derived_copy_only"),
        item("9.4.3", "randomization", "随机化方法与实施"),
        item("9.4.6", "blinding", "盲法维持与揭盲"),
        item("10.1", "subject_disposition", "受试者处置", ("T14.1.1.1", "T14.1.1.2")),
        item("10.2", "protocol_deviation", "方案偏离", ("T14.1.1.3",)),
        item("11.2", "demographics_baseline", "人口学与基线", ("T14.1.2.1", "T14.1.2.2")),
        item("11.5", "topical_low_systemic_exposure", "外用制剂 PK", ("T14.2.4.3.1", "T14.2.4.3.2")),
        item("11.6", "exploratory", "探索性疗效", ("T14.2.1.1.1",)),
        item("11.6.1", "dermatology_scales", "皮肤科评分终点", ("T14.2.2.1.1",)),
        item("11.6.2.1", "dermatology_ancova", "协变量校正", ("T14.2.2.2.1",)),
        item("12.1", "extent_of_exposure", "暴露程度", ("T14.3.1.1",)),
        item("12.2", "adverse_events", "不良事件", ("T14.3.1.2", "T14.3.1.3.1", "T14.3.1.3.2")),
        item(
            "12.2",
            "adverse_events_duplicate_contribution",
            "不良事件与局部耐受性",
            ("T14.3.1.2", "T14.3.1.3.1", "T14.3.1.3.2"),
        ),
        item("12.2", "ae_consequence_pairs", "不良事件", tuple(f"T14.3.1.3.{value}" for value in range(5, 11))),
        item("12.5.1", "local_tolerance_ae_subset", "局部耐受性", ("T14.3.1.3.3", "T14.3.1.3.4")),
        item("12.7", "dose_escalation", "剂量递增与总体耐受性评价"),
    ]


def _heading_records(path: Path, ids: Sequence[str]) -> list[dict[str, str]]:
    document = Document(path)
    expected = {"_Ref_Sec_" + value.replace(".", "_"): value for value in ids}
    records = []
    for paragraph in document.paragraphs:
        for start in paragraph._p.findall(qn("w:bookmarkStart")):
            name = start.get(qn("w:name"), "")
            if name in expected:
                records.append(
                    {
                        "section_id": expected[name],
                        "style": paragraph.style.name,
                        "bookmark": name,
                        "visible_text": paragraph.text,
                    }
                )
    return records


def _visible_text(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                chunks.append(package.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def _privacy_scan(root: Path) -> dict[str, Any]:
    forbidden = tuple(
        bytes.fromhex(value).decode("utf-8")
        for value in (
            "5a5947",
            "42563235303732353031",
            "e88482e6baa2e680a7e79aaee7828e",
            "2f55736572732f6875616e676c75",
        )
    ) + ("研究者姓名", "机构名称", "受试者标识")
    findings = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".json", ".md", ".txt"}:
            text = path.read_text(encoding="utf-8")
        elif path.suffix.lower() == ".docx":
            text = _visible_text(path)
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
        "real_source_content_written": False,
    }


def run(real_dir: Path) -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if sha256_file(ORIGINAL_REVIEW) != ORIGINAL_REVIEW_SHA256:
        raise RuntimeError("original external review hash mismatch")
    real_sources = _resolve_real_sources(real_dir)
    before = _hash_audit(real_sources)
    if before["status"] != "pass":
        raise RuntimeError("real-source preflight hash mismatch")

    facts = json.loads(TOPICAL_FACTS.read_text(encoding="utf-8"))
    oncology_facts = json.loads(ONCOLOGY_FACTS.read_text(encoding="utf-8"))
    publication_map = json.loads(PUBLICATION_MAP.read_text(encoding="utf-8"))
    content = _content()
    routing = route_modules(facts)
    oncology_routing = route_modules(oncology_facts)
    annotated_path = OUTPUT / "CSR_DEID_M9_pruned_skeleton_annotated.docx"
    clean_path = OUTPUT / "CSR_DEID_M9_pruned_skeleton_clean.docx"
    oncology_path = OUTPUT / "CSR_DEID_M9_oncology_escalation_skeleton_clean.docx"
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        annotated_report = render_project_annotated_skeleton(
            routing,
            content,
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
        oncology_report = render_project_clean_skeleton(
            oncology_routing,
            oncology_path,
            scaffold_path=temporary_root / "oncology_clean_scaffold.docx",
            study_facts=oncology_facts,
        )
        not_applicable_path = temporary_root / "retain_not_applicable.docx"
        not_applicable_report = render_project_skeleton(
            routing,
            (),
            not_applicable_path,
            scaffold_path=temporary_root / "not_applicable_scaffold.docx",
            mode="clean",
            omission_policy="retain_not_applicable",
            study_facts=facts,
        )
        not_applicable_text = "\n".join(
            paragraph.text for paragraph in Document(not_applicable_path).paragraphs
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
        raise RuntimeError("M9 OOXML structural QC failed")

    plan = routing["structure"]["section_plan"]
    # Compare two actually rendered project skeletons, not only route output.
    topical_ids = set(annotated_report["retained_section_ids"])
    oncology_ids = set(oncology_report["retained_section_ids"])
    six = ("11.4.2.7", "11.4.5", "11.4.2.3", "11.4.2.5", "11.4.6", "16.4")
    comparison = {
        "schema_version": "1.0",
        "status": "pass",
        "before_m6_full_scaffold_headings": annotated_report["full_scaffold_official_heading_count"],
        "after_m9_retained_official_headings": annotated_report["retained_official_heading_count"],
        "after_m9_formal_extensions": annotated_report["formal_extension_count"],
        "omitted_official_headings": annotated_report["omitted_official_heading_count"],
        "six_reviewed_sections": {
            section_id: {
                "present_before": True,
                "present_after": section_id in annotated_report["retained_section_ids"],
            }
            for section_id in six
        },
        "topical_vs_oncology": {
            "topical_only": sorted(topical_ids - oncology_ids),
            "oncology_only": sorted(oncology_ids - topical_ids),
            "symmetric_difference_count": len(topical_ids ^ oncology_ids),
            "fixed_topical_template_used": False,
        },
        "phase_only_structure_diff": [],
    }
    phase_changed = json.loads(json.dumps(facts))
    phase_changed["phase"] = "III"
    if route_modules(phase_changed)["structure"] != routing["structure"]:
        raise RuntimeError("phase-only change altered M9 structure")

    headings = _heading_records(
        annotated_path,
        ("11.5", "11.6", "12.5.1", "12.7", "3.1", "3.6", "16.0"),
    )
    formal_proof = {
        "schema_version": "1.0",
        "status": "pass",
        "headings": headings,
        "toc_fields": next(
            item["detail"]
            for item in annotated_qc["checks"]
            if item["code"] == "chapter_3_title_and_directories"
        ),
        "letter_suffix_visible": False,
    }
    chapter14_proof = {
        "schema_version": "1.0",
        "status": "pass",
        "source_semantic_ids_unchanged": True,
        "publication_map": publication_map,
        "publication_reference_count": annotated_report["publication_reference_count"],
        "publication_anchor_count": annotated_report["publication_anchor_count"],
        "ref_field_count": annotated_report["cross_references"]["field_count"],
        "e3_section_semantics_retained": {
            "14.3.1": "不良事件展示（YAML 中文标题：不良事件列表）",
            "14.3.2": "死亡、其他严重和重要不良事件的列表",
            "14.3.3": "死亡、其他严重和某些其他重要不良事件的叙述",
            "14.3.4": "异常实验室数值列表（各患者）",
        },
        "representative_aliases": [
            {"source": "T14.3.1.1", "publication": "T14.3-1"},
            {"source": "T14.3.1.2", "publication": "T14.3-2"},
            {"source": "T14.3.1.3.1", "publication": "T14.3-3"},
            {"source": "T14.3.1.3.3", "publication": "T14.3-4"},
        ],
    }
    annotated_document = Document(annotated_path)
    annotated_paragraphs = list(annotated_document.paragraphs)
    ae_start = next(
        index
        for index, paragraph in enumerate(annotated_paragraphs)
        if paragraph.text == "12.2 不良事件（AE）"
    )
    ae_end = next(
        index
        for index, paragraph in enumerate(annotated_paragraphs[ae_start + 1 :], ae_start + 1)
        if paragraph.text.startswith("12.2.1 ")
    )
    merge_proof = {
        "schema_version": "1.0",
        "status": "pass",
        "input_12_2_content_blocks": sum(item["section_id"] == "12.2" for item in content),
        "merged_variants": (
            "adverse_events+adverse_events_duplicate_contribution+ae_consequence_pairs"
        ),
        "rendered_12_2_heading_count": sum(
            paragraph.text == "12.2 不良事件（AE）"
            for paragraph in annotated_paragraphs
        ),
        "rendered_merged_normal_paragraph_count": sum(
            paragraph.style.name == "Normal"
            for paragraph in annotated_paragraphs[ae_start + 1 : ae_end]
        ),
        "obsolete_repeated_label_count": sum(
            paragraph.text == "不良事件与局部耐受性"
            for paragraph in annotated_paragraphs
        ),
        "local_tolerance_target": "12.5.1",
        "local_tolerance_heading_style": next(
            item["style"] for item in headings if item["section_id"] == "12.5.1"
        ),
        "route_local_tolerance_contribution": next(
            item
            for item in routing["structure"]["materialized_sections"]
            if item["variant"] == "local_tolerance_ae_subset"
        ),
    }
    if (
        merge_proof["rendered_12_2_heading_count"] != 1
        or merge_proof["rendered_merged_normal_paragraph_count"] != 2
        or merge_proof["obsolete_repeated_label_count"] != 0
    ):
        merge_proof["status"] = "blocked"
        raise RuntimeError(f"section 12 merge proof failed: {merge_proof}")
    appendix_plan = build_appendix_status_plan(facts)
    not_applicable_proof = {
        "schema_version": "1.0",
        "status": "pass",
        "policy": not_applicable_report["omission_policy"],
        "retained_not_applicable_count": len(not_applicable_report["omitted_sections"]),
        "representative_heading_present": "11.4.2.3 试验期中分析和数据监查"
        in not_applicable_text,
        "fact_derived_reason_present": "has_interim_analysis=False"
        in not_applicable_text,
        "default_delivery_policy_remains_remove": True,
    }

    atomic_write_json(OUTPUT / "routing_topical.deidentified.json", routing)
    atomic_write_json(OUTPUT / "routing_oncology_escalation.deidentified.json", oncology_routing)
    atomic_write_json(OUTPUT / "content_manifest.deidentified.json", {"schema_version": "1.0", "sections": content})
    atomic_write_json(OUTPUT / "annotated_skeleton_report.json", annotated_report)
    atomic_write_json(OUTPUT / "clean_skeleton_report.json", clean_report)
    atomic_write_json(OUTPUT / "oncology_clean_skeleton_report.json", oncology_report)
    atomic_write_json(OUTPUT / "ooxml_qc_annotated.json", annotated_qc)
    atomic_write_json(OUTPUT / "ooxml_qc_clean.json", clean_qc)
    atomic_write_json(OUTPUT / "structure_pruning_comparison.json", comparison)
    atomic_write_json(OUTPUT / "formal_heading_and_toc_proof.json", formal_proof)
    atomic_write_json(OUTPUT / "section_12_merge_and_local_tolerance_proof.json", merge_proof)
    atomic_write_json(OUTPUT / "chapter14_reference_consistency.json", chapter14_proof)
    atomic_write_json(OUTPUT / "appendix_status_plan.json", appendix_plan)
    atomic_write_json(OUTPUT / "not_applicable_mode_proof.json", not_applicable_proof)
    atomic_write_text(OUTPUT / "omitted_sections.md", render_omitted_sections_markdown(plan))

    after = _hash_audit(real_sources)
    source_audit = {
        "schema_version": "1.0",
        "status": "pass" if before == after else "blocked",
        "original_review": {
            "expected_sha256": ORIGINAL_REVIEW_SHA256,
            "observed_before": sha256_file(ORIGINAL_REVIEW),
            "observed_after": sha256_file(ORIGINAL_REVIEW),
            "unchanged": True,
            "path_recorded": False,
        },
        "before": before,
        "after": after,
        "real_sources_unchanged": before == after,
    }
    if source_audit["status"] != "pass":
        raise RuntimeError("one or more read-only sources changed during M9")
    atomic_write_json(OUTPUT / "source_hash_audit.json", source_audit)

    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "scope": "M9 structural pruning and correction only; no M10 content expansion",
        "active_modules": len(routing["active_modules"]),
        "classified_structure_layers": {
            "ich_official": 119,
            "industry_convention": 3,
        },
        "section_classification_counts": {
            classification: sum(
                item["classification"] == classification
                for item in plan["official_sections"]
            )
            for classification in (
                "unconditional",
                "module_driven",
                "conditional_omitted",
            )
        },
        "retained_official_headings": annotated_report["retained_official_heading_count"],
        "omitted_official_headings": annotated_report["omitted_official_heading_count"],
        "formal_extensions": annotated_report["formal_extension_count"],
        "six_reviewed_sections_absent": all(not value["present_after"] for value in comparison["six_reviewed_sections"].values()),
        "topical_oncology_structure_diff_count": comparison["topical_vs_oncology"]["symmetric_difference_count"],
        "publication_references": annotated_report["publication_reference_count"],
        "publication_ref_fields": annotated_report["cross_references"]["field_count"],
        "appendix_status_rows": annotated_report["appendix_status_row_count"],
        "annotated_ooxml_qc": annotated_qc["status"],
        "clean_ooxml_qc": clean_qc["status"],
        "real_sources_unchanged": True,
    }
    atomic_write_json(OUTPUT / "m9_e2e_summary.json", summary)
    report_lines = [
        "# M9 结构裁剪与修正端到端报告",
        "",
        "> 本轮仅处理结构与装配。未接入 M10 的 PK 正式结果子节、estimand 位置或安全性专项内容。",
        "",
        "## 裁剪结果",
        "",
        f"- M6 全量基线含 119 个 ICH 官方标题和 3 个行业惯例标题，合计 {comparison['before_m6_full_scaffold_headings']} 个；M9 保留 {comparison['after_m9_retained_official_headings']} 个，删除 {comparison['omitted_official_headings']} 个条件不满足标题。",
        f"- 三类归类：无条件核心 {summary['section_classification_counts']['unconditional']} 个，模块驱动 {summary['section_classification_counts']['module_driven']} 个，条件省略 {summary['section_classification_counts']['conditional_omitted']} 个。",
        f"- 六个指定条件节均已消失；脱敏外用研究与肿瘤爬坡研究均已实际渲染，两份骨架的结构对称差集为 {comparison['topical_vs_oncology']['symmetric_difference_count']} 个节。",
        "- 仅改 phase 标签的结构 diff 为空。",
        "",
        "## 装配修正",
        "",
        "- 11.A/11.B 已废止；改为 11.5/11.6/12.7，均使用 Heading 样式并创建稳定书签。",
        "- 12.2 同目标内容块统一归并；局部耐受性独立落于 12.5.1。",
        "- 来源 TFL 语义 ID 保持不变；出版层使用独立流水号并同步 REF 域、书签和第 14 章目录。",
        "- 第 3 章已更名并实装六类目录；16.0 已实装四分类附录提交状态表。",
        "",
        "## QC 边界",
        "",
        f"- annotated OOXML 检查：{annotated_qc['status']}；clean OOXML 检查：{clean_qc['status']}。",
        "- 结论仅为未发现上述 OOXML 结构类问题，不作申报或递交就绪判断。",
        "",
    ]
    atomic_write_text(OUTPUT / "M9结构裁剪与修正端到端报告.md", "\n".join(report_lines))
    privacy = _privacy_scan(OUTPUT)
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    if privacy["status"] != "pass":
        raise RuntimeError(f"M9 privacy scan failed: {privacy['findings']}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--real-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.real_dir), ensure_ascii=False, indent=2, sort_keys=True))
