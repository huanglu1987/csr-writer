from __future__ import annotations

import json
import copy
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from docx import Document

from csr_writer.constrained_narration import (
    ConstrainedNarrationError,
    _NARRATIVE_GLUE_TERMS,
    emit_fact_card_envelope,
    ingest_constrained_draft,
)
from csr_writer.conditional_conclusions import (
    CONCLUSION_SECTION_ROOTS,
    EXPECTED_CONCLUSION_RULES,
)
from csr_writer.deterministic_sections import (
    DeterministicGenerationError,
    _require_text,
    build_deterministic_bundle,
    render_deterministic_bundle_docx,
)
from csr_writer.docx_result import coverage_metric_views
from csr_writer.fact_card_adversary import (
    enumerate_fact_card_attacks,
    evaluate_attack_candidates,
)
from csr_writer.row_semantics import classify_shell_row_segment
from csr_writer.section_writer import SectionWriterError, build_section_envelope, draft_section
from csr_writer.semantic_guard import MEDICAL_REVIEW_MARKER, review_semantic_redlines
from csr_writer.shell_mapping import (
    ShellMappingError,
    _analysis_set,
    freeze_proposed_shell_map,
    propose_docx_shell_map,
)
from csr_writer.io_utils import sha256_file
from tests.test_m16_production_and_narration import (
    randomization_card,
    randomization_draft,
    sae_card,
    sae_draft,
)
from tests.test_m17_deterministic_sections import m17_deterministic_context


class ReproducibleDocxBuildTests(unittest.TestCase):
    def test_identical_inputs_produce_identical_docx_bytes(self) -> None:
        bundle = build_deterministic_bundle(m17_deterministic_context())
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.docx"
            second = Path(directory) / "second.docx"
            render_deterministic_bundle_docx(bundle, first)
            time.sleep(2.1)  # exceed the ZIP timestamp's two-second resolution
            render_deterministic_bundle_docx(bundle, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as package:
                self.assertEqual(
                    {(1980, 1, 1, 0, 0, 0)},
                    {item.date_time for item in package.infolist()},
                )


class FactCardPolarityAndAtomicCoverageTests(unittest.TestCase):
    def _codes(self, card: dict, draft: dict) -> set[str]:
        with self.assertRaises(ConstrainedNarrationError) as raised:
            ingest_constrained_draft(emit_fact_card_envelope(card), draft)
        return {item["code"] for item in raised.exception.report["findings"]}

    def test_unsourced_negation_cannot_reverse_method_facts(self) -> None:
        # Keep four shapes as separate subtests so one passing form cannot hide another.
        cases = {
            "not_use": "本研究未采用中央随机化，并按等比例分配。分层因素为研究中心和基线严重程度。",
            "not_include": "本研究采用中央随机化，并按等比例分配。分层因素不包括研究中心和基线严重程度。",
            "none": "本研究无中央随机化，并按等比例分配。分层因素为研究中心和基线严重程度。",
            "not_is": "本研究采用中央随机化，并非按等比例分配。分层因素为研究中心和基线严重程度。",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                draft = randomization_draft()
                draft["paragraphs"][0]["text"] = text
                self.assertIn(
                    "E-FACT-CARD-FORWARD-COVERAGE",
                    self._codes(randomization_card(), draft),
                )

    def test_sourced_negation_and_original_method_prose_still_pass(self) -> None:
        artifact = ingest_constrained_draft(
            emit_fact_card_envelope(randomization_card()), randomization_draft()
        )
        self.assertEqual("pass", artifact.metadata["forward_coverage"]["status"])
        root = Path(__file__).resolve().parents[1] / "examples" / "output" / "m16" / "method_pilot"
        card = json.loads((root / "fact_cards" / "9.5.3.json").read_text(encoding="utf-8"))
        draft = json.loads((root / "raw_drafts" / "9.5.3.json").read_text(encoding="utf-8"))
        sourced_negative = ingest_constrained_draft(
            emit_fact_card_envelope(card), draft
        )
        self.assertIn("本研究未采用正式 estimand 框架", sourced_negative.markdown)

    def test_other_reversal_terms_are_not_static_glue(self) -> None:
        self.assertTrue(
            {"不", "未", "无", "非", "除外", "以外", "反之"}.isdisjoint(
                _NARRATIVE_GLUE_TERMS
            )
        )
        draft = randomization_draft()
        draft["paragraphs"][0]["text"] = (
            "本研究采用中央随机化以外，并按等比例分配。"
            "分层因素为研究中心和基线严重程度。"
        )
        self.assertIn(
            "E-FACT-CARD-FORWARD-COVERAGE",
            self._codes(randomization_card(), draft),
        )

    def test_sae_cross_field_recombination_is_blocked(self) -> None:
        draft = sae_draft()
        draft["paragraphs"][0]["text"] = (
            "受试者甲既往有季节性过敏史，既往有用药过敏史。"
            "研究治疗暴露为{{T16.3.1|POP=SS|ROW=SUBJ_A/EXPOSURE|COL=DOSE}}。"
        )
        draft["paragraphs"][0]["fact_ids"] = ["A01", "A02", "A03", "A04E"]
        self.assertIn(
            "E-SAE-FORWARD-COVERAGE", self._codes(sae_card(), draft)
        )

    def test_authorized_surface_forms_are_hash_bound_and_fact_scoped(self) -> None:
        card = randomization_card()
        envelope = emit_fact_card_envelope(card)
        self.assertEqual("1.1", envelope["data"]["fact_card"]["schema_version"])
        self.assertTrue(envelope["data"]["fact_card"]["authorized_surface_forms"])

        wrong_scope = randomization_draft()
        wrong_scope["paragraphs"][0]["fact_ids"] = ["V01"]
        codes = self._codes(card, wrong_scope)
        self.assertIn("E-FACT-CARD-FORWARD-COVERAGE", codes)

        unconfirmed = copy.deepcopy(card)
        unconfirmed["authorized_surface_forms"][0]["review_status"] = "proposed"
        with self.assertRaisesRegex(ConstrainedNarrationError, "not confirmed"):
            emit_fact_card_envelope(unconfirmed)

    def test_automatic_adversary_rediscovers_f1_and_new_guard_has_no_raw_pass(self) -> None:
        card = sae_card()
        for fact in card["facts"]:
            if fact["value"] == "已恢复":
                fact["value"] = "继续研究治疗"
        for surface in card["authorized_surface_forms"]:
            surface["text"] = surface["text"].replace("已恢复", "继续研究治疗")
        candidates = enumerate_fact_card_attacks(card)
        self.assertEqual(
            {
                "cross_field_fragment_recombination",
                "negation_or_limiter_insertion",
                "semantic_reversal_pair",
                "label_value_mismatch",
                "boundary_truncation_splice",
            },
            {item.strategy for item in candidates},
        )
        self.assertIn("既往有用药过敏史", {item.text for item in candidates})
        before = evaluate_attack_candidates(
            card, candidates, coverage_mode="legacy_substrings"
        )
        after = evaluate_attack_candidates(
            card, candidates, coverage_mode="whole_declared_values"
        )
        self.assertIn("既往有用药过敏史", {item["text"] for item in before["raw_passes"]})
        self.assertEqual(0, after["raw_pass_count"])

    def test_method_release_ignores_reviewed_surfaces_and_uses_bound_whole_values(self) -> None:
        card = randomization_card()
        card["authorized_surface_forms"] = [
            {
            "surface_id": "SF-9-4-3-ALT-01",
            "text": "这条历史预审句不应成为方法学发布白名单。",
            "fact_ids": ["S01"],
            "review_status": "confirmed",
            "reviewed_by_role": "protocol_fact_reviewer",
            "review_record_id": "TEST-SURFACE-9.4.3-ALT-01",
            "basis": "仅用于证明方法学不再依赖 authorized_surface_forms",
            }
        ]
        rewrite = {
            "paragraphs": [
                {
                    "text": (
                        "随机化清单由独立统计人员生成，授权人员通过系统实施分配。"
                        "本研究为中央随机化设计，并按等比例分配。"
                        "研究中心和基线严重程度作为分层因素。"
                    ),
                    "fact_ids": ["S01", "V01", "V02", "V03"],
                },
                {
                    "text": "区组长度由独立统计师保密保存；独立统计人员为随机号生成方。",
                    "fact_ids": ["V04", "V05"],
                },
                {
                    "text": (
                        "交互式网络应答系统作为随机系统。"
                        "授权人员登录后按顺序取得分配结果为分配隐藏方法。"
                    ),
                    "fact_ids": ["V06", "V07"],
                },
            ]
        }
        artifact = ingest_constrained_draft(emit_fact_card_envelope(card), rewrite)
        self.assertEqual("whole_values_label_bound", artifact.metadata["forward_coverage"]["coverage_mode"])

        replay = copy.deepcopy(rewrite)
        replay["paragraphs"].append(
            {
                "text": card["authorized_surface_forms"][0]["text"],
                "fact_ids": ["S01"],
            }
        )
        self.assertIn(
            "E-FACT-CARD-FORWARD-COVERAGE", self._codes(card, replay)
        )

    def test_label_value_mismatch_is_not_authorized_by_declaring_both_facts(self) -> None:
        draft = sae_draft()
        draft["paragraphs"].append(
            {"text": "严重程度为已恢复。", "fact_ids": ["A05", "A10"]}
        )
        self.assertIn("E-SAE-FORWARD-COVERAGE", self._codes(sae_card(), draft))


class AuditFindingClosureTests(unittest.TestCase):
    def test_runtime_alias_is_demo_only_and_traced_in_envelope(self) -> None:
        root = Path(__file__).resolve().parents[1]
        request = json.loads(
            (root / "examples/fixtures/m7_agent_safety_request.deidentified.json").read_text(
                encoding="utf-8"
            )
        )
        envelope = build_section_envelope(request)
        authorization = envelope["data"]["production_authorization"]
        self.assertTrue(authorization["runtime_alias_only"])
        self.assertEqual("demo_only", authorization["release_scope"])
        with tempfile.TemporaryDirectory() as directory:
            draft = draft_section(request, directory)
        self.assertEqual(
            authorization, draft.metadata["production_authorization"]
        )
        self.assertEqual("1.2", draft.metadata["schema_version"])

        gap_request = copy.deepcopy(request)
        gap_request["evidence_status"] = "source_gap"
        gap_request["tfl_bindings"] = []
        gap_request["tfl_references"] = []
        gap_request["confirmed_tfl_map"] = None
        gap_authorization = build_section_envelope(gap_request)["data"][
            "production_authorization"
        ]
        self.assertTrue(gap_authorization["runtime_alias_only"])
        self.assertEqual("source_gap_only", gap_authorization["release_scope"])

        request["evidence_status"] = "reviewed_project_evidence"
        with self.assertRaisesRegex(
            SectionWriterError, "E-PRODUCTION-METHOD-RUNTIME-ALIAS-SCOPE"
        ):
            build_section_envelope(request)

    def test_conclusion_registry_drives_semantic_review_and_includes_12_2(self) -> None:
        self.assertIn("12.2", CONCLUSION_SECTION_ROOTS)
        self.assertEqual(
            "SAFETY_TOLERABILITY_V1",
            EXPECTED_CONCLUSION_RULES[("12.2", "adverse_events_conditioned")],
        )
        review = review_semantic_redlines("12.2", "总体耐受性可接受。")
        self.assertTrue(review.requires_human)
        self.assertTrue(review.text.startswith(MEDICAL_REVIEW_MARKER))

    def test_analysis_sets_support_fullwidth_and_unresolved_cannot_freeze(self) -> None:
        expected = {
            "表14.1（FAS）": "FAS",
            "表14.1（mFAS）": "mFAS",
            "表14.1（PPS）": "PPS",
            "表14.1（PPPS）": "PPPS",
            "表14.1（mITT）": "mITT",
            "表14.1（mPPS）": "mPPS",
            "表14.1（All Screened Set）": "All Screened Set",
        }
        self.assertEqual(expected, {title: _analysis_set(title) for title in expected})
        report = {
            "entities": [{"entity_id": "T14.1.1", "analysis_set": "UNSPECIFIED"}]
        }
        with self.assertRaisesRegex(ShellMappingError, "E-ANALYSIS-SET-UNSPECIFIED"):
            freeze_proposed_shell_map(report, {}, {})

    def test_unparsed_analysis_set_is_low_confidence_and_forced_into_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing-analysis-set.docx"
            document = Document()
            document.add_paragraph("表14.2.1 脱敏合成结果表")
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "统计量"
            table.cell(0, 1).text = "安慰剂组"
            table.cell(1, 0).text = "均值"
            table.cell(1, 1).text = "xx"
            document.save(source)
            report, _shell, candidate = propose_docx_shell_map(
                source,
                expected_sha256=sha256_file(source),
                map_version="M18-UNSPECIFIED-TEST",
            )
        entity = report["entities"][0]
        self.assertEqual("UNSPECIFIED", entity["analysis_set"])
        self.assertEqual("low", entity["confidence"])
        self.assertIsNotNone(entity["batch_id"])
        self.assertIn("requires revision", entity["reasons"][0])
        confirmation = {
            "schema_version": "1.0",
            "report_sha256": report["report_sha256"],
            "confirmed_by_role": "test_reviewer",
            "confirmed_at": "2026-08-06T00:00:00+08:00",
            "decisions": [
                {"batch_id": item["batch_id"], "decision": "confirm"}
                for item in report["review_queue"]
            ],
        }
        with self.assertRaisesRegex(ShellMappingError, "E-ANALYSIS-SET-UNSPECIFIED"):
            freeze_proposed_shell_map(report, candidate, confirmation)

    def test_nfkc_aliases_and_placeholder_shapes(self) -> None:
        self.assertEqual(
            "MEAN_SD", classify_shell_row_segment("均值（标准差）").segment
        )
        self.assertEqual(
            "MIN_MAX", classify_shell_row_segment("最小值，最大值").segment
        )
        for value in ("待定", "待确定", "暂缺", "待补齐", "占位内容"):
            with self.subTest(value=value), self.assertRaises(
                DeterministicGenerationError
            ):
                _require_text(value, "audit")
        self.assertEqual("可按监管要求提供", _require_text("可按监管要求提供", "audit"))

    def test_all_three_coverage_denominators_are_reported(self) -> None:
        root = Path(__file__).resolve().parents[1]
        prior = json.loads(
            (root / "examples/output/m15/result_match_report.json").read_text(
                encoding="utf-8"
            )
        )
        metrics = coverage_metric_views(prior["entity_reports"])
        self.assertEqual((147, 250), (
            metrics["result_side_matched_value_count"],
            metrics["result_side_semantic_value_count"],
        ))
        self.assertEqual(0.588, metrics["result_side_match_rate"])
        self.assertEqual((22, 364), (
            metrics["frozen_shell_side_matched_cell_count"],
            metrics["frozen_shell_side_contract_cell_count"],
        ))
        self.assertEqual(0.0604, metrics["frozen_shell_side_match_rate"])
        self.assertEqual(0.588, metrics["result_oriented_match_rate"])
        self.assertEqual(0.2539, metrics["shell_oriented_match_rate"])
        self.assertEqual(0.0604, metrics["explicit_frozen_contract_match_rate"])


if __name__ == "__main__":
    unittest.main()
