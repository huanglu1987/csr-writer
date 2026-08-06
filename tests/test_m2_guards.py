from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from csr_writer.datapack import build_datapack
from csr_writer.errors import ConfigurationError
from csr_writer.number_guard import CompiledWhitelist, compile_whitelist, find_bare_numbers
from csr_writer.resolve_placeholders import resolve


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PLUGIN_ROOT / "examples" / "demo_tfl_manifest.yaml"
FACTS = json.loads((PLUGIN_ROOT / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8"))
DRAFT = (PLUGIN_ROOT / "examples" / "demo_draft.md").read_text(encoding="utf-8")
VALID_KEY = "T14.2.1|POP=FAS|ROW=OBJECTIVE_RESPONSE_CR_PR|COL=XYZ200MG"


class M2GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datapack, _ = build_datapack(MANIFEST)

    def test_end_to_end_resolution_passes_all_three_guards(self):
        result = resolve(DRAFT, self.datapack, FACTS)
        self.assertEqual("pass", result.status)
        self.assertIsNotNone(result.resolved_text)
        self.assertIn("42例（70.0%）", result.resolved_text)
        self.assertIn("1720 (26.8) ng/mL", result.resolved_text)
        self.assertEqual([], result.report["issues"])

    def test_missing_key_stops_before_guards_b_and_c(self):
        draft = "结果为 {{T14.2.1|POP=FAS|ROW=NOT_REAL|COL=PBO}}。"
        result = resolve(draft, self.datapack, FACTS)
        self.assertEqual("blocked", result.status)
        self.assertIsNone(result.resolved_text)
        self.assertEqual("fail", result.report["stages"]["A_key_existence"]["status"])
        self.assertEqual("not_run", result.report["stages"]["B_residual"]["status"])
        self.assertEqual("not_run", result.report["stages"]["C_bare_numbers"]["status"])

    def test_malformed_placeholder_is_blocked(self):
        malformed = (
            "结果为 {{BAD}。",
            "结果为 {{{" + VALID_KEY + "}}}。",
            "结果为 {" + "{{" + VALID_KEY + "}}" + "。",
            "结果为 " + "{{" + VALID_KEY + "}}" + "}。",
        )
        for draft in malformed:
            with self.subTest(draft=draft):
                result = resolve(draft, self.datapack, FACTS)
                self.assertEqual("blocked", result.status)
                self.assertIsNone(result.resolved_text)
                self.assertEqual("E-PLACEHOLDER-SYNTAX", result.report["issues"][0]["code"])
                self.assertEqual("not_run", result.report["stages"]["B_residual"]["status"])
                self.assertEqual("not_run", result.report["stages"]["C_bare_numbers"]["status"])

    def test_literal_result_numbers_require_review(self):
        result = resolve("绝对提高 25 个百分点，且 7 例发生 SAE。", self.datapack, FACTS)
        self.assertEqual("review_required", result.status)
        self.assertIsNone(result.resolved_text)
        self.assertEqual({"25", "7"}, {item["token"] for item in result.report["issues"]})

    def test_fact_whitelist_is_exact_not_generic(self):
        whitelist = compile_whitelist(FACTS)
        valid = (
            "本研究采用 3+3 设计，剂量为 50 mg、100 mg、200 mg、400 mg，"
            "共 4 个剂量水平，周期长度为 21 天，每21天为1个周期。"
        )
        self.assertEqual([], find_bare_numbers(valid, whitelist))
        invalid = "受试者接受 300 mg；于给药后 0.5、1、2、4、8、24、36 小时采血。"
        self.assertEqual(
            ["300", "36"],
            [item["token"] for item in find_bare_numbers(invalid, whitelist)],
        )
        self.assertEqual(
            ["2"],
            [
                item["token"]
                for item in find_bare_numbers("每21天为2个周期。", whitelist)
            ],
        )

    def test_tokenizer_catches_leading_decimal_chinese_and_schedule(self):
        whitelist = compile_whitelist(FACTS)
        findings = find_bare_numbers("p=.049，三例发生 SAE，按 Q5W 给药。", whitelist)
        self.assertEqual([".049", "三例", "Q5W"], [item["token"] for item in findings])

    def test_tokenizer_catches_chinese_adjacent_and_written_percentages(self):
        whitelist = compile_whitelist(FACTS)
        findings = find_bare_numbers(
            "共有7例发生 SAE，约25%受试者缓解，受试者接受300 mg，缓解率提高百分之二十五。",
            whitelist,
        )
        self.assertEqual(
            ["7", "25%", "300", "百分之二十五"],
            [item["token"] for item in findings],
        )

    def test_pk_parameter_tokens_are_masked_without_whitelisting_result_values(self):
        strict = CompiledWhitelist(tuple(), "strict-test")
        findings = find_bare_numbers(
            "血浆消除半衰期（t1/2）约为 12 小时，AUC0-∞ 为 345 ng·h/mL。",
            strict,
        )
        self.assertEqual(["12", "345"], [item["token"] for item in findings])
        self.assertEqual(
            [],
            find_bare_numbers("PK 参数包括 t1/2、AUC0-∞ 和 AUC0-t。", strict),
        )

    def test_acceptance_report_12_chinese_quantity_regressions(self):
        blocked_cases = {
            "ascii_percentage": "试验组客观缓解率为70.0%。",
            "written_percentage": "试验组客观缓解率为百分之七十。",
            "chinese_count": "共有七例受试者达到客观缓解。",
            "percentage_points": "客观缓解率提高二十五个百分点。",
            "tenths": "试验组约七成受试者达到客观缓解。",
            "tenths_decimal": "安慰剂组约四成五达到客观缓解。",
            "fraction": "三分之二受试者达到客观缓解。",
            "multiple": "暴露量增加约三倍。",
            "financial_count": "共有壹佰贰拾例受试者。",
            "fuzzy_half": "超过半数受试者达到客观缓解。",
            "combined_original": "试验组约七成受试者达到客观缓解，安慰剂组约四成五。",
        }
        for label, draft in blocked_cases.items():
            with self.subTest(label=label):
                result = resolve(draft, self.datapack, FACTS)
                self.assertEqual("review_required", result.status, result.report)
                self.assertTrue(result.report["issues"])

        schedule_facts = copy.deepcopy(FACTS)
        schedule_facts["design"]["dosing_interval_weeks"] = 3
        allowed = resolve("方案规定每 3 周一次。", self.datapack, schedule_facts)
        self.assertEqual("pass", allowed.status, allowed.report)

        for non_numeric_phrase in (
            "少数民族受试者可按方案参加研究。",
            "经确认后，研究流程保持一成不变。",
        ):
            with self.subTest(non_numeric_phrase=non_numeric_phrase):
                outcome = resolve(non_numeric_phrase, self.datapack, FACTS)
                self.assertEqual("pass", outcome.status, outcome.report)

    def test_protocol_facts_are_required(self):
        with self.assertRaises(ConfigurationError):
            compile_whitelist(None)


if __name__ == "__main__":
    unittest.main()
