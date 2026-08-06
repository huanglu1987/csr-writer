from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.cli import main
from csr_writer.io_utils import sha256_file


ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "examples" / "demo_protocol_facts.json"
EFFICACY_REQUEST = ROOT / "examples" / "section_inputs" / "efficacy_request.json"
DOCX_FIXTURE = ROOT / "tests" / "fixtures" / "m3-docx" / "deep-extraction-fixture.docx"


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = main(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


class M3CLITests(unittest.TestCase):
    def test_facts_card_and_route_commands_preserve_truth_source(self):
        before = sha256_file(FACTS)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            card = root / "facts.md"
            route = root / "route.json"
            card_status, _, card_error = run_cli(
                ["facts-card", "--study-facts", str(FACTS), "--output", str(card)]
            )
            route_status, _, route_error = run_cli(
                ["route-modules", "--study-facts", str(FACTS), "--output", str(route)]
            )

            self.assertEqual(0, card_status, card_error)
            self.assertEqual(0, route_status, route_error)
            self.assertIn("待人工确认", card.read_text(encoding="utf-8"))
            payload = json.loads(route.read_text(encoding="utf-8"))
            self.assertEqual(before, payload["input_file_sha256"]["study_facts"])
            self.assertIn("M-EFF-EXPLORE", [item["id"] for item in payload["active_modules"]])
        self.assertEqual(before, sha256_file(FACTS))

    def test_extract_docx_command_publishes_accepted_view_report(self):
        before = sha256_file(DOCX_FIXTURE)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "extraction.json"
            status, _, error = run_cli(
                ["extract-docx", "--docx", str(DOCX_FIXTURE), "--output", str(output)]
            )
            self.assertEqual(0, status, error)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("accepted_all_revisions", payload["view"])
            self.assertEqual(3, len(payload["gap_report"]))
        self.assertEqual(before, sha256_file(DOCX_FIXTURE))

    def test_draft_section_command_writes_only_the_requested_section_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "project"
            status, _, error = run_cli(
                [
                    "draft-section",
                    "--request",
                    str(EFFICACY_REQUEST),
                    "--output-dir",
                    str(output_root),
                ]
            )
            self.assertEqual(0, status, error)
            produced = sorted(path.name for path in (output_root / "sections").iterdir())
            self.assertEqual(["11.4.md", "11.4.meta.json"], produced)
            self.assertIn("{{T14.2.1|", (output_root / "sections" / "11.4.md").read_text())

    def test_m3_commands_reject_input_output_aliases(self):
        status, _, error = run_cli(
            ["route-modules", "--study-facts", str(FACTS), "--output", str(FACTS)]
        )
        self.assertEqual(4, status)
        self.assertIn("M3 path collision", error)


if __name__ == "__main__":
    unittest.main()
