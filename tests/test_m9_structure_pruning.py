from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from csr_writer.m9_structure_qc import audit_pruned_project_docx
from csr_writer.module_router import route_modules
from csr_writer.project_skeleton import (
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
    render_project_skeleton,
)


ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
ONCOLOGY = ROOT / "examples" / "demo_protocol_facts.json"
PUBLICATION_MAP = ROOT / "examples" / "fixtures" / "m9_publication_reference_map.deidentified.json"


def _content() -> list[dict]:
    def item(section_id: str, variant: str, title: str, references: list[str]) -> dict:
        refs = "、".join(f"{{{{@ref:{value}}}}}" for value in references)
        return {
            "section_id": section_id,
            "variant": variant,
            "title": title,
            "evidence_status": "source_gap",
            "reference_entities": references,
            "paragraphs": [
                {
                    "text": f"本节保留资料缺口，已登记的表级来源详见{refs}。",
                    "sources": ["deidentified-structure-register"],
                    "confidence": "inferred",
                    "needs_human": True,
                }
            ],
        }

    return [
        item("10.1", "subject_disposition", "受试者处置", ["T14.1.1.1", "T14.1.1.2"]),
        item("11.2", "demographics_baseline", "人口学与基线", ["T14.1.2.1", "T14.1.2.2"]),
        item("11.5", "topical_low_systemic_exposure", "外用制剂 PK", ["T14.2.4.3.1", "T14.2.4.3.2"]),
        item("11.6", "exploratory", "探索性疗效", ["T14.2.1.1.1"]),
        item("12.1", "extent_of_exposure", "暴露程度", ["T14.3.1.1"]),
        item("12.2", "adverse_events", "不良事件", ["T14.3.1.2", "T14.3.1.3.1"]),
        item(
            "12.2",
            "adverse_events_duplicate_contribution",
            "不良事件与局部耐受性",
            ["T14.3.1.2", "T14.3.1.3.1"],
        ),
        item("12.2", "ae_consequence_pairs", "不良事件", ["T14.3.1.3.5", "T14.3.1.3.6"]),
        item("12.5.1", "local_tolerance_ae_subset", "局部耐受性", ["T14.3.1.3.3", "T14.3.1.3.4"]),
    ]


class M9StructurePruningTests(unittest.TestCase):
    def setUp(self):
        self.facts = json.loads(FACTS.read_text(encoding="utf-8"))
        self.oncology = json.loads(ONCOLOGY.read_text(encoding="utf-8"))
        self.publication_map = json.loads(PUBLICATION_MAP.read_text(encoding="utf-8"))

    def test_plan_classifies_every_official_section_and_prunes_six_review_items(self):
        routed = route_modules(self.facts)
        plan = routed["structure"]["section_plan"]
        self.assertEqual(plan["official_section_count"], len(plan["official_sections"]))
        self.assertEqual(
            {
                "unconditional",
                "module_driven",
                "conditional_omitted",
                "manual_review_required",
                "relocated",
            },
            {item["classification"] for item in plan["official_sections"]},
        )
        omitted = {item["section_id"] for item in plan["omitted_sections"]}
        self.assertTrue(
            {"11.4.2.7", "11.4.5", "11.4.2.3", "11.4.2.5"}.issubset(omitted)
        )
        self.assertNotIn("11.4.6", omitted)
        self.assertNotIn("16.4", omitted)
        self.assertEqual(
            {"11.4.3", "11.4.6"},
            {item["section_id"] for item in plan["manual_review_queue"]},
        )
        self.assertFalse(plan["policy"]["phase_used_for_structure"])

    def test_oncology_structure_differs_without_using_phase(self):
        topical = route_modules(self.facts)["structure"]["section_plan"]
        oncology = route_modules(self.oncology)["structure"]["section_plan"]
        topical_extensions = {item["section_id"] for item in topical["formal_extensions"]}
        oncology_extensions = {item["section_id"] for item in oncology["formal_extensions"]}
        self.assertIn("12.5.1", topical_extensions)
        self.assertNotIn("12.5.1", oncology_extensions)
        self.assertTrue(
            {"11.8", "12.5.6", "12.6.3", "12.6.5", "12.6.7", "12.7"}.issubset(
                oncology_extensions
            )
        )
        changed_phase = copy.deepcopy(self.facts)
        changed_phase["phase"] = "III"
        self.assertEqual(
            route_modules(self.facts)["structure"]["section_plan"],
            route_modules(changed_phase)["structure"]["section_plan"],
        )

    def test_annotated_clean_numbering_directories_refs_and_appendices(self):
        routed = route_modules(self.facts)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            annotated = root / "annotated.docx"
            clean = root / "clean.docx"
            annotated_report = render_project_annotated_skeleton(
                routed,
                _content(),
                annotated,
                scaffold_path=root / "annotated_scaffold.docx",
                publication_reference_map=self.publication_map,
                study_facts=self.facts,
            )
            clean_report = render_project_clean_skeleton(
                routed,
                clean,
                scaffold_path=root / "clean_scaffold.docx",
                publication_reference_map=self.publication_map,
                study_facts=self.facts,
            )
            annotated_qc = audit_pruned_project_docx(
                annotated,
                annotated_report,
                publication_reference_map=self.publication_map,
            )
            clean_qc = audit_pruned_project_docx(
                clean,
                clean_report,
                publication_reference_map=self.publication_map,
                clean=True,
            )
            visible = "\n".join(paragraph.text for paragraph in Document(annotated).paragraphs)

        self.assertEqual("pass", annotated_qc["status"], annotated_qc)
        self.assertEqual("pass", clean_qc["status"], clean_qc)
        self.assertNotIn("11.A", visible)
        self.assertNotIn("11.B", visible)
        self.assertNotIn("不良事件与局部耐受性", visible)
        self.assertIn("11.5 药代动力学评价", visible)
        self.assertIn("12.5.1 局部耐受性评分和用药部位反应", visible)
        self.assertEqual(22, annotated_report["publication_anchor_count"])
        self.assertEqual(annotated_report["publication_reference_count"], annotated_report["publication_anchor_count"])

    def test_optional_not_applicable_mode_keeps_reason_with_fact_value(self):
        routed = route_modules(self.facts)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "not_applicable.docx"
            render_project_skeleton(
                routed,
                (),
                output,
                scaffold_path=Path(temporary) / "scaffold.docx",
                mode="clean",
                omission_policy="retain_not_applicable",
                study_facts=self.facts,
            )
            visible = "\n".join(paragraph.text for paragraph in Document(output).paragraphs)
        self.assertIn("11.4.2.3 试验期中分析和数据监查", visible)
        self.assertIn("不适用，理由为：", visible)
        self.assertIn("本研究未设定确证性疗效目的", visible)
        self.assertNotIn("has_interim_analysis=False", visible)


if __name__ == "__main__":
    unittest.main()
