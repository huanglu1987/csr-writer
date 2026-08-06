from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.io_utils import sha256_file
from csr_writer.shell_mapping import freeze_proposed_shell_map, propose_docx_shell_map


def _result_table(document: Document) -> None:
    table = document.add_table(rows=3, cols=3)
    table.cell(0, 0).text = "Parameter"
    table.cell(0, 1).text = "0.5% BID"
    table.cell(0, 2).text = "1.0% QD"
    table.cell(1, 0).text = "Cmax"
    table.cell(1, 1).text = "xx"
    table.cell(1, 2).text = "xx"
    table.cell(2, 0).text = "AUC0-t"
    table.cell(2, 1).text = "xx"
    table.cell(2, 2).text = "xx"


def _placeholder_table(document: Document) -> None:
    table = document.add_table(rows=3, cols=1)
    table.cell(0, 0).text = "De-identified shell placeholder"
    table.cell(1, 0).text = "No row grid before result delivery"
    table.cell(2, 0).text = "Pending confirmed structure"


class M5ShellMappingTests(unittest.TestCase):
    def test_proposal_is_privacy_safe_batched_and_freezable(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "deidentified-shell.docx"
            document = Document()
            document.add_paragraph("表14.2.4.3.1 De-identified PK shell (PKS) single dose D1")
            _result_table(document)
            document.add_paragraph("表14.3.1.3.3 De-identified local tolerance (SS)")
            _placeholder_table(document)
            document.add_paragraph("表14.3.1.3.4 De-identified related subset (SS)")
            _placeholder_table(document)
            document.add_paragraph("图14.2.4.1 De-identified figure (PKS)")
            document.save(source)
            before = sha256_file(source)

            report, shell, candidate = propose_docx_shell_map(
                source, expected_sha256=before, map_version="DEID-SHELL-TEST-1"
            )
            self.assertEqual(before, sha256_file(source))
            self.assertEqual(3, report["metrics"]["tfl_body_table_objects"])
            self.assertEqual(4, report["metrics"]["unique_tfl_entities"])
            self.assertEqual(1, report["metrics"]["human_review_batches"])
            self.assertEqual(2, report["review_queue"][0]["entity_count"])
            self.assertEqual("T14.2.4.3.1", candidate["entities"][1]["entity_id"])
            pk_entity = next(
                item
                for item in report["entities"]
                if item["entity_id"] == "T14.2.4.3.1"
            )
            self.assertEqual("PKS", pk_entity["analysis_set"])
            self.assertIn("F14.2.4.1", {item["entity_id"] for item in candidate["entities"]})
            self.assertEqual("T", report["prefix_normalization"]["表"])
            serialized = json.dumps(
                {"report": report, "shell": shell, "candidate": candidate},
                ensure_ascii=False,
            )
            self.assertNotIn("De-identified PK shell", serialized)
            self.assertNotIn("local tolerance", serialized)

            confirmation = {
                "schema_version": "1.0",
                "report_sha256": report["report_sha256"],
                "confirmed_by_role": "test_reviewer",
                "confirmed_at": "2026-08-04T00:00:00+08:00",
                "decisions": [
                    {"batch_id": item["batch_id"], "decision": "confirm"}
                    for item in report["review_queue"]
                ],
            }
            frozen, proof = freeze_proposed_shell_map(report, candidate, confirmation)
            self.assertEqual("frozen", frozen["status"])
            self.assertEqual("pass", proof["status"])
            self.assertEqual(2, proof["confirmed_entity_count"])


if __name__ == "__main__":
    unittest.main()
