from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.module_router import route_modules
from csr_writer.paths import DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE
from csr_writer.project_skeleton import render_project_annotated_skeleton
from csr_writer.qc_format import run_qc


ROOT = Path(__file__).resolve().parents[1]


class M6ProjectSkeletonTests(unittest.TestCase):
    def test_all_routed_contributions_are_covered_and_scaffold_stays_valid(self):
        facts = json.loads((ROOT / "examples" / "demo_protocol_facts.json").read_text())
        routing = route_modules(facts)
        content = [
            {
                "section_id": "2",
                "variant": "derived_copy_only",
                "title": "概要派生门",
                "evidence_status": "derived_copy_only",
                "reference_entities": [],
                "paragraphs": [
                    {
                        "text": "正文尚待确认，概要保持锁定且不独立起草。",
                        "sources": ["confirmed-body-register"],
                        "confidence": "inferred",
                        "needs_human": True,
                    }
                ],
            },
            {
                "section_id": "11.B",
                "variant": "pk_nca",
                "title": "药代动力学",
                "evidence_status": "source_gap",
                "reference_entities": ["T14.2.4.3.1"],
                "paragraphs": [
                    {
                        "text": "结果来源尚未提供，结构来源详见{{@ref:T14.2.4.3.1}}。",
                        "sources": ["deidentified-shell-map"],
                        "confidence": "inferred",
                        "needs_human": True,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scaffold = root / "scaffold.docx"
            output = root / "annotated.docx"
            report = render_project_annotated_skeleton(
                routing, content, output, scaffold_path=scaffold
            )
            scaffold_qc = run_qc(
                scaffold,
                DEFAULT_FORMAT_SPEC,
                DEFAULT_STRUCTURE,
                ("ich-official", "industry-convention"),
            )
            visible = "\n".join(paragraph.text for paragraph in Document(output).paragraphs)

        self.assertTrue(scaffold_qc.passed, scaffold_qc.format_text())
        self.assertEqual(report["route_contribution_count"], report["route_coverage_count"])
        self.assertEqual("pass", report["cross_references"]["status"])
        self.assertIn("12.6 剂量递增与总体耐受性评价", visible)
        self.assertIn("12.7 安全性结论", visible)
        self.assertIn("11.5 药代动力学评价", visible)
        self.assertIn("概要保持锁定", visible)
        self.assertIn("内容保持空缺", visible)


if __name__ == "__main__":
    unittest.main()
