from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from csr_writer.appendix_status import build_appendix_status_plan
from csr_writer.io_utils import sha256_file
from csr_writer.module_router import route_modules
from csr_writer.project_skeleton import (
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
)
from csr_writer.protocol_fact_scan import scan_protocol_facts
from csr_writer.source_lock import verify_source_file_lock
from csr_writer.title_overrides import load_title_overrides


ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
NO_PK_FACTS = ROOT / "examples" / "fixtures" / "topical_ib_study_facts.deidentified.json"
OFFICIAL = ROOT.parent / "ICH-E3章节树.yaml"


def _visible_docx_text(path: Path) -> str:
    document = Document(path)
    chunks = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            chunks.extend(cell.text for cell in row.cells)
    return "\n".join(chunks)


class M10StructureAndFactScanTests(unittest.TestCase):
    def setUp(self):
        self.facts = json.loads(FACTS.read_text(encoding="utf-8"))

    def test_immutable_sources_and_auditable_title_override(self):
        locked = verify_source_file_lock()
        self.assertEqual("pass", locked["status"])
        self.assertEqual(4, locked["source_count"])
        spec_record = next(
            item for item in locked["records"] if item["role"] == "development_specification"
        )
        self.assertEqual(
            "6053ea6fffa79f98d1c92840a812cd2cf12b4a329fb9420d1efcca5c6fb06bdb",
            spec_record["observed_sha256"],
        )
        self.assertEqual(
            "87ea56d599120e265c1649881a9d3ad1c08db540a91be13de6a2bd31a6bc0942",
            sha256_file(OFFICIAL),
        )
        overrides = load_title_overrides()
        record = overrides["by_section_id"]["3"]
        self.assertEqual("个例临床研究报告目录", record["official_zh"])
        self.assertEqual("本临床研究报告目录", record["localized_zh"])
        self.assertTrue(record["reason"])

    def test_pk_branch_appendix_register_and_omission_policy(self):
        plan = route_modules(self.facts)["structure"]["section_plan"]
        extensions = {item["section_id"] for item in plan["formal_extensions"]}
        self.assertTrue({f"11.5.{value}" for value in range(1, 11)}.issubset(extensions))
        self.assertIn("11.6.7", extensions)
        self.assertFalse(
            any(item["section_id"].startswith("16") for item in plan["omitted_sections"])
        )
        self.assertFalse(
            any(
                basis.get("value") == "no supporting module"
                for item in plan["omitted_sections"]
                for basis in item["basis"]
                if isinstance(basis, dict)
            )
        )
        self.assertEqual(
            {"11.4.3", "11.4.6"},
            {item["section_id"] for item in plan["manual_review_queue"]},
        )
        appendix = build_appendix_status_plan(self.facts)
        self.assertEqual(appendix["rows"], appendix["entries"])
        self.assertEqual(23, appendix["unique_appendix_count"])
        self.assertTrue(
            {"16.1.3", "16.1.4", "16.1.6", "16.1.8", "16.1.10", "16.1.11", "16.1.12"}
            .issubset({row["appendix_id"] for row in appendix["entries"]})
        )

    def test_rendered_gaps_conclusions_titles_and_clean_annotated(self):
        before = sha256_file(OFFICIAL)
        routed = route_modules(self.facts)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            annotated = root / "annotated.docx"
            clean = root / "clean.docx"
            annotated_report = render_project_annotated_skeleton(
                routed,
                (),
                annotated,
                scaffold_path=root / "annotated_scaffold.docx",
                study_facts=self.facts,
            )
            clean_report = render_project_clean_skeleton(
                routed,
                clean,
                scaffold_path=root / "clean_scaffold.docx",
                study_facts=self.facts,
            )
            annotated_text = _visible_docx_text(annotated)
            clean_text = _visible_docx_text(clean)
            doc = Document(clean)
            styles = {}
            for paragraph in doc.paragraphs:
                for start in paragraph._p.findall(qn("w:bookmarkStart")):
                    styles[start.get(qn("w:name"), "")] = paragraph.style.name

        self.assertEqual(before, sha256_file(OFFICIAL))
        for report in (annotated_report, clean_report):
            self.assertTrue(report["omission_declaration_blocks"])
            self.assertTrue(report["conclusion_order"])
            self.assertTrue(all(item["last"] for item in report["conclusion_order"]))
            self.assertEqual(24, report["appendix_status_row_count"])
            self.assertEqual("本临床研究报告目录", report["title_overrides"]["applied"][0]["localized_zh"])
        for text in (annotated_text, clean_text):
            self.assertIn("本章以下小节因本研究设计不适用而未纳入", text)
            self.assertIn("14.2 药代动力学和探索性疗效数据", text)
            self.assertIn("11.5.10 PK 结论", text)
            self.assertLess(
                text.index("12.6 剂量递增与总体耐受性评价"),
                text.index("12.7 安全性结论"),
            )
        self.assertEqual("Heading 3", styles["_Ref_Sec_11_5_10"])

    def test_non_pk_comparator_does_not_gain_pk_category(self):
        facts = json.loads(NO_PK_FACTS.read_text(encoding="utf-8"))
        facts["data_domains"]["pk_concentration"] = False
        facts["analyses"]["nca"] = False
        routed = route_modules(facts)
        plan = routed["structure"]["section_plan"]
        item = next(value for value in plan["official_sections"] if value["section_id"] == "14.2")
        self.assertNotIn("药代动力学", item["render_title"])
        self.assertFalse(
            any(value["section_id"].startswith("11.5") for value in plan["formal_extensions"])
        )

    def test_protocol_scan_detects_injected_false_without_mutating_facts(self):
        facts = copy.deepcopy(self.facts)
        facts["data_domains"]["pd_biomarker"] = False
        facts["data_domains"]["local_tolerance"] = False
        original = copy.deepcopy(facts)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "protocol.docx"
            document = Document()
            document.add_heading("9.5.7 探索性评价", level=1)
            document.add_paragraph(
                "采集样本用于炎症相关生物标志物检测；同时评价局部耐受性。"
            )
            document.save(path)
            report = scan_protocol_facts(path, facts, expected_sha256=sha256_file(path))
        self.assertEqual(original, facts)
        self.assertEqual(
            {"pd_biomarker", "local_tolerance"},
            {item["rule_id"] for item in report["findings"]},
        )
        self.assertTrue(all(item["source_location"].startswith("§9.5.7") for item in report["findings"]))
        self.assertFalse(report["contract"]["automatic_fact_updates"])

    def test_scan_rules_cover_acronyms_adjacent_to_chinese_text(self):
        facts = copy.deepcopy(self.facts)
        facts["data_domains"]["ada"] = False
        facts["data_domains"]["ecg_intensive"] = False
        facts["design"]["interim_analysis"] = False
        facts["design"]["dlt_defined"] = False
        facts["objectives"]["determine_mtd"] = False
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "protocol.docx"
            document = Document()
            document.add_heading("9.5.8 专项评价", level=1)
            document.add_paragraph(
                "计划进行ADA检测，设立DMC委员会，定义RP2D，并开展TQT评价。"
            )
            document.save(path)
            report = scan_protocol_facts(path, facts)
        self.assertTrue(
            {
                "immunogenicity_ada",
                "interim_or_dmc",
                "dlt_mtd_rp2d",
                "intensive_ecg",
            }.issubset({item["rule_id"] for item in report["findings"]})
        )


if __name__ == "__main__":
    unittest.main()
