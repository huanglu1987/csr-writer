from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from csr_writer.constrained_narration import (
    ConstrainedNarrationArtifact,
    ConstrainedNarrationError,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    render_causality_sentence,
    write_constrained_artifact,
)
from tests.test_m16_production_and_narration import sae_card, sae_draft


class M17ChineseForwardCoverageTests(unittest.TestCase):
    def _assert_forward_blocked(self, card: dict, draft: dict) -> set[str]:
        with self.assertRaises(ConstrainedNarrationError) as raised:
            ingest_constrained_draft(emit_fact_card_envelope(card), draft)
        codes = {item["code"] for item in raised.exception.report["findings"]}
        self.assertTrue(
            {"E-SAE-FORWARD-COVERAGE", "E-SAE-CLINICAL-COURSE-SYNTHESIS"}
            & codes,
            raised.exception.report,
        )
        return codes

    def test_normal_sae_prose_is_not_harmed(self) -> None:
        artifact = ingest_constrained_draft(
            emit_fact_card_envelope(sae_card()), sae_draft()
        )
        self.assertEqual("pass", artifact.metadata["forward_coverage"]["status"])
        self.assertTrue(
            all(
                item["status"] == "pass"
                for item in artifact.metadata["forward_coverage"]["paragraph_checks"]
            )
        )

    def test_six_chinese_attack_shapes_are_hard_blocked(self) -> None:
        base_card = sae_card()
        base_draft = sae_draft()
        attacks: list[tuple[str, dict, dict]] = []

        event = copy.deepcopy(base_draft)
        event["paragraphs"].append({"text": "同期报告心悸。", "fact_ids": ["A04E"]})
        attacks.append(("invented_event", base_card, event))

        history = copy.deepcopy(base_draft)
        history["paragraphs"].append(
            {"text": "受试者另有糖尿病和高血压病史。", "fact_ids": ["A02"]}
        )
        attacks.append(("invented_history", base_card, history))

        medicine = copy.deepcopy(base_draft)
        medicine["paragraphs"].append(
            {"text": "同时给予甲泼尼龙冲击治疗。", "fact_ids": ["A07"]}
        )
        attacks.append(("invented_medicine", base_card, medicine))

        gap_card = sae_card(clinical_course=False)
        gap = sae_draft(clinical_course=False)
        gap["paragraphs"].append(
            {
                "text": "入院后行血培养，予静脉抗生素治疗，皮损范围逐渐缩小。",
                "fact_ids": ["B01"],
            }
        )
        attacks.append(("gap_plus_invented_course", gap_card, gap))

        test_name = copy.deepcopy(base_draft)
        test_name["paragraphs"].append(
            {"text": "复查肝功能未见异常。", "fact_ids": ["A08"]}
        )
        attacks.append(("invented_test", base_card, test_name))

        synonym = copy.deepcopy(base_draft)
        synonym["paragraphs"][1]["text"] = synonym["paragraphs"][1]["text"].replace(
            "中度", "较重"
        )
        attacks.append(("severity_synonym", base_card, synonym))

        for label, card, draft in attacks:
            with self.subTest(label=label):
                self._assert_forward_blocked(card, draft)

    def test_all_seven_causality_values_use_natural_exact_sentences(self) -> None:
        terms = (
            "明确相关",
            "很可能有关",
            "可能有关",
            "不太可能有关",
            "无关",
            "未评价",
            "无法评价",
        )
        for term in terms:
            with self.subTest(term=term):
                card = sae_card(causality=term)
                draft = sae_draft()
                draft["paragraphs"][3]["text"] = (
                    "事件已恢复。" + render_causality_sentence(term)
                )
                artifact = ingest_constrained_draft(
                    emit_fact_card_envelope(card), draft
                )
                self.assertIn(term, artifact.markdown)
                self.assertTrue(artifact.metadata["causality_control"]["exact_match"])

    def test_sae_release_requires_certified_forward_coverage(self) -> None:
        artifact = ingest_constrained_draft(
            emit_fact_card_envelope(sae_card()), sae_draft()
        )
        forged_metadata = dict(artifact.metadata)
        forged_metadata.pop("forward_coverage")
        forged = ConstrainedNarrationArtifact(
            artifact.section_id, artifact.markdown, forged_metadata
        )
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            ConstrainedNarrationError, "E-SAE-POSITIVE-COVERAGE-RELEASE-BLOCKED"
        ):
            write_constrained_artifact(
                forged,
                Path(directory) / "section.md",
                Path(directory) / "section.meta.json",
            )


if __name__ == "__main__":
    unittest.main()
