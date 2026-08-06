from __future__ import annotations

import copy
import difflib
import json
import unittest
from pathlib import Path

from csr_writer.module_router import (
    CAPABILITY_CONDITIONS,
    EXPECTED_MODULE_IDS,
    ORIGINAL_M3_MODULE_IDS,
    canonical_structure_json,
    load_module_definitions,
    route_modules,
    route_structure,
)
from csr_writer.study_facts import StudyFactsValidationError, render_fact_card, validate_study_facts


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = PROJECT_ROOT / "examples" / "demo_protocol_facts.json"
TOPICAL_FACTS_PATH = (
    PROJECT_ROOT / "examples" / "fixtures" / "topical_ib_study_facts.deidentified.json"
)


class M3StudyFactsAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.facts = json.loads(FACTS_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _module_ids(result):
        return [item["id"] for item in result["active_modules"]]

    @staticmethod
    def _contributions(result, section=None, variant=None):
        values = result["structure"]["section_contributions"]
        return [
            item
            for item in values
            if (section is None or item["section"] == section)
            and (variant is None or item["variant"] == variant)
        ]

    @staticmethod
    def _materialized(result):
        return result["structure"]["materialized_sections"]

    def test_frozen_schema_accepts_complete_demo_and_rejects_unknown_fields(self):
        self.assertIs(self.facts, validate_study_facts(self.facts))
        for mutation in ("top", "nested"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.facts)
                if mutation == "top":
                    changed["profile"] = "phase1b"
                else:
                    changed["design"]["phase_template"] = "phase1b"
                with self.assertRaises(StudyFactsValidationError):
                    validate_study_facts(changed)

    def test_frozen_schema_requires_all_five_dimensions(self):
        for dimension in ("objectives", "design", "population", "data_domains", "analyses"):
            with self.subTest(dimension=dimension):
                changed = copy.deepcopy(self.facts)
                del changed[dimension]
                with self.assertRaises(StudyFactsValidationError):
                    validate_study_facts(changed)

    def test_frozen_schema_rejects_non_finite_numeric_facts(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.facts)
                changed["design"]["dose_levels"][0]["value"] = value
                with self.assertRaisesRegex(StudyFactsValidationError, "finite JSON number"):
                    validate_study_facts(changed)

    def test_analysis_set_ids_allow_real_abbreviations_and_full_names(self):
        for valid_id in (
            "mFAS",
            "mITT",
            "mPPS",
            "SS",
            "PKS",
            "PKCS",
            "PKPS",
            "BES",
            "All Screened Set",
            "All Enrolled Set",
        ):
            with self.subTest(valid_id=valid_id):
                changed = copy.deepcopy(self.facts)
                changed["analyses"]["analysis_sets"][0]["id"] = valid_id
                self.assertIs(changed, validate_study_facts(changed))
        for invalid_id in ("", "2FAS", "FAS/SS", "FAS-SS", " FAS", "FAS "):
            with self.subTest(invalid_id=invalid_id):
                changed = copy.deepcopy(self.facts)
                changed["analyses"]["analysis_sets"][0]["id"] = invalid_id
                with self.assertRaises(StudyFactsValidationError):
                    validate_study_facts(changed)

    def test_module_inventory_has_exactly_47_reviewed_one_to_one_conditions(self):
        modules = load_module_definitions()
        self.assertEqual(47, len(modules))
        self.assertEqual(EXPECTED_MODULE_IDS, tuple(item["id"] for item in modules))
        self.assertEqual(
            set(CAPABILITY_CONDITIONS),
            {item["activation_flag"] for item in modules},
        )
        self.assertNotIn("phase", " ".join(item["condition"] for item in modules))
        expanded = [item for item in modules if item["id"] not in ORIGINAL_M3_MODULE_IDS]
        self.assertEqual(24, len(expanded))
        implemented = [item for item in expanded if not item.get("framework_only")]
        framework = [item for item in expanded if item.get("framework_only")]
        self.assertEqual(["M-BE"], [item["id"] for item in framework])
        self.assertTrue(all(item["playbook"] for item in implemented))
        self.assertTrue(all(item["expected_tfl_categories"] for item in implemented))
        self.assertTrue(all(item["required_analysis_sets"] for item in implemented))
        self.assertEqual([], framework[0]["structure"])
        self.assertIsNone(framework[0]["playbook"])

    def test_expressible_fact_sets_cover_all_47_module_activation_conditions(self):
        maximal = copy.deepcopy(self.facts)
        maximal["objectives"].update(
            efficacy=True,
            confirmatory_efficacy=True,
            determine_mtd=True,
            determine_rp2d=True,
        )
        maximal["design"].update(
            type="crossover",
            route_of_administration="topical",
            escalation_scheme="BOIN",
            dlt_defined=True,
            expansion_cohorts=2,
            noninferiority=True,
            interim_analysis=True,
            n_centers=8,
        )
        maximal["design"].update(
            randomized=True,
            controlled=True,
            blinding="double_blind",
            control_types=["placebo", "active"],
            clinical_pharmacology_studies=[
                "bioequivalence",
                "drug_drug_interaction",
                "food_effect",
            ],
        )
        maximal["population"].update(
            healthy_volunteer=True,
            patients=False,
            special_populations=[
                "hepatic_impairment",
                "renal_impairment",
                "pediatric",
                "older_adult",
                "pregnancy",
            ],
        )
        maximal["data_domains"].update(
            dlt=True,
            pk_concentration=True,
            ada=True,
            pd_biomarker=True,
            ecg=True,
            ecg_intensive=True,
            local_tolerance=True,
            pulmonary_function=True,
            ophthalmic_safety=True,
            therapeutic_area_endpoints=[
                "infection_microbiology",
                "dermatology_scales",
            ],
            ae_consequence_strata=True,
            ae_study_withdrawal=True,
            aesi=True,
            vital_signs=True,
            pregnancy=True,
            medication_error_overdose_misuse=True,
        )
        maximal["analyses"].update(
            descriptive_only=False,
            hypothesis_test=True,
            multiplicity_control=True,
            estimand_framework=True,
            nca=True,
            population_pk=True,
            dose_proportionality=True,
            exposure_response=True,
            time_to_event=True,
            subgroup=True,
        )
        maximal["analyses"]["estimand"] = {
            "population": "目标患者",
            "treatment_condition": "试验药与对照",
            "variable": "主要终点",
            "intercurrent_event_strategy": "复合策略",
            "population_level_summary": "治疗差异",
        }
        confirmatory = set(self._module_ids(route_modules(maximal)))
        exploratory_facts = copy.deepcopy(maximal)
        exploratory_facts["analyses"]["hypothesis_test"] = False
        exploratory = set(self._module_ids(route_modules(exploratory_facts)))
        inhalation_facts = copy.deepcopy(maximal)
        inhalation_facts["design"]["route_of_administration"] = "inhalation"
        inhalation = set(self._module_ids(route_modules(inhalation_facts)))
        ophthalmic_facts = copy.deepcopy(maximal)
        ophthalmic_facts["design"]["route_of_administration"] = "ophthalmic"
        ophthalmic = set(self._module_ids(route_modules(ophthalmic_facts)))
        self.assertEqual(
            set(EXPECTED_MODULE_IDS),
            confirmatory | exploratory | inhalation | ophthalmic,
        )

    def test_deidentified_topical_fixture_expands_original_five_to_fourteen(self):
        topical = json.loads(TOPICAL_FACTS_PATH.read_text(encoding="utf-8"))
        validate_study_facts(topical)
        routed = route_modules(topical)
        active = set(self._module_ids(routed))
        legacy = active & set(ORIGINAL_M3_MODULE_IDS)
        expanded = active - set(ORIGINAL_M3_MODULE_IDS)
        self.assertEqual(
            {"M-CORE", "M-DOSE-ESC", "M-PK-NCA", "M-EFF-EXPLORE", "M-MULTICENTER"},
            legacy,
        )
        self.assertEqual(
            {
                "M-LOCAL-TOLERANCE",
                "M-AE-CONSEQUENCE",
                "M-TOPICAL-PK",
                "M-RANDOMIZED",
                "M-BLINDING",
                "M-PLACEBO-CTRL",
                "M-INFECTION",
                "M-DERMATOLOGY",
                "M-ECG",
            },
            expanded,
        )
        self.assertEqual(14, len(active))
        section_variants = {
            (item["section"], item["variant"])
            for item in routed["structure"]["materialized_sections"]
        }
        self.assertIn(("12.5.1", "local_tolerance_ae_subset"), section_variants)
        self.assertIn(("12.2", "ae_consequence_pairs"), section_variants)
        self.assertIn(("11.6.2.1", "dermatology_ancova"), section_variants)
        self.assertIn(("9.4.3", "randomization"), section_variants)
        self.assertIn(("9.4.6", "blinding"), section_variants)

    def test_route_and_special_study_predicates_are_explicit_and_phase_independent(self):
        changed = copy.deepcopy(self.facts)
        changed["design"].update(
            route_of_administration="inhalation",
            randomized=True,
            controlled=True,
            blinding="observer_blind",
            control_types=["active"],
            clinical_pharmacology_studies=["bioequivalence", "food_effect"],
        )
        changed["data_domains"]["pulmonary_function"] = True
        changed["population"]["special_populations"] = [
            "hepatic_impairment",
            "pediatric",
        ]
        routed = route_modules(changed)
        ids = set(self._module_ids(routed))
        self.assertTrue(
            {
                "M-INHALATION",
                "M-RANDOMIZED",
                "M-BLINDING",
                "M-ACTIVE-CTRL",
                "M-BE",
                "M-FOOD-EFFECT",
                "M-HEPATIC-IMPAIR",
                "M-PEDIATRIC",
            }.issubset(ids)
        )
        before_structure = route_structure(changed)
        changed["phase"] = "III"
        self.assertEqual(before_structure, route_structure(changed))

    def test_acceptance_escalation_none_removes_dose_escalation_contribution(self):
        baseline = route_modules(self.facts)
        changed_facts = copy.deepcopy(self.facts)
        changed_facts["design"]["escalation_scheme"] = "none"
        changed = route_modules(changed_facts)
        self.assertIn("M-DOSE-ESC", self._module_ids(baseline))
        self.assertNotIn("M-DOSE-ESC", self._module_ids(changed))
        self.assertTrue(self._contributions(baseline, section="12.6", variant="dose_escalation"))
        self.assertFalse(self._contributions(changed, section="12.6", variant="dose_escalation"))
        self.assertFalse(
            any(item["section"].startswith("12.6") for item in self._materialized(changed))
        )
        self.assertEqual(
            {"M-DLT", "M-MTD-RP2D"},
            {
                item["contributed_by"]
                for item in changed["structure"]["unmaterialized_contributions"]
            },
        )

    def test_expansion_can_anchor_12_6_without_dose_escalation(self):
        changed_facts = copy.deepcopy(self.facts)
        changed_facts["design"]["escalation_scheme"] = "none"
        changed_facts["design"]["expansion_cohorts"] = 2
        changed = route_modules(changed_facts)
        materialized = self._materialized(changed)
        self.assertTrue(
            any(
                item["section"] == "12.6" and item["variant"] == "expansion_cohorts"
                for item in materialized
            )
        )
        self.assertTrue(any(item["section"] == "12.6.3" for item in materialized))
        self.assertEqual([], changed["structure"]["unmaterialized_contributions"])

    def test_acceptance_ada_false_removes_12_5_6(self):
        baseline = route_modules(self.facts)
        changed_facts = copy.deepcopy(self.facts)
        changed_facts["data_domains"]["ada"] = False
        changed = route_modules(changed_facts)
        self.assertIn("M-ADA", self._module_ids(baseline))
        self.assertNotIn("M-ADA", self._module_ids(changed))
        self.assertTrue(self._contributions(baseline, section="12.5.6"))
        self.assertFalse(self._contributions(changed, section="12.5.6"))

    def test_acceptance_hypothesis_test_switches_exploratory_to_confirmatory(self):
        exploratory = route_modules(self.facts)
        changed_facts = copy.deepcopy(self.facts)
        changed_facts["analyses"]["hypothesis_test"] = True
        confirmatory = route_modules(changed_facts)
        self.assertIn("M-EFF-EXPLORE", self._module_ids(exploratory))
        self.assertNotIn("M-EFF-CONFIRM", self._module_ids(exploratory))
        self.assertNotIn("M-EFF-EXPLORE", self._module_ids(confirmatory))
        self.assertIn("M-EFF-CONFIRM", self._module_ids(confirmatory))
        self.assertTrue(self._contributions(exploratory, "11.6", "exploratory"))
        self.assertTrue(self._contributions(confirmatory, "11.4.1", "confirmatory"))

    def test_acceptance_phase_only_change_has_exact_empty_structure_diff(self):
        changed_facts = copy.deepcopy(self.facts)
        changed_facts["phase"] = "III"
        before = canonical_structure_json(self.facts)
        after = canonical_structure_json(changed_facts)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="phase-Ib",
                tofile="phase-III",
            )
        )
        self.assertEqual("", diff)
        self.assertEqual(route_structure(self.facts), route_structure(changed_facts))
        self.assertNotEqual(
            route_modules(self.facts)["warnings"],
            route_modules(changed_facts)["warnings"],
            "phase may affect warnings but never the structural artifact",
        )

    def test_phase_plausibility_warning_distinguishes_ib_from_iii(self):
        phase_i = copy.deepcopy(self.facts)
        phase_i["phase"] = "Ib"
        phase_i["analyses"]["multiplicity_control"] = True
        phase_iii = copy.deepcopy(phase_i)
        phase_iii["phase"] = "III"
        i_warnings = route_modules(phase_i)["warnings"]
        iii_warnings = route_modules(phase_iii)["warnings"]
        self.assertTrue(any("期别标为 I 类" in item for item in i_warnings))
        self.assertFalse(any("期别标为 I 类" in item for item in iii_warnings))

    def test_fact_card_is_human_readable_and_marks_confirmation(self):
        card = render_fact_card(self.facts)
        self.assertIn("# 研究事实卡：XYZ-001-101", card)
        self.assertIn("状态：**待人工确认**", card)
        self.assertIn("期别标签（非路由）", card)
        self.assertIn("M-DOSE-ESC", card)
        self.assertIn("- [ ] 已对照当前有效研究方案/SAP", card)
        checked_in = (PROJECT_ROOT / "examples" / "demo_study_facts_card.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(checked_in, card)


if __name__ == "__main__":
    unittest.main()
