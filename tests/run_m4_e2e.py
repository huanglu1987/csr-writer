#!/usr/bin/env python3
"""Rebuild the checked-in M4 acceptance artifacts from synthetic fixtures only."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.cross_chapter import (
    apply_review_decisions,
    build_cross_chapter_report,
    derive_synopsis_manifest,
    load_sections_from_manifest,
    validate_analysis_set_consistency,
    validate_claim_ledger,
    verify_synopsis_copy,
)
from csr_writer.group_semantics import GroupSemanticError, validate_group_semantics
from csr_writer.io_utils import (
    atomic_write_json,
    atomic_write_text_bundle,
    load_json_unique,
)
from csr_writer.m4_assembly import (
    CleanOutputBlocked,
    render_annotated_docx,
    render_clean_and_synopsis,
)
from csr_writer.m4_qc import build_qc_report
from csr_writer.number_guard import CompiledWhitelist, find_bare_numbers
from csr_writer.section_writer import DraftParagraph, draft_section
from csr_writer.section_writer import SectionWriterError
from csr_writer.semantic_guard import review_semantic_redlines
from csr_writer.traceability import analyze_tfl_references, render_unreferenced_tfl


M4 = ROOT / "examples" / "m4"
OUTPUT = ROOT / "examples" / "output" / "m4"
MODULE_EVIDENCE = ROOT / "examples" / "output" / "m4_module_expansion_acceptance.json"
TEMPLATE = ROOT / "skills" / "csr-writer" / "assets" / "csr-template-zh.docx"


def _load(path: Path) -> Any:
    return load_json_unique(path)


def _review_decisions(sections: Sequence[Any]) -> dict[str, Any]:
    source = _load(M4 / "demo_review_replacements.json")
    replacements = source["replacements"]
    return {
        "schema_version": "1.0",
        "sections": {
            section.section_id: {
                "draft_sha256": section.draft_sha256,
                "paragraphs": {
                    paragraph.para_id: {
                        "status": "resolved",
                        "replacement_text": replacements[paragraph.para_id],
                    }
                    for paragraph in section.paragraphs
                    if paragraph.needs_human
                },
            }
            for section in sections
        },
    }


class _RogueAdapter:
    def __init__(self, text: str):
        self.text = text

    def generate(self, envelope: Mapping[str, Any]) -> Sequence[DraftParagraph]:
        bindings = envelope["data"]["tfl_bindings"]
        placeholders = " ".join(f"{{{{{item['key']}}}}}" for item in bindings)
        sources = tuple(item["source_id"] for item in envelope["data"]["source_fragments"])
        return (
            DraftParagraph(
                text=f"{self.text}{placeholders}",
                sources=sources,
                confidence="verbatim",
                needs_human=False,
            ),
        )


class _SwappedGroupAdapter:
    def generate(self, envelope: Mapping[str, Any]) -> Sequence[DraftParagraph]:
        bindings = envelope["data"]["tfl_bindings"]
        swapped = [bindings[1], bindings[0], *bindings[2:]]
        placeholders = " ".join(f"{{{{{item['key']}}}}}" for item in swapped)
        return (
            DraftParagraph(
                text=f"试验组与对照组结果依次为 {placeholders}",
                sources=(envelope["data"]["source_fragments"][0]["source_id"],),
                confidence="derived",
                needs_human=False,
            ),
        )


def _semantic_redline_evidence() -> dict[str, Any]:
    request = _load(ROOT / "examples" / "section_inputs" / "efficacy_request.json")
    cases = (
        (
            "benefit_risk",
            "综上，本品疗效确切、安全性良好，获益风险比显著优于现有治疗，建议推进 III 期。",
            "benefit_risk_conclusion",
        ),
        (
            "causality",
            "该不良事件经判断与研究药物明确相关。",
            "causal_determination",
        ),
    )
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as temporary:
        for case_id, text, expected_category in cases:
            draft = draft_section(
                request,
                Path(temporary) / case_id,
                adapter=_RogueAdapter(text),
            )
            paragraph = draft.metadata["paragraphs"][0]
            categories = sorted(
                {item["category"] for item in paragraph["semantic_findings"]}
            )
            passed = (
                "[待医学负责人确认]" in draft.markdown
                and paragraph["needs_human"] is True
                and paragraph["confidence"] == "inferred"
                and expected_category in categories
            )
            results.append(
                {
                    "id": case_id,
                    "rogue_text": text,
                    "expected_category": expected_category,
                    "marker_inserted": "[待医学负责人确认]" in draft.markdown,
                    "needs_human": paragraph["needs_human"],
                    "confidence": paragraph["confidence"],
                    "categories": categories,
                    "status": "pass" if passed else "fail",
                }
            )

    default_cases = []
    for section_id in ("11.4.7", "12.6", "13"):
        review = review_semantic_redlines(section_id, "本段为中性结论草稿。")
        default_cases.append(
            {
                "section": section_id,
                "marker_inserted": review.text.startswith("[待医学负责人确认]"),
                "categories": sorted({item["category"] for item in review.findings}),
                "status": "pass" if review.requires_human else "fail",
            }
        )

    passed = sum(item["status"] == "pass" for item in [*results, *default_cases])
    return {
        "schema_version": "1.0",
        "evidence_status": "synthetic_adversarial_only",
        "rogue_adapter_cases": results,
        "conclusion_section_defaults": default_cases,
        "summary": {
            "total": len(results) + len(default_cases),
            "passed": passed,
            "failed": len(results) + len(default_cases) - passed,
        },
    }


def _pk_token_evidence() -> dict[str, Any]:
    strict = CompiledWhitelist(tuple(), "strict-no-numeric-whitelist")
    cases = (
        ("t_half_parameter_only", "参数 t1/2。", []),
        ("auc_parameter_only", "参数 AUC0-∞。", []),
        ("t_half_with_result", "血浆消除半衰期（t1/2）约为 12 小时。", ["12"]),
        ("auc_with_result", "AUC0-∞ 约为 345 ng·h/mL。", ["345"]),
    )
    records = []
    for case_id, text, expected in cases:
        observed = [item["token"] for item in find_bare_numbers(text, strict)]
        records.append(
            {
                "id": case_id,
                "text": text,
                "expected_tokens": expected,
                "observed_tokens": observed,
                "status": "pass" if observed == expected else "fail",
            }
        )
    return {
        "schema_version": "1.0",
        "mechanism": "lexical PK parameter masking before tokenization; not a numeric whitelist",
        "cases": records,
        "summary": {
            "total": len(records),
            "passed": sum(item["status"] == "pass" for item in records),
            "failed": sum(item["status"] != "pass" for item in records),
        },
    }


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sections = load_sections_from_manifest(M4 / "section_artifacts.json")
    decisions = _review_decisions(sections)
    atomic_write_json(OUTPUT / "demo_review_decisions.json", decisions)

    semantic = _semantic_redline_evidence()
    pk_tokens = _pk_token_evidence()
    if semantic["summary"]["failed"] or pk_tokens["summary"]["failed"]:
        raise RuntimeError("Semantic-redline or PK-token acceptance failed")
    atomic_write_json(OUTPUT / "semantic_redline_regression.json", semantic)
    atomic_write_json(OUTPUT / "pk_token_regression.json", pk_tokens)

    datapack = _load(M4 / "synthetic_datapack.json")
    metadata = _load(M4 / "section_metadata.json")
    contract = _load(M4 / "group_contract.json")
    group_pass = validate_group_semantics(datapack, metadata, contract)
    atomic_write_json(OUTPUT / "group_semantics_pass.json", group_pass)

    swapped = copy.deepcopy(metadata)
    efficacy_bindings = next(
        item["binding_manifest"] for item in swapped if item["section"] == "11.4"
    )
    experimental = next(item for item in efficacy_bindings if item["role"] == "experimental_result")
    control = next(item for item in efficacy_bindings if item["role"] == "control_result")
    experimental["key"], control["key"] = control["key"], experimental["key"]
    try:
        validate_group_semantics(datapack, swapped, contract)
    except GroupSemanticError as exc:
        swap_blocked = exc.report
    else:
        raise RuntimeError("Swapped experimental/control bindings were not blocked")
    atomic_write_json(OUTPUT / "group_semantics_swap_blocked.json", swap_blocked)

    efficacy_request = _load(
        ROOT / "examples" / "section_inputs" / "efficacy_request.json"
    )
    with tempfile.TemporaryDirectory() as temporary:
        try:
            draft_section(
                efficacy_request,
                temporary,
                adapter=_SwappedGroupAdapter(),
            )
        except SectionWriterError as exc:
            literal_swap = {
                "schema_version": "1.0",
                "status": "blocked" if "E-GROUP-BINDING-ORDER" in str(exc) else "unexpected_error",
                "code": "E-GROUP-BINDING-ORDER",
                "injection": "正文中直接对调 COL=XYZ200MG 与 COL=PBO 两个占位符",
                "message": str(exc),
            }
        else:
            raise RuntimeError("Literal placeholder swap in generated prose was not blocked")
    if literal_swap["status"] != "blocked":
        raise RuntimeError("Literal placeholder swap produced the wrong failure")
    atomic_write_json(OUTPUT / "group_placeholder_swap_blocked.json", literal_swap)

    trace = analyze_tfl_references(datapack, metadata)
    atomic_write_text_bundle(
        [
            (OUTPUT / "unreferenced_tfl.md", render_unreferenced_tfl(trace)),
            (
                OUTPUT / "unreferenced_tfl.json",
                json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
        ]
    )

    selection_payload = _load(M4 / "synopsis_selection.json")
    clean_paragraphs = apply_review_decisions(sections, decisions)
    synopsis, synopsis_manifest = derive_synopsis_manifest(
        sections, clean_paragraphs, selection_payload["selection"]
    )
    synopsis_check = verify_synopsis_copy(synopsis, synopsis_manifest)
    analysis_check = validate_analysis_set_consistency(
        _load(M4 / "analysis_set_contract.json")["records"]
    )
    claim_check = validate_claim_ledger(_load(M4 / "claim_ledger.json"))
    cross = build_cross_chapter_report(synopsis_check, analysis_check, claim_check)
    if cross["status"] != "pass":
        raise RuntimeError("Cross-chapter acceptance failed")
    atomic_write_json(OUTPUT / "cross_chapter_report.json", cross)

    annotated = render_annotated_docx(
        sections,
        OUTPUT / "CSR_DEMO_annotated.docx",
        protocol_id="DEMO-M4-001",
        template_path=TEMPLATE,
    )
    with tempfile.TemporaryDirectory() as temporary:
        blocked_root = Path(temporary)
        empty_decisions = {
            "schema_version": "1.0",
            "sections": {
                section.section_id: {
                    "draft_sha256": section.draft_sha256,
                    "paragraphs": {},
                }
                for section in sections
            },
        }
        try:
            render_clean_and_synopsis(
                sections,
                empty_decisions,
                selection_payload["selection"],
                blocked_root / "clean.docx",
                blocked_root / "synopsis.docx",
                blocked_root / "synopsis.json",
                protocol_id="DEMO-M4-001",
                template_path=TEMPLATE,
            )
        except CleanOutputBlocked as exc:
            clean_gate = {
                "schema_version": "1.0",
                "status": "pass",
                "attempt_status": "blocked",
                "reason": str(exc),
                "clean_output_created": (blocked_root / "clean.docx").exists(),
                "synopsis_output_created": (blocked_root / "synopsis.docx").exists(),
            }
        else:
            raise RuntimeError("Clean output was generated without review decisions")
    atomic_write_json(OUTPUT / "clean_gate_blocked.json", clean_gate)

    assembly = render_clean_and_synopsis(
        sections,
        decisions,
        selection_payload["selection"],
        OUTPUT / "CSR_DEMO_clean.docx",
        OUTPUT / "CSR_Synopsis_DEMO.docx",
        OUTPUT / "synopsis_manifest.json",
        protocol_id="DEMO-M4-001",
        template_path=TEMPLATE,
    )
    assembly_report = {
        "schema_version": "1.0",
        "status": "pass",
        "annotated": annotated,
        **assembly,
    }
    atomic_write_json(OUTPUT / "m4_assembly_report.json", assembly_report)

    checks = {
        "组别语义": {
            "status": "pass",
            "summary": "显式组别角色、语义列及随机化与分析集例数锚点一致。",
        },
        "跨章一致性": {
            "status": "pass",
            "summary": "Synopsis 为确认后正文逐段复制，分析集定义与受控结论台账一致。",
        },
        "读者版纯净性": {
            "status": "pass",
            "summary": "OOXML 文本层未检出待确认、内部溯源或工具过程标记。",
        },
        "反向溯源": {
            "status": "warning" if trace["unreferenced_table_count"] else "pass",
            "summary": (
                "存在有映射结果但尚未被章节引用的合成 TFL，须人工确认是否遗漏。"
                if trace["unreferenced_table_count"]
                else "未发现有映射结果但未被章节引用的 TFL。"
            ),
        },
    }
    qc_markdown, qc_machine = build_qc_report(checks)
    atomic_write_json(OUTPUT / "m4_qc_checks.json", {"schema_version": "1.0", "checks": checks})
    atomic_write_text_bundle(
        [
            (OUTPUT / "m4_qc_report.md", qc_markdown),
            (
                OUTPUT / "m4_qc_report.json",
                json.dumps(qc_machine, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
        ]
    )

    module_evidence = _load(MODULE_EVIDENCE)
    first_swap_issue = swap_blocked["issues"][0]
    summary = {
        "schema_version": "1.0",
        "scope": "M3 carry-over fixes, module expansion, and M4 only; M5 not run",
        "evidence_status": "synthetic_and_deidentified_equivalent_only",
        "semantic_redline": semantic["summary"],
        "pk_token": pk_tokens["summary"],
        "module_expansion": {
            "inventory_before": module_evidence["inventory"]["original_m3_count"],
            "inventory_after": module_evidence["inventory"]["expanded_count"],
            "topical_before": module_evidence["topical_comparison"]["original_23_intersection_count"],
            "topical_after": module_evidence["topical_comparison"]["expanded_active_count"],
            "phase_independence": module_evidence["phase_independence"]["status"],
        },
        "group_semantics": {
            "normal": group_pass["status"],
            "swapped": swap_blocked["status"],
            "literal_placeholder_swap": literal_swap["status"],
            "literal_placeholder_swap_code": literal_swap["code"],
            "swap_detection_code": first_swap_issue["code"],
            "expected_column": first_swap_issue["expected_column"],
            "observed_column": first_swap_issue["observed_column"],
        },
        "cross_chapter": cross["status"],
        "reverse_traceability": {
            "status": trace["status"],
            "tables": trace["table_count"],
            "unreferenced": trace["unreferenced_table_count"],
        },
        "assembly": {
            "annotated": annotated["status"],
            "clean_gate_without_decisions": clean_gate["attempt_status"],
            "clean_after_decisions": assembly["clean"]["validation"]["status"],
            "synopsis_copy": assembly["synopsis"]["copy_check"]["status"],
        },
        "qc": {
            "status": qc_machine["status"],
            "regulatory_readiness_assessed": qc_machine["regulatory_readiness_assessed"],
        },
    }
    atomic_write_json(OUTPUT / "m4_e2e_summary.json", summary)
    print("M4 e2e PASS")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
