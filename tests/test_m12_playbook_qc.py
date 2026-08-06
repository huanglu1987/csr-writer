from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from csr_writer.playbook_qc import (
    COMPLETE_PLAYBOOK_FILENAMES,
    audit_complete_playbooks,
    inspect_complete_playbook,
)
from csr_writer.authoring_inventory import build_authoring_inventory
from csr_writer.module_router import route_modules


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_DIR = ROOT / "skills" / "csr-writer" / "section-playbooks"


class M12PlaybookQualityTests(unittest.TestCase):
    def test_all_declared_complete_playbooks_meet_depth_contract(self) -> None:
        report = audit_complete_playbooks(PLAYBOOK_DIR)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["playbook_count"], len(COMPLETE_PLAYBOOK_FILENAMES))
        self.assertGreaterEqual(report["playbook_count"], 16)
        for item in report["records"]:
            self.assertGreaterEqual(item["line_count"], 150, item)
            self.assertGreaterEqual(item["review_issue_count"], 5, item)
            self.assertTrue(all(item["seven_elements"].values()), item)
            self.assertTrue(all(item["process_separation"].values()), item)
            self.assertFalse(item["standard_sentence_internal_language_findings"], item)
            self.assertFalse(item["standard_sentence_style_findings"], item)
            self.assertTrue(item["style_section_present"], item)
            self.assertTrue(all(item["style_requirements"].values()), item)

    def test_shallow_playbook_is_blocked_with_specific_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shallow.md"
            path.write_text("# shallow\n\n## 1. 本节的权威依据\n", encoding="utf-8")
            record = inspect_complete_playbook(path)
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(any("line_count" in item for item in record["failures"]))
        self.assertTrue(any("missing_element" in item for item in record["failures"]))
        self.assertTrue(any("review_issue_count" in item for item in record["failures"]))

    def test_six_new_references_cover_all_previously_uncovered_full_units(self) -> None:
        facts = __import__("json").loads(
            (ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json").read_text(
                encoding="utf-8"
            )
        )
        inventory = build_authoring_inventory(
            route_modules(facts)["structure"]["section_plan"]
        )
        self.assertEqual(inventory["full_playbook_coverage_units"], 35)
        self.assertEqual(inventory["full_units_without_existing_playbook"], 0)
        expected = {
            "s11-3-treatment-compliance.md",
            "s12-3-1-deaths-serious-significant-listings.md",
            "s12-3-2-sae-narratives-boundary.md",
            "s12-3-3-serious-event-analysis.md",
            "s12-4-2-laboratory-evaluation.md",
            "s13-discussion-overall-conclusions.md",
        }
        self.assertTrue(expected.issubset(set(inventory["distinct_existing_playbook_files"])))
        section_13 = next(item for item in inventory["rows"] if item["section_id"] == "13")
        self.assertEqual(section_13["existing_playbook_scope"], "derived_from_confirmed_body")


if __name__ == "__main__":
    unittest.main()
