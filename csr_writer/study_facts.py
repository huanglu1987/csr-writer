"""Frozen study-facts contract and human-review fact card."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigurationError
from .io_utils import atomic_write_text, load_json_unique


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_PATH = (
    PROJECT_ROOT / "skills" / "csr-writer" / "references" / "study-facts.schema.json"
)


class StudyFactsValidationError(ConfigurationError):
    """The study facts do not conform to the frozen M3 contract."""


def _json_type_matches(value: Any, expected: str) -> bool:
    checks = {
        "null": lambda item: item is None,
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
    }
    if expected not in checks:
        raise StudyFactsValidationError(f"Unsupported schema type: {expected!r}")
    return checks[expected](value)


def _resolve_local_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise StudyFactsValidationError(f"Only local schema references are supported: {reference}")
    node: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise StudyFactsValidationError(f"Broken schema reference: {reference}")
        node = node[part]
    if not isinstance(node, dict):
        raise StudyFactsValidationError(f"Schema reference is not an object: {reference}")
    return node


def _validate_node(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> None:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str):
            raise StudyFactsValidationError(f"Invalid $ref at {path}")
        _validate_node(value, _resolve_local_ref(root, reference), root, path)
        return

    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise StudyFactsValidationError(f"Invalid anyOf in schema at {path}")
        failures: list[str] = []
        for alternative in alternatives:
            try:
                _validate_node(value, alternative, root, path)
                break
            except StudyFactsValidationError as exc:
                failures.append(str(exc))
        else:
            raise StudyFactsValidationError(
                f"{path} does not match any permitted shape: {'; '.join(failures)}"
            )
        return

    if "const" in schema and value != schema["const"]:
        raise StudyFactsValidationError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise StudyFactsValidationError(f"{path} must be one of {schema['enum']!r}; got {value!r}")

    expected_types = schema.get("type")
    if expected_types is not None:
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not isinstance(expected_types, list) or not all(
            isinstance(item, str) for item in expected_types
        ):
            raise StudyFactsValidationError(f"Invalid type declaration in schema at {path}")
        if not any(_json_type_matches(value, item) for item in expected_types):
            raise StudyFactsValidationError(
                f"{path} must have type {' or '.join(expected_types)}; got {type(value).__name__}"
            )

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise StudyFactsValidationError(f"Invalid object schema at {path}")
        missing = [key for key in required if key not in value]
        if missing:
            raise StudyFactsValidationError(f"{path} is missing required fields: {missing!r}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise StudyFactsValidationError(f"{path} contains unknown fields: {unknown!r}")
        for key, child in value.items():
            if key in properties:
                _validate_node(child, properties[key], root, f"{path}.{key}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if minimum_items is not None and len(value) < minimum_items:
            raise StudyFactsValidationError(f"{path} needs at least {minimum_items} items")
        if schema.get("uniqueItems"):
            canonical = [
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(canonical) != len(set(canonical)):
                raise StudyFactsValidationError(f"{path} must not contain duplicate items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, child in enumerate(value):
                _validate_node(child, item_schema, root, f"{path}[{index}]")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if minimum_length is not None and len(value) < minimum_length:
            raise StudyFactsValidationError(f"{path} must contain at least {minimum_length} characters")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise StudyFactsValidationError(f"{path} must match {pattern!r}; got {value!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise StudyFactsValidationError(f"{path} must be a finite JSON number")
        if "minimum" in schema and value < schema["minimum"]:
            raise StudyFactsValidationError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise StudyFactsValidationError(f"{path} must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise StudyFactsValidationError(f"{path} must be > {schema['exclusiveMinimum']}")


def validate_study_facts(
    facts: Any,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> dict[str, Any]:
    """Validate and return ``facts`` against the frozen contract.

    Validation is deliberately local and deterministic; it implements the JSON Schema
    keywords used by the checked-in contract, so runtime correctness does not depend on
    an undeclared validator package.
    """
    schema = load_json_unique(schema_path)
    if not isinstance(schema, dict):
        raise StudyFactsValidationError("Study-facts schema root must be an object")
    _validate_node(facts, schema, schema, "study_facts")
    if not isinstance(facts, dict):
        raise StudyFactsValidationError("study_facts must be an object")
    return facts


def load_study_facts(
    path: str | Path,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> dict[str, Any]:
    facts = load_json_unique(path)
    return validate_study_facts(facts, schema_path)


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _escape_markdown(value: Any) -> str:
    if value is None:
        return "未设置"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _enabled_labels(values: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    enabled = [labels[key] for key, value in values.items() if value and key in labels]
    return "、".join(enabled) if enabled else "无"


def render_fact_card(
    facts: Mapping[str, Any],
    routing: Mapping[str, Any] | None = None,
) -> str:
    """Render a compact Markdown fact card for mandatory human confirmation."""
    validated = validate_study_facts(dict(facts))
    if routing is None:
        from .module_router import route_modules

        routing = route_modules(validated)

    objectives = validated["objectives"]
    design = validated["design"]
    population = validated["population"]
    domains = validated["data_domains"]
    analyses = validated["analyses"]

    objective_labels = {
        "safety_tolerability": "安全性/耐受性",
        "pk_characterization": "PK 表征",
        "dose_exploration": "剂量探索",
        "efficacy": "疗效",
        "confirmatory_efficacy": "确证性疗效",
        "pharmacodynamics": "PD",
        "determine_mtd": "确定 MTD",
        "determine_rp2d": "确定 RP2D",
    }
    domain_labels = {
        "safety": "安全性",
        "efficacy": "疗效",
        "dlt": "DLT",
        "pk_concentration": "PK 浓度",
        "ada": "ADA",
        "pd_biomarker": "PD/生物标志物",
        "tumor_response": "肿瘤缓解",
        "survival": "生存",
        "ecg": "ECG",
        "ecg_intensive": "密集 ECG",
        "local_tolerance": "局部耐受性",
        "pulmonary_function": "肺功能",
        "ophthalmic_safety": "眼部安全性",
        "ae_consequence_strata": "AE 后果分层",
        "ae_study_withdrawal": "AE 导致研究退出",
        "aesi": "AESI",
        "vital_signs": "临床重要生命体征异常",
        "pregnancy": "妊娠报告",
        "medication_error_overdose_misuse": "用药错误/过量/误用",
    }
    analysis_labels = {
        "descriptive_only": "仅描述性",
        "hypothesis_test": "假设检验",
        "multiplicity_control": "多重性控制",
        "estimand_framework": "Estimand",
        "nca": "NCA",
        "population_pk": "群体 PK",
        "dose_proportionality": "剂量比例性",
        "exposure_response": "暴露-效应",
        "time_to_event": "生存/事件时间",
        "subgroup": "亚组",
    }
    population_summary = []
    if population["healthy_volunteer"]:
        population_summary.append("健康受试者")
    if population["patients"]:
        population_summary.append("患者")
    if population["enriched"]:
        population_summary.append("富集人群")
    population_summary.extend(population["special_populations"])

    doses = "、".join(
        f"{item['value']:g} {_escape_markdown(item['unit'])}" for item in design["dose_levels"]
    ) or "无"
    endpoint_names = [item["name"] for item in objectives["primary_endpoints"]]
    analysis_sets = [item["id"] for item in analyses["analysis_sets"]]
    dlt_window = design["dlt_window"]
    if dlt_window is None:
        dlt_window_text = "无"
    else:
        dlt_window_text = (
            f"第 {dlt_window['cycle']} 周期，"
            f"第 {dlt_window['day_from']}–{dlt_window['day_to']} 天"
        )
    active_modules = routing.get("active_modules", [])
    if active_modules and isinstance(active_modules[0], dict):
        active_module_ids = [item["id"] for item in active_modules]
    else:
        active_module_ids = list(active_modules)
    warnings = routing.get("warnings", [])
    warning_text = (
        "；".join(_escape_markdown(item).rstrip("。；") for item in warnings) + "。"
        if warnings
        else "无。"
    )

    lines = [
        f"# 研究事实卡：{_escape_markdown(validated['protocol_id'])}",
        "",
        "> 状态：**待人工确认**。本卡是模块路由与方案事实白名单的确认入口；期别仅作元数据和合理性提示，不参与结构路由。",
        "",
        "| 项目 | 已抽取事实 |",
        "|---|---|",
        f"| 研究标题 | {_escape_markdown(validated.get('study_title'))} |",
        f"| 期别标签（非路由） | {_escape_markdown(validated['phase'])} |",
        f"| 研究目的 | {_enabled_labels(objectives, objective_labels)} |",
        f"| 设计 | {design['type']}；研究类型={design['study_type']}；给药途径={design['route_of_administration']}；随机={_yes_no(design['randomized'])}；对照={_yes_no(design['controlled'])}；盲法={design['blinding']} |",
        f"| 人群 | {'、'.join(population_summary) or '未指定'}；目标疾病={_escape_markdown(population['target_condition'])} |",
        f"| 数据域 | {_enabled_labels(domains, domain_labels)} |",
        f"| 分析模块 | {_enabled_labels(analyses, analysis_labels)} |",
        "",
        "## 关键方案事实",
        "",
        f"- 剂量递增：`{_escape_markdown(design['escalation_scheme'])}`；剂量水平：{doses}；扩展队列：{design['expansion_cohorts']}。",
        f"- 对照类型：{'、'.join(design['control_types']) or '无'}；临床药理专项：{'、'.join(design['clinical_pharmacology_studies']) or '无'}。",
        f"- 周期：{_escape_markdown(design['cycle_days'])} 天；DLT 窗：{dlt_window_text}。",
        f"- 主要终点：{'；'.join(_escape_markdown(item) for item in endpoint_names) or '无'}。",
        f"- 分析集：{'、'.join(_escape_markdown(item) for item in analysis_sets) or '无'}。",
        f"- PK 采样时点（h）：{'、'.join(str(item) for item in domains['pk_sampling_timepoints_h']) or '无'}。",
        f"- 治疗领域端点标签：{'、'.join(domains['therapeutic_area_endpoints']) or '无'}。",
        f"- AE 后果分层（减量/暂停/永久停药，TEAE/TRAE 成对）：{_yes_no(domains['ae_consequence_strata'])}。",
        "",
        "## 确定性路由结果",
        "",
        f"- 激活模块（{len(active_module_ids)}）：{'、'.join(active_module_ids)}。",
        f"- 路由警告：{warning_text}",
        "",
        "## 人工确认",
        "",
        "- [ ] 已对照当前有效研究方案/SAP 核对五维事实。",
        "- [ ] 已核对终点、分析集、剂量/周期/DLT 窗及 PK 时点。",
        "- [ ] 已确认激活模块与预期 CSR 结构一致。",
        "",
    ]
    return "\n".join(lines)


def write_fact_card(
    facts: Mapping[str, Any],
    output_path: str | Path,
    routing: Mapping[str, Any] | None = None,
) -> None:
    atomic_write_text(output_path, render_fact_card(facts, routing))
