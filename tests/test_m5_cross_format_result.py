from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from csr_writer.cross_format_result import (
    CrossFormatResultError,
    apply_rtf_values_to_shell,
    build_deidentified_result_rtf,
)
from csr_writer.tfl_map import build_tfl_map_candidate, freeze_tfl_map


def _cell(row: str, column: str, row_index: int, column_index: int) -> dict:
    return {
        "v": "xx",
        "display_value": "xx",
        "type": "statistic",
        "row": {
            "id": row,
            "label": row,
            "indent": 0,
            "physical_path": row,
            "parent_path": None,
        },
        "column": {"id": column, "label": column},
        "source_locator": {
            "source_id": "sha256:" + "a" * 64,
            "row_index": row_index,
            "column_index": column_index,
        },
        "dimensions": {"STAGE": "D1"},
    }


class M5CrossFormatResultTests(unittest.TestCase):
    def setUp(self):
        self.shell = {
            "schema_version": "1.0",
            "semantic_key_version": "2.0",
            "adapter_scope": "deidentified test shell",
            "listing_policy": {"default": "cell_level"},
            "mapping_manifest": {"filename": "shell.json", "sha256": "b" * 64},
            "sources": [
                {
                    "source_id": "sha256:" + "a" * 64,
                    "filename": "shell.docx",
                    "format": "test",
                }
            ],
            "tables": [
                {
                    "table_id": "T14.2.4.3.1",
                    "population": "PKS",
                    "source_id": "sha256:" + "a" * 64,
                    "titles": [],
                    "footnotes": [],
                    "trailer": {},
                    "adapter": "test",
                    "ingest_strategy": "cell_level",
                    "dimensions": {"STAGE": "D1"},
                }
            ],
            "cells": {
                "T14.2.4.3.1|POP=PKS|ROW=CMAX|COL=C0_5PCT_BID|STAGE=D1": _cell(
                    "CMAX", "C0_5PCT_BID", 1, 1
                ),
                "T14.2.4.3.1|POP=PKS|ROW=CMAX|COL=C1_0PCT_QD|STAGE=D1": _cell(
                    "CMAX", "C1_0PCT_QD", 1, 2
                ),
            },
        }
        candidate = build_tfl_map_candidate(
            self.shell,
            {
                "schema_version": "1.0",
                "map_version": "DEID-XFORMAT-1",
                "numbering_system": {"id": "ich-14x", "description": "deidentified"},
                "routes": [
                    {"entity_id": "T14.2.4.3.1", "ich_e3_sections": ["11.B"]}
                ],
            },
        )
        self.frozen = freeze_tfl_map(
            candidate,
            {
                "status": "confirmed",
                "confirmation_id": "DEID-CONFIRM-1",
                "confirmed_by_role": "test_reviewer",
                "confirmed_at": "2026-08-04T00:00:00+08:00",
                "candidate_sha256": candidate["candidate_sha256"],
            },
        )

    def test_docx_shell_contract_accepts_matching_rtf_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            rtf = Path(temporary) / "result.rtf"
            rtf.write_text(
                build_deidentified_result_rtf(self.frozen, "T14.2.4.3.1"),
                encoding="ascii",
            )
            result, report = apply_rtf_values_to_shell(
                self.shell, self.frozen, "T14.2.4.3.1", rtf
            )
        self.assertEqual("pass", report["status"])
        self.assertEqual("pass", report["checks"]["semantic_key_and_contract_drift"])
        self.assertEqual(2, report["semantic_cell_count"])
        self.assertNotEqual("xx", next(iter(result["cells"].values()))["v"])

    def test_result_row_identifier_drift_hard_fails(self):
        payload = build_deidentified_result_rtf(self.frozen, "T14.2.4.3.1")
        with tempfile.TemporaryDirectory() as temporary:
            rtf = Path(temporary) / "drift.rtf"
            rtf.write_text(payload.replace("CMAX", "CMAX_DRIFT", 1), encoding="ascii")
            with self.assertRaises(CrossFormatResultError) as caught:
                apply_rtf_values_to_shell(
                    self.shell, self.frozen, "T14.2.4.3.1", rtf
                )
        self.assertEqual("blocked", caught.exception.report["status"])
        self.assertIn(
            "E-RTF-ROW-DRIFT",
            {item["code"] for item in caught.exception.report["issues"]},
        )


if __name__ == "__main__":
    unittest.main()
