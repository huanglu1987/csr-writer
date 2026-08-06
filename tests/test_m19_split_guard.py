from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.constrained_narration import (
    ConstrainedNarrationArtifact,
    ConstrainedNarrationError,
    constrained_guard_policy_snapshot,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    write_constrained_artifact,
)
from csr_writer.fact_card_adversary import (
    enumerate_whole_value_relation_attacks,
    evaluate_attack_candidates,
)
from csr_writer.production_methods import load_production_method_registry
from csr_writer.tfl_map import migrate_legacy_unspecified_population_candidate
from tests.test_m16_production_and_narration import (
    randomization_card,
    randomization_draft,
    sae_card,
    sae_draft,
)


ROOT = Path(__file__).resolve().parents[1]


def _finding_codes(card: dict, draft: dict) -> set[str]:
    with unittest.TestCase().assertRaises(ConstrainedNarrationError) as raised:
        ingest_constrained_draft(emit_fact_card_envelope(card), draft)
    return {item["code"] for item in raised.exception.report["findings"]}


class SplitRiskGuardTests(unittest.TestCase):
    def test_policy_partitions_all_30_constrained_units(self) -> None:
        snapshot = constrained_guard_policy_snapshot()
        policy = snapshot["policy"]
        self.assertEqual(["12.3.2"], policy["sae"]["section_ids"])
        self.assertEqual(29, len(policy["methodology"]["section_ids"]))
        registry = load_production_method_registry()["assignments"]
        expected = {
            section_id
            for section_id, assignment in registry.items()
            if assignment.get("narration") == "constrained_llm"
        }
        self.assertEqual(
            expected,
            set(policy["sae"]["section_ids"])
            | set(policy["methodology"]["section_ids"]),
        )

    def test_method_guard_needs_no_reviewed_surface_and_labels_are_optional(self) -> None:
        card = randomization_card()
        card["authorized_surface_forms"] = []
        draft = randomization_draft()
        draft["paragraphs"][0]["text"] = draft["paragraphs"][0]["text"].replace(
            "本研究采用中央随机化", "本研究为中央随机化设计"
        )
        draft["paragraphs"][1]["text"] = (
            "区组长度由独立统计师保密保存；独立统计人员为随机号生成方。"
        )
        artifact = ingest_constrained_draft(emit_fact_card_envelope(card), draft)
        forward = artifact.metadata["forward_coverage"]
        self.assertEqual("method_whole_value_label_bound", forward["guard_profile"])
        self.assertEqual("whole_values_label_bound", forward["coverage_mode"])
        self.assertTrue(forward["release_eligible"])

    def test_method_guard_blocks_fragment_negation_and_label_value_mismatch(self) -> None:
        card = randomization_card()
        cases = {
            "fragment": ("中央随机。", ["V01"]),
            "negation": ("本研究未采用中央随机化。", ["V01"]),
            "label_value_mismatch": ("分配比为中央随机化。", ["V01", "V02"]),
        }
        for label, (text, fact_ids) in cases.items():
            with self.subTest(label=label):
                draft = randomization_draft()
                draft["paragraphs"].append({"text": text, "fact_ids": fact_ids})
                codes = _finding_codes(card, draft)
                self.assertTrue(
                    {
                        "E-FACT-CARD-FORWARD-COVERAGE",
                        "E-METHOD-LABEL-VALUE-BINDING",
                        "E-METHOD-PARAGRAPH-FACT-ANCHOR",
                    }
                    & codes
                )

    def test_method_guard_blocks_connector_only_fact_claim(self) -> None:
        card = randomization_card()
        draft = randomization_draft()
        draft["paragraphs"].append(
            {"text": "研究中分配。", "fact_ids": ["V03", "S01"]}
        )
        self.assertIn(
            "E-METHOD-PARAGRAPH-FACT-ANCHOR", _finding_codes(card, draft)
        )

    def test_sae_still_requires_exact_reviewed_surface(self) -> None:
        card = sae_card()
        draft = sae_draft()
        draft["paragraphs"][0]["text"] = draft["paragraphs"][0]["text"].replace(
            "。研究治疗暴露", "，研究治疗暴露"
        )
        codes = _finding_codes(card, draft)
        self.assertIn("E-SAE-FORWARD-COVERAGE", codes)

    def test_guard_policy_hash_is_bound_into_envelope(self) -> None:
        envelope = emit_fact_card_envelope(randomization_card())
        snapshot = constrained_guard_policy_snapshot()
        self.assertEqual(
            snapshot["policy_sha256"], envelope["data"]["guard_policy_sha256"]
        )
        tampered = copy.deepcopy(envelope)
        tampered["data"]["guard_policy_sha256"] = "0" * 64
        body = {
            key: value
            for key, value in tampered.items()
            if key != "envelope_sha256"
        }
        import hashlib

        tampered["envelope_sha256"] = hashlib.sha256(
            json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(
            ConstrainedNarrationError, "guard policy hash/profile mismatch"
        ):
            ingest_constrained_draft(tampered, randomization_draft())

    def test_method_release_rejects_forged_sae_profile(self) -> None:
        artifact = ingest_constrained_draft(
            emit_fact_card_envelope(randomization_card()), randomization_draft()
        )
        forged_metadata = copy.deepcopy(artifact.metadata)
        forged_metadata["forward_coverage"]["guard_profile"] = (
            "sae_exact_reviewed_surface"
        )
        forged = ConstrainedNarrationArtifact(
            artifact.section_id, artifact.markdown, forged_metadata
        )
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            ConstrainedNarrationError, "E-METHOD-POSITIVE-COVERAGE-RELEASE-BLOCKED"
        ):
            write_constrained_artifact(
                forged,
                Path(directory) / "section.md",
                Path(directory) / "section.meta.json",
            )

    def test_method_release_rejects_missing_coverage_certificate(self) -> None:
        artifact = ingest_constrained_draft(
            emit_fact_card_envelope(randomization_card()), randomization_draft()
        )
        forged_metadata = copy.deepcopy(artifact.metadata)
        forged_metadata.pop("forward_coverage")
        forged = ConstrainedNarrationArtifact(
            artifact.section_id, artifact.markdown, forged_metadata
        )
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            ConstrainedNarrationError, "E-METHOD-POSITIVE-COVERAGE-RELEASE-BLOCKED"
        ):
            write_constrained_artifact(
                forged,
                Path(directory) / "section.md",
                Path(directory) / "section.meta.json",
            )

    def test_whole_value_adversary_exposes_residual_relation_risk(self) -> None:
        card = randomization_card()
        candidates = enumerate_whole_value_relation_attacks(card)
        self.assertTrue(candidates)
        report = evaluate_attack_candidates(
            card, candidates, coverage_mode="whole_values_label_bound"
        )
        self.assertGreater(report["raw_pass_count"], 0)
        self.assertTrue(
            all(
                item["strategy"] == "whole_value_relation_recombination"
                for item in report["raw_passes"]
            )
        )


class LegacyMigrationTests(unittest.TestCase):
    def test_m8_unspecified_population_is_retired_not_rewritten(self) -> None:
        source = ROOT / "examples/output/m8/tfl_map_candidate.deidentified.json"
        before_bytes = source.read_bytes()
        candidate = json.loads(before_bytes)
        migration = migrate_legacy_unspecified_population_candidate(candidate)
        self.assertEqual("blocked_pending_reparse", migration["status"])
        self.assertFalse(migration["release_eligible"])
        self.assertEqual(1, migration["before"]["unspecified_entity_count"])
        self.assertEqual(28, migration["before"]["unspecified_key_count"])
        self.assertEqual(0, migration["after"]["active_unspecified_key_count"])
        self.assertEqual(1, migration["after"]["pending_reparse_entity_count"])
        self.assertEqual([], migration["retired_entities"][0]["replacement_keys"])
        self.assertEqual(before_bytes, source.read_bytes())


if __name__ == "__main__":
    unittest.main()
