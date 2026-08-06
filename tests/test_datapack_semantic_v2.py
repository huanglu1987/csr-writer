from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from csr_writer.datapack import CELL_KEY_RE, _map_table, validate_datapack
from csr_writer.errors import DatapackError, SemanticMappingError
from csr_writer.physical_table import PhysicalCell, PhysicalRow
from csr_writer.rtf_parser import ParsedTable
from csr_writer.semantic_key import (
    SemanticKeyError,
    build_semantic_cell_key,
    parse_semantic_cell_key,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PLUGIN_ROOT / "examples" / "fixtures" / "datapack_semantic_v2.deidentified.json"
SOURCE_ID = "sha256:" + ("b" * 64)


def _hierarchical_table(rows: tuple[PhysicalRow, ...]) -> ParsedTable:
    return ParsedTable(
        source_path="deidentified-hierarchical.fixture",
        source_sha256="b" * 64,
        declared_codepage=65001,
        codec="utf-8",
        titles=("Deidentified hierarchical AE shell",),
        rows=rows,
        header_row_count=1,
        footnotes=(),
        trailer={},
    )


def _row(
    values: tuple[str, ...],
    *,
    header: bool = False,
    first_indent: int = 0,
) -> PhysicalRow:
    return PhysicalRow(
        tuple(
            PhysicalCell(
                text=value,
                bold=header,
                indent=first_indent if index == 0 else 0,
            )
            for index, value in enumerate(values)
        ),
        is_header=header,
    )


class SemanticKeyV2Tests(unittest.TestCase):
    def test_legacy_key_and_match_groups_remain_compatible(self):
        key = "T14.2.1|POP=FAS|ROW=OBJECTIVE_RESPONSE|COL=TRT"
        parsed = parse_semantic_cell_key(key)
        self.assertEqual(key, parsed.as_string())
        match = CELL_KEY_RE.fullmatch(key)
        self.assertIsNotNone(match)
        self.assertEqual("T14.2.1", match.group("table"))
        self.assertEqual("FAS", match.group("pop"))
        self.assertEqual("OBJECTIVE_RESPONSE", match.group("row"))
        self.assertEqual("TRT", match.group("col"))

    def test_extensible_dimensions_are_canonical_and_unique(self):
        key = build_semantic_cell_key(
            "T14.2.4.3.1",
            "mFAS",
            "SOC_胃肠系统/PT_恶心",
            "DOSE_0P5_BID",
            {"STAGE": "D1", "PERIOD": "SINGLE_DOSE"},
        )
        self.assertEqual(
            "T14.2.4.3.1|POP=mFAS|ROW=SOC_胃肠系统/PT_恶心|"
            "COL=DOSE_0P5_BID|PERIOD=SINGLE_DOSE|STAGE=D1",
            key,
        )
        self.assertEqual(
            {"PERIOD": "SINGLE_DOSE", "STAGE": "D1"},
            parse_semantic_cell_key(key).extra_dimensions,
        )
        with self.assertRaisesRegex(SemanticKeyError, "repeated"):
            parse_semantic_cell_key(
                "T14.2.1|POP=FAS|ROW=R|COL=C|STAGE=D1|STAGE=D28"
            )
        with self.assertRaisesRegex(SemanticKeyError, "lexical order"):
            parse_semantic_cell_key(
                "T14.2.1|POP=FAS|ROW=R|COL=C|STAGE=D1|PERIOD=P1"
            )
        with self.assertRaisesRegex(SemanticKeyError, "exactly one"):
            parse_semantic_cell_key("T14.2.1|POP=FAS|ROW=R|COL=C=BAD")

    def test_table_kinds_and_analysis_set_names_are_not_enumerated(self):
        for table_id in ("T14.2.1", "F14.2.4.1", "L1.1"):
            for population in ("mFAS", "mITT", "mPPS", "All Screened Set"):
                key = build_semantic_cell_key(table_id, population, "RESULT", "TOTAL")
                self.assertEqual(population, parse_semantic_cell_key(key).population)

    def test_deidentified_fixture_expresses_all_required_real_structures(self):
        datapack = json.loads(FIXTURE.read_text(encoding="utf-8"))
        validate_datapack(datapack)
        keys = set(datapack["cells"])
        self.assertIn(
            "T14.3.1.3.1|POP=SS|ROW=SOC_GI/PT_SHARED|COL=PBO", keys
        )
        self.assertIn(
            "T14.3.1.3.1|POP=SS|ROW=SOC_NERVOUS/PT_SHARED|COL=PBO", keys
        )
        for column in (
            "DOSE_0P5_BID",
            "DOSE_0P75_BID",
            "DOSE_1P0_BID",
            "DOSE_1P0_QD",
            "ACTIVE_TOTAL",
            "TOTAL",
        ):
            self.assertIn(f"T14.3.1.3.1|POP=SS|ROW=SUMMARY|COL={column}", keys)
        self.assertIn(
            "T14.2.4.3.1|POP=PKS|ROW=CMAX|COL=DOSE_1P0_BID|STAGE=D1",
            keys,
        )
        self.assertIn(
            "T14.2.4.3.2|POP=PKS|ROW=CMAX|COL=DOSE_1P0_BID|STAGE=D28",
            keys,
        )
        self.assertEqual(
            {"date", "text_judgement", "composite", "status"},
            {
                cell["type"]
                for key, cell in datapack["cells"].items()
                if key.startswith("T14.1.2.1|")
            },
        )
        listing = next(table for table in datapack["tables"] if table["table_id"] == "L16.2.1")
        self.assertEqual("metadata_only", listing["ingest_strategy"])
        self.assertFalse(any(key.startswith("L16.2.1|") for key in keys))

    def test_indent_builds_parent_qualified_rows_and_allows_cross_parent_leaf(self):
        parsed = _hierarchical_table(
            (
                _row(("Term", "Placebo"), header=True),
                _row(("SOC GI", "xx")),
                _row(("Shared PT", "xx"), first_indent=2),
                _row(("SOC Nervous", "xx")),
                _row(("Shared PT", "xx"), first_indent=2),
            )
        )
        source_spec = {
            "table_id": "T14.3.1.3.1",
            "population": "mFAS",
            "expected_titles": ["Deidentified hierarchical AE shell"],
            "mapping": {
                "columns": {"Placebo": "PBO"},
                "rows": {
                    "SOC GI": "SOC_GI",
                    "SOC GI/Shared PT": "SOC_GI/PT_SHARED",
                    "SOC Nervous": "SOC_NERVOUS",
                    "SOC Nervous/Shared PT": "SOC_NERVOUS/PT_SHARED",
                },
                "types": {
                    "SOC_GI": "n_pct",
                    "SOC_GI/PT_SHARED": "n_pct",
                    "SOC_NERVOUS": "n_pct",
                    "SOC_NERVOUS/PT_SHARED": "n_pct",
                },
                "cell_column_overrides": {},
            },
        }
        mapped = _map_table(parsed, source_spec, SOURCE_ID)
        gi_key = "T14.3.1.3.1|POP=mFAS|ROW=SOC_GI/PT_SHARED|COL=PBO"
        nervous_key = "T14.3.1.3.1|POP=mFAS|ROW=SOC_NERVOUS/PT_SHARED|COL=PBO"
        self.assertIn(gi_key, mapped["cells"])
        self.assertIn(nervous_key, mapped["cells"])
        self.assertEqual("SOC GI", mapped["cells"][gi_key]["row"]["parent_path"])

    def test_duplicate_leaf_inside_same_parent_is_hard_failure(self):
        parsed = _hierarchical_table(
            (
                _row(("Term", "Placebo"), header=True),
                _row(("SOC GI", "xx")),
                _row(("Shared PT", "xx"), first_indent=2),
                _row(("Shared PT", "xx"), first_indent=2),
            )
        )
        source_spec = {
            "table_id": "T14.3.1.3.1",
            "population": "SS",
            "mapping": {
                "columns": {"Placebo": "PBO"},
                "rows": {
                    "SOC GI": "SOC_GI",
                    "SOC GI/Shared PT": "SOC_GI/PT_SHARED",
                },
                "types": {"SOC_GI": "n_pct", "SOC_GI/PT_SHARED": "n_pct"},
                "cell_column_overrides": {},
            },
        }
        with self.assertRaisesRegex(
            SemanticMappingError, "Duplicate row label within the same parent"
        ):
            _map_table(parsed, source_spec, SOURCE_ID)

    def test_metadata_only_table_cannot_hide_cell_keys(self):
        datapack = json.loads(FIXTURE.read_text(encoding="utf-8"))
        sample = copy.deepcopy(next(iter(datapack["cells"].values())))
        sample["row"]["id"] = "SUBJECT_001"
        sample["column"]["id"] = "STATUS"
        datapack["cells"][
            "L16.2.1|POP=All Enrolled Set|ROW=SUBJECT_001|COL=STATUS"
        ] = sample
        with self.assertRaisesRegex(DatapackError, "metadata_only"):
            validate_datapack(datapack)

    def test_metadata_only_strategy_builds_table_record_without_subject_cells(self):
        parsed = _hierarchical_table(
            (
                _row(("Subject", "Status"), header=True),
                _row(("SYNTHETIC-001", "completed")),
            )
        )
        mapped = _map_table(
            parsed,
            {
                "table_id": "L16.2.1",
                "population": "All Enrolled Set",
                "ingest_strategy": "metadata_only",
                "metadata_only_reason": "Subject-level wide listing; table metadata only.",
            },
            SOURCE_ID,
        )
        self.assertEqual({}, mapped["cells"])
        self.assertEqual("metadata_only", mapped["table"]["ingest_strategy"])
        self.assertEqual(1, mapped["table"]["physical_row_count"])

    def test_datapack_may_contain_only_metadata_only_entities(self):
        datapack = json.loads(FIXTURE.read_text(encoding="utf-8"))
        datapack["tables"] = [
            table
            for table in datapack["tables"]
            if table["ingest_strategy"] == "metadata_only"
        ]
        datapack["cells"] = {}
        validate_datapack(datapack)


if __name__ == "__main__":
    unittest.main()
