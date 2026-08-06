from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.io_utils import load_json_unique
from csr_writer.section_writer import (
    AgentDraftGuardError,
    emit_agent_envelope,
    ingest_agent_draft,
)
from csr_writer.semantic_guard import scan_internal_process_language


ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "examples" / "section_inputs" / "efficacy_request.json"


KNOWN_LEAKS = (
    "退出原因沿用经审核分类，不由写手合并或改写。",
    "当前证据包未提供完成、退出及退出原因的结果绑定。",
    "该陈述仅限已提供的记录范围，不外推至未核验资料。",
    "这些结果只反映当前绑定的参数汇总。",
)


def _valid_agent_draft(envelope: dict) -> dict:
    value = {
        item["role"]: "{{" + item["key"] + "}}"
        for item in envelope["data"]["tfl_bindings"]
    }
    return {
        "paragraphs": [
            {
                "text": "主要终点、分析集和统计方法按本节经审核来源陈述。",
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
                    f"检验结果为 {value['p_value']}。"
                    "支持性结果详见{{@ref:T14.2.1}}。"
                ),
                "sources": ["T14.2.1"],
                "confidence": "derived",
                "needs_human": False,
            },
            {
                "text": "上述分析属于探索性分析，不用于确证性结论。",
                "sources": ["Protocol-v3-endpoint"],
                "confidence": "derived",
                "needs_human": False,
            },
        ]
    }


class M13InternalProcessLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.envelope = emit_agent_envelope(load_json_unique(REQUEST))

    def test_four_observed_leaks_are_detected_and_hard_rejected(self) -> None:
        observed_subclasses = []
        for leak in KNOWN_LEAKS:
            with self.subTest(leak=leak):
                review = scan_internal_process_language(leak)
                self.assertTrue(review.blocked)
                self.assertTrue(
                    all(item["category"] == "internal_process_language" for item in review.findings)
                )
                observed_subclasses.extend(item["subclass"] for item in review.findings)
                rogue = copy.deepcopy(_valid_agent_draft(self.envelope))
                rogue["paragraphs"][0]["text"] = leak
                with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
                    AgentDraftGuardError
                ) as caught:
                    ingest_agent_draft(self.envelope, rogue, temporary)
                self.assertIn("internal process language", caught.exception.report["reason"])
                self.assertEqual("rejected", caught.exception.report["status"])
        self.assertIn("tool_self_reference", observed_subclasses)
        self.assertIn("internal_data_term", observed_subclasses)
        self.assertIn("process_negation", observed_subclasses)

    def test_csr_facing_standard_sentences_pass(self) -> None:
        replacements = (
            "退出原因按经审核的分类呈现。",
            "本节完成与退出情况待相应统计结果到位后补充。",
            "本节结论基于现有盲态维护记录，未涵盖记录范围之外的情形。",
            "上述为可计算参数的汇总结果，不代表全部采样时点。",
        )
        for text in replacements:
            with self.subTest(text=text):
                self.assertFalse(scan_internal_process_language(text).blocked)

    def test_legitimate_review_and_pk_model_language_is_not_blocked(self) -> None:
        text = "经审核的统计分析计划采用幂函数模型评价剂量比例关系。"
        self.assertFalse(scan_internal_process_language(text).blocked)
        self.assertTrue(scan_internal_process_language("该句由模型生成。").blocked)

    def test_internal_status_terms_are_a_separate_hard_block_class(self) -> None:
        review = scan_internal_process_language(
            "该段置信度为 inferred，状态为 source-gap。"
        )
        self.assertTrue(review.blocked)
        self.assertEqual(
            {"internal_state"},
            {item["subclass"] for item in review.findings},
        )

    def test_standard_sentence_library_is_versioned_and_complete(self) -> None:
        path = (
            ROOT
            / "skills"
            / "csr-writer"
            / "references"
            / "evidence-boundary-standard-sentences.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("1.0", payload["schema_version"])
        self.assertEqual(
            {"data_pending", "scope_limitation", "computable_parameters"},
            {item["id"] for item in payload["sentences"]},
        )


if __name__ == "__main__":
    unittest.main()
