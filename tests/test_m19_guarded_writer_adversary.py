from __future__ import annotations

import unittest
from pathlib import Path

from csr_writer.section_writer_adversary import (
    audit_guarded_section_writer_units,
    guarded_unit_ids,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "examples/output/m16/production_method_inventory_120.json"


class GuardedWriterAdversaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_guarded_section_writer_units(INVENTORY)

    def test_inventory_contains_all_34_guarded_units(self) -> None:
        self.assertEqual(34, len(guarded_unit_ids(INVENTORY)))
        self.assertEqual(34, self.report["unit_count"])
        self.assertEqual(34, self.report["baseline_pass_count"])

    def test_shared_deterministic_attacks_are_blocked(self) -> None:
        for unit in self.report["records"]:
            with self.subTest(section_id=unit["section_id"]):
                outcomes = {
                    item["attack_id"]: item["status"] for item in unit["attacks"]
                }
                for attack_id in (
                    "unbound_placeholder",
                    "literal_arabic_number",
                    "literal_chinese_number",
                    "literal_zero_event",
                    "internal_process_language",
                    "unbound_reference",
                    "unscoped_source",
                    "physical_paragraph_injection",
                    "forbidden_style",
                ):
                    self.assertEqual("blocked", outcomes[attack_id])

    def test_sentence_driven_branch_blocks_unsourced_prose_after_m20_fix(self) -> None:
        sentence = self.report["sentence_driven"]
        self.assertEqual(4, sentence["unit_count"])
        self.assertEqual([], sentence["arbitrary_prose_passed_section_ids"])
        self.assertEqual(4, len(sentence["partial_binding_blocked_section_ids"]))

    def test_all_model_parser_attacks_are_blocked(self) -> None:
        self.assertTrue(
            all(item["status"] == "blocked" for item in self.report["parser_attacks"])
        )


if __name__ == "__main__":
    unittest.main()
