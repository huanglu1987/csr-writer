from __future__ import annotations

import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document

from csr_writer.appendix_status import build_appendix_status_plan
from csr_writer.cli import main
from csr_writer.deterministic_sections import (
    DeterministicGenerationError,
    build_deterministic_bundle,
    render_deterministic_bundle_docx,
)


ROOT = Path(__file__).resolve().parents[1]


def m17_deterministic_context() -> dict:
    inventory = json.loads(
        (ROOT / "examples/output/m16/production_method_inventory_120.json").read_text(
            encoding="utf-8"
        )
    )
    titles = {
        row["section_id"]: row["title"]
        for row in inventory["rows"]
        if row.get("is_authoring_unit")
    }
    source_ids = [
        "T14.1.1.1",
        "T14.1.2.1",
        "T14.1.3.1",
        "T14.2.1",
        "T14.3.1.1",
        "T14.3.2.1",
        "T14.3.3.1",
        "T14.3.4.1",
    ]
    publication_ids = [
        "T14.1-1",
        "T14.1-2",
        "T14.1-3",
        "T14.2-1",
        "T14.3-1",
        "T14.3-2",
        "T14.3-3",
        "T14.3-4",
    ]
    publication_entries = [
        {
            "source_entity_id": source,
            "publication_entity_id": publication,
            "title": f"脱敏合成表图{index}",
            "expected_section": "§10.1" if index <= 3 else "§11/§12",
        }
        for index, (source, publication) in enumerate(
            zip(source_ids, publication_ids, strict=True), start=1
        )
    ]
    return {
        "unit_titles": titles,
        "study_facts": {
            "study_identifiers": {
                "study_title": "脱敏合成临床研究",
                "protocol_id": "SYN-CSR-001",
            },
            "design": {"randomized": True},
        },
        "sponsor_metadata": {"sponsor": "脱敏申办方"},
        "report_version_and_date": {
            "version": "1.0",
            "date": "2026-08-05",
            "confidentiality": "保密",
        },
        "approved_signatory_register": [
            {
                "control_id": f"CDE-{index}",
                "label": (
                    "药品注册申请人签字及盖章",
                    "主要或协调研究者签字",
                    "负责或协调研究单位名称",
                    "统计学负责人签字",
                    "统计单位名称",
                    "ICH E3要求的其他标题页信息",
                    "申办方负责医学专员签名",
                )[index - 1],
                "value": "已确认登记值",
                "evidence_status": "已确认",
                "location": "标题页" if index < 7 else "16.1.5",
            }
            for index in range(1, 8)
        ],
        "confirmed_clean_body": {
            "10.1": ["受试者处置情况已在正文确认。"],
            "12.2": ["不良事件结果已在正文确认。"],
        },
        "synopsis_selection_manifest": {
            "selection": [
                {"source_section_id": "10.1", "paragraph_indices": [0]},
                {"source_section_id": "12.2", "paragraph_indices": [0]},
            ]
        },
        "document_heading_styles": "Heading 1-5",
        "caption_style_table": "Table",
        "caption_style_figure": "Figure",
        "frozen_tfl_map": {
            "status": "frozen",
            "entities": [{"entity_id": entity_id} for entity_id in source_ids],
        },
        "publication_reference_map": {
            "schema_version": "1.0",
            "entries": publication_entries,
        },
        "accepted_result_tfl": {"entity_ids": source_ids},
        "appendix_status_plan": build_appendix_status_plan(
            {"design": {"randomized": True}}
        ),
        "confirmed_submission_registry": {
            "entries": [
                {
                    "report_id": "NONE-CONFIRMED",
                    "title": "经确认无单独递交的支持性报告",
                    "submission_location": "不适用",
                    "status": "已确认无",
                }
            ]
        },
        "committee_register": {"committees": ["脱敏伦理委员会"]},
        "approval_identifier": "SYN-IEC-001",
        "approval_date": "2026-01-01",
        "protocol_version": "V1.0",
        "applicable_ethics_standard": "《赫尔辛基宣言》",
        "gcp_standard": "药物临床试验质量管理规范",
        "confirmed_ethics_deviations": "未登记伦理相关偏离",
        "consent_form_versions": ["V1.0"],
        "consent_process_register": "所有受试者在参加研究前均完成书面知情同意",
        "waiver_if_any": "无豁免",
        "individual_listing_targets": {
            "11.4.3": {"display": "附录16.2.6", "bookmark": "_Ref_Sec_16_2_6"},
            "11.4.6": {"display": "附录16.2.6", "bookmark": "_Ref_Sec_16_2_6"},
            "12.2.2": {"display": "附录16.2.7", "bookmark": "_Ref_Sec_16_2_7"},
            "12.2.4": {"display": "附录16.2.7", "bookmark": "_Ref_Sec_16_2_7"},
            "12.3.1.1": {
                "target_type": "publication",
                "entity_id": "T14.3-2",
                "display": "表14.3-2",
                "bookmark": "_Ref_Tab_14_3_2",
            },
            "12.3.1.2": {
                "target_type": "publication",
                "entity_id": "T14.3-2",
                "display": "表14.3-2",
                "bookmark": "_Ref_Tab_14_3_2",
            },
            "12.3.1.3": {
                "target_type": "publication",
                "entity_id": "T14.3-2",
                "display": "表14.3-2",
                "bookmark": "_Ref_Tab_14_3_2",
            },
            "12.4.1": {"display": "附录16.2.8", "bookmark": "_Ref_Sec_16_2_8"},
        },
        "confirmed_claim_ledger": {
            "claims": [{"claim_id": "CLM-001", "source_id": "ICH-E3"}]
        },
        "bibliography_registry": {
            "sources": {"ICH-E3": "ICH E3：临床研究报告的结构和内容。"}
        },
    }


class M17DeterministicSectionTests(unittest.TestCase):
    def test_all_48_plus_4_units_are_zero_llm_and_individually_validated(self) -> None:
        bundle = build_deterministic_bundle(m17_deterministic_context())
        self.assertEqual("pass", bundle["status"])
        self.assertEqual(52, bundle["unit_count"])
        self.assertEqual(
            {"deterministic_generation": 48, "template_slot": 4},
            bundle["producer_counts"],
        )
        self.assertFalse(bundle["model_called"])
        self.assertEqual(52, len(bundle["minimal_validations"]))
        self.assertTrue(
            all(
                item["status"] == "pass" and item["model_called"] is False
                for item in bundle["minimal_validations"]
            )
        )

    def test_missing_or_drifting_sources_hard_fail_instead_of_empty_output(self) -> None:
        context = m17_deterministic_context()
        context.pop("appendix_status_plan")
        with self.assertRaisesRegex(
            DeterministicGenerationError, "E-DETERMINISTIC-INPUT"
        ):
            build_deterministic_bundle(context)

        context = m17_deterministic_context()
        context["accepted_result_tfl"]["entity_ids"].remove("T14.3.4.1")
        with self.assertRaisesRegex(
            DeterministicGenerationError, "E-CHAPTER14-RESULT-MISSING"
        ):
            build_deterministic_bundle(context)

    def test_docx_contains_six_live_directory_fields_and_concrete_outputs(self) -> None:
        bundle = build_deterministic_bundle(m17_deterministic_context())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "deterministic.docx"
            report = render_deterministic_bundle_docx(bundle, output)
            self.assertEqual(6, report["directory_word_field_count"])
            self.assertEqual(8, report["chapter14_entity_count"])
            self.assertEqual(24, report["appendix_status_row_count"])
            self.assertEqual(6, report["title_page_control_count"])
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                settings_xml = archive.read("word/settings.xml").decode("utf-8")
            for marker in (
                'TOC \\o "1-5"',
                'TOC \\c "Table"',
                'TOC \\c "Figure"',
                'TOC \\f "TFL"',
                'TOC \\f "APP"',
                'TOC \\f "SUP"',
            ):
                self.assertIn(marker, document_xml)
            self.assertIn('TC "T14.3-4', document_xml)
            self.assertIn('TC "16.2.8', document_xml)
            self.assertIn("w:updateFields", settings_xml)

            rendered = Document(output)
            text = "\n".join(paragraph.text for paragraph in rendered.paragraphs)
            self.assertIn("脱敏合成临床研究", text)
            self.assertIn("申办方负责医学专员", " ".join(cell.text for table in rendered.tables for row in table.rows for cell in row.cells) + text)
            appendix_tables = [
                table
                for table in rendered.tables
                if table.rows and [cell.text for cell in table.rows[0].cells] == list(
                    ("附录编号", "文件名称", "本次是否提交", "提交位置", "未提交理由/是否可按要求提供")
                )
            ]
            self.assertEqual(1, len(appendix_tables))
            self.assertEqual(29, len(appendix_tables[0].rows))  # header + four groups + 24 rows

    def test_cli_exposes_one_reproducible_zero_llm_generation_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "context.json"
            output = root / "deterministic.docx"
            manifest = root / "manifest.json"
            context.write_text(
                json.dumps(m17_deterministic_context(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            code = main(
                [
                    "generate-deterministic-sections",
                    "--context",
                    str(context),
                    "--output-docx",
                    str(output),
                    "--manifest-output",
                    str(manifest),
                ]
            )
            self.assertEqual(0, code)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(52, payload["unit_count"])
            self.assertFalse(payload["model_called"])
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
