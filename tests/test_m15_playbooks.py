from __future__ import annotations

import unittest
from pathlib import Path

from csr_writer.playbook_qc import (
    COMPLETE_PLAYBOOK_FILENAMES,
    M15_NEW_COMPLETE_PLAYBOOK_FILENAMES,
    audit_complete_playbooks,
)


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_DIR = ROOT / "skills" / "csr-writer" / "section-playbooks"


class M15CompletePlaybookTests(unittest.TestCase):
    def test_all_35_complete_playbooks_meet_depth_and_structure_gate(self) -> None:
        report = audit_complete_playbooks(PLAYBOOK_DIR)
        self.assertEqual(35, len(COMPLETE_PLAYBOOK_FILENAMES))
        self.assertEqual(35, report["playbook_count"])
        self.assertEqual("pass", report["status"], report["records"])

    def test_m15_adds_exactly_16_complete_playbooks(self) -> None:
        self.assertEqual(16, len(M15_NEW_COMPLETE_PLAYBOOK_FILENAMES))
        self.assertEqual(
            len(M15_NEW_COMPLETE_PLAYBOOK_FILENAMES),
            len(set(M15_NEW_COMPLETE_PLAYBOOK_FILENAMES)),
        )
        for filename in M15_NEW_COMPLETE_PLAYBOOK_FILENAMES:
            self.assertTrue((PLAYBOOK_DIR / filename).is_file(), filename)


if __name__ == "__main__":
    unittest.main()
