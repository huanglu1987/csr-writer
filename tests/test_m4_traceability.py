from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.traceability import (
    TraceabilityError,
    analyze_tfl_references,
    render_unreferenced_tfl,
    write_unreferenced_tfl,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "m4"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestM4Traceability(unittest.TestCase):
    def setUp(self):
        self.datapack = load_json("synthetic_datapack.json")
        self.metadata = load_json("section_metadata.json")

    def test_unreferenced_pk_table_is_listed(self):
        report = analyze_tfl_references(self.datapack, self.metadata)

        self.assertEqual("review_required", report["status"])
        self.assertEqual(4, report["table_count"])
        self.assertEqual(3, report["referenced_table_count"])
        self.assertEqual(1, report["unreferenced_table_count"])
        self.assertEqual(
            ["T14.4.1.1"],
            [table["table_id"] for table in report["unreferenced_tables"]],
        )
        markdown = render_unreferenced_tfl(report)
        self.assertIn("T14.4.1.1", markdown)
        self.assertIn("Synthetic plasma PK parameters", markdown)
        self.assertIn("人工确认是否遗漏", markdown)

    def test_markdown_writer_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence" / "unreferenced_tfl.md"
            first = write_unreferenced_tfl(self.datapack, self.metadata, path)
            first_text = path.read_text(encoding="utf-8")
            second = write_unreferenced_tfl(self.datapack, self.metadata, path)

            self.assertEqual(first, second)
            self.assertEqual(first_text, path.read_text(encoding="utf-8"))
            self.assertTrue(first_text.endswith("\n"))

    def test_unknown_table_reference_hard_fails(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"][0].update(
            {
                "key": "T14.9.9|POP=FAS|ROW=PRIMARY_RESPONSE|COL=XYZ200MG",
                "source_id": "T14.9.9",
            }
        )

        with self.assertRaisesRegex(TraceabilityError, "E-TRACE-UNKNOWN-TABLE"):
            analyze_tfl_references(self.datapack, rogue)

    def test_unknown_cell_in_known_table_hard_fails(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"][0]["key"] = (
            "T14.2.1|POP=FAS|ROW=UNKNOWN_RESULT|COL=XYZ200MG"
        )

        with self.assertRaisesRegex(TraceabilityError, "E-TRACE-UNKNOWN-CELL"):
            analyze_tfl_references(self.datapack, rogue)

    def test_source_id_must_equal_semantic_key_table(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"][0]["source_id"] = "T14.4.1.1"

        with self.assertRaisesRegex(TraceabilityError, "E-TRACE-BINDING-SOURCE"):
            analyze_tfl_references(self.datapack, rogue)

    def test_duplicate_role_or_key_hard_fails(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"].append(
            copy.deepcopy(rogue[2]["binding_manifest"][0])
        )

        with self.assertRaisesRegex(TraceabilityError, "E-TRACE-BINDING-DUPLICATE"):
            analyze_tfl_references(self.datapack, rogue)

    def test_referencing_pk_table_clears_reverse_finding(self):
        complete = copy.deepcopy(self.metadata)
        complete.append(
            {
                "section": "11.5",
                "binding_manifest": [
                    {
                        "role": "pk_cmax",
                        "key": "T14.4.1.1|POP=PKAS|ROW=CMAX|COL=XYZ200MG",
                        "source_id": "T14.4.1.1",
                    }
                ],
            }
        )

        report = analyze_tfl_references(self.datapack, complete)

        self.assertEqual("pass", report["status"])
        self.assertEqual(0, report["unreferenced_table_count"])
        self.assertIn("未发现有映射结果", render_unreferenced_tfl(report))

    def test_figures_listings_and_subject_wide_tables_are_registered_not_false_result_gaps(self):
        datapack = json.loads(
            (ROOT / "examples" / "fixtures" / "datapack_semantic_v2.deidentified.json")
            .read_text(encoding="utf-8")
        )
        metadata = {
            "section": "12.2",
            "binding_manifest": [
                {
                    "role": "local_tolerance_result",
                    "key": "T14.3.1.3.1|POP=SS|ROW=SOC_GI/PT_SHARED|COL=PBO",
                    "source_id": "T14.3.1.3.1",
                }
            ],
        }
        report = analyze_tfl_references(datapack, metadata)
        self.assertEqual(2, report["metadata_only_table_count"])
        self.assertEqual(
            {"L16.2.1", "F14.2.4.1"},
            {item["table_id"] for item in report["metadata_only_tables"]},
        )
        self.assertFalse(
            {"L16.2.1", "F14.2.4.1"}
            & {item["table_id"] for item in report["unreferenced_tables"]}
        )


if __name__ == "__main__":
    unittest.main()
