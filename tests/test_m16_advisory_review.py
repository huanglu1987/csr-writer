from __future__ import annotations

import hashlib
import unittest

from csr_writer.advisory_review import (
    AdvisoryReviewError,
    emit_advisory_review_envelope,
    ingest_advisory_review,
)


TEXT = "# 12.2 不良事件\n\n33 例受试者发生 TEAE。结果详见相应表格。"


def _envelope():
    return emit_advisory_review_envelope(
        TEXT,
        source_label="synthetic.md",
        source_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
    )


def _payload():
    return {
        "findings": [
            {
                "section": "12.2",
                "problem_type": "missing_expected_content",
                "quote": "33 例受试者发生 TEAE。",
                "problem_description": "发生例数未同时说明适用分析集和分母。",
                "suggestion_direction": "核对来源后补充分析集和对应分母信息。",
                "severity": "medium",
            }
        ]
    }


class M16AdvisoryReviewTests(unittest.TestCase):
    def test_review_is_flags_only_and_never_a_release_gate(self) -> None:
        report = ingest_advisory_review(_envelope(), _payload()).report
        self.assertEqual("advisory_complete", report["status"])
        self.assertTrue(report["advisory_only"])
        self.assertFalse(report["release_gate"])
        self.assertFalse(report["source_modified"])
        self.assertFalse(report["replacement_text_generated"])
        self.assertEqual("not_permitted", report["release_decision"])

    def test_quote_must_be_verbatim_and_replacement_text_is_rejected(self) -> None:
        wrong_quote = _payload()
        wrong_quote["findings"][0]["quote"] = "不存在的原文"
        with self.assertRaisesRegex(AdvisoryReviewError, "quote is not verbatim"):
            ingest_advisory_review(_envelope(), wrong_quote)

        replacement = _payload()
        replacement["findings"][0]["suggestion_direction"] = "替换为另一句话。"
        with self.assertRaisesRegex(AdvisoryReviewError, "replacement text"):
            ingest_advisory_review(_envelope(), replacement)

    def test_extra_replacement_field_is_contract_drift(self) -> None:
        payload = _payload()
        payload["findings"][0]["replacement_text"] = "不可出现"
        with self.assertRaisesRegex(AdvisoryReviewError, "contract drift"):
            ingest_advisory_review(_envelope(), payload)


if __name__ == "__main__":
    unittest.main()
