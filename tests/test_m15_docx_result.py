from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.docx_result import apply_docx_result_to_frozen_shell, _match_ancova_row
from csr_writer.docx_tfl_parser import parse_docx_tfl_tables
from csr_writer.io_utils import sha256_file
from csr_writer.shell_mapping import (
    _column_id,
    _flatten_header_paths_with_visual_spanners,
    propose_docx_shell_map,
)
from csr_writer.tfl_map import (
    build_tfl_map_candidate,
    check_result_against_frozen_map,
    freeze_tfl_map,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT_FIXTURE = ROOT / "examples" / "fixtures" / "synthetic_result_tfl.deidentified.docx"


class M15DocxResultTests(unittest.TestCase):
    @staticmethod
    def _mixed_document(path: Path, *, result: bool) -> None:
        document = Document()
        document.add_paragraph("表14.3.1.3.2 Mixed rows (SS)")
        table = document.add_table(rows=4, cols=3)
        for index, value in enumerate(("ROW", "PBO", "C1.0% QD")):
            table.cell(0, index).text = value
        table.cell(1, 0).text = "N"
        table.cell(2, 0).text = "系统器官分类1" if not result else "SOC_A"
        table.cell(3, 0).paragraphs[0].text = "  首选术语1" if not result else "  PT_A"
        for row_index in range(1, 4):
            table.cell(row_index, 1).text = str(row_index) if result else "xx"
            table.cell(row_index, 2).text = str(row_index + 3) if result else "xx"
        document.save(path)

    def test_visual_spanner_is_flattened_without_relaxing_physical_parser(self):
        parsed = parse_docx_tfl_tables(
            RESULT_FIXTURE,
            allow_first_row_header=True,
            allow_ambiguous_header_paths=True,
            selected_table_indexes=[6],
        ).tables[0]
        paths, inferred = _flatten_header_paths_with_visual_spanners(
            parsed.physical_table
        )
        self.assertEqual(inferred, 2)
        self.assertEqual(
            paths,
            (
                "参数",
                "单位",
                "0.5% BID / N",
                "0.5% BID / Geo Mean (CV%)",
                "1.0% BID / N",
                "1.0% BID / Geo Mean (CV%)",
            ),
        )

    def test_ae_column_ids_preserve_group_and_statistic(self):
        self.assertEqual(_column_id("安慰剂 (N=XX) / 例数 n (%)"), "PBO_N_PCT")
        self.assertEqual(_column_id("安慰剂 (N=XX) / 例次"), "PBO_EVENT_COUNT")
        self.assertEqual(
            _column_id("试验药Total (N=XX) / 例数 n (%)"),
            "ACTIVE_TOTAL_N_PCT",
        )

    def test_change_from_baseline_cannot_alias_baseline(self):
        frozen_rows = {
            "BASELINE/OBSERVED_VALUE/MEAN_SD",
            "D15/CHANGE_FROM_BASELINE/MEAN_SD",
            "D29/CHANGE_FROM_BASELINE/MEAN_SD",
        }
        row_id, reason = _match_ancova_row("较基线变化值 Mean (SD)", frozen_rows)
        self.assertIsNone(row_id)
        self.assertEqual(reason, "stable_row_ambiguous_without_visit")

    def test_semantic_map_survives_physical_coordinate_change(self):
        source_id = "sha256:" + "1" * 64
        key = "T14.2.1|POP=FAS|ROW=MEAN|COL=PBO"
        datapack = {
            "schema_version": "1.0",
            "semantic_key_version": "2.0",
            "adapter_scope": "test",
            "listing_policy": {},
            "mapping_manifest": {"filename": "test.json", "sha256": "2" * 64},
            "sources": [{"source_id": source_id}],
            "tables": [
                {
                    "table_id": "T14.2.1",
                    "population": "FAS",
                    "source_id": source_id,
                    "titles": [],
                    "footnotes": [],
                    "trailer": {},
                    "adapter": "test",
                    "ingest_strategy": "cell_level",
                    "dimensions": {},
                }
            ],
            "cells": {
                key: {
                    "v": "xx",
                    "display_value": "xx",
                    "type": "statistic",
                    "row": {
                        "id": "MEAN",
                        "label": "MEAN",
                        "indent": 0,
                        "physical_path": "MEAN",
                        "parent_path": None,
                    },
                    "column": {"id": "PBO", "label": "PBO"},
                    "source_locator": {
                        "source_id": source_id,
                        "row_index": 1,
                        "column_index": 1,
                    },
                    "dimensions": {},
                }
            },
        }
        candidate = build_tfl_map_candidate(
            datapack,
            {
                "schema_version": "1.0",
                "map_version": "M15-TEST",
                "numbering_system": {"id": "ich", "description": "test"},
                "routes": [{"entity_id": "T14.2.1", "ich_e3_sections": ["11.4"]}],
            },
        )
        frozen = freeze_tfl_map(
            candidate,
            {
                "status": "confirmed",
                "confirmation_id": "M15-TEST",
                "confirmed_by_role": "test_reviewer",
                "confirmed_at": "2026-08-05T00:00:00+08:00",
                "candidate_sha256": candidate["candidate_sha256"],
            },
        )
        result = copy.deepcopy(datapack)
        result["cells"][key]["v"] = "1.2"
        result["cells"][key]["display_value"] = "1.2"
        result["cells"][key]["source_locator"]["row_index"] = 8
        result["cells"][key]["source_locator"]["column_index"] = 4
        self.assertEqual(check_result_against_frozen_map(result, frozen)["status"], "pass")

    def test_mixed_table_applies_stable_row_and_defers_only_unreadable_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            shell_path = Path(temporary) / "shell.docx"
            result_path = Path(temporary) / "result.docx"
            self._mixed_document(shell_path, result=False)
            self._mixed_document(result_path, result=True)
            proposal, shell, candidate = propose_docx_shell_map(
                shell_path,
                expected_sha256=sha256_file(shell_path),
                map_version="M15-MIXED-TEST",
            )
            frozen = freeze_tfl_map(
                candidate,
                {
                    "status": "confirmed",
                    "confirmation_id": "M15-MIXED-TEST",
                    "confirmed_by_role": "test_reviewer",
                    "confirmed_at": "2026-08-05T00:00:00+08:00",
                    "candidate_sha256": candidate["candidate_sha256"],
                },
            )
            result, report = apply_docx_result_to_frozen_shell(
                shell,
                frozen,
                result_path,
                expected_sha256=sha256_file(result_path),
            )
        entity = report["entity_reports"][0]
        self.assertEqual("pass", report["status"])
        self.assertEqual(1, entity["stable_result_row_count"])
        self.assertEqual(2, entity["matched_stable_cell_count"])
        self.assertEqual(4, entity["matched_result_value_count"])
        self.assertEqual(0, entity["unresolved_frozen_cell_count"])
        observed = [
            cell
            for cell in result["cells"].values()
            if cell.get("result_value_status") == "observed_result"
        ]
        self.assertEqual(4, len(observed))


if __name__ == "__main__":
    unittest.main()
