from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from docx import Document

from csr_writer.io_utils import load_json_unique, sha256_file
from csr_writer.cli import main as cli_main
from csr_writer.review_decisions import (
    ReviewDecisionError,
    apply_decision_template,
    build_decision_ledger,
    build_review_decision_queue,
    compare_review_versions,
    create_decision_record,
    export_decision_template,
    freeze_candidate_with_decisions,
    render_deferred_row_expectations,
    validate_complete_ledger,
)
from csr_writer.shell_mapping import propose_docx_shell_map


ROOT = Path(__file__).resolve().parents[1]
M7_REPORT = ROOT / "examples" / "output" / "m7" / "real_shell_proposal_report.json"
M7_CANDIDATE = ROOT / "examples" / "output" / "m7" / "tfl_map_candidate.deidentified.json"


def _grid(document: Document, columns: list[str], rows: list[str]) -> None:
    table = document.add_table(rows=len(rows) + 1, cols=len(columns) + 1)
    table.cell(0, 0).text = "ROW"
    for column_index, column in enumerate(columns, start=1):
        table.cell(0, column_index).text = column
    for row_index, row in enumerate(rows, start=1):
        table.cell(row_index, 0).text = row
        for column_index in range(1, len(columns) + 1):
            table.cell(row_index, column_index).text = "xx"


def _layout_placeholder(document: Document, target: str) -> None:
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = f"请根据表{target}的模板输出本表。"


def _version_shell(path: Path, *, changed_parent: bool) -> None:
    document = Document()
    document.add_paragraph("表14.1.1.3 Deidentified deviation categories (All Enrolled Set)")
    _grid(document, ["PBO", "1.0% QD"], ["偏离类别1", "偏离类别2"])
    document.add_paragraph("表14.1.4.1 Deidentified medication rows (FAS)")
    columns = ["PBO", "1.0% QD"]
    if changed_parent:
        columns.append("0.5% BID")
    _grid(document, columns, ["ATC分类1", "药物名1"])
    document.add_paragraph("表14.1.4.2 Deidentified medication child (FAS)")
    _layout_placeholder(document, "14.1.4.1")
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)


def _all_default_records(queue, *, reviewer="reviewer_demo_001"):
    records = []
    for index, group in enumerate(queue["decision_groups"]):
        resolved = {
            item["field"]: "DEIDENTIFIED-DEMO-V1"
            for item in group["required_reviewer_inputs"]
        }
        records.append(
            create_decision_record(
                queue,
                group_id=group["group_id"],
                reviewer_role="lead_statistician",
                reviewer_identifier=reviewer,
                decided_at=f"2026-08-04T{index:02d}:00:00+08:00",
                outcome="confirm_default",
                resolved_inputs=resolved,
                decision_notes="deidentified test decision",
            )
        )
    return records


class M8ReviewDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load_json_unique(M7_REPORT)
        cls.candidate = load_json_unique(M7_CANDIDATE)
        cls.queue = build_review_decision_queue(cls.report, cls.candidate)

    def test_real_scale_queue_uses_human_decisions_and_overlapping_membership(self):
        queue = self.queue
        self.assertEqual(63, queue["ergonomics"]["unique_review_entity_count"])
        self.assertEqual(15, queue["ergonomics"]["decision_group_count"])
        self.assertEqual("pass", queue["coverage"]["status"])
        self.assertGreater(queue["coverage"]["multiple_decision_type_entities"], 0)
        types = {item["decision_type"] for item in queue["decision_groups"]}
        self.assertEqual(
            {
                "coding_dictionary_dependency",
                "analysis_set_parallelism",
                "family_batch_policy",
                "layout_inheritance_scope",
                "structural_exception",
                "deferred_row_source_contract",
            },
            types,
        )
        for group in queue["decision_groups"]:
            self.assertIn("common_decision_point", group)
            self.assertTrue(group["representatives"])
            self.assertIn("default_decision", group)
            self.assertIn("override_consequences", group)
            self.assertGreater(group["information_volume"]["character_count"], 0)

    def test_deferred_expectations_explain_source_count_and_failure_policy(self):
        output = render_deferred_row_expectations(
            self.queue, ["T14.1.3.1", "T14.1.4.2", "T14.1.1.3"]
        )
        self.assertIn("MedDRA 编码的 SOC/PT", output)
        self.assertIn("WHODrug", output)
        self.assertIn("偏离判定计划", output)
        self.assertIn("硬失败", output)
        self.assertIn("位置哈希永不允许", output)

    def test_decision_record_is_attributable_and_override_needs_reason(self):
        group = next(
            item for item in self.queue["decision_groups"]
            if item["group_id"] == "DG-CODE-MEDDRA"
        )
        record = create_decision_record(
            self.queue,
            group_id=group["group_id"],
            reviewer_role="lead_statistician",
            reviewer_identifier="reviewer_demo_001",
            decided_at="2026-08-04T10:30:00+08:00",
            outcome="confirm_default",
            resolved_inputs={"dictionary_version": "DEIDENTIFIED-DEMO-V1"},
        )
        self.assertEqual(self.queue["proposal_report_sha256"], record["proposal_report_sha256"])
        self.assertEqual(group["decision_group_sha256"], record["decision_group_sha256"])
        self.assertEqual("reviewer_demo_001", record["reviewer"]["identifier"])
        with self.assertRaises(ReviewDecisionError):
            create_decision_record(
                self.queue,
                group_id=group["group_id"],
                reviewer_role="lead_statistician",
                reviewer_identifier="reviewer_demo_001",
                decided_at="2026-08-04T10:30:00+08:00",
                outcome="override_default",
                resolved_inputs={"dictionary_version": "DEIDENTIFIED-DEMO-V1"},
            )
        with self.assertRaises(ReviewDecisionError):
            create_decision_record(
                self.queue,
                group_id=group["group_id"],
                reviewer_role="lead_statistician",
                reviewer_identifier="reviewer_demo_001",
                decided_at="2026-08-04T10:30:00+08:00",
                outcome="override_default",
                resolved_inputs={"dictionary_version": "DEIDENTIFIED-DEMO-V1"},
                override_reason="病史与 AE 使用不同版本",
            )
        override = create_decision_record(
            self.queue,
            group_id=group["group_id"],
            reviewer_role="lead_statistician",
            reviewer_identifier="reviewer_demo_001",
            decided_at="2026-08-04T10:30:00+08:00",
            outcome="override_default",
            resolved_inputs={"dictionary_version": "DEIDENTIFIED-DEMO-V1"},
            override_parameters={"split_by_domain": "true"},
            override_reason="病史与 AE 使用不同版本",
        )
        self.assertEqual(
            {"split_by_domain": "true"}, override["decision"]["override_parameters"]
        )

    def test_complete_ledger_freezes_and_template_never_auto_confirms(self):
        ledger = build_decision_ledger(self.queue, _all_default_records(self.queue))
        self.assertEqual("complete", ledger["status"])
        frozen, proof = freeze_candidate_with_decisions(
            self.report, self.candidate, self.queue, ledger
        )
        self.assertEqual("frozen", frozen["status"])
        self.assertEqual("pass", proof["status"])
        template = export_decision_template(
            self.queue, ledger, template_id="DEIDENTIFIED-SPONSOR-VENDOR-DEMO"
        )
        self.assertFalse(template["source"]["reviewer_identity_exported"])
        application = apply_decision_template(self.queue, template)
        self.assertEqual(0, application["automatic_decision_count"])
        self.assertEqual(
            "applicability_confirmation_required", application["status"]
        )
        self.assertTrue(
            all(not item["auto_applied"] for item in application["suggestions"])
        )

    def test_version_reuse_distinguishes_unchanged_direct_and_dependency_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            v10 = root / "shell-v1.0.docx"
            v11 = root / "shell-v1.1.docx"
            _version_shell(v10, changed_parent=False)
            _version_shell(v11, changed_parent=True)
            report10, _shell10, candidate10 = propose_docx_shell_map(
                v10, expected_sha256=sha256_file(v10), map_version="M8-DEID-V1.0"
            )
            report11, _shell11, candidate11 = propose_docx_shell_map(
                v11, expected_sha256=sha256_file(v11), map_version="M8-DEID-V1.1"
            )
            queue10 = build_review_decision_queue(report10, candidate10)
            queue11 = build_review_decision_queue(report11, candidate11)
            ledger10 = build_decision_ledger(queue10, _all_default_records(queue10))
            comparison = compare_review_versions(queue10, ledger10, queue11)
            with self.assertRaises(ReviewDecisionError):
                validate_complete_ledger(queue11, ledger10)

        statuses = {item["entity_id"]: item["status"] for item in comparison["entities"]}
        self.assertEqual("unchanged_decision_reused", statuses["T14.1.1.3"])
        self.assertEqual("changed_requeued", statuses["T14.1.4.1"])
        self.assertEqual(
            "upstream_change_invalidated_reconfirmation", statuses["T14.1.4.2"]
        )
        child = next(item for item in comparison["entities"] if item["entity_id"] == "T14.1.4.2")
        self.assertTrue(any(item["path"].startswith("/dependencies") for item in child["differences"]))

    def test_cli_builds_queue_and_attributable_record(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary)
            queue_path = root / "queue.json"
            markdown_path = root / "queue.md"
            deferred_path = root / "deferred.md"
            self.assertEqual(
                0,
                cli_main(
                    [
                        "tfl-review-queue",
                        "--report",
                        str(M7_REPORT),
                        "--candidate",
                        str(M7_CANDIDATE),
                        "--output",
                        str(queue_path),
                        "--markdown-output",
                        str(markdown_path),
                        "--deferred-rows-output",
                        str(deferred_path),
                    ]
                ),
            )
            record_path = root / "record.json"
            self.assertEqual(
                0,
                cli_main(
                    [
                        "tfl-review-decide",
                        "--queue",
                        str(queue_path),
                        "--group-id",
                        "DG-CODE-MEDDRA",
                        "--reviewer-role",
                        "lead_statistician",
                        "--reviewer-identifier",
                        "reviewer_demo_001",
                        "--decided-at",
                        "2026-08-04T10:30:00+08:00",
                        "--outcome",
                        "confirm_default",
                        "--resolved-input",
                        "dictionary_version=DEIDENTIFIED-DEMO-V1",
                        "--output",
                        str(record_path),
                    ]
                ),
            )
            record = load_json_unique(record_path)
            self.assertTrue(markdown_path.is_file())
            self.assertTrue(deferred_path.is_file())
            self.assertEqual("reviewer_demo_001", record["reviewer"]["identifier"])


if __name__ == "__main__":
    unittest.main()
