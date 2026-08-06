from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.conditional_conclusions import resolve_conditional_conclusions
from csr_writer.playbook_qc import (
    M14_COMPLETE_PLAYBOOK_FILENAMES,
    audit_complete_playbooks,
)
from csr_writer.resolve_placeholders import resolve
from csr_writer.section_writer import (
    DraftParagraph,
    SectionWriterError,
    build_section_envelope,
    draft_section,
)
from csr_writer.writing_style import analyze_writing_style
from tests.test_m12_conditioned_conclusions import (
    _datapack as safety_datapack,
    _request as safety_request,
)
from tests.test_m13_conditioned_conclusions import (
    _datapack as conclusion_datapack,
    _request as conclusion_request,
)


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_DIR = ROOT / "skills" / "csr-writer" / "section-playbooks"
PROTOCOL_FACTS = json.loads(
    (ROOT / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8")
)


class _LiteralZeroEventAdapter:
    def generate(self, envelope):
        from csr_writer.section_writer import DeterministicSectionAdapter

        paragraphs = list(DeterministicSectionAdapter().generate(envelope))
        last = paragraphs[-1]
        paragraphs[-1] = DraftParagraph(
            text=(
                "[待医学负责人确认] 未发生严重不良事件。"
                "{{@conclusion:SAFETY_TOLERABILITY_V1}}"
            ),
            sources=last.sources,
            confidence="inferred",
            needs_human=True,
        )
        return tuple(paragraphs)


class M14WritingStyleTests(unittest.TestCase):
    def test_m13_baseline_reproduces_six_diagnosed_lesions(self) -> None:
        text = (ROOT / "examples" / "output" / "m13" / "M13起草样例.md").read_text(
            encoding="utf-8"
        )
        metrics = analyze_writing_style(text).metrics
        self.assertEqual(15, metrics["summary_copula_count"])
        self.assertEqual(8, metrics["reviewed_qualifier_count"])
        self.assertEqual(4, metrics["prespecified_qualifier_count"])
        self.assertEqual(28, metrics["semicolon_count"])
        self.assertEqual(12, metrics["abstract_noun_count"])
        self.assertEqual(7, metrics["field_enumeration_paragraph_count"])
        self.assertEqual(4, metrics["overlong_sentence_count"])

    def test_banned_expressions_are_blocking_and_budgets_warn(self) -> None:
        review = analyze_writing_style(
            "TEAE 汇总结果为〔数值〕。Cmax 可计算性为可计算。"
        )
        self.assertTrue(review.blocked)
        self.assertEqual(
            {"summary_as_copula", "abstract_noun_as_field"},
            {item["category"] for item in review.findings if item["severity"] == "block"},
        )

        warning = analyze_writing_style(
            "经审核方案规定本节方法。经审核的 SAP 规定分析集。"
            "第一项；第二项；第三项。"
        )
        self.assertFalse(warning.blocked)
        self.assertEqual(
            {"qualifier_budget", "semicolon_budget"},
            {item["category"] for item in warning.findings},
        )

    def test_placeholder_length_does_not_create_false_long_sentence_warning(self) -> None:
        key = "T14.2.4.3.1|POP=PKPS|ROW=VERY_LONG_SEMANTIC_ROW_NAME|COL=ACTIVE_TOTAL"
        review = analyze_writing_style(f"Cmax 为 {{{{{key}}}}}。")
        self.assertEqual(0, review.metrics["overlong_sentence_count"])

    def test_natural_sentence_library_is_versioned_and_zero_event_is_conditioned(self) -> None:
        payload = json.loads(
            (
                ROOT
                / "skills"
                / "csr-writer"
                / "references"
                / "natural-csr-sentence-library.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("1.0", payload["schema_version"])
        by_id = {item["id"]: item for item in payload["sentences"]}
        self.assertEqual(
            {
                "randomized_and_analysis_counts",
                "event_incidence",
                "zero_event",
                "pk_parameters_calculable",
                "table_reference",
                "boundary_at_paragraph_end",
            },
            set(by_id),
        )
        self.assertFalse(by_id["zero_event"]["literal_use_in_agent_draft"])

    def test_all_19_playbooks_have_style_partition(self) -> None:
        report = audit_complete_playbooks(
            PLAYBOOK_DIR, M14_COMPLETE_PLAYBOOK_FILENAMES
        )
        self.assertEqual("pass", report["status"], report)
        self.assertEqual(19, report["playbook_count"])
        for record in report["records"]:
            self.assertTrue(record["style_section_present"], record)
            self.assertTrue(all(record["style_requirements"].values()), record)

    def test_three_conclusion_sections_use_sentence_driven_mode_without_branch_drift(self) -> None:
        expected = {
            "11.5.10": "exposure_characterizable",
            "11.6.7": "prespecified_descriptive",
            "13": "confirmed_body_complete_no_critical_events",
        }
        for section, expected_branch in expected.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as temporary:
                draft = draft_section(conclusion_request(section), temporary)
                self.assertNotIn("汇总为", draft.markdown)
                outcome = resolve_conditional_conclusions(
                    draft.markdown, conclusion_datapack(), draft.metadata
                )
                rule = outcome.report["rules"][0]
                self.assertEqual(expected_branch, rule["branch"])
                self.assertEqual("sentence_driven", rule["render_mode"])
                self.assertTrue(rule["all_roles_rendered"])
                self.assertEqual(
                    len(draft.metadata["binding_manifest"]),
                    len(rule["role_renderings"]),
                )
                self.assertFalse(outcome.report["style_qc"]["blocked"])

    def test_literal_zero_safety_claim_is_blocked_but_conditioned_zero_is_allowed(self) -> None:
        request = safety_request()
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            SectionWriterError, "E-STYLE-LITERAL-ZERO-EVENT"
        ):
            draft_section(request, temporary, adapter=_LiteralZeroEventAdapter())

        with tempfile.TemporaryDirectory() as temporary:
            draft = draft_section(request, temporary)
            datapack = safety_datapack(
                {
                    "sae_count": 0,
                    "death_count": 0,
                    "discontinuation_count": 0,
                    "grade3plus_count": 0,
                }
            )
            conditioned = resolve_conditional_conclusions(
                draft.markdown, datapack, draft.metadata
            )
            self.assertIn("未发生严重不良事件", conditioned.resolved_text)
            self.assertEqual("no_critical_events", conditioned.report["rules"][0]["branch"])
            final = resolve(
                conditioned.resolved_text,
                datapack,
                PROTOCOL_FACTS,
                binding_manifest=draft.metadata,
            )
            self.assertEqual("pass", final.status, final.report)


if __name__ == "__main__":
    unittest.main()
