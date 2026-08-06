from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from csr_writer.datapack import build_datapack
from csr_writer.production_methods import load_method_extraction_contracts
from csr_writer.resolve_placeholders import resolve
from csr_writer.writing_style import n_percent_format_findings, style_report


ROOT = Path(__file__).resolve().parents[1]
FACTS = json.loads((ROOT / "examples/demo_protocol_facts.json").read_text(encoding="utf-8"))
KEY = "T14.2.1|POP=FAS|ROW=OBJECTIVE_RESPONSE_CR_PR|COL=XYZ200MG"


class M17ResultFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.datapack, _ = build_datapack(ROOT / "examples/demo_tfl_manifest.yaml")

    def test_n_percent_prose_rule_is_blocking_and_does_not_confuse_mean_sd(self) -> None:
        bad = style_report("用药部位红斑见于 2 (4.2)。")
        self.assertTrue(bad["blocked"])
        self.assertEqual(
            ["E-STYLE-N-PERCENT-FORMAT"],
            [item["code"] for item in bad["findings"]],
        )
        self.assertEqual([], n_percent_format_findings("均值为 2 (4.2)。"))
        self.assertFalse(style_report("2例（4.2%）发生用药部位红斑。")["blocked"])

    def test_n_pct_cells_are_deterministically_rendered_with_unit_and_percent(self) -> None:
        result = resolve(f"事件见于 {{{{{KEY}}}}}。", self.datapack, FACTS)
        self.assertEqual("pass", result.status, result.report)
        self.assertEqual("事件见于 42例（70.0%）。", result.resolved_text)
        self.assertEqual("pass", result.report["stages"]["D_result_format"]["status"])

    def test_malformed_n_pct_cell_is_hard_blocked_after_resolution(self) -> None:
        datapack = copy.deepcopy(self.datapack)
        datapack["cells"][KEY]["v"] = "42 / 70.0"
        datapack["cells"][KEY]["display_value"] = "42 / 70.0"
        result = resolve(f"事件见于 {{{{{KEY}}}}}。", datapack, FACTS)
        self.assertEqual("blocked", result.status)
        self.assertEqual("fail", result.report["stages"]["D_result_format"]["status"])
        self.assertEqual("E-STYLE-N-PERCENT-FORMAT", result.report["issues"][0]["code"])


class M17ExtractionNarrationTests(unittest.TestCase):
    def test_endpoint_definitions_use_constrained_prose_not_field_dump_lists(self) -> None:
        contracts = load_method_extraction_contracts()["contracts"]
        reclassified = {"9.5.3", "9.5.4", "9.5.6", "9.5.7", "9.5.8", "9.5.9"}
        retained_lists = {"9.3.1", "9.3.2", "9.3.3", "9.4.7", "9.5.1.1"}
        self.assertTrue(all(contracts[item]["narration"] == "constrained_llm" for item in reclassified))
        self.assertTrue(all(contracts[item]["narration"] == "verbatim_list" for item in retained_lists))
        self.assertEqual(
            retained_lists,
            {section_id for section_id, item in contracts.items() if item["narration"] == "verbatim_list"},
        )


if __name__ == "__main__":
    unittest.main()

