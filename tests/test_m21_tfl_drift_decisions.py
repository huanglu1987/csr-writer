from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.docx_result import apply_docx_result_to_frozen_shell
from csr_writer.io_utils import sha256_file
from csr_writer.tfl_drift_decisions import (
    TFLDriftDecisionError,
    build_drift_decision_queue,
    freeze_reconciled_tfl_map,
    reconcile_expected_drift,
)


ROOT = Path(__file__).resolve().parents[1]
M20 = ROOT / "examples" / "output" / "m20" / "true_e2e_final"
FIXTURE = ROOT / "examples" / "fixtures" / "m20_unfamiliar"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class M21TFLDriftDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = _load(M20 / "06_result_apply.strict_blocked.json")
        self.frozen = _load(M20 / "05_frozen_map.json")
        self.shell = _load(M20 / "04_shell_datapack.json")
        self.result = FIXTURE / "result_tfl.single_arm_migraine.deidentified.docx"
        self.queue = build_drift_decision_queue(
            self.report,
            self.frozen,
            shell_path=FIXTURE / "tfl_shell.single_arm_migraine.deidentified.docx",
            result_path=self.result,
            result_route_proposals={"T14.3.9.1": ["12"]},
        )

    def _record(self, *, result_error: bool = False) -> dict:
        decisions = []
        for item in self.queue["decisions"]:
            decisions.append(
                {
                    "decision_id": item["decision_id"],
                    "decision": (
                        "result_error"
                        if result_error and item["direction"] == "result_only"
                        else "expected_change"
                    ),
                    "reason": "脱敏夹具中显式设置的版本差异。",
                    "ich_e3_sections": ["12"]
                    if item["direction"] == "result_only"
                    else [],
                }
            )
        return {
            "schema_version": "1.0",
            "queue_sha256": self.queue["queue_sha256"],
            "decided_by_role": "synthetic_tfl_reviewer",
            "decided_at": "2026-08-06T00:00:00+08:00",
            "decisions": decisions,
        }

    def test_queue_has_one_shell_only_and_one_result_only_decision(self) -> None:
        self.assertEqual(2, self.queue["decision_count"])
        self.assertEqual(
            {("shell_only", "T14.2.9.1"), ("result_only", "T14.3.9.1")},
            {(item["direction"], item["entity_id"]) for item in self.queue["decisions"]},
        )
        self.assertFalse(self.queue["release_eligible"])

    def test_missing_decision_remains_a_hard_blocker(self) -> None:
        record = self._record()
        record["decisions"].pop()
        with self.assertRaisesRegex(
            TFLDriftDecisionError, "E-TFL-DRIFT-DECISION-INCOMPLETE"
        ):
            reconcile_expected_drift(
                self.queue, record, self.shell, self.frozen, self.result
            )

    def test_result_error_returns_to_data_without_a_candidate(self) -> None:
        report = reconcile_expected_drift(
            self.queue, self._record(result_error=True), self.shell, self.frozen, self.result
        )
        self.assertEqual("blocked_return_to_data", report["status"])
        self.assertFalse(report["release_eligible"])
        self.assertNotIn("candidate", report)

    def test_expected_change_refreezes_and_strict_application_passes(self) -> None:
        reconciliation = reconcile_expected_drift(
            self.queue, self._record(), self.shell, self.frozen, self.result
        )
        confirmation = {
            "status": "confirmed",
            "confirmation_id": "M21-DRIFT-REFREEZE",
            "confirmed_by_role": "synthetic_tfl_reviewer",
            "confirmed_at": "2026-08-06T00:00:00+08:00",
            "candidate_sha256": reconciliation["candidate"]["candidate_sha256"],
        }
        new_frozen, provenance = freeze_reconciled_tfl_map(
            reconciliation, confirmation
        )
        datapack, report = apply_docx_result_to_frozen_shell(
            reconciliation["revised_shell_datapack"],
            new_frozen,
            self.result,
            expected_sha256=sha256_file(self.result),
        )
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["release_eligible"])
        self.assertEqual(44, report["metrics"]["matched_result_value_count"])
        self.assertIn("T14.3.9.1", {item["table_id"] for item in datapack["tables"]})
        self.assertNotIn("T14.2.9.1", {item["table_id"] for item in datapack["tables"]})
        self.assertEqual(new_frozen["frozen_sha256"], provenance["new_frozen_sha256"])


if __name__ == "__main__":
    unittest.main()

