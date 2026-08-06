from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.bidirectional_trace import BidirectionalTraceError, validate_bidirectional_trace
from csr_writer.cli import main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "fixtures" / "bidirectional_trace.deidentified.json"


class BidirectionalTraceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_forward_and_both_reverse_paths_are_proven(self):
        report = validate_bidirectional_trace(self.manifest)
        self.assertEqual("pass", report["status"])
        self.assertEqual(
            ["PROT-DERM-1", "SAP-DERM-1", "TFL-DERM-1", "CSR-DERM-1"],
            report["paths"][0]["forward"],
        )
        self.assertEqual(
            ["CSR-DERM-1", "TFL-DERM-1", "SAP-DERM-1", "PROT-DERM-1"],
            report["paths"][0]["reverse_from_csr"],
        )
        self.assertEqual(
            ["TFL-DERM-1", "SAP-DERM-1", "PROT-DERM-1"],
            report["paths"][0]["reverse_from_tfl"],
        )

    def test_missing_sap_to_tfl_link_hard_fails(self):
        rogue = copy.deepcopy(self.manifest)
        del rogue["links"][1]
        with self.assertRaises(BidirectionalTraceError) as caught:
            validate_bidirectional_trace(rogue)
        self.assertEqual("E-TRACE-CHAIN-GAP", caught.exception.code)

    def test_wrong_direction_or_orphan_hard_fails(self):
        rogue = copy.deepcopy(self.manifest)
        rogue["links"][1] = {
            "from": "TFL-DERM-1",
            "to": "SAP-DERM-1",
            "relation": "implemented_by",
        }
        with self.assertRaises(BidirectionalTraceError) as caught:
            validate_bidirectional_trace(rogue)
        self.assertEqual("E-TRACE-CHAIN-DIRECTION", caught.exception.code)

    def test_cli_writes_hashed_pass_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "trace.json"
            self.assertEqual(
                0,
                main(
                    [
                        "m4-bidirectional-trace",
                        "--manifest",
                        str(FIXTURE),
                        "--output",
                        str(output),
                    ]
                ),
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("pass", report["status"])
            self.assertIn("trace manifest", report["input_file_sha256"])


if __name__ == "__main__":
    unittest.main()
