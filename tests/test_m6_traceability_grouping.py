from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from csr_writer.traceability import analyze_tfl_references, render_unreferenced_tfl
from csr_writer.tfl_routing import expected_tfl_route


ROOT = Path(__file__).resolve().parents[1]


class M6TraceabilityGroupingTests(unittest.TestCase):
    def test_number_node_boundaries_do_not_confuse_three_with_thirty_five(self):
        self.assertEqual(
            "12.5.1.local_tolerance", expected_tfl_route("T14.3.1.3.3").category_id
        )
        self.assertEqual("12.2.ae", expected_tfl_route("T14.3.1.3.35").category_id)

    def test_only_unclassified_tables_are_immediate_human_review(self):
        datapack = json.loads(
            (ROOT / "examples" / "m4" / "synthetic_datapack.json").read_text()
        )
        keep = {"T14.2.1", "T14.4.1.1"}
        datapack = copy.deepcopy(datapack)
        datapack["tables"] = [
            table for table in datapack["tables"] if table["table_id"] in keep
        ]
        datapack["cells"] = {
            key: value
            for key, value in datapack["cells"].items()
            if key.split("|", 1)[0] in keep
        }
        report = analyze_tfl_references(
            datapack,
            {"section": "9.1", "variant": "design", "binding_manifest": []},
        )
        rendered = render_unreferenced_tfl(report)

        self.assertEqual(2, report["unreferenced_table_count"])
        self.assertEqual(1, report["immediate_human_review_count"])
        self.assertEqual("11.4", report["unreferenced_by_expected_section"][0]["expected_section"])
        self.assertEqual(
            ["T14.4.1.1"],
            [item["table_id"] for item in report["unclassified_unreferenced_tables"]],
        )
        self.assertIn("应归 §11.4 疗效（尚未起草）：1 张", rendered)
        self.assertIn("⚠ 无法归类、需人工判断：1 张", rendered)


if __name__ == "__main__":
    unittest.main()
