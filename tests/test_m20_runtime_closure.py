from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.conditional_conclusions import EXPECTED_CONCLUSION_RULES
from csr_writer.cli import main as cli_main
from csr_writer.constrained_narration import (
    emit_fact_card_envelope,
    ingest_constrained_draft,
    write_constrained_artifact,
)
from csr_writer.cross_chapter import load_constrained_resolved_section
from csr_writer.io_utils import atomic_write_json, load_data
from csr_writer.production_methods import load_production_method_registry
from csr_writer.section_writer import (
    PLAYBOOK_FILENAMES,
    SECTION_VARIANTS,
    DraftParagraph,
    SectionWriterError,
    _validate_generated_paragraphs,
    draft_section,
)
from csr_writer.io_utils import sha256_file
from csr_writer.docx_result import _match_ancova_row
from csr_writer.docx_result import DocxResultError, apply_docx_result_to_frozen_shell
from csr_writer.shell_mapping import _analysis_set, propose_docx_shell_map
from csr_writer.tfl_map import freeze_tfl_map
from csr_writer.section_writer_adversary import _baseline, _envelope
from csr_writer.semantic_guard import is_conclusion_section
from csr_writer.writing_style import style_report
from tests.test_m13_conditioned_conclusions import _datapack, _request


class M20RuntimeClosureTests(unittest.TestCase):
    @staticmethod
    def _stable_tfl(path: Path, entity_ids: tuple[str, ...], *, result: bool) -> None:
        document = Document()
        for entity_id in entity_ids:
            document.add_paragraph(f"表{entity_id[1:]} 合成统计表 (ITT)")
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "统计量"
            table.cell(0, 1).text = "Active Total"
            table.cell(1, 0).text = "N"
            table.cell(1, 1).text = "8" if result else "xx"
        document.save(path)

    def test_whole_shell_entity_missing_from_result_is_a_strict_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shell_path = root / "shell.docx"
            result_path = root / "result.docx"
            self._stable_tfl(shell_path, ("T14.2.1", "T14.2.2"), result=False)
            self._stable_tfl(result_path, ("T14.2.1",), result=True)
            _report, shell, candidate = propose_docx_shell_map(
                shell_path,
                expected_sha256=sha256_file(shell_path),
                map_version="M20-MISSING-RESULT-ENTITY",
            )
            frozen = freeze_tfl_map(
                candidate,
                {
                    "status": "confirmed",
                    "confirmation_id": "M20-MISSING-RESULT-ENTITY",
                    "confirmed_by_role": "test_reviewer",
                    "confirmed_at": "2026-08-06T00:00:00+08:00",
                    "candidate_sha256": candidate["candidate_sha256"],
                },
            )
            with self.assertRaises(DocxResultError) as caught:
                apply_docx_result_to_frozen_shell(
                    shell,
                    frozen,
                    result_path,
                    expected_sha256=sha256_file(result_path),
                )
        self.assertEqual(["T14.2.2"], caught.exception.report["missing_result_entities"])
        self.assertGreater(
            caught.exception.report["metrics"]["unresolved_frozen_cell_count"], 0
        )

    def test_root_level_n_and_p_value_match_their_frozen_semantic_rows(self):
        frozen = {"N", "P_VALUE", "MEAN_SD"}
        self.assertEqual(("N", "matched"), _match_ancova_row("N", frozen))
        self.assertEqual(
            ("P_VALUE", "matched"), _match_ancova_row("P值", frozen)
        )

    def test_analysis_set_parser_accepts_saf_without_turning_registry_into_enum(self):
        self.assertEqual("SAF", _analysis_set("表14.3.1.1 安全性汇总 (SAF)"))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "generic-analysis-set.docx"
            document = Document()
            document.add_paragraph("表14.3.1.1 安全性汇总 (SAF)")
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "统计量"
            table.cell(0, 1).text = "Active Total"
            table.cell(1, 0).text = "N"
            table.cell(1, 1).text = "xx"
            document.save(source)
            report, _shell, _candidate = propose_docx_shell_map(
                source,
                expected_sha256=sha256_file(source),
                map_version="M20-GENERIC-ANALYSIS-SET",
            )
        self.assertEqual("SAF", report["entities"][0]["analysis_set"])
        self.assertEqual("low", report["entities"][0]["confidence"])
        self.assertIn("requires confirmation", report["entities"][0]["reasons"][0])

    def test_n4_units_have_an_explicit_current_runtime_owner(self) -> None:
        registry = load_production_method_registry()
        assignments = registry["assignments"]
        deterministic = {"12.3.1.1", "12.3.1.2", "12.3.1.3"}
        guarded = {"11.3", "12.3.3", "12.4.2.1", "12.4.2.2", "12.4.2.3"}

        for section_id in deterministic:
            with self.subTest(section_id=section_id):
                self.assertEqual(
                    "deterministic_generation",
                    assignments[section_id]["production_method"],
                )
                self.assertEqual(
                    "deterministic_generation", assignments[section_id]["narration"]
                )
                self.assertNotIn(section_id, PLAYBOOK_FILENAMES)

        for section_id in guarded:
            with self.subTest(section_id=section_id):
                self.assertEqual("llm_drafting", assignments[section_id]["production_method"])
                self.assertEqual(
                    "guarded_section_writer", assignments[section_id]["narration"]
                )
                self.assertIn(section_id, PLAYBOOK_FILENAMES)
                self.assertIn(section_id, SECTION_VARIANTS)

        self.assertEqual(
            {
                "deterministic_generation": 48,
                "extraction": 29,
                "llm_drafting": 34,
                "mixed": 7,
                "template_slot": 4,
            },
            registry["production_method_counts"],
        )

    def test_serious_event_analysis_is_a_medical_review_section(self) -> None:
        self.assertTrue(is_conclusion_section("12.3.3"))
        self.assertIn(
            ("12.3.3", "serious_event_analysis_conditioned"),
            EXPECTED_CONCLUSION_RULES,
        )

    def test_sentence_driven_token_is_exclusive_and_extra_prose_hard_fails(self) -> None:
        for section_id in ("11.5.10", "11.6.7", "13"):
            envelope = _envelope(section_id)
            baseline = _baseline(section_id, envelope)
            with self.subTest(section_id=section_id, case="baseline"):
                self.assertEqual(1, len(_validate_generated_paragraphs(baseline, envelope)))
            attack = baseline + (
                DraftParagraph(
                    text="[待医学负责人确认] 主要结果支持积极判断。",
                    sources=("audit_source",),
                    confidence="inferred",
                    needs_human=True,
                ),
            )
            with self.subTest(section_id=section_id, case="unsourced"), self.assertRaisesRegex(
                SectionWriterError, "E-CONCLUSION-UNSOURCED-PROSE"
            ):
                _validate_generated_paragraphs(attack, envelope)

    def test_cli_resolve_expands_conditioned_conclusion_before_numeric_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft = draft_section(_request("11.5.10"), root / "project")
            datapack = root / "datapack.json"
            facts = root / "facts.json"
            output = root / "resolved.md"
            report = root / "guard.json"
            atomic_write_json(datapack, _datapack())
            atomic_write_json(
                facts,
                json.loads(
                    (Path(__file__).resolve().parents[1] / "examples" / "demo_protocol_facts.json")
                    .read_text(encoding="utf-8")
                ),
            )
            rc = cli_main(
                [
                    "resolve",
                    "--draft",
                    str(draft.markdown_path),
                    "--datapack",
                    str(datapack),
                    "--protocol-facts",
                    str(facts),
                    "--section-metadata",
                    str(draft.metadata_path),
                    "--output",
                    str(output),
                    "--report",
                    str(report),
                ]
            )
            payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(0, rc)
        self.assertEqual("pass", payload["status"])
        self.assertEqual("pass", payload["conditional_conclusion"]["status"])

    def test_certified_zero_placeholder_method_artifact_can_enter_assembly(self) -> None:
        root = Path(__file__).resolve().parents[1]
        card = load_data(
            root / "examples" / "fixtures" / "m20_unfamiliar" / "method_fact_card.9.1.json"
        )
        draft = load_data(
            root
            / "examples"
            / "fixtures"
            / "m20_unfamiliar"
            / "method_draft.9.1.retry1.json"
        )
        artifact = ingest_constrained_draft(emit_fact_card_envelope(card), draft)
        with tempfile.TemporaryDirectory() as directory:
            markdown = Path(directory) / "9.1.md"
            metadata = Path(directory) / "9.1.meta.json"
            write_constrained_artifact(artifact, markdown, metadata)
            section = load_constrained_resolved_section(
                markdown, metadata, evidence_status="demo_only"
            )
        self.assertEqual("9.1", section.section_id)
        self.assertEqual(3, len(section.paragraphs))
        self.assertFalse(any(item.needs_human for item in section.paragraphs))

    def test_three_consecutive_field_copula_sentences_are_reported(self) -> None:
        report = style_report(
            "频次为每日一次或每日两次。"
            "途径为皮肤局部外用。"
            "疗程为连续治疗期。"
        )
        self.assertEqual(1, report["metrics"]["field_dump_sequence_count"])
        self.assertIn(
            "W-STYLE-FIELD-DUMP-SEQUENCE",
            {item["code"] for item in report["findings"]},
        )


if __name__ == "__main__":
    unittest.main()
