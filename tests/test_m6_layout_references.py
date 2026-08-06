from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.io_utils import sha256_file
from csr_writer.semantic_key import build_semantic_cell_key
from csr_writer.shell_mapping import ShellMappingError, propose_docx_shell_map
from csr_writer.tfl_map import TFLMapDriftError, check_result_against_frozen_map, freeze_tfl_map


def _grid(document: Document, rows: list[str], *, soc_pt: bool = False) -> None:
    table = document.add_table(rows=len(rows) + 1, cols=3)
    table.cell(0, 0).text = "SOC/PT" if soc_pt else "Statistic"
    table.cell(0, 1).text = "Placebo"
    table.cell(0, 2).text = "1.0% QD"
    for index, label in enumerate(rows, start=1):
        table.cell(index, 0).text = label
        table.cell(index, 1).text = "xx"
        table.cell(index, 2).text = "xx"


def _reference_placeholder(document: Document, text: str) -> None:
    table = document.add_table(rows=2, cols=1)
    table.cell(0, 0).text = text
    table.cell(1, 0).text = "N = analysis population"


def _confirmation(candidate):
    return {
        "status": "confirmed",
        "confirmation_id": "M6-LAYOUT-TEST",
        "confirmed_by_role": "test_reviewer",
        "confirmed_at": "2026-08-04T00:00:00+08:00",
        "candidate_sha256": candidate["candidate_sha256"],
    }


class M6LayoutReferenceTests(unittest.TestCase):
    def _build_valid_fixture(self, path: Path) -> None:
        document = Document()
        document.add_paragraph("表14.1.3.1 SOC and PT history (FAS)")
        _grid(document, ["System Organ Class", "  Preferred Term"], soc_pt=True)
        document.add_paragraph("表14.1.3.2 SOC and PT procedure history (mFAS)")
        _reference_placeholder(document, "Use the template from Table14.1.3.1")
        document.add_paragraph("表14.2.2.1.1 ANCOVA statistics (FAS)")
        _grid(document, ["N", "Mean", "SD", "LS Mean Diff", "95% CI", "P-value"])
        document.add_paragraph("表14.2.2.2.1 Related endpoint (mFAS)")
        _reference_placeholder(document, "Same as Table 14.2.2.1.1.")
        document.save(path)

    def _config(self):
        return {
            "schema_version": "1.0",
            "patterns": [
                {
                    "id": "use-template-en",
                    "regex": r"Use the template from Table(?P<target>\d+(?:\.\d+)+)",
                },
                {
                    "id": "same-table-en",
                    "regex": r"Same as Table\s*(?P<target>\d+(?:\.\d+)+)",
                },
            ],
            "data_driven_row_patterns": [
                {"id": "soc-pt", "regex": r"SOC\s*(?:and|/)\s*PT"}
            ],
        }

    def test_columns_and_statistics_inherit_but_data_rows_do_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "layout-shell.docx"
            self._build_valid_fixture(source)
            report, datapack, candidate = propose_docx_shell_map(
                source,
                expected_sha256=sha256_file(source),
                map_version="M6-LAYOUT-TEST",
                layout_reference_config=self._config(),
            )

        entities = {item["entity_id"]: item for item in report["entities"]}
        tables = {item["table_id"]: item for item in datapack["tables"]}
        self.assertEqual(2, report["metrics"]["layout_inherited_grid_entities"])
        self.assertEqual(
            "columns_only_deferred_rows", tables["T14.1.3.2"]["ingest_strategy"]
        )
        self.assertEqual("mFAS", tables["T14.1.3.2"]["population"])
        self.assertEqual("T14.1.3.1", tables["T14.1.3.2"]["layout_reference"]["inherited_from"])
        self.assertEqual("deferred_until_result", tables["T14.1.3.2"]["layout_reference"]["rows"])
        self.assertFalse(any(key.startswith("T14.1.3.2|") for key in datapack["cells"]))
        self.assertEqual("cell_level", tables["T14.2.2.2.1"]["ingest_strategy"])
        self.assertEqual("mFAS", tables["T14.2.2.2.1"]["population"])
        self.assertTrue(any(key.startswith("T14.2.2.2.1|") for key in datapack["cells"]))
        self.assertEqual("medium", entities["T14.1.3.2"]["confidence"])
        self.assertEqual("high", entities["T14.2.2.2.1"]["confidence"])
        self.assertEqual(
            "nonsemantic_row_id",
            tables["T14.1.3.2"]["deferred_row_contract"]["detection"],
        )
        frozen = freeze_tfl_map(candidate, _confirmation(candidate))
        inherited = next(item for item in frozen["entities"] if item["entity_id"] == "T14.1.3.2")
        self.assertEqual("T14.1.3.1", inherited["layout_reference"]["inherited_from"])

    def test_deferred_rows_may_materialize_only_under_frozen_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "layout-shell.docx"
            self._build_valid_fixture(source)
            _, shell, candidate = propose_docx_shell_map(
                source,
                expected_sha256=sha256_file(source),
                map_version="M6-LAYOUT-LATE-ROWS",
                layout_reference_config=self._config(),
            )
        frozen = freeze_tfl_map(candidate, _confirmation(candidate))
        result = copy.deepcopy(shell)
        table = next(item for item in result["tables"] if item["table_id"] == "T14.1.3.2")
        source_id = table["source_id"]
        for column_index, column_id in enumerate(
            table["layout_reference"]["inherited_column_ids"], start=1
        ):
            key = build_semantic_cell_key("T14.1.3.2", "mFAS", "SOC_A/PT_X", column_id)
            result["cells"][key] = {
                "v": "xx",
                "display_value": "xx",
                "type": "statistic",
                "row": {
                    "id": "SOC_A/PT_X",
                    "label": "SOC_A/PT_X",
                    "indent": 1,
                    "physical_path": "SOC_A/PT_X",
                    "parent_path": "SOC_A",
                },
                "column": {"id": column_id, "label": column_id},
                "source_locator": {
                    "source_id": source_id,
                    "row_index": 1,
                    "column_index": column_index,
                },
                "dimensions": {},
            }
        report = check_result_against_frozen_map(result, frozen)
        self.assertEqual("pass", report["status"])
        self.assertEqual(1, len(report["late_bound_row_entities"]))

        rogue = copy.deepcopy(result)
        key = build_semantic_cell_key("T14.1.3.2", "mFAS", "SOC_A/PT_X", "UNREVIEWED")
        inherited_key = next(
            item for item in result["cells"] if item.startswith("T14.1.3.2|")
        )
        rogue["cells"][key] = copy.deepcopy(result["cells"][inherited_key])
        rogue["cells"][key]["column"] = {"id": "UNREVIEWED", "label": "UNREVIEWED"}
        with self.assertRaises(TFLMapDriftError):
            check_result_against_frozen_map(rogue, frozen)

    def test_cycle_missing_target_and_target_without_grid_are_hard_failures(self):
        cases = (
            ("missing", [("14.2.1.1", "14.2.9.9")]),
            ("target-without-grid", [("14.2.1.1", "14.2.1.2"), ("14.2.1.2", "14.2.1.3")]),
            ("cycle", [("14.2.1.1", "14.2.1.2"), ("14.2.1.2", "14.2.1.1")]),
        )
        for label, pairs in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "invalid-layout.docx"
                document = Document()
                for entity, target in pairs:
                    document.add_paragraph(f"表{entity} Placeholder (FAS)")
                    _reference_placeholder(document, f"Same as Table {target}.")
                if label == "target-without-grid":
                    document.add_paragraph("表14.2.1.3 Placeholder (FAS)")
                    _reference_placeholder(document, "No reference")
                document.save(source)
                with self.assertRaises(ShellMappingError):
                    propose_docx_shell_map(
                        source,
                        expected_sha256=sha256_file(source),
                        layout_reference_config=self._config(),
                    )


if __name__ == "__main__":
    unittest.main()
