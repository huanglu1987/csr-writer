from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from csr_writer.cross_chapter import (
    CrossChapterError,
    apply_review_decisions,
    build_cross_chapter_report,
    derive_synopsis_manifest,
    load_resolved_section,
    validate_analysis_set_consistency,
    validate_claim_ledger,
    verify_synopsis_copy,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_sections():
    sections = []
    for section_id in ("11.4", "11.A"):
        sections.append(
            load_resolved_section(
                PROJECT_ROOT / "sections" / f"{section_id}.md",
                PROJECT_ROOT / "sections" / "resolved" / f"{section_id}.md",
                PROJECT_ROOT / "sections" / f"{section_id}.meta.json",
                PROJECT_ROOT / "sections" / "resolved" / f"{section_id}.guard.json",
            )
        )
    return tuple(sections)


def _resolved_decisions(sections):
    replacements = {
        "p_11_4_001": "主要疗效终点定义、分析集和分析方法与已确认的研究文件一致。",
        "p_11_4_003": "本节仅呈现预先规定的分析结果，不作超出研究资料的结论。",
        "p_11_A_001": "剂量递增设计、队列转换、DLT 定义和停止规则按已确认的研究文件执行。",
        "p_11_A_003": "本节仅呈现已有来源支持的递增路径和剂量决策信息。",
    }
    return {
        "schema_version": "1.0",
        "sections": {
            section.section_id: {
                "draft_sha256": section.draft_sha256,
                "paragraphs": {
                    paragraph.para_id: {
                        "status": "resolved",
                        "replacement_text": replacements.get(
                            paragraph.para_id, "本段仅保留经确认来源支持的客观内容。"
                        ),
                    }
                    for paragraph in section.paragraphs
                    if paragraph.needs_human
                },
            }
            for section in sections
        },
    }


class M4CrossChapterTests(unittest.TestCase):
    def setUp(self):
        self.sections = _load_sections()
        self.decisions = _resolved_decisions(self.sections)

    def test_clean_gate_requires_every_pending_paragraph_and_current_hash(self):
        missing = copy.deepcopy(self.decisions)
        first_section = next(iter(missing["sections"].values()))
        first_section["paragraphs"].pop(next(iter(first_section["paragraphs"])))
        with self.assertRaisesRegex(CrossChapterError, "blocked until every pending"):
            apply_review_decisions(self.sections, missing)

        stale = copy.deepcopy(self.decisions)
        stale["sections"]["11.4"]["draft_sha256"] = "0" * 64
        with self.assertRaisesRegex(CrossChapterError, "Stale review decision"):
            apply_review_decisions(self.sections, stale)

    def test_synopsis_is_exact_confirmed_body_copy_and_tampering_fails(self):
        clean = apply_review_decisions(self.sections, self.decisions)
        selection = {"11.4": ["p_11_4_002"], "11.A": ["p_11_A_002"]}
        synopsis, manifest = derive_synopsis_manifest(self.sections, clean, selection)
        result = verify_synopsis_copy(synopsis, manifest)
        self.assertEqual("pass", result["status"])
        self.assertEqual("derived_copy_only", result["generation_mode"])
        self.assertFalse(result["independent_drafting_allowed"])
        tampered = list(synopsis)
        tampered[0] += "不经正文的新结论。"
        with self.assertRaisesRegex(CrossChapterError, "differs from confirmed body"):
            verify_synopsis_copy(tampered, manifest)

    def test_analysis_set_definitions_are_compared_across_required_chapters(self):
        records = [
            {"section": section, "id": "mFAS", "definition": "至少接受研究用药的受试者"}
            for section in ("9.7", "10.1", "11", "12")
        ]
        self.assertEqual("pass", validate_analysis_set_consistency(records)["status"])
        records[-1]["definition"] = "所有随机化受试者"
        failed = validate_analysis_set_consistency(records)
        self.assertEqual("fail", failed["status"])
        self.assertEqual("E-ANALYSIS-SET-CONFLICT", failed["issues"][0]["code"])

    def test_claim_ledger_catches_body_discussion_synopsis_contradiction(self):
        base = {
            "schema_version": "1.0",
            "required_sections": ["11.4.7", "13", "Synopsis"],
            "claims": [
                {
                    "claim_id": "PRIMARY_ENDPOINT_INTERPRETATION",
                    "section": section,
                    "stance": "descriptive_only",
                    "evidence_keys": ["T14.2.1|POP=FAS|ROW=RESULT|COL=EXP"],
                }
                for section in ("11.4.7", "13", "Synopsis")
            ],
        }
        self.assertEqual("pass", validate_claim_ledger(base)["status"])
        conflicted = copy.deepcopy(base)
        conflicted["claims"][1]["stance"] = "confirmatory_success"
        report = validate_claim_ledger(conflicted)
        self.assertEqual("fail", report["status"])
        self.assertIn("E-CLAIM-STANCE-CONFLICT", {item["code"] for item in report["issues"]})

    def test_combined_cross_chapter_report_is_bounded_to_contract_checks(self):
        clean = apply_review_decisions(self.sections, self.decisions)
        synopsis, manifest = derive_synopsis_manifest(
            self.sections, clean, {"11.4": ["p_11_4_002"]}
        )
        synopsis_check = verify_synopsis_copy(synopsis, manifest)
        analysis_check = validate_analysis_set_consistency(
            [
                {"section": section, "id": "FAS", "definition": "全分析集统一定义"}
                for section in ("9.7", "10.1", "11", "12")
            ]
        )
        claim_check = validate_claim_ledger(
            {
                "schema_version": "1.0",
                "required_sections": ["11.4.7", "13", "Synopsis"],
                "claims": [
                    {
                        "claim_id": "C1",
                        "section": section,
                        "stance": "reviewed",
                        "evidence_keys": ["K1"],
                    }
                    for section in ("11.4.7", "13", "Synopsis")
                ],
            }
        )
        report = build_cross_chapter_report(synopsis_check, analysis_check, claim_check)
        self.assertEqual("pass", report["status"])
        self.assertEqual([], report["failed_checks"])


if __name__ == "__main__":
    unittest.main()
