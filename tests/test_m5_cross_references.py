from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.cross_chapter import ResolvedParagraph, ResolvedSection
from csr_writer.cross_references import (
    bookmark_name,
    display_reference,
    make_reference_placeholder,
    normalize_entity_id,
)
from csr_writer.m4_assembly import (
    render_annotated_docx,
    render_clean_and_synopsis,
    validate_docx_references,
)
from csr_writer.resolve_placeholders import resolve


ROOT = Path(__file__).resolve().parents[1]


class M5CrossReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datapack = json.loads(
            (ROOT / "examples" / "output" / "datapack.json").read_text(encoding="utf-8")
        )
        cls.facts = json.loads(
            (ROOT / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8")
        )

    def test_chinese_prefix_normalization_is_explicit(self):
        self.assertEqual("T14.2.4.1", normalize_entity_id("表14.2.4.1"))
        self.assertEqual("F14.2.4.1", normalize_entity_id("图 14.2.4.1"))
        self.assertEqual("L1.1", normalize_entity_id("清单1.1"))
        self.assertEqual("{{@ref:F14.2.4.1}}", make_reference_placeholder("图14.2.4.1"))
        self.assertEqual("图 14.2.4.1", display_reference("F14.2.4.1"))
        self.assertEqual("_Ref_Lst_1_1", bookmark_name("L1.1"))

    def test_literal_table_number_stays_blocked(self):
        outcome = resolve("上述结果详见表 14.2.1。", self.datapack, self.facts)
        self.assertEqual("review_required", outcome.status)
        self.assertEqual("14.2", outcome.report["issues"][0]["token"])

    def test_unbound_reference_stays_blocked(self):
        outcome = resolve("上述结果详见{{@ref:T14.2.1}}。", self.datapack, self.facts)
        self.assertEqual("blocked", outcome.status)
        self.assertEqual("E-REF-UNBOUND", outcome.report["issues"][0]["code"])

    def test_scoped_reference_is_not_a_value_placeholder(self):
        metadata = {"binding_manifest": [{"source_id": "T14.2.1"}]}
        outcome = resolve(
            "上述结果详见{{@ref:T14.2.1}}。",
            self.datapack,
            self.facts,
            binding_manifest=metadata,
        )
        self.assertEqual("pass", outcome.status, outcome.report)
        self.assertEqual(0, outcome.report["placeholder_count"])
        self.assertEqual(1, outcome.report["reference_count"])
        self.assertEqual("上述结果详见{{@ref:T14.2.1}}。", outcome.resolved_text)

    def test_annotated_clean_and_synopsis_use_ref_fields_and_bookmarks(self):
        paragraph = ResolvedParagraph(
            section_id="11.4",
            para_id="11.4-p001",
            text="支持性结果详见{{@ref:T14.2.1}}。",
            needs_human=False,
            confidence="verbatim",
            sources=("T14.2.1",),
            placeholders=(),
            references=("T14.2.1",),
        )
        section = ResolvedSection(
            section_id="11.4",
            heading="主要疗效",
            notices=(),
            evidence_status="demo_only",
            draft_sha256="a" * 64,
            resolved_sha256="b" * 64,
            metadata_path=Path("deidentified.meta.json"),
            reference_entities=("T14.2.1",),
            paragraphs=(paragraph,),
        )
        decisions = {
            "schema_version": "1.0",
            "sections": {
                "11.4": {"draft_sha256": "a" * 64, "paragraphs": {}}
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            annotated = root / "annotated.docx"
            clean = root / "clean.docx"
            synopsis = root / "synopsis.docx"
            manifest = root / "synopsis.json"
            annotated_report = render_annotated_docx(
                (section,), annotated, protocol_id="DEID"
            )
            clean_report = render_clean_and_synopsis(
                (section,),
                decisions,
                {"11.4": ["11.4-p001"]},
                clean,
                synopsis,
                manifest,
                protocol_id="DEID",
            )
            self.assertEqual(1, annotated_report["cross_references"]["field_count"])
            self.assertEqual(1, clean_report["clean"]["cross_references"]["field_count"])
            self.assertEqual(1, clean_report["synopsis"]["cross_references"]["field_count"])
            for output in (annotated, clean, synopsis):
                check = validate_docx_references(output)
                self.assertEqual("pass", check["status"])
                self.assertEqual(1, check["bookmark_count"])


if __name__ == "__main__":
    unittest.main()
