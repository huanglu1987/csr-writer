from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.cross_format_result import (
    CrossFormatResultError,
    apply_rtf_values_to_shell,
    build_deidentified_heteromorphic_result_rtf,
    build_deidentified_result_rtf,
)
from csr_writer.io_utils import load_json_unique, sha256_file
from csr_writer.semantic_key import parse_semantic_cell_key
from csr_writer.shell_mapping import propose_docx_shell_map
from csr_writer.tfl_map import freeze_tfl_map


ROOT = Path(__file__).resolve().parents[1]
ROW_FIXTURE = ROOT / "examples" / "fixtures" / "m7_heteromorphic_rows.deidentified.json"


def _grid(document: Document, rows: list[tuple[int, str]]) -> None:
    table = document.add_table(rows=len(rows) + 1, cols=3)
    table.cell(0, 0).text = "ROW"
    table.cell(0, 1).text = "PBO"
    table.cell(0, 2).text = "C1_0PCT_QD"
    for row_index, (indent, label) in enumerate(rows, start=1):
        paragraph = table.cell(row_index, 0).paragraphs[0]
        paragraph.text = (" " * indent) + label
        table.cell(row_index, 1).text = "xx"
        table.cell(row_index, 2).text = "xx"


def _confirmation(candidate):
    return {
        "status": "confirmed",
        "confirmation_id": "M7-HETERO-TEST",
        "confirmed_by_role": "test_reviewer",
        "confirmed_at": "2026-08-04T00:00:00+08:00",
        "candidate_sha256": candidate["candidate_sha256"],
    }


class M7HeteromorphicResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_json_unique(ROW_FIXTURE)

    def _fixture(self, path: Path):
        document = Document()
        document.add_paragraph("表14.3.1.3.1 SOC/PT result (SS)")
        _grid(
            document,
            [
                (0, "系统器官分类1"),
                (2, "首选术语1"),
                (2, "首选术语2"),
            ],
        )
        document.add_paragraph("表14.1.4.1 Medication result (FAS)")
        _grid(document, [(0, "ATC分类1"), (2, "药物名1")])
        document.add_paragraph("表14.2.2.1.1 ANCOVA result (FAS)")
        _grid(
            document,
            [
                (0, "N"),
                (0, "Mean"),
                (0, "SD"),
                (0, "LS Mean Diff"),
                (0, "95% CI"),
                (0, "P-value"),
            ],
        )
        document.add_paragraph("表14.3.1.3.2 Mixed rows (SS)")
        _grid(document, [(0, "N"), (0, "系统器官分类1"), (2, "首选术语1")])
        document.save(path)

    def _build(self, temporary: str):
        shell_path = Path(temporary) / "heteromorphic-shell.docx"
        self._fixture(shell_path)
        report, shell, candidate = propose_docx_shell_map(
            shell_path,
            expected_sha256=sha256_file(shell_path),
            map_version="M7-HETERO-1",
        )
        frozen = freeze_tfl_map(candidate, _confirmation(candidate))
        return report, shell, frozen

    def test_placeholder_rows_are_deferred_but_stable_statistics_are_frozen(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, shell, _frozen = self._build(temporary)
        tables = {item["table_id"]: item for item in shell["tables"]}
        entities = {item["entity_id"]: item for item in report["entities"]}
        self.assertEqual(
            "columns_only_deferred_rows", tables["T14.3.1.3.1"]["ingest_strategy"]
        )
        self.assertEqual(
            "columns_only_deferred_rows", tables["T14.1.4.1"]["ingest_strategy"]
        )
        self.assertFalse(
            any(key.startswith("T14.3.1.3.1|") for key in shell["cells"])
        )
        self.assertFalse(any(key.startswith("T14.1.4.1|") for key in shell["cells"]))
        self.assertEqual("cell_level", tables["T14.2.2.1.1"]["ingest_strategy"])
        self.assertTrue(any(key.startswith("T14.2.2.1.1|") for key in shell["cells"]))
        self.assertEqual(
            "mixed_stable_and_deferred_rows",
            tables["T14.3.1.3.2"]["ingest_strategy"],
        )
        mixed_keys = [key for key in shell["cells"] if key.startswith("T14.3.1.3.2|")]
        self.assertEqual({"N"}, {parse_semantic_cell_key(key).row for key in mixed_keys})
        self.assertEqual(2, entities["T14.3.1.3.2"]["deferred_row_contract"]["shell_placeholder_row_count"])

    def test_data_rows_materialize_from_heteromorphic_results_and_ancova_stays_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            _report, shell, frozen = self._build(temporary)
            soc_rows = [tuple(item) for item in self.rows["soc_pt"]["rows"]]
            soc_rtf = Path(temporary) / "soc-result.rtf"
            soc_rtf.write_text(
                build_deidentified_heteromorphic_result_rtf(
                    frozen, "T14.3.1.3.1", soc_rows
                ),
                encoding="ascii",
            )
            result, soc_report = apply_rtf_values_to_shell(
                shell, frozen, "T14.3.1.3.1", soc_rtf
            )
            medication_rtf = Path(temporary) / "medication-result.rtf"
            medication_rtf.write_text(
                build_deidentified_heteromorphic_result_rtf(
                    frozen,
                    "T14.1.4.1",
                    [tuple(item) for item in self.rows["medication"]["rows"]],
                ),
                encoding="ascii",
            )
            result, medication_report = apply_rtf_values_to_shell(
                result, frozen, "T14.1.4.1", medication_rtf
            )
            mixed_rtf = Path(temporary) / "mixed-result.rtf"
            mixed_rtf.write_text(
                build_deidentified_heteromorphic_result_rtf(
                    frozen,
                    "T14.3.1.3.2",
                    [tuple(item) for item in self.rows["mixed"]["rows"]],
                ),
                encoding="ascii",
            )
            result, mixed_report = apply_rtf_values_to_shell(
                result, frozen, "T14.3.1.3.2", mixed_rtf
            )
            ancova_rtf = Path(temporary) / "ancova-result.rtf"
            ancova_rtf.write_text(
                build_deidentified_result_rtf(frozen, "T14.2.2.1.1"),
                encoding="ascii",
            )
            result, ancova_report = apply_rtf_values_to_shell(
                result, frozen, "T14.2.2.1.1", ancova_rtf
            )

        soc_keys = [
            parse_semantic_cell_key(key)
            for key in result["cells"]
            if key.startswith("T14.3.1.3.1|")
        ]
        shared = sorted(item.row for item in soc_keys if item.row_leaf == "PT_SHARED")
        self.assertEqual(
            ["SOC_1/PT_SHARED", "SOC_1/PT_SHARED", "SOC_2/PT_SHARED", "SOC_2/PT_SHARED"],
            shared,
        )
        self.assertEqual(1, soc_report["cross_parent_duplicate_leaf_count"])
        self.assertEqual(26, soc_report["materialized_row_count"])
        self.assertEqual(5, medication_report["materialized_row_count"])
        self.assertEqual(1, mixed_report["stable_row_count"])
        self.assertEqual(5, mixed_report["late_bound_row_count"])
        self.assertEqual("pass", ancova_report["checks"]["row_identifiers"])
        self.assertEqual("pass", ancova_report["checks"]["semantic_key_and_contract_drift"])

    def test_stable_result_row_drift_reports_table_and_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            _report, shell, frozen = self._build(temporary)
            payload = build_deidentified_result_rtf(frozen, "T14.2.2.1.1")
            rtf = Path(temporary) / "ancova-drift.rtf"
            rtf.write_text(
                payload.replace(
                    self.rows["ancova_control"]["drift_from"],
                    self.rows["ancova_control"]["drift_to"],
                    1,
                ),
                encoding="ascii",
            )
            with self.assertRaises(CrossFormatResultError) as caught:
                apply_rtf_values_to_shell(
                    shell, frozen, "T14.2.2.1.1", rtf
                )
        issue = next(
            item for item in caught.exception.report["issues"]
            if item["code"] == "E-RTF-ROW-DRIFT"
        )
        self.assertEqual("T14.2.2.1.1", caught.exception.report["entity_id"])
        self.assertEqual("MEAN", issue["expected"])

    def test_duplicate_pt_inside_one_parent_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            _report, shell, frozen = self._build(temporary)
            rtf = Path(temporary) / "same-parent-duplicate.rtf"
            rtf.write_text(
                build_deidentified_heteromorphic_result_rtf(
                    frozen,
                    "T14.3.1.3.1",
                    [(0, "SOC_A"), (1, "PT_DUP"), (1, "PT_DUP")],
                ),
                encoding="ascii",
            )
            with self.assertRaises(CrossFormatResultError) as caught:
                apply_rtf_values_to_shell(shell, frozen, "T14.3.1.3.1", rtf)
        issue_codes = {item["code"] for item in caught.exception.report["issues"]}
        self.assertIn("E-RTF-ROW-COLLISION", issue_codes)


if __name__ == "__main__":
    unittest.main()
