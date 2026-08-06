from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.cli import main
from csr_writer.cross_chapter import load_sections_from_manifest
from csr_writer.io_utils import load_json_unique
from csr_writer.section_writer import (
    AGENT_DATA_NOTICE,
    AgentDraftGuardError,
    SectionWriterError,
    emit_agent_envelope,
    ingest_agent_draft,
)


ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "examples" / "section_inputs" / "efficacy_request.json"
AGENT_ASSEMBLY_MANIFEST = (
    ROOT / "examples" / "output" / "m7" / "agent_writer" / "assembly" / "sections_manifest.json"
)


def _valid_agent_draft(envelope: dict) -> dict:
    bindings = envelope["data"]["tfl_bindings"]
    value = {item["role"]: "{{" + item["key"] + "}}" for item in bindings}
    return {
        "paragraphs": [
            {
                "text": "主要终点、分析集和统计方法仅按本节经审核来源陈述。",
                "sources": [
                    "Protocol-v3-endpoint",
                    "SAP-v2-analysis-set",
                    "SAP-v2-primary-method",
                ],
                "confidence": "derived",
                "needs_human": False,
            },
            {
                "text": (
                    f"试验组主要终点结果为 {value['experimental_result']}，"
                    f"对照组为 {value['control_result']}；相应区间依次为 "
                    f"{value['experimental_ci']} 和 {value['control_ci']}；"
                    f"组间差异为 {value['between_group_difference']}，"
                    f"检验结果为 {value['p_value']}。支持性结果详见{{{{@ref:T14.2.1}}}}。"
                ),
                "sources": ["T14.2.1"],
                "confidence": "derived",
                "needs_human": False,
            },
            {
                "text": "[待医学负责人确认] 结果的临床意义须由医学负责人结合全部证据确认。",
                "sources": ["Protocol-v3-endpoint"],
                "confidence": "inferred",
                "needs_human": True,
            },
        ]
    }


class M7AgentInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.request = load_json_unique(REQUEST)
        self.envelope = emit_agent_envelope(self.request)

    def test_emitted_envelope_is_self_contained_hash_bound_and_data_isolated(self):
        self.assertEqual(AGENT_DATA_NOTICE, self.envelope["data_notice"])
        self.assertIn("content", self.envelope["data"]["playbook"])
        self.assertTrue(self.envelope["data"]["tfl_bindings"])
        self.assertTrue(self.envelope["data"]["source_fragments"])
        source_text = self.envelope["data"]["source_fragments"][0]["text"]
        self.assertNotIn(source_text, self.envelope["system_prompt"])
        tampered = copy.deepcopy(self.envelope)
        tampered["data"]["source_fragments"][0]["text"] = "tampered"
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
            SectionWriterError
        ):
            ingest_agent_draft(tampered, _valid_agent_draft(self.envelope), temporary)

    def test_guard_rejection_is_hash_chained_into_successful_retry(self):
        rogue = _valid_agent_draft(self.envelope)
        rogue["paragraphs"][0]["text"] += " 暴露量约增加三倍。"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(AgentDraftGuardError) as caught:
                ingest_agent_draft(self.envelope, rogue, temporary)
            rejected = caught.exception.report
            self.assertEqual("rejected", rejected["status"])
            self.assertIn("三倍", rejected["reason"])
            result, passed = ingest_agent_draft(
                self.envelope,
                _valid_agent_draft(self.envelope),
                temporary,
                prior_guard_reports=[rejected],
            )
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual("pass", passed["status"])
        self.assertEqual(2, passed["attempt"])
        self.assertEqual([rejected["report_sha256"]], passed["prior_guard_report_sha256s"])
        self.assertEqual("AgentEnvelopeIngest", metadata["writer"])
        self.assertEqual(2, metadata["generation_attempts"])
        self.assertEqual(1, metadata["guard_retry_count"])
        self.assertFalse(metadata["agent_interface"]["external_model_api_called"])

    def test_needs_human_marker_must_be_exact(self):
        malformed = _valid_agent_draft(self.envelope)
        malformed["paragraphs"][2]["text"] = (
            "[ 待医学负责人确认 ] 结果的临床意义须由医学负责人结合全部证据确认。"
        )
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
            AgentDraftGuardError
        ) as caught:
            ingest_agent_draft(self.envelope, malformed, temporary)
        self.assertIn("exact [待医学负责人确认] marker", str(caught.exception))

    def test_cli_emit_and_ingest_modes_publish_expected_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            envelope_path = root / "envelope.json"
            self.assertEqual(
                0,
                main(
                    [
                        "draft-section",
                        "--request",
                        str(REQUEST),
                        "--emit-envelope",
                        str(envelope_path),
                    ]
                ),
            )
            envelope = load_json_unique(envelope_path)
            draft_path = root / "draft.json"
            draft_path.write_text(
                json.dumps(_valid_agent_draft(envelope), ensure_ascii=False),
                encoding="utf-8",
            )
            guard_path = root / "guard.json"
            output_dir = root / "project"
            self.assertEqual(
                0,
                main(
                    [
                        "draft-section",
                        "--envelope",
                        str(envelope_path),
                        "--ingest-draft",
                        str(draft_path),
                        "--output-dir",
                        str(output_dir),
                        "--guard-report",
                        str(guard_path),
                    ]
                ),
            )
            self.assertTrue((output_dir / "sections" / "11.4.md").is_file())
            self.assertEqual("pass", load_json_unique(guard_path)["status"])

    def test_agent_metadata_is_accepted_by_the_assembly_contract(self):
        sections = load_sections_from_manifest(AGENT_ASSEMBLY_MANIFEST)
        self.assertEqual(["11.4", "12.2"], [item.section_id for item in sections])


if __name__ == "__main__":
    unittest.main()
