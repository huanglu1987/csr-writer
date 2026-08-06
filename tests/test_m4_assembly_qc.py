from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.cross_chapter import load_resolved_section
from csr_writer.cross_references import REF_PLACEHOLDER_RE, display_reference
from csr_writer.m4_assembly import (
    CleanOutputBlocked,
    render_annotated_docx,
    render_clean_and_synopsis,
    validate_clean_docx,
)
from csr_writer.m4_qc import QCAuthorityError, build_qc_report, validate_qc_authority


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sections():
    return tuple(
        load_resolved_section(
            PROJECT_ROOT / "sections" / f"{section_id}.md",
            PROJECT_ROOT / "sections" / "resolved" / f"{section_id}.md",
            PROJECT_ROOT / "sections" / f"{section_id}.meta.json",
            PROJECT_ROOT / "sections" / "resolved" / f"{section_id}.guard.json",
        )
        for section_id in ("11.4", "11.A")
    )


def _decisions(sections):
    replacement_by_id = {
        "p_11_4_001": "主要疗效终点、分析集和方法按已确认的研究文件执行。",
        "p_11_4_003": "本节仅呈现预先规定的分析结果。",
        "p_11_A_001": "剂量递增设计和停止规则按已确认的研究文件执行。",
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
                        "replacement_text": replacement_by_id.get(
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


def _body_text(path: Path) -> str:
    document = Document(path)
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


class M4AssemblyAndQCTests(unittest.TestCase):
    def setUp(self):
        self.sections = _sections()

    def test_annotated_contains_markers_and_clean_is_blocked_without_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            annotated = root / "CSR_DEMO_annotated.docx"
            report = render_annotated_docx(
                self.sections, annotated, protocol_id="DEMO-001"
            )
            self.assertGreater(report["pending_count"], 0)
            self.assertIn("待医学负责人确认", _body_text(annotated))
            with self.assertRaises(CleanOutputBlocked):
                render_clean_and_synopsis(
                    self.sections,
                    {"schema_version": "1.0", "sections": {}},
                    {"11.4": ["p_11_4_002"]},
                    root / "CSR_DEMO_clean.docx",
                    root / "CSR_Synopsis_DEMO.docx",
                    root / "synopsis_manifest.json",
                    protocol_id="DEMO-001",
                )
            self.assertFalse((root / "CSR_DEMO_clean.docx").exists())

    def test_clean_and_synopsis_are_pure_and_synopsis_is_copy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean = root / "CSR_DEMO_clean.docx"
            synopsis = root / "CSR_Synopsis_DEMO.docx"
            manifest = root / "synopsis_manifest.json"
            report = render_clean_and_synopsis(
                self.sections,
                _decisions(self.sections),
                {"11.4": ["p_11_4_002"], "11.A": ["p_11_A_002"]},
                clean,
                synopsis,
                manifest,
                protocol_id="DEMO-001",
            )
            self.assertEqual("pass", report["status"])
            self.assertEqual("pass", validate_clean_docx(clean)["status"])
            self.assertEqual("pass", validate_clean_docx(synopsis)["status"])
            clean_text = _body_text(clean)
            synopsis_text = _body_text(synopsis)
            self.assertNotIn("待医学负责人确认", clean_text)
            self.assertNotIn("内部溯源", clean_text)
            expected_visible = REF_PLACEHOLDER_RE.sub(
                lambda match: display_reference(match.group("entity")),
                self.sections[0].paragraphs[1].text,
            )
            self.assertIn(expected_visible, synopsis_text)
            self.assertTrue(manifest.is_file())

    def test_pending_text_in_review_replacement_is_blocked_before_publish(self):
        decisions = _decisions(self.sections)
        first_section = next(iter(decisions["sections"].values()))
        first_decision = next(iter(first_section["paragraphs"].values()))
        first_decision["replacement_text"] = "该内容仍为[待核实]。"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean = root / "CSR_DEMO_clean.docx"
            synopsis = root / "CSR_Synopsis_DEMO.docx"
            with self.assertRaises(CleanOutputBlocked):
                render_clean_and_synopsis(
                    self.sections,
                    decisions,
                    {"11.4": ["p_11_4_002"], "11.A": ["p_11_A_002"]},
                    clean,
                    synopsis,
                    root / "synopsis_manifest.json",
                    protocol_id="DEMO-001",
                )
            self.assertFalse(clean.exists())
            self.assertFalse(synopsis.exists())

    def test_qc_language_gate_rejects_readiness_claims(self):
        for rogue in ("本报告已申报就绪", "经检查可以递交", "符合 CDE 要求"):
            with self.assertRaises(QCAuthorityError):
                validate_qc_authority(rogue)

    def test_generated_qc_report_is_bounded_and_uses_no_problem_wording(self):
        markdown, report = build_qc_report(
            {
                "跨章一致性": {"status": "pass", "summary": "Synopsis 均为已确认正文逐段复制"},
                "组别语义": {"status": "pass", "summary": "组别契约与语义列一致"},
                "读者版纯净性": {"status": "pass", "summary": "未检出待确认或内部溯源标记"},
            }
        )
        self.assertEqual("pass", report["status"])
        self.assertFalse(report["regulatory_readiness_assessed"])
        self.assertIn("未发现本次已执行自动检查所覆盖的", markdown)
        validate_qc_authority(markdown)


if __name__ == "__main__":
    unittest.main()
