"""Deterministic, datapack-conditioned CSR conclusion expansion.

The Agent may request one reviewed rule token, but it never sees cell values and
never selects the wording branch.  Expansion occurs only after section bindings
and the datapack pass their contracts.  Section 13 additionally proves that
every result key was already used in confirmed body text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .cross_references import CONCLUSION_PLACEHOLDER_RE
from .datapack import validate_datapack
from .resolve_placeholders import PLACEHOLDER_RE
from .writing_style import style_report


SAFETY_RULE_ID = "SAFETY_TOLERABILITY_V1"
PK_RULE_ID = "PK_CONCLUSION_V1"
EXPLORATORY_EFFICACY_RULE_ID = "EXPLORATORY_EFFICACY_CONCLUSION_V1"
OVERALL_RULE_ID = "OVERALL_CONCLUSION_V1"
SERIOUS_EVENT_ANALYSIS_RULE_ID = "SERIOUS_EVENT_ANALYSIS_V1"

SAFETY_REQUIRED_ROLES = (
    "teae_result",
    "sae_count",
    "death_count",
    "discontinuation_count",
    "grade3plus_count",
)
PK_REQUIRED_ROLES = (
    "blq_ratio",
    "quantifiable_timepoint_count",
    "cmax_calculable",
    "auc_calculable",
    "lloq",
    "noncalculable_reason",
    "sampling_coverage_complete",
)
EXPLORATORY_EFFICACY_REQUIRED_ROLES = (
    "primary_endpoint_result",
    "analysis_set",
    "prespecified",
    "hypothesis_test",
)
OVERALL_REQUIRED_ROLES = (
    "primary_endpoint_result",
    "pk_key_finding",
    "teae_result",
    "sae_count",
    "death_count",
    "discontinuation_count",
)
SERIOUS_EVENT_ANALYSIS_REQUIRED_ROLES = (
    "death_count",
    "sae_count",
    "important_ae_count",
    "discontinuation_count",
)


@dataclass(frozen=True)
class RuleContract:
    section: str
    variant: str
    roles: tuple[str, ...]


RULE_CONTRACTS: Mapping[str, RuleContract] = {
    SAFETY_RULE_ID: RuleContract(
        "12.2", "adverse_events_conditioned", SAFETY_REQUIRED_ROLES
    ),
    PK_RULE_ID: RuleContract("11.5.10", "pk_conclusion_conditioned", PK_REQUIRED_ROLES),
    EXPLORATORY_EFFICACY_RULE_ID: RuleContract(
        "11.6.7",
        "exploratory_efficacy_conclusion_conditioned",
        EXPLORATORY_EFFICACY_REQUIRED_ROLES,
    ),
    OVERALL_RULE_ID: RuleContract(
        "13", "overall_conclusion_conditioned", OVERALL_REQUIRED_ROLES
    ),
    SERIOUS_EVENT_ANALYSIS_RULE_ID: RuleContract(
        "12.3.3",
        "serious_event_analysis_conditioned",
        SERIOUS_EVENT_ANALYSIS_REQUIRED_ROLES,
    ),
}

# One reviewed source drives both conditional-token validation and the semantic
# medical-review boundary.  The two legacy conclusion sections do not expand a
# conditional token, but remain part of the same section policy.
EXPECTED_CONCLUSION_RULES: Mapping[tuple[str, str], str] = {
    (contract.section, contract.variant): rule_id
    for rule_id, contract in RULE_CONTRACTS.items()
}
CONCLUSION_SECTION_ROOTS = frozenset(
    {"11.4.7", "12.7"} | {contract.section for contract in RULE_CONTRACTS.values()}
)


class ConditionalConclusionError(ValueError):
    """The conclusion request is malformed or escapes its reviewed scope."""


@dataclass(frozen=True)
class ConditionalConclusionResult:
    status: str
    resolved_text: str
    report: dict[str, Any]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(encoded)


def _binding_index(
    metadata: Mapping[str, Any], rule_id: str
) -> tuple[RuleContract, dict[str, str]]:
    contract = RULE_CONTRACTS[rule_id]
    if (
        metadata.get("section") != contract.section
        or metadata.get("variant") != contract.variant
    ):
        raise ConditionalConclusionError(
            f"{rule_id} is allowed only for section {contract.section}/{contract.variant}"
        )
    bindings = metadata.get("binding_manifest")
    if not isinstance(bindings, list):
        raise ConditionalConclusionError("section metadata binding_manifest is missing")
    result: dict[str, str] = {}
    for item in bindings:
        if not isinstance(item, Mapping):
            raise ConditionalConclusionError("section metadata contains a malformed binding")
        role = item.get("role")
        key = item.get("key")
        if not isinstance(role, str) or not isinstance(key, str) or role in result:
            raise ConditionalConclusionError("section metadata contains an invalid binding role")
        result[role] = key

    if rule_id == OVERALL_RULE_ID:
        confirmed = metadata.get("confirmed_body_binding_manifest")
        expected = {
            "schema_version",
            "generation_mode",
            "keys",
            "sources",
            "manifest_sha256",
        }
        if not isinstance(confirmed, Mapping) or set(confirmed) != expected:
            raise ConditionalConclusionError(
                "OVERALL_CONCLUSION_V1 needs a confirmed-body binding manifest"
            )
        confirmed_keys = confirmed.get("keys")
        confirmed_sources = confirmed.get("sources")
        if not isinstance(confirmed_keys, list) or not all(
            isinstance(item, str) for item in confirmed_keys
        ) or not isinstance(confirmed_sources, list):
            raise ConditionalConclusionError("confirmed-body binding keys are malformed")
        body = {
            key: confirmed[key]
            for key in ("schema_version", "generation_mode", "keys", "sources")
        }
        if (
            confirmed.get("schema_version") != "1.0"
            or confirmed.get("generation_mode") != "confirmed_body_only"
            or confirmed.get("manifest_sha256") != _hash_json(body)
        ):
            raise ConditionalConclusionError("confirmed-body binding manifest hash mismatch")
        source_keys = [
            item.get("key") if isinstance(item, Mapping) else None
            for item in confirmed_sources
        ]
        if list(result.values()) != confirmed_keys:
            raise ConditionalConclusionError(
                "section 13 attempted to introduce a result absent from confirmed body text"
            )
        if source_keys != confirmed_keys:
            raise ConditionalConclusionError(
                "confirmed-body provenance does not cover every section 13 result"
            )
    return contract, result


def _display_present(cell: Mapping[str, Any] | None) -> bool:
    if cell is None:
        return False
    display = cell.get("v")
    return isinstance(display, str) and bool(display.strip()) and "⟦缺失:" not in display


def _display_value(cell: Mapping[str, Any] | None) -> str | None:
    if not _display_present(cell):
        return None
    return str(cell["v"]).strip()


def _numeric_value(cell: Mapping[str, Any] | None) -> float | None:
    if cell is None:
        return None
    machine_value = cell.get("numeric_value")
    if isinstance(machine_value, (int, float)) and not isinstance(machine_value, bool):
        return float(machine_value)
    display = cell.get("v")
    if not isinstance(display, str) or "⟦缺失:" in display:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\b", display)
    return float(match.group(1)) if match is not None else None


def _boolean_value(cell: Mapping[str, Any] | None) -> bool | None:
    if cell is None:
        return None
    for field in ("boolean_value", "machine_value"):
        value = cell.get(field)
        if isinstance(value, bool):
            return value
    return None


def _role_value(
    rule_id: str, role: str, cell: Mapping[str, Any] | None
) -> tuple[bool, Any]:
    if rule_id == SAFETY_RULE_ID:
        value = _display_present(cell) if role == "teae_result" else _numeric_value(cell)
        return (value is True, value) if role == "teae_result" else (value is not None, value)
    if rule_id == PK_RULE_ID:
        if role in {"blq_ratio", "quantifiable_timepoint_count"}:
            value = _numeric_value(cell)
            return value is not None, value
        if role in {"cmax_calculable", "auc_calculable", "sampling_coverage_complete"}:
            value = _boolean_value(cell)
            return value is not None, value
        value = _display_value(cell)
        return value is not None, value
    if rule_id == SERIOUS_EVENT_ANALYSIS_RULE_ID:
        value = _numeric_value(cell)
        return value is not None, value
    if rule_id == EXPLORATORY_EFFICACY_RULE_ID:
        if role in {"prespecified", "hypothesis_test"}:
            value = _boolean_value(cell)
            return value is not None, value
        value = _display_value(cell)
        return value is not None, value
    if rule_id == OVERALL_RULE_ID:
        if role in {"sae_count", "death_count", "discontinuation_count"}:
            value = _numeric_value(cell)
            return value is not None, value
        value = _display_value(cell)
        return value is not None, value
    raise ConditionalConclusionError(f"Unsupported conditional conclusion rule: {rule_id}")


def _resolve_safety(values: Mapping[str, Any], missing: list[str]) -> tuple[str, str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if missing:
        warnings.append(
            {
                "code": "W-CONCLUSION-CRITICAL-BINDING-MISSING",
                "message": "Conclusion paragraph lacks one or more v1.3 critical safety cells",
                "missing_roles": missing,
            }
        )
        return (
            "critical_binding_missing",
            "本节总体评价待严重不良事件、死亡、导致停药及重度事件数据到位后补充。数据到位前不作总体耐受性判断。",
            warnings,
        )
    adverse_roles = [
        role
        for role in ("sae_count", "death_count", "discontinuation_count", "grade3plus_count")
        if float(values[role]) > 0
    ]
    if adverse_roles:
        return (
            "important_events_present",
            "关键事件指标中存在非零结果。总体耐受性需结合事件性质、严重程度、转归及剂量关系评价，不作“总体耐受性良好”的概括。",
            warnings,
        )
    return (
        "no_critical_events",
        "未发生严重不良事件、死亡、导致永久停用研究治疗的不良事件或重度事件。结合总体事件发生情况，研究治疗总体耐受性良好。",
        warnings,
    )


def _resolve_pk(values: Mapping[str, Any], missing: list[str]) -> tuple[str, str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if missing:
        warnings.append(
            {
                "code": "W-PK-CONCLUSION-BINDING-MISSING",
                "message": "PK conclusion lacks a critical quantifiability or method binding",
                "missing_roles": missing,
            }
        )
        return (
            "critical_binding_missing",
            "本节 PK 结论待 BLQ 比例、可定量时点、参数计算情况、分析方法 LLOQ、不能计算的原因及采样覆盖结果到位后补充。当前不作系统暴露判断。",
            warnings,
        )
    limited = (
        float(values["blq_ratio"]) >= 50.0
        or float(values["quantifiable_timepoint_count"]) <= 0.0
        or values["cmax_calculable"] is not True
        or values["auc_calculable"] is not True
        or values["sampling_coverage_complete"] is not True
    )
    if limited:
        return (
            "quantifiability_limited",
            "定量数据或参数计算受到限制。解释结果时应结合 BLQ 比例、可定量时点、LLOQ、不能计算的原因及采样覆盖，不得据此表述为“系统暴露低”。",
            warnings,
        )
    return (
        "exposure_characterizable",
        "现有数据可用于描述观察到的系统暴露特征。其临床意义仍需医学和临床药理负责人确认。",
        warnings,
    )


def _resolve_exploratory_efficacy(
    values: Mapping[str, Any], missing: list[str]
) -> tuple[str, str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if missing:
        warnings.append(
            {
                "code": "W-EXPLORATORY-EFFICACY-CONCLUSION-BINDING-MISSING",
                "message": "Exploratory efficacy conclusion lacks a critical design or result binding",
                "missing_roles": missing,
            }
        )
        return (
            "critical_binding_missing",
            "本节探索性疗效结论待主要终点结果、分析集、分析预设情况及假设检验信息到位后补充。当前不作疗效判断。",
            warnings,
        )
    if values["prespecified"] is not True:
        return (
            "not_prespecified",
            "上述结果并非预设的确证性评价，作为补充探索性结果呈现，不用于确证性结论。",
            warnings,
        )
    if values["hypothesis_test"] is True:
        return (
            "prespecified_hypothesis_test",
            "虽然分析包含假设检验，相关结果仍仅作探索性解释，不用于确证性结论。",
            warnings,
        )
    return (
        "prespecified_descriptive",
        "相关结果仅作探索性解释，不用于确证性结论。",
        warnings,
    )


def _resolve_overall(values: Mapping[str, Any], missing: list[str]) -> tuple[str, str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if missing:
        warnings.append(
            {
                "code": "W-OVERALL-CONCLUSION-BINDING-MISSING",
                "message": "Overall conclusion lacks a confirmed efficacy, PK, or safety result",
                "missing_roles": missing,
            }
        )
        return (
            "critical_binding_missing",
            "本研究总体结论待主要终点、关键安全性事件和药代动力学结果完成确认后补充。当前不作总体获益风险判断。",
            warnings,
        )
    important_events = any(
        float(values[role]) > 0
        for role in ("sae_count", "death_count", "discontinuation_count")
    )
    if important_events:
        return (
            "important_safety_events_present",
            "总体讨论应先说明关键安全性事件的性质、严重程度和转归，不作有利的获益风险表述。最终判断待医学负责人确认。",
            warnings,
        )
    return (
        "confirmed_body_complete_no_critical_events",
        "主要终点、药代动力学和安全性结果可用于研究目标层面的综合讨论。总体获益风险判断仍待医学负责人确认。",
        warnings,
    )


def _resolve_serious_event_analysis(
    values: Mapping[str, Any], missing: list[str]
) -> tuple[str, str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if missing:
        warnings.append(
            {
                "code": "W-SERIOUS-EVENT-ANALYSIS-BINDING-MISSING",
                "message": "Serious-event analysis lacks one or more confirmed event counts",
                "missing_roles": missing,
            }
        )
        return (
            "critical_binding_missing",
            "本节关键事件分析待死亡、严重不良事件、其他重要不良事件及导致停药事件的完整数据到位后补充。当前不作事件模式或临床意义判断。",
            warnings,
        )
    if any(float(values[role]) > 0 for role in SERIOUS_EVENT_ANALYSIS_REQUIRED_ROLES):
        return (
            "important_events_present",
            "关键事件指标中存在非零结果。事件分布、转归及临床意义须结合个例资料和暴露情况，由医学负责人确认。",
            warnings,
        )
    return (
        "no_listed_critical_events",
        "上述结果仅反映当前研究和数据截止范围，不能据此排除未观察到的安全性风险。医学综合判断仍待医学负责人确认。",
        warnings,
    )


def _placeholder(bindings: Mapping[str, str], role: str) -> str:
    return "{{" + bindings[role] + "}}"


def _reference_sentence(metadata: Mapping[str, Any]) -> str:
    raw_manifest = metadata.get("reference_manifest", [])
    if not isinstance(raw_manifest, list):
        return ""
    entities = [
        item.get("entity_id")
        for item in raw_manifest
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    ]
    if not entities:
        return ""
    placeholders = [f"{{{{@ref:{entity}}}}}" for entity in entities]
    if len(placeholders) == 1:
        joined = placeholders[0]
    else:
        joined = "、".join(placeholders[:-1]) + "和" + placeholders[-1]
    return f"详见{joined}。"


def _rendering_record(role: str, key: str, mode: str) -> dict[str, str]:
    return {"role": role, "key": key, "rendering_mode": mode}


def _pk_sentence_plan(
    branch: str,
    wording: str,
    values: Mapping[str, Any],
    missing: list[str],
    bindings: Mapping[str, str],
    metadata: Mapping[str, Any],
    cell_types: Mapping[str, str],
) -> tuple[str, list[dict[str, str]]]:
    del cell_types
    sentences: list[str] = []
    renderings: list[dict[str, str]] = []

    if "blq_ratio" in bindings and "quantifiable_timepoint_count" in bindings:
        sentences.append(
            f"BLQ 比例为 {_placeholder(bindings, 'blq_ratio')}，"
            f"{_placeholder(bindings, 'quantifiable_timepoint_count')} 个采样时点获得可定量结果。"
        )
        renderings.extend(
            [
                _rendering_record("blq_ratio", bindings["blq_ratio"], "value_placeholder"),
                _rendering_record(
                    "quantifiable_timepoint_count",
                    bindings["quantifiable_timepoint_count"],
                    "value_placeholder",
                ),
            ]
        )
    else:
        for role, label in (
            ("blq_ratio", "BLQ 比例"),
            ("quantifiable_timepoint_count", "可定量时点数"),
        ):
            if role in bindings:
                sentences.append(f"{label}为 {_placeholder(bindings, role)}。")
                renderings.append(_rendering_record(role, bindings[role], "value_placeholder"))

    cmax_present = "cmax_calculable" in bindings
    auc_present = "auc_calculable" in bindings
    if cmax_present and auc_present:
        cmax = values.get("cmax_calculable")
        auc = values.get("auc_calculable")
        if cmax is True and auc is True:
            sentences.append("Cmax 与 AUC 均可计算。")
        elif cmax is False and auc is False:
            sentences.append("Cmax 与 AUC 均无法计算。")
        elif cmax is True:
            sentences.append("Cmax 可计算，但 AUC 无法计算。")
        else:
            sentences.append("AUC 可计算，但 Cmax 无法计算。")
        renderings.extend(
            [
                _rendering_record("cmax_calculable", bindings["cmax_calculable"], "conditioned_phrase"),
                _rendering_record("auc_calculable", bindings["auc_calculable"], "conditioned_phrase"),
            ]
        )
    else:
        for role, label in (
            ("cmax_calculable", "Cmax"),
            ("auc_calculable", "AUC"),
        ):
            if role in bindings:
                state = "可计算" if values.get(role) is True else "无法计算"
                sentences.append(f"{label} {state}。")
                renderings.append(_rendering_record(role, bindings[role], "conditioned_phrase"))

    if "lloq" in bindings:
        sentences.append(f"分析方法定量下限为 {_placeholder(bindings, 'lloq')}。")
        renderings.append(_rendering_record("lloq", bindings["lloq"], "value_placeholder"))

    if "noncalculable_reason" in bindings:
        reason = str(values.get("noncalculable_reason") or "").strip().lower()
        if reason in {"不适用", "无", "n/a", "na", "not applicable"}:
            sentences.append("本情形不涉及参数不能计算的原因。")
            mode = "conditioned_not_applicable_phrase"
        else:
            sentences.append(
                f"参数不能计算的原因为 {_placeholder(bindings, 'noncalculable_reason')}。"
            )
            mode = "value_placeholder"
        renderings.append(
            _rendering_record(
                "noncalculable_reason", bindings["noncalculable_reason"], mode
            )
        )

    if "sampling_coverage_complete" in bindings:
        complete = values.get("sampling_coverage_complete") is True
        sentences.append("采样覆盖完整。" if complete else "采样覆盖不完整。")
        renderings.append(
            _rendering_record(
                "sampling_coverage_complete",
                bindings["sampling_coverage_complete"],
                "conditioned_phrase",
            )
        )

    if missing:
        # The branch wording names the absent facts without fabricating values.
        renderings.extend(
            _rendering_record(role, "", "missing_no_value_rendered") for role in missing
        )
    reference = _reference_sentence(metadata)
    if reference:
        sentences.append(reference)
    sentences.append(wording)
    return "".join(sentences), renderings


def _efficacy_sentence_plan(
    branch: str,
    wording: str,
    values: Mapping[str, Any],
    missing: list[str],
    bindings: Mapping[str, str],
    metadata: Mapping[str, Any],
    cell_types: Mapping[str, str],
) -> tuple[str, list[dict[str, str]]]:
    del branch, cell_types
    sentences: list[str] = []
    renderings: list[dict[str, str]] = []
    if "analysis_set" in bindings and "primary_endpoint_result" in bindings:
        sentences.append(
            f"在 {_placeholder(bindings, 'analysis_set')} 中，主要终点结果为 "
            f"{_placeholder(bindings, 'primary_endpoint_result')}。"
        )
        renderings.extend(
            [
                _rendering_record("analysis_set", bindings["analysis_set"], "value_placeholder"),
                _rendering_record(
                    "primary_endpoint_result",
                    bindings["primary_endpoint_result"],
                    "value_placeholder",
                ),
            ]
        )
    else:
        for role, label in (
            ("analysis_set", "分析集"),
            ("primary_endpoint_result", "主要终点结果"),
        ):
            if role in bindings:
                sentences.append(f"{label}为 {_placeholder(bindings, role)}。")
                renderings.append(_rendering_record(role, bindings[role], "value_placeholder"))

    if "prespecified" in bindings:
        sentences.append(
            "该分析为预设分析。"
            if values.get("prespecified") is True
            else "该分析并非预设分析。"
        )
        renderings.append(
            _rendering_record("prespecified", bindings["prespecified"], "conditioned_phrase")
        )
    if "hypothesis_test" in bindings:
        sentences.append(
            "分析中实施了假设检验。"
            if values.get("hypothesis_test") is True
            else "分析中未实施假设检验。"
        )
        renderings.append(
            _rendering_record("hypothesis_test", bindings["hypothesis_test"], "conditioned_phrase")
        )
    if missing:
        renderings.extend(
            _rendering_record(role, "", "missing_no_value_rendered") for role in missing
        )
    reference = _reference_sentence(metadata)
    if reference:
        sentences.append(reference)
    sentences.append(wording)
    return "".join(sentences), renderings


def _event_count_sentences(
    values: Mapping[str, Any],
    bindings: Mapping[str, str],
    cell_types: Mapping[str, str],
) -> tuple[list[str], list[dict[str, str]]]:
    labels = {
        "sae_count": "严重不良事件",
        "death_count": "死亡",
        "discontinuation_count": "导致停用研究治疗的不良事件",
    }
    zero_labels: list[str] = []
    sentences: list[str] = []
    renderings: list[dict[str, str]] = []
    for role, label in labels.items():
        if role not in bindings:
            continue
        if float(values.get(role, -1.0)) == 0.0:
            zero_labels.append(label)
            mode = "conditioned_zero_event_phrase"
        else:
            suffix = "。" if cell_types.get(role) == "n_pct" else " 例。"
            sentences.append(f"发生{label}者为 {_placeholder(bindings, role)}{suffix}")
            mode = "value_placeholder"
        renderings.append(_rendering_record(role, bindings[role], mode))
    if zero_labels:
        if len(zero_labels) == 1:
            joined = zero_labels[0]
        else:
            joined = "、".join(zero_labels[:-1]) + "或" + zero_labels[-1]
        sentences.insert(0, f"未发生{joined}。")
    return sentences, renderings


def _overall_sentence_plan(
    branch: str,
    wording: str,
    values: Mapping[str, Any],
    missing: list[str],
    bindings: Mapping[str, str],
    metadata: Mapping[str, Any],
    cell_types: Mapping[str, str],
) -> tuple[str, list[dict[str, str]]]:
    del branch
    sentences: list[str] = []
    renderings: list[dict[str, str]] = []
    if "primary_endpoint_result" in bindings:
        sentences.append(
            f"主要终点结果为 {_placeholder(bindings, 'primary_endpoint_result')}。"
        )
        renderings.append(
            _rendering_record(
                "primary_endpoint_result",
                bindings["primary_endpoint_result"],
                "value_placeholder",
            )
        )
    if "pk_key_finding" in bindings:
        pk_value = str(values.get("pk_key_finding") or "").strip()
        if pk_value == "可计算":
            sentences.append("主要 PK 参数可以计算。")
            mode = "conditioned_phrase"
        elif pk_value == "不可计算":
            sentences.append("主要 PK 参数无法计算。")
            mode = "conditioned_phrase"
        else:
            sentences.append(
                f"PK 关键结果为 {_placeholder(bindings, 'pk_key_finding')}。"
            )
            mode = "value_placeholder"
        renderings.append(
            _rendering_record("pk_key_finding", bindings["pk_key_finding"], mode)
        )
    if "teae_result" in bindings:
        sentences.append(f"TEAE 发生情况为 {_placeholder(bindings, 'teae_result')}。")
        renderings.append(
            _rendering_record("teae_result", bindings["teae_result"], "value_placeholder")
        )
    event_sentences, event_renderings = _event_count_sentences(
        values, bindings, cell_types
    )
    sentences.extend(event_sentences)
    renderings.extend(event_renderings)
    if missing:
        renderings.extend(
            _rendering_record(role, "", "missing_no_value_rendered") for role in missing
        )
    reference = _reference_sentence(metadata)
    if reference:
        sentences.append(reference)
    sentences.append(wording)
    return "".join(sentences), renderings


def _serious_event_sentence_plan(
    branch: str,
    wording: str,
    values: Mapping[str, Any],
    missing: list[str],
    bindings: Mapping[str, str],
    metadata: Mapping[str, Any],
    cell_types: Mapping[str, str],
) -> tuple[str, list[dict[str, str]]]:
    del branch
    labels = {
        "death_count": "死亡",
        "sae_count": "严重不良事件",
        "important_ae_count": "其他重要不良事件",
        "discontinuation_count": "导致永久停用研究治疗的不良事件",
    }
    zero_labels: list[str] = []
    sentences: list[str] = []
    renderings: list[dict[str, str]] = []
    for role in SERIOUS_EVENT_ANALYSIS_REQUIRED_ROLES:
        if role not in bindings:
            continue
        if float(values.get(role, -1.0)) == 0.0:
            zero_labels.append(labels[role])
            mode = "conditioned_zero_event_phrase"
        else:
            suffix = "。" if cell_types.get(role) == "n_pct" else " 例。"
            sentences.append(
                f"发生{labels[role]}者为 {_placeholder(bindings, role)}{suffix}"
            )
            mode = "value_placeholder"
        renderings.append(_rendering_record(role, bindings[role], mode))
    if zero_labels:
        joined = (
            zero_labels[0]
            if len(zero_labels) == 1
            else "、".join(zero_labels[:-1]) + "或" + zero_labels[-1]
        )
        sentences.insert(0, f"未发生{joined}。")
    if missing:
        renderings.extend(
            _rendering_record(role, "", "missing_no_value_rendered") for role in missing
        )
    reference = _reference_sentence(metadata)
    if reference:
        sentences.append(reference)
    sentences.append(wording)
    return "".join(sentences), renderings


SENTENCE_PLAN_RENDERERS = {
    PK_RULE_ID: _pk_sentence_plan,
    EXPLORATORY_EFFICACY_RULE_ID: _efficacy_sentence_plan,
    OVERALL_RULE_ID: _overall_sentence_plan,
    SERIOUS_EVENT_ANALYSIS_RULE_ID: _serious_event_sentence_plan,
}


RULE_RESOLVERS = {
    SAFETY_RULE_ID: _resolve_safety,
    PK_RULE_ID: _resolve_pk,
    EXPLORATORY_EFFICACY_RULE_ID: _resolve_exploratory_efficacy,
    OVERALL_RULE_ID: _resolve_overall,
    SERIOUS_EVENT_ANALYSIS_RULE_ID: _resolve_serious_event_analysis,
}


def resolve_conditional_conclusions(
    draft: str,
    datapack: dict[str, Any],
    section_metadata: Mapping[str, Any],
) -> ConditionalConclusionResult:
    """Expand one reviewed conclusion token before the ordinary three guards."""

    validate_datapack(datapack)
    matches = list(CONCLUSION_PLACEHOLDER_RE.finditer(draft))
    if not matches:
        return ConditionalConclusionResult(
            status="not_applicable",
            resolved_text=draft,
            report={
                "schema_version": "1.1",
                "status": "not_applicable",
                "draft_sha256": _hash_text(draft),
                "datapack_sha256": _hash_json(datapack),
                "rules": [],
                "warnings": [],
            },
        )
    if len(matches) != 1:
        raise ConditionalConclusionError("duplicated conditional conclusion token")
    rule_id = matches[0].group("rule")
    if rule_id not in RULE_CONTRACTS:
        raise ConditionalConclusionError(f"unsupported conditional conclusion token: {rule_id}")

    contract, bindings = _binding_index(section_metadata, rule_id)
    cells = datapack["cells"]
    role_records: list[dict[str, Any]] = []
    missing: list[str] = []
    values: dict[str, Any] = {}
    cell_types: dict[str, str] = {}
    for role in contract.roles:
        key = bindings.get(role)
        cell = cells.get(key) if key is not None else None
        machine_readable, value = _role_value(
            rule_id, role, cell if isinstance(cell, Mapping) else None
        )
        if not machine_readable:
            missing.append(role)
        else:
            values[role] = value
        if isinstance(cell, Mapping) and isinstance(cell.get("type"), str):
            cell_types[role] = str(cell["type"])
        role_records.append(
            {
                "role": role,
                "key": key,
                "cell_present": isinstance(cell, Mapping),
                "machine_readable": machine_readable,
                "value_sha256": _hash_text(str(cell.get("v"))) if isinstance(cell, Mapping) else None,
            }
        )

    token_start = matches[0].start()
    observed_value_keys = [match.group(1) for match in PLACEHOLDER_RE.finditer(draft)]
    expected_value_keys = list(bindings.values())
    if not observed_value_keys and rule_id in SENTENCE_PLAN_RENDERERS:
        render_mode = "sentence_driven"
    elif observed_value_keys == expected_value_keys and all(
        draft.find(f"{{{{{key}}}}}") < token_start for key in expected_value_keys
    ):
        render_mode = "legacy_role_driven"
    else:
        raise ConditionalConclusionError(
            "conditional conclusion values must either be fully sentence-driven or "
            "stated once in reviewed role order before the conclusion token"
        )

    branch, wording, warnings = RULE_RESOLVERS[rule_id](values, missing)
    role_renderings: list[dict[str, str]] = []
    if render_mode == "sentence_driven":
        sentence_plan, role_renderings = SENTENCE_PLAN_RENDERERS[rule_id](
            branch,
            wording,
            values,
            missing,
            bindings,
            section_metadata,
            cell_types,
        )
        rendering_index = {item["role"]: item for item in role_renderings}
        if len(rendering_index) != len(role_renderings) or set(rendering_index) != set(
            contract.roles
        ):
            raise ConditionalConclusionError(
                "sentence-driven conclusion did not account for every reviewed role"
            )
        role_renderings = [rendering_index[role] for role in contract.roles]
        replacement = sentence_plan
    else:
        replacement = wording
        role_renderings = [
            _rendering_record(role, bindings.get(role, ""), "legacy_role_statement")
            for role in contract.roles
        ]
    resolved = CONCLUSION_PLACEHOLDER_RE.sub(replacement, draft)
    prose_style = style_report(resolved)
    if prose_style["blocked"]:
        raise ConditionalConclusionError(
            "conditioned sentence plan violated the CSR writing-style gate"
        )
    # Style warnings are reported independently and do not change the semantic
    # branch status used by the M13 conclusion contract.
    status = "pass_with_warning" if warnings else "pass"
    report = {
        "schema_version": "1.1",
        "status": status,
        "draft_sha256": _hash_text(draft),
        "resolved_sha256": _hash_text(resolved),
        "datapack_sha256": _hash_json(datapack),
        "rules": [
            {
                "rule_id": rule_id,
                "branch": branch,
                "critical_roles": role_records,
                "missing_roles": missing,
                "render_mode": render_mode,
                "role_renderings": role_renderings,
                "all_roles_rendered": len(role_renderings) == len(contract.roles),
                "derived_zero_event_roles": [
                    item["role"]
                    for item in role_renderings
                    if item["rendering_mode"] == "conditioned_zero_event_phrase"
                ],
                "numeric_values_not_logged": True,
                "medical_review_marker_required": True,
                "confirmed_body_only": rule_id == OVERALL_RULE_ID,
            }
        ],
        "warnings": warnings,
        "style_qc": prose_style,
    }
    return ConditionalConclusionResult(status, resolved, report)
