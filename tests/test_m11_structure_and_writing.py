from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.authoring_inventory import build_authoring_inventory
from csr_writer.group_semantics import validate_group_semantics
from csr_writer.module_router import route_modules
from csr_writer.project_skeleton import (
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
)


ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = (
    ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
)


def _extension_ids(routing: dict) -> set[str]:
    return {
        item["section_id"]
        for item in routing["structure"]["section_plan"]["formal_extensions"]
    }


class M11StructureAndWritingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = json.loads(FACTS_PATH.read_text(encoding="utf-8"))

    def test_appendix_rows_omission_body_and_numbering_are_rendered(self):
        routing = route_modules(self.facts)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            annotated = root / "annotated.docx"
            clean = root / "clean.docx"
            annotated_report = render_project_annotated_skeleton(
                routing,
                (),
                annotated,
                scaffold_path=root / "annotated_scaffold.docx",
                study_facts=self.facts,
            )
            clean_report = render_project_clean_skeleton(
                routing,
                clean,
                scaffold_path=root / "clean_scaffold.docx",
                study_facts=self.facts,
            )

            for path, report in (
                (annotated, annotated_report),
                (clean, clean_report),
            ):
                document = Document(path)
                body_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
                self.assertIn("本章以下小节因本研究设计不适用而未纳入", body_text)
                self.assertIn("本研究未设定确证性疗效目的", body_text)
                self.assertIn(
                    "11.4 主节为保持 ICH E3 定位而保留；其中 11.4.3、11.4.6",
                    body_text,
                )
                self.assertLess(
                    body_text.index("12.6 剂量递增与总体耐受性评价"),
                    body_text.index("12.7 安全性结论"),
                )
                status_tables = [
                    table
                    for table in document.tables
                    if [cell.text for cell in table.rows[0].cells]
                    == [
                        "附录编号",
                        "文件名称",
                        "本次是否提交",
                        "提交位置",
                        "未提交理由/是否可按要求提供",
                    ]
                ]
                self.assertEqual(2, len(status_tables))
                for table in status_tables:
                    self.assertEqual(29, len(table.rows))
                    rows = [[cell.text for cell in row.cells] for row in table.rows]
                    self.assertTrue(
                        any(
                            row[0] == "16.1.4"
                            and "研究者" in row[1]
                            and row[2] in {"部分提交", "否"}
                            for row in rows
                        )
                    )
                self.assertEqual(24, report["appendix_status_row_count"])
                self.assertTrue(report["numbering_order"]["status"] == "pass")

    def test_analysis_sets_are_relocated_and_driven_by_fact_ids(self):
        facts = copy.deepcopy(self.facts)
        facts["analyses"]["analysis_sets"] = [
            {"id": "mITT", "name": "修正意向治疗集", "definition": "经审核定义"},
            {"id": "PKCS", "name": "药代浓度分析集", "definition": "经审核定义"},
            {
                "id": "All Enrolled Set",
                "name": "全部入组集",
                "definition": "经审核定义",
            },
        ]
        plan = route_modules(facts)["structure"]["section_plan"]
        official_11_1 = next(
            item for item in plan["official_sections"] if item["section_id"] == "11.1"
        )
        self.assertEqual("relocated", official_11_1["classification"])
        self.assertEqual("10.3", official_11_1["relocate_to"])
        titles = {
            item["section_id"]: item["title"] for item in plan["formal_extensions"]
        }
        self.assertEqual("mITT（修正意向治疗集）", titles["10.3.1"])
        self.assertEqual("PKCS（药代浓度分析集）", titles["10.3.2"])
        self.assertEqual("All Enrolled Set（全部入组集）", titles["10.3.3"])

    def test_endpoint_and_special_safety_sections_are_fact_driven(self):
        routing = route_modules(self.facts)
        ids = _extension_ids(routing)
        self.assertTrue(
            {
                "9.5.7",
                "9.5.8",
                "9.5.9",
                "11.6.1.4",
                "12.2.6",
                "12.2.7",
                "12.2.9",
                "12.5.1",
                "12.5.2",
            }.issubset(ids)
        )
        self.assertNotIn("9.5.10", ids)
        self.assertNotIn("12.2.5", ids)
        self.assertNotIn("12.2.8", ids)

        enriched = copy.deepcopy(self.facts)
        enriched["data_domains"].update(
            pd_biomarker=True,
            ada=True,
            aesi=True,
            vital_signs=True,
            pregnancy=True,
            medication_error_overdose_misuse=True,
            ae_study_withdrawal=True,
        )
        enriched_ids = _extension_ids(route_modules(enriched))
        self.assertTrue(
            {
                "9.5.10",
                "12.2.5",
                "12.2.8",
                "12.5.3",
                "12.5.4",
                "12.5.5",
                "12.5.6",
            }.issubset(enriched_ids)
        )

        absent = copy.deepcopy(self.facts)
        absent["data_domains"].update(
            local_tolerance=False,
            ecg=False,
            ada=False,
            pd_biomarker=False,
            aesi=False,
            vital_signs=False,
            pregnancy=False,
            medication_error_overdose_misuse=False,
            ae_study_withdrawal=False,
            ae_consequence_strata=False,
        )
        absent_ids = _extension_ids(route_modules(absent))
        for section_id in {
            "9.5.9",
            "9.5.10",
            "12.2.5",
            "12.2.6",
            "12.2.7",
            "12.2.8",
            "12.2.9",
            "12.5.1",
            "12.5.2",
            "12.5.3",
            "12.5.4",
            "12.5.5",
            "12.5.6",
        }:
            self.assertNotIn(section_id, absent_ids)

    def test_authoring_inventory_counts_every_visible_heading(self):
        plan = route_modules(self.facts)["structure"]["section_plan"]
        inventory = build_authoring_inventory(plan)
        self.assertEqual("pass", inventory["status"])
        self.assertEqual(
            inventory["visible_heading_count"],
            inventory["authoring_unit_count"] + inventory["container_count"],
        )
        self.assertEqual(35, inventory["full_playbook_coverage_units"])
        section_three = next(
            row for row in inventory["rows"] if row["section_id"] == "3"
        )
        self.assertEqual("本临床研究报告目录", section_three["title"])
        self.assertEqual(1, len(inventory["title_overrides"]["applied"]))
        self.assertEqual(
            {"事务类", "方法类", "结果类"}, set(inventory["type_counts"])
        )
        self.assertTrue(
            all(
                item["type"] in {"容器", "事务类", "方法类", "结果类"}
                and isinstance(item["ai_savings_percent"], int)
                for item in inventory["rows"]
            )
        )

    def test_group_semantics_uses_new_10_3_analysis_set_anchor(self):
        fixture_root = ROOT / "examples" / "m4"
        datapack = json.loads(
            (fixture_root / "synthetic_datapack.json").read_text(encoding="utf-8")
        )
        contract = json.loads(
            (fixture_root / "group_contract.json").read_text(encoding="utf-8")
        )
        metadata = json.loads(
            (fixture_root / "section_metadata.json").read_text(encoding="utf-8")
        )
        for table in datapack["tables"]:
            if table["table_id"] == "T11.1":
                table["table_id"] = "T10.3"
        datapack["cells"] = {
            key.replace("T11.1|", "T10.3|"): value
            for key, value in datapack["cells"].items()
        }
        for group in contract["groups"].values():
            group["analysis_set_anchor"] = group["analysis_set_anchor"].replace(
                "T11.1|", "T10.3|"
            )
        for section in metadata:
            if section["section"] == "11.1":
                section["section"] = "10.3"
            for binding in section["binding_manifest"]:
                binding["key"] = binding["key"].replace("T11.1|", "T10.3|")
                if binding["source_id"] == "T11.1":
                    binding["source_id"] = "T10.3"
        report = validate_group_semantics(datapack, metadata, contract)
        self.assertEqual("pass", report["status"])
        self.assertEqual("10.3", report["anchor_section_policy"]["analysis_set"])


if __name__ == "__main__":
    unittest.main()
