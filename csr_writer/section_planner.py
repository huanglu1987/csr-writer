"""Study-fact-driven applicability planning for a pruned CSR skeleton."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .paths import DEFAULT_STRUCTURE
from .structure import flatten_sections, load_chapter_tree


# These rules identify official E3 sections that are not unconditional.  They
# are deliberately capability based: a therapeutic-area template is never a
# routing input.  Sections not listed here remain part of the cross-study core.
OPTIONAL_OFFICIAL_RULES: dict[str, dict[str, Any]] = {
    # Analysis populations serve disposition, efficacy, safety and PK.  When a
    # reviewed analysis-set register exists they are rendered once at 10.3 and
    # the original 11.1 location is retained only as an explicit relocation
    # declaration.
    "11.1": {"relocate_to": "10.3", "nonempty": "analyses.analysis_sets"},
    "9.4.3": {"any": ("is_randomized",)},
    "9.4.6": {"any": ("is_blinded",)},
    "9.5.3": {"nonempty": "objectives.primary_endpoints"},
    "9.5.4": {
        "any": (
            "has_pk_nca",
            "has_population_pk",
            "has_topical_pk",
            "is_ddi_study",
            "is_food_effect_study",
            "has_hepatic_impairment_pk",
            "has_renal_impairment_pk",
        )
    },
    # The official 11.4 branch is retained for confirmatory efficacy.  An
    # exploratory study uses the formally numbered 11.6 extension instead.
    "11.4": {"any": ("has_confirmatory_efficacy",)},
    "11.4.1": {"any": ("has_confirmatory_efficacy",)},
    "11.4.2": {"any": ("has_confirmatory_efficacy",)},
    "11.4.2.1": {"any": ("has_confirmatory_efficacy",)},
    "11.4.2.2": {"any": ("has_confirmatory_efficacy",)},
    "11.4.2.3": {"all": ("has_confirmatory_efficacy", "has_interim_analysis")},
    "11.4.2.4": {"all": ("has_confirmatory_efficacy", "is_multicenter")},
    "11.4.2.5": {"all": ("has_confirmatory_efficacy", "has_multiplicity_control")},
    "11.4.2.6": {"all": ("has_confirmatory_efficacy", "has_estimand_framework")},
    "11.4.2.7": {
        "all": ("has_confirmatory_efficacy",),
        "any_additional": ("has_noninferiority", "is_bioequivalence_study"),
    },
    "11.4.2.8": {"all": ("has_confirmatory_efficacy", "has_subgroup_analysis")},
    # The schema has no protocol fact that proves individual-efficacy listings
    # absent.  These sections therefore remain visible and enter the human
    # review queue instead of being deleted for lack of a module.
    "11.4.3": {"manual_review_if_unsupported": True},
    "11.4.4": {"any": ("has_exposure_response",)},
    "11.4.5": {"any": ("is_ddi_study",)},
    "11.4.6": {"manual_review_if_unsupported": True},
    "11.4.7": {"any": ("has_confirmatory_efficacy",)},
    "12": {"fact": ("data_domains.safety", True)},
    "12.1": {"fact": ("data_domains.safety", True)},
    "12.2": {"fact": ("data_domains.safety", True)},
    "12.3": {"fact": ("data_domains.safety", True)},
    "12.4": {"fact": ("data_domains.safety", True)},
    "12.5": {"fact": ("data_domains.safety", True)},
    "12.6": {"fact": ("data_domains.safety", True)},
    "14.2": {
        "any": (
            "has_confirmatory_efficacy",
            "has_exploratory_efficacy",
            "has_pk_nca",
            "has_population_pk",
            "has_topical_pk",
        )
    },
    "14.3": {"fact": ("data_domains.safety", True)},
}


FORMAL_EXTENSION_TITLES: dict[str, str] = {
    "9.5.1.1": "研究评估流程图",
    "9.5.5": "关键次要终点",
    "9.5.6": "其他次要终点",
    "9.5.7": "探索性终点",
    "9.5.8": "安全性终点",
    "9.5.9": "局部耐受性终点",
    "9.5.10": "PD、生物标志物或 PRO 终点",
    "10.3": "分析数据集",
    "11.5": "药代动力学评价",
    "11.5.1": "PK 分析集",
    "11.5.2": "血药浓度数据",
    "11.5.3": "低于定量下限（BLQ）数据的处理",
    "11.5.4": "PK 参数",
    "11.5.5": "达峰时间与系统暴露",
    "11.5.6": "剂量比例关系",
    "11.5.7": "多次给药蓄积",
    "11.5.8": "给药部位、面积或皮肤状态对暴露的影响",
    "11.5.9": "个体 PK 数据及异常个体",
    "11.5.10": "PK 结论",
    "11.6": "探索性疗效评价",
    "11.6.1": "探索性疗效分析",
    "11.6.2": "统计/分析内容",
    "11.6.2.1": "协变量的调整",
    "11.6.2.2": "脱落或缺失数据的处理",
    "11.6.2.3": "试验期中分析和数据监查",
    "11.6.2.4": "多中心研究",
    "11.6.2.5": "多重比较/多重性",
    "11.6.2.6": "敏感性分析",
    "11.6.2.7": "对照设计的专项分析",
    "11.6.2.8": "亚组的考察",
    "11.6.4": "药物剂量、药物浓度与效应关系",
    "11.6.7": "探索性疗效结论",
    "11.8": "药效动力学与生物标志物评价",
    "12.2.5": "特别关注不良事件（AESI）",
    "12.2.6": "导致永久停用研究药物的不良事件",
    "12.2.7": "导致临时停药或剂量调整的不良事件",
    "12.2.8": "导致研究退出的不良事件",
    "12.2.9": "用药部位不良反应",
    "12.5.1": "局部耐受性评分和用药部位反应",
    "12.5.2": "心电图评价",
    "12.5.3": "临床重要生命体征异常",
    "12.5.4": "妊娠报告",
    "12.5.5": "用药错误、过量和误用",
    "12.5.6": "免疫原性评价",
    "12.6.3": "DLT 定义与评价窗",
    "12.6.5": "DLT 结果",
    "12.6.7": "MTD/RP2D 结论与依据",
    "12.7": "安全性结论",
    "16.0": "附录目录及提交状态",
}


CONCLUSION_SECTION_ROLES: dict[str, str] = {
    "11.4.7": "efficacy_conclusion",
    "11.5.10": "pk_conclusion",
    "11.6.7": "exploratory_efficacy_conclusion",
}


def _lookup_path(payload: Mapping[str, Any], dotted: str) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def condition_fact_evidence(condition: str, facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the exact study-fact fields named by a reviewed predicate."""

    paths = sorted(
        set(
            re.findall(
                r"\b(?:objectives|design|population|data_domains|analyses)\.[a-zA-Z0-9_]+",
                condition,
            )
        )
    )
    return [{"path": path, "value": _lookup_path(facts, path)} for path in paths]


def _rule_passes(
    rule: Mapping[str, Any],
    facts: Mapping[str, Any],
    flags: Mapping[str, bool],
    render_policy: Mapping[str, Any],
) -> bool:
    if rule.get("manual_review_if_unsupported"):
        return False
    if "fact" in rule:
        path, expected = rule["fact"]
        return _lookup_path(facts, path) == expected
    if "nonempty" in rule:
        return bool(_lookup_path(facts, str(rule["nonempty"])))
    if "render_policy" in rule:
        key, expected = rule["render_policy"]
        return render_policy.get(key) == expected
    all_flags = tuple(rule.get("all", ()))
    any_flags = tuple(rule.get("any", ()))
    extra = tuple(rule.get("any_additional", ()))
    passed = all(flags.get(flag, False) for flag in all_flags)
    if any_flags:
        passed = passed and any(flags.get(flag, False) for flag in any_flags)
    if extra:
        passed = passed and any(flags.get(flag, False) for flag in extra)
    return passed


def _rule_basis(
    rule: Mapping[str, Any],
    facts: Mapping[str, Any],
    flags: Mapping[str, bool],
    capability_conditions: Mapping[str, str],
    render_policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if rule.get("manual_review_if_unsupported"):
        return [{"path": "manual_review", "value": "specific protocol fact required"}]
    if "fact" in rule:
        path, _expected = rule["fact"]
        return [{"path": path, "value": _lookup_path(facts, path)}]
    if "nonempty" in rule:
        path = str(rule["nonempty"])
        return [{"path": path, "value": _lookup_path(facts, path)}]
    if "render_policy" in rule:
        key, _expected = rule["render_policy"]
        return [{"path": f"render_policy.{key}", "value": render_policy.get(key)}]
    names = tuple(rule.get("all", ())) + tuple(rule.get("any", ())) + tuple(
        rule.get("any_additional", ())
    )
    evidence: list[dict[str, Any]] = []
    for flag in dict.fromkeys(names):
        condition = capability_conditions[flag]
        evidence.append(
            {
                "capability": flag,
                "condition": condition,
                "value": flags[flag],
                "facts": condition_fact_evidence(condition, facts),
            }
        )
    return evidence


def _descendant_or_same(candidate: str, parent: str) -> bool:
    return candidate == parent or candidate.startswith(parent + ".")


def _endpoint_groups(
    facts: Mapping[str, Any], flags: Mapping[str, bool]
) -> dict[str, list[Mapping[str, Any]]]:
    """Classify reviewed endpoints without inferring a missing hierarchy label."""

    objectives = facts["objectives"]
    groups: dict[str, list[Mapping[str, Any]]] = {
        "primary": list(objectives["primary_endpoints"]),
        "key_secondary": [],
        "secondary": [],
        "exploratory": [],
    }
    for endpoint in objectives["secondary_endpoints"]:
        hierarchy = endpoint.get("hierarchy")
        if hierarchy is None:
            hierarchy = (
                "exploratory"
                if endpoint.get("domain") == "efficacy"
                and flags.get("has_exploratory_efficacy")
                else "secondary"
            )
        if hierarchy not in {"key_secondary", "secondary", "exploratory"}:
            hierarchy = "secondary"
        groups[hierarchy].append(endpoint)
    return groups


def _human_omission_reason(
    section_id: str,
    facts: Mapping[str, Any],
    flags: Mapping[str, bool],
    render_policy: Mapping[str, Any],
) -> str:
    """Explain an omission in clinical-design language, never implementation syntax."""

    if section_id == "9.4.3":
        return "本研究未采用随机分配。"
    if section_id == "9.4.6":
        return "本研究采用开放性设计，未设置盲法维持或揭盲程序。"
    if section_id.startswith("11.4"):
        if section_id == "11.4.2.3" and not flags.get("has_interim_analysis"):
            return "方案未设置期中分析或独立数据监查安排。"
        if section_id == "11.4.2.5" and not flags.get("has_multiplicity_control"):
            return "统计分析未设置多重比较或多重性控制。"
        if section_id == "11.4.2.6" and not flags.get("has_estimand_framework"):
            return "方案和统计分析未采用估计目标框架。"
        if section_id == "11.4.2.7" and not flags.get("has_noninferiority"):
            return "本研究不是非劣效或等效性设计。"
        if section_id == "11.4.2.8" and not flags.get("has_subgroup_analysis"):
            return "统计分析未预设亚组分析。"
        if section_id == "11.4.2.4" and not flags.get("is_multicenter"):
            return "本研究不是多中心研究。"
        if section_id == "11.4.4":
            return "方案未设置暴露量或剂量与疗效反应关系分析。"
        if section_id == "11.4.5":
            return "方案未设置药物相互作用或药物与疾病相互作用评价。"
        if not flags.get("has_confirmatory_efficacy"):
            exploratory_relocations = {
                "11.4.1": "本研究未设定确证性疗效目的，疗效结果按探索性结构在 11.6.1 呈现。",
                "11.4.2.1": "本研究未设定确证性疗效目的，适用的协变量校正分析在 11.6.2.1 呈现。",
                "11.4.2.4": "本研究未设定确证性疗效目的，适用的多中心分析在 11.6.2.4 呈现。",
                "11.4.7": "本研究未设定确证性疗效目的，探索性疗效结论在 11.6.7 呈现。",
            }
            if section_id in exploratory_relocations:
                return exploratory_relocations[section_id]
            if section_id in {"11.4.2", "11.4.2.2"}:
                return (
                    "本研究未设定确证性疗效分析，本项未作为独立确证性分析节呈现；"
                    "适用方法须在探索性统计分析内容中依据 SAP 核对。"
                )
        return "本研究未设定确证性疗效目的，统计分析以探索性或描述性评价为主。"
    if section_id == "9.5.3":
        return "方案未设置疗效评价目的或疗效终点。"
    if section_id == "9.5.4":
        return "方案未设置药代动力学或临床药理评价。"
    if section_id == "11.1":
        return "分析数据集同时服务受试者处置、疗效、安全性和药代动力学评价，已统一移至 10.3 呈现。"
    if section_id.startswith("12") or section_id == "14.3":
        return "方案未设置安全性数据收集与分析。"
    if section_id == "14.2":
        return "方案未设置药代动力学或疗效结果展示。"
    return "已核对的研究目的、设计、数据域和分析计划均未设置本节对应内容。"


def _dynamic_render_title(
    section_id: str,
    title: str,
    facts: Mapping[str, Any],
    flags: Mapping[str, bool],
) -> tuple[str, list[str]]:
    if section_id == "9.5.3":
        return "主要终点", ["objectives.primary_endpoints"]
    if section_id == "9.5.4" and (
        flags.get("has_pk_nca")
        or flags.get("has_population_pk")
        or flags.get("has_topical_pk")
    ):
        return "PK 终点和采样", ["data_domains.pk_concentration"]
    if section_id == "12.6" and flags.get("has_dose_escalation"):
        return "剂量递增与总体耐受性评价", ["M-DOSE-ESC"]
    if section_id == "12.6" and flags.get("has_expansion_cohorts"):
        return "剂量扩展队列与总体耐受性评价", ["M-EXPANSION"]
    if section_id != "14.2":
        return title, []
    pk_modules = [
        module_id
        for flag, module_id in (
            ("has_pk_nca", "M-PK-NCA"),
            ("has_population_pk", "M-PK-POP"),
            ("has_topical_pk", "M-TOPICAL-PK"),
        )
        if flags.get(flag)
    ]
    efficacy = flags.get("has_confirmatory_efficacy") or flags.get(
        "has_exploratory_efficacy"
    )
    if pk_modules and efficacy:
        return "药代动力学和探索性疗效数据", pk_modules
    if pk_modules:
        return "药代动力学数据", pk_modules
    return title, []


def build_section_applicability_plan(
    facts: Mapping[str, Any],
    flags: Mapping[str, bool],
    active_modules: Sequence[Mapping[str, Any]],
    materialized_sections: Sequence[Mapping[str, Any]],
    capability_conditions: Mapping[str, str],
    *,
    structure_path: str | Path = DEFAULT_STRUCTURE,
    submission_region: str = "CDE",
) -> dict[str, Any]:
    """Classify every official/convention E3 node and all active extensions."""

    tree = load_chapter_tree(structure_path)
    nodes = flatten_sections(tree.select_layers(("ich-official", "industry-convention")))
    module_by_id = {str(item["id"]): item for item in active_modules}
    active_targets = [
        str(item["section"])
        for item in materialized_sections
        if re.fullmatch(r"\d+(?:\.\d+)*", str(item.get("section", "")))
    ]
    target_modules: dict[str, list[str]] = {}
    for node in nodes:
        target_modules[node.id] = sorted(
            {
                str(item["contributed_by"])
                for item in materialized_sections
                if _descendant_or_same(str(item["section"]), node.id)
                or _descendant_or_same(node.id, str(item["section"]))
            }
        )

    render_policy = {"submission_region": submission_region}
    classifications: list[dict[str, Any]] = []
    for node in nodes:
        rule = OPTIONAL_OFFICIAL_RULES.get(node.id)
        relocate_to: str | None = None
        drivers = target_modules[node.id]
        # A contribution retains its exact target and required ancestors.  A
        # contribution to a parent must not silently retain every optional
        # descendant under that parent.
        directly_driven = any(_descendant_or_same(target, node.id) for target in active_targets)
        appendix_status_managed = node.id == "16" or node.id.startswith("16.")
        if (
            rule is not None
            and "relocate_to" in rule
            and bool(_lookup_path(facts, str(rule["nonempty"])))
        ):
            classification = "relocated"
            retained = False
            target = str(rule["relocate_to"])
            relocate_to = target
            basis = [
                {
                    "path": str(rule["nonempty"]),
                    "value": [
                        item.get("id")
                        for item in _lookup_path(facts, str(rule["nonempty"]))
                        if isinstance(item, Mapping)
                    ],
                },
                {"path": "relocated_to", "value": target},
            ]
            reason = f"本节内容已按跨结果域结构调整移至 {target}。"
        elif appendix_status_managed:
            classification = "unconditional"
            retained = True
            basis = [{"path": "section_policy", "value": "complete appendix status register"}]
            reason = "附录项目始终保留，由 16.0 清单标注提交、备索或不适用状态。"
        elif rule is not None and rule.get("manual_review_if_unsupported") and not directly_driven:
            classification = "manual_review_required"
            retained = True
            basis = _rule_basis(rule, facts, flags, capability_conditions, render_policy)
            reason = "缺少可支持自动删节的具体方案事实；本节保留并进入人工确认队列。"
        elif directly_driven:
            classification = "module_driven"
            retained = True
            basis = [
                {
                    "module_id": module_id,
                    "activation_flag": module_by_id[module_id]["activation_flag"],
                    "condition": module_by_id[module_id]["condition"],
                    "value": True,
                    "facts": condition_fact_evidence(
                        str(module_by_id[module_id]["condition"]), facts
                    ),
                }
                for module_id in drivers
                if module_id in module_by_id
            ]
            reason = "激活模块贡献或其必需的父级结构。"
        elif rule is None:
            classification = "unconditional"
            retained = True
            basis = [{"path": "section_policy", "value": "cross-study core"}]
            reason = "跨研究无条件核心节。"
        else:
            retained = _rule_passes(rule, facts, flags, render_policy)
            classification = "module_driven" if retained else "conditional_omitted"
            basis = _rule_basis(
                rule, facts, flags, capability_conditions, render_policy
            )
            reason = (
                "研究事实满足本节适用条件。"
                if retained
                else "研究事实不满足本节适用条件。"
            )
        render_title, title_drivers = _dynamic_render_title(
            node.id, node.title("zh"), facts, flags
        )
        classifications.append(
            {
                "section_id": node.id,
                "title": node.title("zh"),
                "render_title": render_title,
                "title_drivers": title_drivers,
                "layer": node.layer,
                "classification": classification,
                "retained": retained,
                "reason": reason,
                "human_reason": (
                    _human_omission_reason(node.id, facts, flags, render_policy)
                    if not retained
                    else reason
                ),
                "disposition": (
                    "relocated" if classification == "relocated" else "not_applicable"
                    if not retained
                    else "retained"
                ),
                "relocate_to": relocate_to,
                "basis": basis,
                "driving_modules": drivers,
                "appendix_status_managed": appendix_status_managed,
                "semantic_role": (
                    None
                    if node.id == "12.6"
                    and (
                        flags.get("has_dose_escalation")
                        or flags.get("has_expansion_cohorts")
                    )
                    else "safety_conclusion"
                    if node.id == "12.6"
                    else CONCLUSION_SECTION_ROLES.get(node.id)
                ),
            }
        )

    # A retained descendant always retains its ancestor, even when the ancestor
    # has its own false optional predicate.  This is structural closure, not an
    # applicability override.
    retained_ids = {item["section_id"] for item in classifications if item["retained"]}
    for item in reversed(classifications):
        if item["retained"]:
            continue
        prefix = item["section_id"] + "."
        descendants = sorted(value for value in retained_ids if value.startswith(prefix))
        if descendants:
            item["classification"] = "module_driven"
            item["retained"] = True
            item["reason"] = "作为已激活子节的必需父级结构保留。"
            item["human_reason"] = item["reason"]
            item["basis"] = [{"path": "retained_descendants", "value": descendants}]
            retained_ids.add(item["section_id"])

    extensions: list[dict[str, Any]] = []
    extension_ids: set[str] = set()

    def add_extension(
        section_id: str,
        title: str,
        *,
        driving_modules: Sequence[str],
        semantic_role: str | None = None,
        basis: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if section_id in extension_ids:
            return
        extension_ids.add(section_id)
        extensions.append(
            {
                "section_id": section_id,
                "title": title,
                "classification": "module_driven" if driving_modules else "fact_driven",
                "retained": True,
                "driving_modules": list(driving_modules),
                "semantic_role": semantic_role,
                "basis": [dict(item) for item in basis],
            }
        )

    for section_id in sorted(
        {
            section_id
            for target in active_targets
            for section_id in (
                ".".join(target.split(".")[:index])
                for index in range(2, len(target.split(".")) + 1)
            )
            if section_id in FORMAL_EXTENSION_TITLES
        },
        key=lambda value: tuple(int(part) for part in value.split(".")),
    ):
        add_extension(
            section_id,
            FORMAL_EXTENSION_TITLES[section_id],
            driving_modules=sorted(
                {
                    str(item["contributed_by"])
                    for item in materialized_sections
                    if _descendant_or_same(str(item["section"]), section_id)
                    or _descendant_or_same(section_id, str(item["section"]))
                }
            ),
            semantic_role=CONCLUSION_SECTION_ROLES.get(section_id),
        )

    endpoint_groups = _endpoint_groups(facts, flags)
    all_endpoints = [item for values in endpoint_groups.values() for item in values]
    if all_endpoints or facts["data_domains"]["safety"]:
        add_extension(
            "9.5.1.1",
            FORMAL_EXTENSION_TITLES["9.5.1.1"],
            driving_modules=[],
            basis=[{"path": "objectives.primary_endpoints", "value": len(endpoint_groups["primary"])}],
        )
    endpoint_method_slots = (
        ("9.5.5", "key_secondary"),
        ("9.5.6", "secondary"),
        ("9.5.7", "exploratory"),
    )
    for section_id, group in endpoint_method_slots:
        if endpoint_groups[group]:
            add_extension(
                section_id,
                FORMAL_EXTENSION_TITLES[section_id],
                driving_modules=[],
                basis=[
                    {
                        "path": f"objectives.endpoints.{group}",
                        "value": [item["id"] for item in endpoint_groups[group]],
                    }
                ],
            )
    if facts["data_domains"]["safety"]:
        add_extension(
            "9.5.8",
            FORMAL_EXTENSION_TITLES["9.5.8"],
            driving_modules=["M-CORE"],
            basis=[{"path": "data_domains.safety", "value": True}],
        )
    if flags.get("has_local_tolerance"):
        add_extension(
            "9.5.9",
            FORMAL_EXTENSION_TITLES["9.5.9"],
            driving_modules=["M-LOCAL-TOLERANCE"],
            basis=[{"path": "data_domains.local_tolerance", "value": True}],
        )
    has_pd_or_pro = flags.get("has_pd_biomarker") or any(
        item.get("domain") in {"pd", "pro"} for item in all_endpoints
    )
    if has_pd_or_pro:
        add_extension(
            "9.5.10",
            FORMAL_EXTENSION_TITLES["9.5.10"],
            driving_modules=["M-PD-BIOMARKER"] if flags.get("has_pd_biomarker") else [],
            basis=[{"path": "data_domains.pd_biomarker", "value": facts["data_domains"]["pd_biomarker"]}],
        )

    analysis_sets = facts["analyses"]["analysis_sets"]
    if analysis_sets:
        add_extension(
            "10.3",
            FORMAL_EXTENSION_TITLES["10.3"],
            driving_modules=["M-CORE"],
            basis=[{"path": "analyses.analysis_sets", "value": [item["id"] for item in analysis_sets]}],
        )
        for index, analysis_set in enumerate(analysis_sets, start=1):
            add_extension(
                f"10.3.{index}",
                f"{analysis_set['id']}（{analysis_set['name']}）",
                driving_modules=["M-CORE"],
                basis=[{"path": f"analyses.analysis_sets[{index - 1}].id", "value": analysis_set["id"]}],
            )

    result_root = (
        "11.4.1"
        if flags.get("has_confirmatory_efficacy")
        else "11.6.1"
        if flags.get("has_exploratory_efficacy")
        else None
    )
    if result_root is not None:
        result_slots = (
            ("1", "primary", "主要终点结果"),
            ("2", "key_secondary", "关键次要终点结果"),
            ("3", "secondary", "其他次要终点结果"),
            ("4", "exploratory", "探索性终点结果"),
        )
        for suffix, group, title in result_slots:
            efficacy_endpoints = [
                item for item in endpoint_groups[group] if item.get("domain") == "efficacy"
            ]
            if efficacy_endpoints:
                add_extension(
                    f"{result_root}.{suffix}",
                    title,
                    driving_modules=[
                        "M-EFF-CONFIRM"
                        if flags.get("has_confirmatory_efficacy")
                        else "M-EFF-EXPLORE"
                    ],
                    basis=[
                        {
                            "path": f"objectives.endpoints.{group}",
                            "value": [item["id"] for item in efficacy_endpoints],
                        }
                    ],
                )

    if (
        flags.get("has_dose_escalation") or flags.get("has_expansion_cohorts")
    ) and facts["data_domains"]["safety"]:
        add_extension(
            "12.7",
            FORMAL_EXTENSION_TITLES["12.7"],
            driving_modules=[
                module_id
                for flag, module_id in (
                    ("has_dose_escalation", "M-DOSE-ESC"),
                    ("has_expansion_cohorts", "M-EXPANSION"),
                )
                if flags.get(flag)
            ],
            semantic_role="safety_conclusion",
            basis=[{"path": "design.escalation_scheme", "value": facts["design"]["escalation_scheme"]}],
        )

    # 16.0 is an unconditional implementation of E3 section 16's intro note.
    add_extension(
        "16.0",
        FORMAL_EXTENSION_TITLES["16.0"],
        driving_modules=["M-CORE"],
    )
    extensions.sort(key=lambda item: tuple(int(part) for part in item["section_id"].split(".")))

    omitted = [item for item in classifications if not item["retained"]]
    illegal = [
        item["section_id"]
        for item in omitted
        if any(
            basis.get("value") == "no supporting module"
            for basis in item.get("basis", [])
            if isinstance(basis, Mapping)
        )
    ]
    if illegal:
        raise ValueError(f"unsupported-module omission is forbidden: {illegal}")
    return {
        "schema_version": "1.2",
        "policy": {
            "model": "active_module_contributions_union_unconditional_core",
            "submission_region": submission_region,
            "phase_used_for_structure": False,
        },
        "official_section_count": len(classifications),
        "official_sections": classifications,
        "formal_extensions": extensions,
        "retained_official_section_ids": [
            item["section_id"] for item in classifications if item["retained"]
        ],
        "omitted_sections": omitted,
        "manual_review_queue": [
            item for item in classifications if item["classification"] == "manual_review_required"
        ],
        "conclusion_semantics": [
            {
                "section_id": item["section_id"],
                "semantic_role": item["semantic_role"],
            }
            for item in [*classifications, *extensions]
            if item.get("semantic_role")
        ],
    }
