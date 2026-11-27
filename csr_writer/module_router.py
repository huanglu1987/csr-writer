"""Deterministic routing: study facts -> capabilities -> content modules."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigurationError
from .io_utils import load_yaml
from .section_planner import build_section_applicability_plan
from .study_facts import validate_study_facts


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODULES_PATH = PROJECT_ROOT / "skills" / "csr-writer" / "references" / "modules.yaml"
DEFAULT_PLAYBOOK_ROOT = PROJECT_ROOT / "skills" / "csr-writer" / "section-playbooks"


# These reviewed predicates are the executable form of specification v1.2, section 2.3.2,
# plus the real-material-driven route/control/clinical-pharmacology/endpoint extension.
# They intentionally name only the five routing dimensions; `phase` is absent by contract.
CAPABILITY_CONDITIONS: dict[str, str] = {
    "core": "always true",
    "has_dose_escalation": "design.escalation_scheme != none",
    "has_dlt": "design.dlt_defined",
    "determines_mtd_or_rp2d": "objectives.determine_mtd || objectives.determine_rp2d",
    "has_expansion_cohorts": "design.expansion_cohorts > 0",
    "has_pk_nca": "data_domains.pk_concentration && analyses.nca",
    "has_population_pk": "analyses.population_pk",
    "has_dose_proportionality": "analyses.dose_proportionality",
    "has_exposure_response": "analyses.exposure_response",
    "has_ada": "data_domains.ada",
    "has_pd_biomarker": "data_domains.pd_biomarker",
    "has_confirmatory_efficacy": (
        "objectives.confirmatory_efficacy && analyses.hypothesis_test"
    ),
    "has_exploratory_efficacy": "objectives.efficacy && !(objectives.confirmatory_efficacy && analyses.hypothesis_test)",
    "has_multiplicity_control": "analyses.multiplicity_control",
    "has_noninferiority": "design.noninferiority",
    "has_estimand_framework": "analyses.estimand_framework",
    "has_interim_analysis": "design.interim_analysis",
    "has_subgroup_analysis": "analyses.subgroup",
    "is_multicenter": "design.n_centers > 1",
    "is_crossover": "design.type == crossover",
    "has_time_to_event_analysis": "analyses.time_to_event",
    "has_intensive_ecg": "data_domains.ecg_intensive",
    "has_ecg": "data_domains.ecg",
    "has_aesi": "data_domains.aesi",
    "has_vital_signs": "data_domains.vital_signs",
    "has_pregnancy_reporting": (
        "data_domains.pregnancy || pregnancy in population.special_populations"
    ),
    "has_medication_error_reporting": "data_domains.medication_error_overdose_misuse",
    "is_healthy_volunteer": "population.healthy_volunteer",
    "has_local_tolerance": "data_domains.local_tolerance",
    "has_ae_consequence_strata": "data_domains.ae_consequence_strata",
    "has_ae_study_withdrawal": "data_domains.ae_study_withdrawal",
    "has_topical_pk": (
        "design.route_of_administration == topical && data_domains.pk_concentration"
    ),
    "is_inhalation_route": "design.route_of_administration == inhalation",
    "is_ophthalmic_route": "design.route_of_administration == ophthalmic",
    "is_randomized": "design.randomized",
    "is_blinded": "design.blinding != open_label",
    "has_placebo_control": "placebo in design.control_types",
    "has_active_control": "active in design.control_types",
    "is_bioequivalence_study": (
        "design.study_type == bioequivalence || "
        "bioequivalence in design.clinical_pharmacology_studies"
    ),
    "is_ddi_study": (
        "design.study_type == drug_drug_interaction || "
        "drug_drug_interaction in design.clinical_pharmacology_studies"
    ),
    "is_food_effect_study": (
        "design.study_type == food_effect || "
        "food_effect in design.clinical_pharmacology_studies"
    ),
    "has_hepatic_impairment_pk": (
        "(design.study_type == hepatic_impairment || "
        "hepatic_impairment in population.special_populations) && "
        "data_domains.pk_concentration"
    ),
    "has_renal_impairment_pk": (
        "(design.study_type == renal_impairment || "
        "renal_impairment in population.special_populations) && "
        "data_domains.pk_concentration"
    ),
    "has_pediatric_population": "pediatric in population.special_populations",
    "has_geriatric_population": "older_adult in population.special_populations",
    "has_infection_endpoints": (
        "infection_microbiology in data_domains.therapeutic_area_endpoints"
    ),
    "has_dermatology_endpoints": (
        "dermatology_scales in data_domains.therapeutic_area_endpoints"
    ),
}


ORIGINAL_M3_MODULE_IDS = (
    "M-CORE",
    "M-DOSE-ESC",
    "M-DLT",
    "M-MTD-RP2D",
    "M-EXPANSION",
    "M-PK-NCA",
    "M-PK-POP",
    "M-DOSE-PROP",
    "M-EXPOSURE-RESP",
    "M-ADA",
    "M-PD-BIOMARKER",
    "M-EFF-CONFIRM",
    "M-EFF-EXPLORE",
    "M-MULTIPLICITY",
    "M-NONINF",
    "M-ESTIMAND",
    "M-INTERIM",
    "M-SUBGROUP",
    "M-MULTICENTER",
    "M-CROSSOVER",
    "M-SURVIVAL",
    "M-QT",
    "M-HV",
)


EXPECTED_MODULE_IDS = ORIGINAL_M3_MODULE_IDS + (
    "M-LOCAL-TOLERANCE",
    "M-AE-CONSEQUENCE",
    "M-TOPICAL-PK",
    "M-INHALATION",
    "M-OPHTHALMIC",
    "M-RANDOMIZED",
    "M-BLINDING",
    "M-PLACEBO-CTRL",
    "M-ACTIVE-CTRL",
    "M-BE",
    "M-DDI",
    "M-FOOD-EFFECT",
    "M-HEPATIC-IMPAIR",
    "M-RENAL-IMPAIR",
    "M-PEDIATRIC",
    "M-GERIATRIC",
    "M-INFECTION",
    "M-DERMATOLOGY",
    "M-ECG",
    "M-AESI",
    "M-VITAL-SIGNS",
    "M-PREGNANCY",
    "M-MEDICATION-ERROR",
    "M-AE-WITHDRAWAL",
)


CONTEXTUAL_SECTION_RE = re.compile(r"^\$EFFICACY(?P<suffix>(?:\.\d+)*)$")
NUMERIC_SECTION_RE = re.compile(r"^\d+(?:\.\d+)*$")
SECTION_RANGE_RE = re.compile(r"^\d+(?:\.\d+)*-\d+(?:\.\d+)*$")


def derive_capability_flags(facts: Mapping[str, Any]) -> dict[str, bool]:
    """Evaluate all reviewed module predicates without reading ``phase``."""
    validated = validate_study_facts(dict(facts))
    objectives = validated["objectives"]
    design = validated["design"]
    population = validated["population"]
    data_domains = validated["data_domains"]
    analyses = validated["analyses"]

    flags = {
        "core": True,
        "has_dose_escalation": design["escalation_scheme"] != "none",
        "has_dlt": design["dlt_defined"],
        "determines_mtd_or_rp2d": (
            objectives["determine_mtd"] or objectives["determine_rp2d"]
        ),
        "has_expansion_cohorts": design["expansion_cohorts"] > 0,
        "has_pk_nca": data_domains["pk_concentration"] and analyses["nca"],
        "has_population_pk": analyses["population_pk"],
        "has_dose_proportionality": analyses["dose_proportionality"],
        "has_exposure_response": analyses["exposure_response"],
        "has_ada": data_domains["ada"],
        "has_pd_biomarker": data_domains["pd_biomarker"],
        "has_confirmatory_efficacy": (
            objectives["confirmatory_efficacy"] and analyses["hypothesis_test"]
        ),
        "has_exploratory_efficacy": (
            objectives["efficacy"] and not (objectives["confirmatory_efficacy"] and analyses["hypothesis_test"])
        ),
        "has_multiplicity_control": analyses["multiplicity_control"],
        "has_noninferiority": design["noninferiority"],
        "has_estimand_framework": analyses["estimand_framework"],
        "has_interim_analysis": design["interim_analysis"],
        "has_subgroup_analysis": analyses["subgroup"],
        "is_multicenter": design["n_centers"] > 1,
        "is_crossover": design["type"] == "crossover",
        "has_time_to_event_analysis": analyses["time_to_event"],
        "has_intensive_ecg": data_domains["ecg_intensive"],
        "has_ecg": data_domains["ecg"],
        "has_aesi": bool(data_domains.get("aesi", False)),
        "has_vital_signs": bool(data_domains.get("vital_signs", False)),
        "has_pregnancy_reporting": bool(data_domains.get("pregnancy", False))
        or "pregnancy" in population["special_populations"],
        "has_medication_error_reporting": bool(
            data_domains.get("medication_error_overdose_misuse", False)
        ),
        "is_healthy_volunteer": population["healthy_volunteer"],
        "has_local_tolerance": data_domains["local_tolerance"],
        "has_ae_consequence_strata": data_domains["ae_consequence_strata"],
        "has_ae_study_withdrawal": bool(
            data_domains.get("ae_study_withdrawal", False)
        ),
        "has_topical_pk": (
            design["route_of_administration"] == "topical"
            and data_domains["pk_concentration"]
        ),
        "is_inhalation_route": design["route_of_administration"] == "inhalation",
        "is_ophthalmic_route": design["route_of_administration"] == "ophthalmic",
        "is_randomized": design["randomized"],
        "is_blinded": design["blinding"] != "open_label",
        "has_placebo_control": "placebo" in design["control_types"],
        "has_active_control": "active" in design["control_types"],
        "is_bioequivalence_study": (
            design["study_type"] == "bioequivalence"
            or
            "bioequivalence" in design["clinical_pharmacology_studies"]
        ),
        "is_ddi_study": (
            design["study_type"] == "drug_drug_interaction"
            or
            "drug_drug_interaction" in design["clinical_pharmacology_studies"]
        ),
        "is_food_effect_study": (
            design["study_type"] == "food_effect"
            or
            "food_effect" in design["clinical_pharmacology_studies"]
        ),
        "has_hepatic_impairment_pk": (
            (
                design["study_type"] == "hepatic_impairment"
                or "hepatic_impairment" in population["special_populations"]
            )
            and data_domains["pk_concentration"]
        ),
        "has_renal_impairment_pk": (
            (
                design["study_type"] == "renal_impairment"
                or "renal_impairment" in population["special_populations"]
            )
            and data_domains["pk_concentration"]
        ),
        "has_pediatric_population": "pediatric" in population["special_populations"],
        "has_geriatric_population": "older_adult" in population["special_populations"],
        "has_infection_endpoints": (
            "infection_microbiology" in data_domains["therapeutic_area_endpoints"]
        ),
        "has_dermatology_endpoints": (
            "dermatology_scales" in data_domains["therapeutic_area_endpoints"]
        ),
    }
    if set(flags) != set(CAPABILITY_CONDITIONS):
        missing = sorted(set(CAPABILITY_CONDITIONS) - set(flags))
        extra = sorted(set(flags) - set(CAPABILITY_CONDITIONS))
        raise ConfigurationError(f"Capability implementation drift: missing={missing}, extra={extra}")
    if not all(isinstance(value, bool) for value in flags.values()):
        raise ConfigurationError("Every derived capability must be boolean")
    return flags


def _validate_modules_config(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "routing_contract",
        "modules",
    }:
        raise ConfigurationError("modules.yaml root contract is invalid or has unknown fields")
    if payload["schema_version"] != "1.2":
        raise ConfigurationError("Unsupported modules.yaml schema_version")
    contract = payload["routing_contract"]
    if not isinstance(contract, dict) or contract.get("forbidden_routing_fields") != ["phase"]:
        raise ConfigurationError("modules.yaml must explicitly forbid phase as a routing field")
    modules = payload["modules"]
    if not isinstance(modules, list):
        raise ConfigurationError("modules.yaml modules must be a list")
    required_fields = {
        "id",
        "name_zh",
        "activation_flag",
        "condition",
        "structure",
        "playbook",
        "expected_tfl_categories",
        "required_analysis_sets",
    }
    seen_ids: list[str] = []
    seen_flags: list[str] = []
    for index, module in enumerate(modules):
        allowed_fields = required_fields | {"framework_only"}
        if not isinstance(module, dict) or not required_fields.issubset(module) or not set(module).issubset(allowed_fields):
            raise ConfigurationError(
                f"modules[{index}] must have {sorted(required_fields)!r} and only optional framework_only"
            )
        module_id = module["id"]
        flag = module["activation_flag"]
        if not isinstance(module_id, str) or not isinstance(flag, str):
            raise ConfigurationError(f"modules[{index}] id and activation_flag must be strings")
        if flag not in CAPABILITY_CONDITIONS:
            raise ConfigurationError(f"Unknown activation flag for {module_id}: {flag}")
        if module["condition"] != CAPABILITY_CONDITIONS[flag]:
            raise ConfigurationError(
                f"Reviewed condition drift for {module_id}: {module['condition']!r}"
            )
        seen_ids.append(module_id)
        seen_flags.append(flag)
        framework_only = module.get("framework_only", False)
        if not isinstance(framework_only, bool):
            raise ConfigurationError(f"{module_id}.framework_only must be boolean")
        if framework_only != (module_id == "M-BE"):
            raise ConfigurationError("Only M-BE may be the framework-only module in this release")
        if not isinstance(module["structure"], list) or (
            not module["structure"] and not framework_only
        ):
            raise ConfigurationError(f"{module_id} needs at least one structure contribution")
        for contribution in module["structure"]:
            if not isinstance(contribution, dict) or set(contribution) != {
                "section",
                "action",
                "variant",
                "title",
            }:
                raise ConfigurationError(f"Invalid structure contribution in {module_id}")
            if contribution["action"] not in {"add", "extend", "override"}:
                raise ConfigurationError(
                    f"Invalid structure action in {module_id}: {contribution['action']!r}"
                )
            if not all(
                isinstance(contribution[key], str) and contribution[key]
                for key in ("section", "variant", "title")
            ):
                raise ConfigurationError(f"Empty structure value in {module_id}")
            if not (
                NUMERIC_SECTION_RE.fullmatch(contribution["section"])
                or CONTEXTUAL_SECTION_RE.fullmatch(contribution["section"])
                or SECTION_RANGE_RE.fullmatch(contribution["section"])
            ):
                raise ConfigurationError(
                    f"Unsupported section selector in {module_id}: {contribution['section']!r}"
                )
        for list_field in ("expected_tfl_categories", "required_analysis_sets"):
            if not isinstance(module[list_field], list) or not all(
                isinstance(item, str) and item for item in module[list_field]
            ):
                raise ConfigurationError(f"{module_id}.{list_field} must be a string list")
        if module["playbook"] is not None and not isinstance(module["playbook"], str):
            raise ConfigurationError(f"{module_id}.playbook must be string or null")
        if module["playbook"] is not None:
            playbook_name = module["playbook"]
            if Path(playbook_name).name != playbook_name:
                raise ConfigurationError(f"{module_id}.playbook must be a plain filename")
            playbook_path = DEFAULT_PLAYBOOK_ROOT / playbook_name
            if not playbook_path.is_file():
                raise ConfigurationError(f"Missing declared playbook for {module_id}: {playbook_path}")
        elif module_id not in ORIGINAL_M3_MODULE_IDS and not framework_only:
            raise ConfigurationError(f"Expanded module {module_id} must declare a playbook")
        if framework_only and (
            module["structure"]
            or module["playbook"] is not None
            or module["expected_tfl_categories"]
            or module["required_analysis_sets"]
        ):
            raise ConfigurationError(
                f"{module_id} is framework-only and must not declare content or statistical contracts"
            )

    if tuple(seen_ids) != EXPECTED_MODULE_IDS:
        raise ConfigurationError(
            f"Module inventory/order drift: expected={EXPECTED_MODULE_IDS!r}, got={tuple(seen_ids)!r}"
        )
    if len(seen_ids) != len(set(seen_ids)):
        raise ConfigurationError("Duplicate module IDs are forbidden")
    if set(seen_flags) != set(CAPABILITY_CONDITIONS) or len(seen_flags) != len(set(seen_flags)):
        raise ConfigurationError("Each reviewed capability must map to exactly one module")
    return modules


def load_module_definitions(path: str | Path = DEFAULT_MODULES_PATH) -> list[dict[str, Any]]:
    return _validate_modules_config(load_yaml(path))


def _routing_warnings(facts: Mapping[str, Any], flags: Mapping[str, bool]) -> list[str]:
    """Return non-structural review warnings; phase is read only in this function."""
    warnings: list[str] = []
    objectives = facts["objectives"]
    design = facts["design"]
    population = facts["population"]
    domains = facts["data_domains"]
    analyses = facts["analyses"]

    if objectives["confirmatory_efficacy"] and not objectives["efficacy"]:
        warnings.append("确证性疗效目的为真，但一般疗效目的为假，请核对。")
    if objectives["confirmatory_efficacy"] and not analyses["hypothesis_test"]:
        warnings.append(
            "已标记确证性疗效目的，但未标记假设检验；"
            "当前按探索性疗效模块路由，请确认。"
        )
    if analyses["descriptive_only"] and analyses["hypothesis_test"]:
        warnings.append("同时标记仅描述性和假设检验，请核对分析策略。")
    if domains["ecg_intensive"] and not domains["ecg"]:
        warnings.append("密集 ECG 为真但 ECG 数据域为假，请核对。")
    if design["dlt_defined"] != domains["dlt"]:
        warnings.append("设计中的 DLT 定义与 DLT 数据域标记不一致，请核对。")
    if population["healthy_volunteer"] and population["patients"]:
        warnings.append("同时标记健康受试者与患者，请确认是否为混合人群。")
    control_types = design["control_types"]
    if design["controlled"] != bool(control_types):
        warnings.append("对照设计标记与 control_types 是否为空不一致，请核对。")
    if not design["randomized"] and design["blinding"] != "open_label":
        warnings.append("非随机设计标记了盲法，请核对盲法对象和实施方式。")
    if design["route_of_administration"] == "topical" and not domains["local_tolerance"]:
        warnings.append("外用给药未标记局部耐受性数据域，请确认是否未评价。")
    if design["route_of_administration"] == "inhalation" and not domains["pulmonary_function"]:
        warnings.append("吸入给药未标记肺功能数据域，请核对。")
    if design["route_of_administration"] == "ophthalmic" and not domains["ophthalmic_safety"]:
        warnings.append("眼用给药未标记眼部安全性数据域，请核对。")
    for extension_tag in ("oncology_response", "cardiovascular_events"):
        if extension_tag in domains["therapeutic_area_endpoints"]:
            warnings.append(
                f"治疗领域端点 {extension_tag} 已声明为扩展槽，"
                "M4.1 未附加专项内容模块。"
            )

    # Phase is deliberately isolated here. These warnings never feed flags/modules/structure.
    phase = str(facts["phase"]).strip().upper().replace("Ⅰ", "I").replace("Ⅱ", "II").replace("Ⅲ", "III")
    phase_label = re.sub(r"\s+", "", phase).replace("PHASE", "").replace("期", "")
    if phase_label == "III" and not flags["has_confirmatory_efficacy"]:
        warnings.append("期别标为 III，但未激活确证性疗效模块；期别不改结构，请人工确认。")
    if phase_label in {"I", "IA", "IB"} and analyses["multiplicity_control"]:
        warnings.append("期别标为 I 类且启用多重性控制；期别不改结构，请人工确认。")
    return warnings


def _materialize_structure(
    contributions: list[dict[str, Any]],
    flags: Mapping[str, bool],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve contextual efficacy targets without phase-based routing."""
    resolved: list[dict[str, Any]] = []
    unmaterialized: list[dict[str, Any]] = []
    for contribution in contributions:
        item = copy.deepcopy(contribution)
        match = CONTEXTUAL_SECTION_RE.fullmatch(item["section"])
        if match is not None:
            if flags["has_confirmatory_efficacy"]:
                root = "11.4"
            elif flags["has_exploratory_efficacy"]:
                root = "11.6"
            else:
                root = None
            if root is None:
                item["reason"] = "no active confirmatory or exploratory efficacy result branch"
                unmaterialized.append(item)
                continue
            item["contextual_section"] = item["section"]
            item["section"] = root + match.group("suffix")
        if not NUMERIC_SECTION_RE.fullmatch(item["section"]) and not SECTION_RANGE_RE.fullmatch(
            item["section"]
        ):
            blocked = copy.deepcopy(contribution)
            blocked["reason"] = "section selector did not resolve to a formal numeric section"
            unmaterialized.append(blocked)
        else:
            resolved.append(item)
    extension_roots = {
        "11.5",
        "11.6",
        "11.8",
        "12.5.1",
        "12.5.6",
        "12.6",
    }
    anchors = {
        item["section"]
        for item in resolved
        if item["action"] == "add" and item["section"] in extension_roots
    }
    materialized: list[dict[str, Any]] = []
    for item in resolved:
        root = next(
            (
                candidate
                for candidate in extension_roots
                if item["section"].startswith(candidate + ".")
            ),
            None,
        )
        if root is not None and root not in anchors:
            blocked = copy.deepcopy(item)
            blocked["reason"] = f"missing active formal extension anchor {root}"
            unmaterialized.append(blocked)
        else:
            materialized.append(item)
    return materialized, unmaterialized


def route_modules(
    facts: Mapping[str, Any],
    modules_path: str | Path = DEFAULT_MODULES_PATH,
) -> dict[str, Any]:
    """Route a validated study into modules and deterministic structure contributions."""
    validated = validate_study_facts(dict(facts))
    flags = derive_capability_flags(validated)
    definitions = load_module_definitions(modules_path)
    active_definitions = [item for item in definitions if flags[item["activation_flag"]]]

    active_modules: list[dict[str, Any]] = []
    section_contributions: list[dict[str, Any]] = []
    for module in active_definitions:
        active_modules.append(
            {
                "id": module["id"],
                "name_zh": module["name_zh"],
                "activation_flag": module["activation_flag"],
                "condition": module["condition"],
                "implementation_status": (
                    "framework_only" if module.get("framework_only", False) else "implemented"
                ),
                "playbook": module["playbook"],
                "expected_tfl_categories": copy.deepcopy(module["expected_tfl_categories"]),
                "required_analysis_sets": copy.deepcopy(module["required_analysis_sets"]),
            }
        )
        for contribution in module["structure"]:
            item = copy.deepcopy(contribution)
            item["contributed_by"] = module["id"]
            section_contributions.append(item)

    materialized_sections, unmaterialized_contributions = _materialize_structure(
        section_contributions, flags
    )

    # This object is the phase-independent routing artifact used for structural diffs.
    structure = {
        "active_module_ids": [item["id"] for item in active_modules],
        "section_contributions": section_contributions,
        "materialized_sections": materialized_sections,
        "unmaterialized_contributions": unmaterialized_contributions,
    }
    structure["section_plan"] = build_section_applicability_plan(
        validated,
        flags,
        active_modules,
        materialized_sections,
        CAPABILITY_CONDITIONS,
    )
    warnings = _routing_warnings(validated, flags)
    framework_active = [
        item["id"] for item in active_modules if item["implementation_status"] == "framework_only"
    ]
    if framework_active:
        warnings.append(
            "以下模块仅保留路由槽，不生成内容或统计结论："
            f"{', '.join(framework_active)}。"
        )
    if unmaterialized_contributions:
        blocked_modules = sorted(
            {item["contributed_by"] for item in unmaterialized_contributions}
        )
        warnings.append(
            "已激活模块存在无法解析到正式数字编号的结构贡献，"
            f"未物化并需人工核对：{', '.join(blocked_modules)}。"
        )
    return {
        "routing_schema_version": "1.2",
        "capability_flags": flags,
        "active_modules": active_modules,
        "structure": structure,
        "warnings": warnings,
    }


def route_structure(
    facts: Mapping[str, Any],
    modules_path: str | Path = DEFAULT_MODULES_PATH,
) -> dict[str, Any]:
    """Return the canonical phase-independent artifact for acceptance diffs."""
    routed = route_modules(facts, modules_path)
    return {
        "capability_flags": routed["capability_flags"],
        "structure": routed["structure"],
    }


def canonical_structure_json(
    facts: Mapping[str, Any],
    modules_path: str | Path = DEFAULT_MODULES_PATH,
) -> str:
    return json.dumps(
        route_structure(facts, modules_path),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
