from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.group_semantics import GroupSemanticError, validate_group_semantics
from csr_writer.section_writer import DraftParagraph, SectionWriterError, draft_section


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "m4"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestM4GroupSemantics(unittest.TestCase):
    def setUp(self):
        self.datapack = load_json("synthetic_datapack.json")
        self.contract = load_json("group_contract.json")
        self.metadata = load_json("section_metadata.json")

    def _five_group_fixture(self):
        contract = json.loads(
            (ROOT / "examples" / "fixtures" / "five_group_contract.deidentified.json")
            .read_text(encoding="utf-8")
        )
        metadata = json.loads(
            (ROOT / "examples" / "fixtures" / "five_group_section_metadata.deidentified.json")
            .read_text(encoding="utf-8")
        )
        datapack = copy.deepcopy(self.datapack)
        datapack["cells"] = {}
        datapack["tables"] = [
            item for item in datapack["tables"] if item["table_id"] in {"T10.1", "T11.1", "T14.2.1"}
        ]
        source_id = datapack["sources"][0]["source_id"]
        for index, (group_id, group) in enumerate(contract["groups"].items(), start=1):
            column = group["column"]
            label = group_id
            randomized = 20 + index
            analysis = randomized - 1
            for key, value, row_id, row_label, row_index, value_type in (
                (
                    group["randomized_anchor"],
                    str(randomized),
                    "RANDOMIZED_COUNT",
                    "Randomized",
                    1,
                    "count",
                ),
                (
                    group["analysis_set_anchor"],
                    str(analysis),
                    "ANALYSIS_SET_COUNT",
                    "Full analysis set",
                    2,
                    "count",
                ),
                (
                    f"T14.2.1|POP=FAS|ROW=PRIMARY_RESPONSE|COL={column}",
                    f"{index + 4} ({20 + index}.0)",
                    "PRIMARY_RESPONSE",
                    "Primary response",
                    3,
                    "n_pct",
                ),
            ):
                datapack["cells"][key] = {
                    "column": {"id": column, "label": label},
                    "display_value": value,
                    "row": {"id": row_id, "indent": 0, "label": row_label},
                    "source_locator": {
                        "column_index": index,
                        "row_index": row_index,
                        "source_id": source_id,
                    },
                    "type": value_type,
                    "v": value,
                }
        return datapack, metadata, contract

    def test_normal_binding_passes_with_both_population_anchors(self):
        report = validate_group_semantics(self.datapack, self.metadata, self.contract)

        self.assertEqual("pass", report["status"])
        self.assertEqual(8, report["binding_count_checked"])
        self.assertEqual(
            {
                "explicit_role_to_column": "pass",
                "randomized_vs_analysis_set_column": "pass",
                "randomized_count_gte_analysis_set_count": "pass",
            },
            report["checks"],
        )
        self.assertEqual("XYZ200MG", report["groups"]["experimental"]["column"])
        self.assertEqual(60, report["groups"]["experimental"]["randomized_count"])
        self.assertEqual(58, report["groups"]["experimental"]["analysis_set_count"])
        self.assertEqual("PBO", report["groups"]["control"]["column"])
        self.assertEqual(60, report["groups"]["control"]["randomized_count"])
        self.assertEqual(57, report["groups"]["control"]["analysis_set_count"])

    def test_swapped_experimental_and_control_result_bindings_are_blocked(self):
        rogue = copy.deepcopy(self.metadata)
        efficacy_bindings = rogue[2]["binding_manifest"]
        efficacy_bindings[0]["key"], efficacy_bindings[1]["key"] = (
            efficacy_bindings[1]["key"],
            efficacy_bindings[0]["key"],
        )

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(self.datapack, rogue, self.contract)

        error = caught.exception
        self.assertEqual("blocked", error.report["status"])
        self.assertEqual("E-GROUP-ROLE-COLUMN", error.issues[0]["code"])
        self.assertEqual("experimental_result", error.issues[0]["role"])
        self.assertEqual("XYZ200MG", error.issues[0]["expected_column"])
        self.assertEqual("PBO", error.issues[0]["observed_column"])

    def test_five_group_contract_passes_and_any_two_result_columns_cannot_swap(self):
        datapack, metadata, contract = self._five_group_fixture()
        report = validate_group_semantics(datapack, metadata, contract)
        self.assertEqual("pass", report["status"])
        self.assertEqual("2.0", report["schema_version"])
        self.assertEqual(5, len(report["groups"]))
        self.assertEqual(5, report["binding_count_checked"])

        rogue = copy.deepcopy(metadata)
        bindings = rogue[0]["binding_manifest"]
        bindings[1]["key"], bindings[4]["key"] = bindings[4]["key"], bindings[1]["key"]
        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(datapack, rogue, contract)
        self.assertEqual("E-GROUP-ROLE-COLUMN", caught.exception.issues[0]["code"])
        self.assertEqual("C0_5PCT_BID", caught.exception.issues[0]["expected_column"])
        self.assertEqual("C1_0PCT_QD", caught.exception.issues[0]["observed_column"])

    def test_literal_placeholder_swap_in_generated_sentence_is_blocked(self):
        request = load_json("../section_inputs/efficacy_request.json")

        class SwappedSentenceAdapter:
            def generate(self, envelope):
                bindings = envelope["data"]["tfl_bindings"]
                swapped = [bindings[1], bindings[0], *bindings[2:]]
                placeholders = " ".join(
                    f"{{{{{item['key']}}}}}" for item in swapped
                )
                return (
                    DraftParagraph(
                        text=f"试验组与对照组结果依次为 {placeholders}",
                        sources=(envelope["data"]["source_fragments"][0]["source_id"],),
                        confidence="derived",
                        needs_human=False,
                    ),
                )

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SectionWriterError, "E-GROUP-BINDING-ORDER"):
                draft_section(request, temporary, adapter=SwappedSentenceAdapter())

    def test_anchor_columns_must_agree_with_explicit_group_column(self):
        rogue = copy.deepcopy(self.contract)
        rogue["groups"]["experimental"]["analysis_set_anchor"] = (
            "T11.1|POP=FAS|ROW=ANALYSIS_SET_COUNT|COL=PBO"
        )

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(self.datapack, self.metadata, rogue)

        self.assertEqual("E-GROUP-ANCHOR-COLUMN", caught.exception.issues[0]["code"])

    def test_analysis_set_count_cannot_exceed_randomized_count(self):
        rogue = copy.deepcopy(self.datapack)
        key = "T11.1|POP=FAS|ROW=ANALYSIS_SET_COUNT|COL=XYZ200MG"
        rogue["cells"][key]["v"] = "61"
        rogue["cells"][key]["display_value"] = "61"

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(rogue, self.metadata, self.contract)

        self.assertEqual("E-GROUP-COUNT-ORDER", caught.exception.issues[0]["code"])
        self.assertEqual(60, caught.exception.issues[0]["randomized_count"])
        self.assertEqual(61, caught.exception.issues[0]["analysis_set_count"])

    def test_group_bearing_role_cannot_bypass_contract_by_renaming(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"][0]["role"] = "experimental_sensitivity_result"

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(self.datapack, rogue, self.contract)

        self.assertEqual("E-GROUP-ROLE-UNMAPPED", caught.exception.issues[0]["code"])

    def test_unknown_datapack_binding_is_blocked(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"][0]["key"] = (
            "T14.2.1|POP=FAS|ROW=UNKNOWN_RESULT|COL=XYZ200MG"
        )

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(self.datapack, rogue, self.contract)

        self.assertEqual("E-GROUP-BINDING-UNKNOWN", caught.exception.issues[0]["code"])

    def test_duplicate_role_or_key_is_blocked_as_ambiguous_metadata(self):
        rogue = copy.deepcopy(self.metadata)
        rogue[2]["binding_manifest"].append(
            copy.deepcopy(rogue[2]["binding_manifest"][0])
        )

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(self.datapack, rogue, self.contract)

        self.assertEqual("E-GROUP-BINDING-DUPLICATE", caught.exception.issues[0]["code"])

    def test_non_string_contract_target_is_domain_error_not_type_error(self):
        rogue = copy.deepcopy(self.contract)
        rogue["binding_roles"]["experimental_result"] = ["experimental"]

        with self.assertRaises(GroupSemanticError) as caught:
            validate_group_semantics(self.datapack, self.metadata, rogue)

        self.assertEqual("E-GROUP-ROLE", caught.exception.issues[0]["code"])


if __name__ == "__main__":
    unittest.main()
