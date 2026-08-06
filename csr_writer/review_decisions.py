"""Reviewer-centred TFL-shell decisions, audit records, and version reuse.

M5-M7 deliberately emitted only privacy-safe structural metadata.  M8 keeps
that boundary and reorganises the technical ambiguity queue around questions a
statistician or medical writer can actually answer.  Source titles, cell text,
paths, and participant data are never accepted by this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .errors import CSRWriterError


class ReviewDecisionError(CSRWriterError):
    """A reviewer queue, decision, template, or reuse contract is invalid."""


ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ReviewDecisionError(
            f"{label} keys must be exactly {sorted(expected)}; "
            f"missing={sorted(expected-actual)}, extra={sorted(actual-expected)}"
        )


def _verify_signed_payload(
    payload: Mapping[str, Any], signature_field: str, label: str
) -> None:
    signature = payload.get(signature_field)
    if not isinstance(signature, str) or SHA256_RE.fullmatch(signature) is None:
        raise ReviewDecisionError(f"{label} lacks a valid {signature_field}")
    body = {key: copy.deepcopy(value) for key, value in payload.items() if key != signature_field}
    if _canonical_hash(body) != signature:
        raise ReviewDecisionError(f"{label} {signature_field} mismatch")


def _verify_proposal_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping) or report.get("schema_version") != "1.0":
        raise ReviewDecisionError("Proposal report must be a schema 1.0 object")
    _verify_signed_payload(report, "report_sha256", "proposal report")
    if not isinstance(report.get("entities"), list) or not isinstance(
        report.get("review_queue"), list
    ):
        raise ReviewDecisionError("Proposal report lacks entities or review_queue")


def _verify_candidate(candidate: Mapping[str, Any]) -> None:
    if not isinstance(candidate, Mapping) or candidate.get("schema_version") != "1.0":
        raise ReviewDecisionError("TFL map candidate must be a schema 1.0 object")
    signature = candidate.get("candidate_sha256")
    if not isinstance(signature, str) or SHA256_RE.fullmatch(signature) is None:
        raise ReviewDecisionError("TFL map candidate lacks candidate_sha256")
    body = {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if key not in {"candidate_sha256", "input_file_sha256"}
    }
    if _canonical_hash(body) != signature:
        raise ReviewDecisionError("TFL map candidate hash mismatch")
    if not isinstance(candidate.get("entities"), list):
        raise ReviewDecisionError("TFL map candidate lacks entities")


def _entity_indexes(
    report: Mapping[str, Any], candidate: Mapping[str, Any]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]], set[str]]:
    report_entities: dict[str, Mapping[str, Any]] = {}
    for raw in report["entities"]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("entity_id"), str):
            raise ReviewDecisionError("Malformed proposal entity")
        entity_id = raw["entity_id"]
        if entity_id in report_entities:
            raise ReviewDecisionError(f"Duplicate proposal entity: {entity_id}")
        report_entities[entity_id] = raw

    candidate_entities: dict[str, Mapping[str, Any]] = {}
    for raw in candidate["entities"]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("entity_id"), str):
            raise ReviewDecisionError("Malformed candidate entity")
        entity_id = raw["entity_id"]
        if entity_id in candidate_entities:
            raise ReviewDecisionError(f"Duplicate candidate entity: {entity_id}")
        candidate_entities[entity_id] = raw
    if set(report_entities) != set(candidate_entities):
        raise ReviewDecisionError("Proposal report and candidate entity sets differ")
    if candidate.get("candidate_sha256") != report.get("candidate_sha256"):
        raise ReviewDecisionError("Proposal report and candidate hashes disagree")

    review_ids: set[str] = set()
    for batch in report["review_queue"]:
        if not isinstance(batch, Mapping) or not isinstance(batch.get("entity_ids"), list):
            raise ReviewDecisionError("Malformed technical review batch")
        for entity_id in batch["entity_ids"]:
            if not isinstance(entity_id, str) or entity_id not in report_entities:
                raise ReviewDecisionError(f"Unknown review entity: {entity_id!r}")
            review_ids.add(entity_id)
    return report_entities, candidate_entities, review_ids


_DEFERRED_SOURCE_POLICIES: dict[str, dict[str, Any]] = {
    "meddra_soc_pt_from_result_tfl": {
        "label": "MedDRA SOC/PT 编码结果",
        "authority": "结果 TFL 中经 MedDRA 编码的 SOC/PT 层级；字典版本由 SAP 或编码计划确认",
        "row_count_mode": "unknown_data_driven",
        "row_count_basis": "取决于实际发生并完成编码的术语；shell 占位行数不构成预期行数",
        "hard_failures": [
            "冻结列 ID 漂移",
            "PT 缺少父级 SOC 路径",
            "同一父级内语义行重名",
            "结果行仍无法生成可读语义 ID",
        ],
        "warnings": ["结果行数与 shell 占位行数不同", "出现 shell 中未展示的新 SOC/PT"],
    },
    "whodrug_atc_and_drug_name_from_result_tfl": {
        "label": "WHODrug ATC/药物名编码结果",
        "authority": "结果 TFL 中经 WHODrug 编码的 ATC 层级及药物名；字典版本由 SAP 或编码计划确认",
        "row_count_mode": "unknown_data_driven",
        "row_count_basis": "取决于实际记录并完成编码的用药；shell 占位行数不构成预期行数",
        "hard_failures": [
            "冻结列 ID 漂移",
            "药物名缺少约定的 ATC 父级路径",
            "同一父级内语义行重名",
            "结果行仍无法生成可读语义 ID",
        ],
        "warnings": ["结果行数与 shell 占位行数不同", "出现 shell 中未展示的新 ATC/药物名"],
    },
    "protocol_deviation_category_from_result_tfl": {
        "label": "方案偏离分类结果",
        "authority": "SAP/偏离判定计划确认的分类，最终行标签取自结果 TFL",
        "row_count_mode": "non_binding_shell_placeholder_count",
        "row_count_basis": "shell 占位行数仅用于工作量提示；分类增删必须在结果阶段显式呈现",
        "hard_failures": ["冻结列 ID 漂移", "同一层级内分类重名", "结果行无法语义化"],
        "warnings": ["分类数与 shell 占位行数不同", "出现未在 shell 中展示的新分类"],
    },
    "pk_steady_state_row_labels_from_result_tfl": {
        "label": "PK 参数/阶段结果行",
        "authority": "SAP/TFL 规范定义的 PK 参数及阶段，最终行标签取自结果 TFL",
        "row_count_mode": "non_binding_shell_placeholder_count",
        "row_count_basis": "shell 占位行数为非约束提示；参数名和 D1/D28 阶段必须在结果阶段语义化",
        "hard_failures": ["冻结列或阶段维度漂移", "参数行重名", "结果行无法语义化"],
        "warnings": ["参数行数与 shell 占位行数不同"],
    },
    "result_tfl_row_labels": {
        "label": "结果 TFL 数据驱动行",
        "authority": "经人工确认的 SAP/TFL 规范及锁库后结果 TFL 行标签",
        "row_count_mode": "non_binding_shell_placeholder_count",
        "row_count_basis": "shell 占位行数仅用于提示；结果行可增删，但必须形成唯一语义 ID",
        "hard_failures": ["冻结列 ID 漂移", "同一父级内语义行重名", "结果行无法语义化"],
        "warnings": ["结果行数与 shell 占位行数不同"],
    },
}


def _deferred_expectation(
    entity_id: str,
    report_entity: Mapping[str, Any],
    candidate_entity: Mapping[str, Any],
) -> dict[str, Any] | None:
    contract = candidate_entity.get("deferred_row_contract") or report_entity.get(
        "deferred_row_contract"
    )
    if not isinstance(contract, Mapping):
        return None
    source_id = contract.get("expected_semantic_source")
    if not isinstance(source_id, str):
        raise ReviewDecisionError(f"Deferred entity {entity_id} lacks semantic source")
    policy = _DEFERRED_SOURCE_POLICIES.get(source_id)
    if policy is None:
        policy = {
            "label": "未登记的结果行来源",
            "authority": "必须在人工确认时指定，不能从 shell 位置推断",
            "row_count_mode": "unknown_data_driven",
            "row_count_basis": "无已确认依据",
            "hard_failures": ["行来源未确认", "冻结列 ID 漂移", "结果行无法语义化"],
            "warnings": ["结果行数与 shell 占位行数不同"],
        }
    shell_count = int(contract.get("shell_placeholder_row_count", 0))
    template_count = int(contract.get("template_placeholder_row_count", 0))
    stable_count = int(contract.get("stable_row_count", 0))
    mode = policy["row_count_mode"]
    estimated = shell_count + template_count if mode == "non_binding_shell_placeholder_count" else None
    frozen_columns = contract.get("frozen_column_ids", [])
    if not isinstance(frozen_columns, list) or not all(
        isinstance(item, str) for item in frozen_columns
    ):
        raise ReviewDecisionError(f"Deferred entity {entity_id} has malformed columns")
    return {
        "entity_id": entity_id,
        "semantic_source_id": source_id,
        "semantic_source_label": policy["label"],
        "expected_from": policy["authority"],
        "row_count_expectation": {
            "mode": mode,
            "estimated_deferred_rows": estimated,
            "shell_placeholder_rows": shell_count,
            "template_placeholder_rows": template_count,
            "already_stable_rows": stable_count,
            "binding": False,
            "basis": policy["row_count_basis"],
        },
        "frozen_column_ids": list(frozen_columns),
        "result_arrival_behavior": {
            "hard_fail": list(policy["hard_failures"]),
            "warn_only": list(policy["warnings"]),
            "position_hash_keys_allowed": False,
            "new_semantic_rows_materialized_at_result": True,
        },
    }


def _cell_semantics(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
    contracts = entity.get("cell_contracts", [])
    if not isinstance(contracts, list):
        raise ReviewDecisionError("Candidate cell_contracts must be a list")
    return [
        {
            "type": raw.get("type"),
            "row_id": raw.get("row_id"),
            "column_id": raw.get("column_id"),
            "dimensions": copy.deepcopy(raw.get("dimensions", {})),
        }
        for raw in contracts
        if isinstance(raw, Mapping)
    ]


def _own_structure(
    report_entity: Mapping[str, Any], candidate_entity: Mapping[str, Any]
) -> dict[str, Any]:
    """Return decision-relevant structure without physical positions or inherited expansion."""

    layout = candidate_entity.get("layout_reference")
    inherited = isinstance(layout, Mapping)
    layout_own = None
    if inherited:
        layout_own = {
            key: copy.deepcopy(layout.get(key))
            for key in ("inherited_from", "pattern_id", "columns", "rows", "row_classifier_ids")
        }
    deferred = candidate_entity.get("deferred_row_contract")
    deferred_own = None
    if isinstance(deferred, Mapping):
        deferred_own = {
            key: copy.deepcopy(deferred.get(key))
            for key in (
                "schema_version",
                "detection",
                "materialize_at",
                "expected_semantic_source",
            )
        }
        if not inherited:
            deferred_own.update(
                {
                    "shell_placeholder_row_count": deferred.get(
                        "shell_placeholder_row_count", 0
                    ),
                    "template_placeholder_row_count": deferred.get(
                        "template_placeholder_row_count", 0
                    ),
                    "stable_row_count": deferred.get("stable_row_count", 0),
                    "frozen_column_ids": copy.deepcopy(
                        deferred.get("frozen_column_ids", [])
                    ),
                }
            )
    cells = [] if inherited else _cell_semantics(candidate_entity)
    return {
        "entity_kind": candidate_entity.get("entity_kind"),
        "analysis_set": report_entity.get("analysis_set"),
        "title_sha256": report_entity.get("title_sha256"),
        "dimensions": copy.deepcopy(candidate_entity.get("dimensions", {})),
        "ich_e3_sections": copy.deepcopy(candidate_entity.get("ich_e3_sections", [])),
        "ingest_strategy": candidate_entity.get("ingest_strategy"),
        "layout_reference": layout_own,
        "deferred_row_contract": deferred_own,
        "own_cell_semantics": cells,
        "physical_shape": copy.deepcopy(report_entity.get("physical_shape")),
        "physical_object_count": len(report_entity.get("physical_table_indexes", [])),
    }


def _build_entity_structures(
    report_entities: Mapping[str, Mapping[str, Any]],
    candidate_entities: Mapping[str, Mapping[str, Any]],
    review_ids: set[str],
) -> dict[str, dict[str, Any]]:
    own_by_id = {
        entity_id: _own_structure(report_entities[entity_id], candidate_entities[entity_id])
        for entity_id in sorted(candidate_entities)
    }
    own_hashes = {entity_id: _canonical_hash(value) for entity_id, value in own_by_id.items()}

    visiting: set[str] = set()
    memo: dict[str, dict[str, Any]] = {}

    def materialize(entity_id: str) -> dict[str, Any]:
        if entity_id in memo:
            return memo[entity_id]
        if entity_id in visiting:
            raise ReviewDecisionError(f"Layout dependency cycle at {entity_id}")
        visiting.add(entity_id)
        layout = own_by_id[entity_id].get("layout_reference")
        dependencies: list[dict[str, str]] = []
        if isinstance(layout, Mapping):
            target = layout.get("inherited_from")
            if not isinstance(target, str) or target not in own_by_id:
                raise ReviewDecisionError(f"Unknown layout dependency: {entity_id} -> {target}")
            target_snapshot = materialize(target)
            dependencies.append(
                {
                    "entity_id": target,
                    "effective_structure_sha256": target_snapshot[
                        "effective_structure_sha256"
                    ],
                }
            )
        dependency_hash = _canonical_hash(dependencies)
        effective_hash = _canonical_hash(
            {"own_structure_sha256": own_hashes[entity_id], "dependencies": dependencies}
        )
        memo[entity_id] = {
            "entity_id": entity_id,
            "requires_review": entity_id in review_ids,
            "own_structure": own_by_id[entity_id],
            "own_structure_sha256": own_hashes[entity_id],
            "dependencies": dependencies,
            "dependency_sha256": dependency_hash,
            "effective_structure_sha256": effective_hash,
        }
        visiting.remove(entity_id)
        return memo[entity_id]

    for entity_id in sorted(own_by_id):
        materialize(entity_id)
    return memo


def _structure_summary(
    entity_id: str,
    report_entity: Mapping[str, Any],
    candidate_entity: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    expectation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contracts = candidate_entity.get("cell_contracts", [])
    stable_rows = sorted(
        {str(item.get("row_id")) for item in contracts if isinstance(item, Mapping)}
    )
    columns = sorted(
        {str(item.get("column_id")) for item in contracts if isinstance(item, Mapping)}
    )
    if expectation is not None:
        columns = sorted(set(columns) | set(expectation["frozen_column_ids"]))
    return {
        "entity_id": entity_id,
        "entity_kind": candidate_entity.get("entity_kind"),
        "analysis_set": report_entity.get("analysis_set"),
        "ich_e3_sections": copy.deepcopy(candidate_entity.get("ich_e3_sections", [])),
        "dimensions": copy.deepcopy(candidate_entity.get("dimensions", {})),
        "ingest_strategy": candidate_entity.get("ingest_strategy"),
        "column_ids": columns,
        "stable_row_ids": stable_rows,
        "existing_semantic_key_count": len(candidate_entity.get("semantic_keys", [])),
        "layout_reference": copy.deepcopy(candidate_entity.get("layout_reference")),
        "deferred_row_expectation": copy.deepcopy(expectation),
        "physical_structure": {
            "object_count": len(report_entity.get("physical_table_indexes", [])),
            "first_object_shape": copy.deepcopy(report_entity.get("physical_shape")),
        },
        "own_structure_sha256": snapshot["own_structure_sha256"],
        "dependency_sha256": snapshot["dependency_sha256"],
        "effective_structure_sha256": snapshot["effective_structure_sha256"],
    }


def _difference_fields(reference: Mapping[str, Any], other: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "analysis_set",
        "ich_e3_sections",
        "dimensions",
        "ingest_strategy",
        "column_ids",
        "stable_row_ids",
        "existing_semantic_key_count",
        "layout_reference",
        "deferred_row_expectation",
        "physical_structure",
    )
    def comparable(field: str, value: Any) -> Any:
        result = copy.deepcopy(value)
        # The expectation embeds its owning entity solely for lookup.  Treating
        # that identifier as a structural difference would turn a 35-table AE
        # family back into 35 pseudo-decisions.
        if field == "deferred_row_expectation" and isinstance(result, dict):
            result.pop("entity_id", None)
        return result

    differences: dict[str, Any] = {}
    for field in fields:
        reference_value = comparable(field, reference.get(field))
        member_value = comparable(field, other.get(field))
        if reference_value != member_value:
            differences[field] = {
                "representative": reference_value,
                "member": member_value,
            }
    return differences


def _select_representatives(
    member_ids: Sequence[str], structures: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    def score(entity_id: str) -> tuple[int, int, int, str]:
        item = structures[entity_id]
        direct = 1 if item.get("layout_reference") is None else 0
        return (
            direct,
            len(item.get("stable_row_ids", [])),
            len(item.get("column_ids", [])),
            entity_id,
        )

    first = max(member_ids, key=score)
    if len(member_ids) == 1:
        return [first]
    first_structure = structures[first]
    candidates = [item for item in member_ids if item != first]
    second = max(
        candidates,
        key=lambda entity_id: (
            len(_difference_fields(first_structure, structures[entity_id])),
            score(entity_id),
        ),
    )
    return [first, second]


def _group_differences(
    member_ids: Sequence[str],
    representative_ids: Sequence[str],
    structures: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    reference = structures[representative_ids[0]]
    grouped: dict[str, dict[str, Any]] = {}
    for entity_id in member_ids:
        if entity_id in representative_ids:
            continue
        differences = _difference_fields(reference, structures[entity_id])
        signature = _canonical_hash(differences)
        grouped.setdefault(
            signature,
            {"entity_ids": [], "differences_from_primary_representative": differences},
        )["entity_ids"].append(entity_id)
    return [
        {**item, "entity_ids": sorted(item["entity_ids"])}
        for _, item in sorted(grouped.items())
    ]


def _required_input(field: str, prompt: str) -> dict[str, Any]:
    return {
        "field": field,
        "prompt": prompt,
        "required_before_confirm": True,
        "proposed_value": None,
    }


def _group_specs(
    review_ids: set[str],
    report_entities: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    def source(entity_id: str) -> str | None:
        contract = report_entities[entity_id].get("deferred_row_contract")
        return contract.get("expected_semantic_source") if isinstance(contract, Mapping) else None

    def members(predicate) -> list[str]:
        return sorted(entity_id for entity_id in review_ids if predicate(entity_id))

    specs: list[dict[str, Any]] = []

    def add(
        group_id: str,
        decision_type: str,
        label: str,
        common_point: str,
        entity_ids: Sequence[str],
        default_action: str,
        default_parameters: Mapping[str, Any],
        override_consequences: str,
        required_inputs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if entity_ids:
            specs.append(
                {
                    "group_id": group_id,
                    "decision_type": decision_type,
                    "label": label,
                    "common_decision_point": common_point,
                    "member_entity_ids": sorted(set(entity_ids)),
                    "default_decision": {
                        "action": default_action,
                        "parameters": copy.deepcopy(dict(default_parameters)),
                        "origin": "tool_proposal",
                        "requires_human_confirmation": True,
                    },
                    "required_reviewer_inputs": [copy.deepcopy(dict(item)) for item in required_inputs],
                    "override_consequences": override_consequences,
                }
            )

    meddra = members(lambda item: source(item) == "meddra_soc_pt_from_result_tfl")
    add(
        "DG-CODE-MEDDRA",
        "coding_dictionary_dependency",
        "编码字典依赖：MedDRA",
        "确认病史与 AE 是否使用同一版 MedDRA 及相同 SOC/PT 父子规则；若版本不同，必须推翻默认并拆分。",
        meddra,
        "confirm_dictionary_and_late_bound_hierarchy",
        {
            "dictionary": "MedDRA",
            "domains": ["medical_history", "adverse_events"],
            "row_hierarchy": "SOC/PT",
            "same_pt_across_soc_allowed": True,
        },
        "推翻后需按病史/AE 分别登记版本与分类器；§11.2 和 §12.2 的晚绑定行决定均不得沿用。",
        [_required_input("dictionary_version", "填写经 SAP/编码计划确认的 MedDRA 版本；若分域不同须改用 override")],
    )
    add(
        "DG-CODE-WHODRUG-MEDICATION",
        "coding_dictionary_dependency",
        "编码字典依赖：用药",
        "确认既往/合并用药行来自哪一版 WHODrug，以及 ATC→药物名层级是否一致。",
        members(lambda item: source(item) == "whodrug_atc_and_drug_name_from_result_tfl"),
        "confirm_dictionary_and_late_bound_hierarchy",
        {"dictionary": "WHODrug", "row_hierarchy": "ATC/drug_name", "row_count_is_binding": False},
        "推翻后需重定义用药行层级，相关 §11.2 表全部重新入队，结果键不得按现策略生成。",
        [_required_input("dictionary_version", "填写经 SAP/编码计划确认的 WHODrug 版本")],
    )
    add(
        "DG-ROWS-PROTOCOL-DEVIATION",
        "deferred_row_source_contract",
        "延后行来源：方案偏离",
        "确认偏离类别的权威来源及版本；shell 行数仅作提示，不能按位置冻结。",
        members(lambda item: source(item) == "protocol_deviation_category_from_result_tfl"),
        "confirm_result_driven_rows",
        {"authority": "SAP_or_deviation_plan_then_result_tfl", "position_keys_allowed": False},
        "推翻后 §10.1 的方案偏离表需更换分类契约，并在结果到位前保持不可冻结。",
        [_required_input("category_authority", "填写偏离分类的 SAP/判定计划定位或版本")],
    )
    add(
        "DG-ROWS-PK-PARAMETERS",
        "deferred_row_source_contract",
        "延后行来源：PK 参数",
        "确认 PK 参数名及 D1/D28 阶段来自已审 SAP/TFL 规范，而不是 shell 占位位置。",
        members(lambda item: source(item) == "pk_steady_state_row_labels_from_result_tfl"),
        "confirm_result_driven_rows",
        {"authority": "reviewed_SAP_TFL_specification", "position_keys_allowed": False},
        "推翻后 §11.5 的参数/阶段维度需重建，跨格式结果套值必须继续硬阻断。",
        [_required_input("parameter_authority", "填写 PK 参数及阶段定义的 SAP/TFL 规范定位或版本")],
    )
    generic_result_rows = members(lambda item: source(item) == "result_tfl_row_labels")
    add(
        "DG-ROWS-BASELINE-EFFICACY",
        "deferred_row_source_contract",
        "延后行来源：基线与疗效",
        "确认基线分类与疗效统计行来自已审 SAP 终点/分析规范及结果 TFL，不按 shell 位置锁定。",
        [
            item
            for item in generic_result_rows
            if set(report_entities[item].get("ich_e3_sections", [])) & {"11.2", "11.4"}
        ],
        "confirm_result_driven_rows",
        {"authority": "reviewed_endpoint_analysis_specification_and_result_tfl", "position_keys_allowed": False},
        "推翻后 §11.2/§11.4 需分别补充分类或终点行契约，相关表保持不可冻结。",
    )
    add(
        "DG-ROWS-SAFETY-SUMMARY",
        "deferred_row_source_contract",
        "延后行来源：暴露与 AE 汇总",
        "确认暴露和 AE 汇总中的非 SOC/PT 行来自已审安全性分析规范，而不是编码字典或占位位置。",
        [
            item
            for item in generic_result_rows
            if set(report_entities[item].get("ich_e3_sections", [])) & {"12.1", "12.2"}
        ],
        "confirm_result_driven_rows",
        {"authority": "reviewed_safety_summary_specification_and_result_tfl", "position_keys_allowed": False},
        "推翻后 §12.1/§12.2 汇总表必须各自补充行类别契约，不得套用 MedDRA SOC/PT 规则。",
    )
    assigned_generic = {
        item
        for item in generic_result_rows
        if set(report_entities[item].get("ich_e3_sections", []))
        & {"11.2", "11.4", "12.1", "12.2"}
    }
    add(
        "DG-ROWS-OTHER-SAFETY",
        "deferred_row_source_contract",
        "延后行来源：其他安全性域",
        "确认实验室、生命体征等其他安全性域的行由各自 SAP/TFL 域规范及结果标签生成。",
        [item for item in generic_result_rows if item not in assigned_generic],
        "confirm_result_driven_rows",
        {"authority": "reviewed_domain_specification_and_result_tfl", "position_keys_allowed": False},
        "推翻后相应 §12 子域需分别登记参数/类别来源，并在结果到位前保持不可冻结。",
    )

    parallel: set[str] = set()
    for entity_id in review_ids:
        layout = report_entities[entity_id].get("layout_reference")
        if not isinstance(layout, Mapping):
            continue
        target = layout.get("inherited_from")
        if (
            isinstance(target, str)
            and target in review_ids
            and report_entities[target].get("analysis_set")
            != report_entities[entity_id].get("analysis_set")
        ):
            parallel.update((target, entity_id))
    add(
        "DG-POP-PARALLEL",
        "analysis_set_parallelism",
        "分析集平行表",
        "确认 FAS/mFAS 等平行版本仅共享版式，POP 维度与实际结果始终各自独立。",
        sorted(parallel),
        "confirm_parallel_structure_keep_population_separate",
        {"inherit_columns": True, "inherit_statistic_rows": True, "inherit_population": False},
        "推翻后平行表不能批量复用；对应分析集的映射、绑定和章节引用均需独立重审。",
    )

    inherited = members(lambda item: isinstance(report_entities[item].get("layout_reference"), Mapping))
    add(
        "DG-LAYOUT-BASELINE",
        "layout_inheritance_scope",
        "版式继承：病史与用药",
        "确认每个病史/用药子表的继承源正确，且只继承列；数据驱动行必须留到结果阶段。",
        [item for item in inherited if item.startswith("T14.1.")],
        "confirm_layout_target_and_scope",
        {"inherit_columns": True, "inherit_data_driven_rows": False},
        "推翻某一继承源会使该子表结构哈希失效，并使 §11.2 的相关列映射重新入队。",
    )
    add(
        "DG-LAYOUT-EFFICACY",
        "layout_inheritance_scope",
        "版式继承：疗效平行表",
        "确认疗效平行表的模板源和继承范围；分析集标签必须从各自标题解析，绝不继承。",
        [item for item in inherited if item.startswith("T14.2.")],
        "confirm_layout_target_and_scope",
        {"inherit_columns": True, "inherit_statistic_rows": True, "inherit_population": False},
        "推翻后 §11.4 的平行分析集表需独立建立稳定统计量行与列契约。",
    )
    add(
        "DG-LAYOUT-AE-FAMILY",
        "layout_inheritance_scope",
        "版式继承：AE 表族",
        "确认 AE 子集表引用的根模板正确；共享列结构，但 SOC/PT 或其他结果行不继承。",
        [
            item
            for item in inherited
            if item.startswith("T14.3.1.3.") or item.startswith("T14.3.2.")
        ],
        "confirm_layout_target_and_scope",
        {"inherit_columns": True, "inherit_data_driven_rows": False},
        "推翻根模板会使所有依赖该模板的 §12/§12.2 子表决定失效并要求重新确认。",
    )
    add(
        "DG-LAYOUT-OTHER-SAFETY",
        "layout_inheritance_scope",
        "版式继承：其他安全性表",
        "确认其他安全性平行表的模板源，并区分可继承的稳定统计量行与延后数据行。",
        [item for item in inherited if item.startswith("T14.3.") and not (
            item.startswith("T14.3.1.3.") or item.startswith("T14.3.2.")
        )],
        "confirm_layout_target_and_scope",
        {"inherit_columns": True, "inherit_stable_statistic_rows": True, "inherit_data_driven_rows": False},
        "推翻后相应 §12 子表必须独立解析网格；依赖源变化时既有决定不得静默沿用。",
    )

    add(
        "DG-FAMILY-AE",
        "family_batch_policy",
        "同族批量：AE 及后果子集",
        "确认 TEAE/TRAE 及导致减量、暂停、永久停药等表族使用一致的列和晚绑定行策略。",
        members(lambda item: item.startswith("T14.3.1.3.") or item.startswith("T14.3.2.")),
        "confirm_family_policy",
        {"column_contract_consistent_per_template": True, "rows_materialize_per_result_table": True},
        "推翻后不能对该表族批量确认；每个差异子族需拆分并分别重审，影响 §12/§12.2。",
    )
    add(
        "DG-FAMILY-OTHER-SAFETY",
        "family_batch_policy",
        "同族批量：其他安全性评估",
        "确认实验室、生命体征及其他安全性表只在相同结构子族内复用策略，差异表保持单独契约。",
        members(lambda item: re.match(r"^T14\.3\.(?:4|5|6|7)\.", item) is not None),
        "confirm_family_policy",
        {"reuse_only_within_equal_structure": True, "structure_hash_required": True},
        "推翻后相关 §12 表需拆成更细子族；结构异常实体仍不得转为 cell_level。",
    )
    add(
        "DG-STRUCTURE-ANOMALY",
        "structural_exception",
        "结构异常个案",
        "确认多 Word 表对象或无网格个案暂按表级元数据登记，直至人工定义对象边界。",
        members(lambda item: report_entities[item].get("ingest_strategy") == "metadata_only"),
        "confirm_metadata_only_until_subtables_classified",
        {"materialize_cell_keys": False, "manual_subtable_boundary_required": True},
        "推翻后必须先提交每个 Word 表对象的归属与合并规则；未完成前不得生成任何单元格键。",
        [_required_input("subtable_boundary_disposition", "确认保持表级元数据，或登记经审阅的子表边界方案")],
    )
    return specs


def _group_impact(
    member_ids: Sequence[str], structures: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    sections = sorted(
        {
            section
            for entity_id in member_ids
            for section in structures[entity_id].get("ich_e3_sections", [])
        }
    )
    key_count = sum(
        int(structures[entity_id].get("existing_semantic_key_count", 0))
        for entity_id in member_ids
    )
    deferred_count = sum(
        structures[entity_id].get("deferred_row_expectation") is not None
        for entity_id in member_ids
    )
    return {
        "affected_entity_count": len(member_ids),
        "affected_ich_e3_sections": sections,
        "existing_semantic_key_count": key_count,
        "deferred_entity_count": deferred_count,
        "future_late_bound_key_count": "unknown_until_result_tfl",
        "statement": (
            f"影响 {len(member_ids)} 个 TFL 实体、现有 {key_count} 个稳定键；"
            f"{deferred_count} 个实体的晚绑定键数量须等结果 TFL 到位后确定。"
        ),
    }


def _render_group_core(group: Mapping[str, Any]) -> str:
    def difference_text(field: str, change: Mapping[str, Any]) -> str:
        value = change.get("member")
        if field == "layout_reference":
            if not isinstance(value, Mapping):
                return "无继承"
            return (
                f"继承自 {value.get('inherited_from')}；列={value.get('columns')}；"
                f"行={value.get('rows')}"
            )
        if field == "deferred_row_expectation":
            if not isinstance(value, Mapping):
                return "无延后行"
            count = value.get("row_count_expectation", {})
            return (
                f"来源={value.get('semantic_source_id')}；"
                f"shell/template 占位={count.get('shell_placeholder_rows', 0)}/"
                f"{count.get('template_placeholder_rows', 0)}"
            )
        if field == "physical_structure" and isinstance(value, Mapping):
            shape = value.get("first_object_shape") or {}
            return (
                f"对象数={value.get('object_count')}；"
                f"首对象={shape.get('rows', 0)}行×{shape.get('grid_columns', 0)}列"
            )
        if field in {"column_ids", "stable_row_ids"} and isinstance(value, list):
            if len(value) <= 8:
                return f"{len(value)}项：" + ", ".join(str(item) for item in value)
            return f"{len(value)}项；SHA-256={_canonical_hash(value)[:12]}…"
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    lines = [
        f"### {group['group_id']} · {group['label']}",
        "",
        f"- 共同判断点：{group['common_decision_point']}",
        f"- 默认决定：`{group['default_decision']['action']}`；参数 "
        + json.dumps(group["default_decision"]["parameters"], ensure_ascii=False, sort_keys=True),
    ]
    if group["required_reviewer_inputs"]:
        lines.append(
            "- 确认前必填："
            + "；".join(item["prompt"] for item in group["required_reviewer_inputs"])
        )
    lines.extend(
        [
            f"- 推翻后果：{group['override_consequences']}",
            f"- 影响面：{group['downstream_impact']['statement']}",
            "",
            "代表样例（完整结构）：",
        ]
    )
    for item in group["representatives"]:
        layout = item.get("layout_reference")
        deferred = item.get("deferred_row_expectation")
        lines.extend(
            [
                "",
                f"- `{item['entity_id']}`：分析集 `{item['analysis_set']}`；策略 "
                f"`{item['ingest_strategy']}`；章节 {', '.join(item['ich_e3_sections'])}",
                "  - 列：" + (", ".join(item["column_ids"]) or "无单元格键（表级元数据）"),
                "  - 稳定行：" + (", ".join(item["stable_row_ids"]) or "无；行在结果阶段生成"),
                "  - 继承：" + (
                    json.dumps(layout, ensure_ascii=False, sort_keys=True) if layout else "无"
                ),
                "  - 延后行：" + (
                    f"{deferred['semantic_source_label']}；预计行数模式 "
                    f"`{deferred['row_count_expectation']['mode']}`；列漂移/语义碰撞硬失败"
                    if deferred
                    else "无"
                ),
                f"  - 结构哈希：`{item['effective_structure_sha256']}`",
            ]
        )
    lines.extend(["", "组内差异（仅列与首个代表不同处）："])
    if not group["difference_sets"]:
        lines.append("- 无其他成员差异。")
    for difference in group["difference_sets"]:
        changes = difference["differences_from_primary_representative"]
        fields = "；".join(
            f"{field}：{difference_text(field, change)}"
            for field, change in changes.items()
        ) or "无结构差异"
        lines.append(
            f"- {', '.join('`'+item+'`' for item in difference['entity_ids'])}：{fields}"
        )
    return "\n".join(lines) + "\n"


def build_review_decision_queue(
    report: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a privacy-safe queue organised by human decision, not parser failure."""

    _verify_proposal_report(report)
    _verify_candidate(candidate)
    report_entities, candidate_entities, review_ids = _entity_indexes(report, candidate)
    snapshots = _build_entity_structures(report_entities, candidate_entities, review_ids)
    expectations = {
        entity_id: expectation
        for entity_id in sorted(candidate_entities)
        if (
            expectation := _deferred_expectation(
                entity_id, report_entities[entity_id], candidate_entities[entity_id]
            )
        )
        is not None
    }
    structures = {
        entity_id: _structure_summary(
            entity_id,
            report_entities[entity_id],
            candidate_entities[entity_id],
            snapshots[entity_id],
            expectations.get(entity_id),
        )
        for entity_id in sorted(candidate_entities)
    }
    groups: list[dict[str, Any]] = []
    membership: defaultdict[str, list[str]] = defaultdict(list)
    for spec in _group_specs(review_ids, report_entities):
        member_ids = spec["member_entity_ids"]
        representatives = _select_representatives(member_ids, structures)
        body = {
            **spec,
            "representative_entity_ids": representatives,
            "representatives": [copy.deepcopy(structures[item]) for item in representatives],
            "difference_sets": _group_differences(
                member_ids, representatives, structures
            ),
            "downstream_impact": _group_impact(member_ids, structures),
            "allowed_outcomes": ["confirm_default", "override_default", "defer"],
        }
        template_contract = {
            "decision_type": body["decision_type"],
            "default_decision": body["default_decision"],
            "required_input_fields": [
                item["field"] for item in body["required_reviewer_inputs"]
            ],
            "common_decision_point": body["common_decision_point"],
        }
        body["template_match_signature"] = _canonical_hash(template_contract)
        core = _render_group_core(body)
        body["information_volume"] = {
            "character_count": len(core),
            "screen_equivalents": round(max(1.0, len(core) / 900), 1),
            "measurement": "rendered_group_core; 900 characters per screen",
        }
        group_hash_body = copy.deepcopy(body)
        body["decision_group_sha256"] = _canonical_hash(group_hash_body)
        groups.append(body)
        for entity_id in member_ids:
            membership[entity_id].append(body["group_id"])

    uncovered = sorted(review_ids - set(membership))
    if uncovered:
        raise ReviewDecisionError(f"Review entities lack a human decision group: {uncovered}")
    if len(groups) < 1:
        raise ReviewDecisionError("Decision queue contains no groups")

    total_characters = sum(item["information_volume"]["character_count"] for item in groups)
    difference_sets = sum(len(item["difference_sets"]) for item in groups)
    reading_rate = 500
    decision_seconds = 75
    difference_seconds = 12
    estimated_seconds = (
        total_characters / reading_rate * 60
        + len(groups) * decision_seconds
        + difference_sets * difference_seconds
    )
    ergonomics = {
        "decision_group_count": len(groups),
        "unique_review_entity_count": len(review_ids),
        "entity_decision_memberships": sum(len(value) for value in membership.values()),
        "total_group_characters": total_characters,
        "total_screen_equivalents": round(
            sum(item["information_volume"]["screen_equivalents"] for item in groups), 1
        ),
        "estimated_review_minutes": round(estimated_seconds / 60, 1),
        "estimate_basis": {
            "structured_technical_reading_rate_characters_per_minute": reading_rate,
            "judgement_overhead_seconds_per_group": decision_seconds,
            "difference_set_overhead_seconds": difference_seconds,
            "reviewer_assumption": "熟悉本项目、SAP 和编码计划的统计/医学撰写审阅者",
            "scope": "首轮结构决定；不含向统计师追问未决字典版本的等待时间",
        },
    }
    queue_body = {
        "schema_version": "1.0",
        "status": "awaiting_human_decisions",
        "proposal_report_sha256": report["report_sha256"],
        "candidate_sha256": candidate["candidate_sha256"],
        "source_shell_sha256": report.get("source_sha256"),
        "legacy_technical_batch_count": len(report["review_queue"]),
        "decision_groups": groups,
        "review_entity_membership": [
            {"entity_id": entity_id, "decision_group_ids": sorted(membership[entity_id])}
            for entity_id in sorted(review_ids)
        ],
        "entity_structures": [snapshots[item] for item in sorted(snapshots)],
        "deferred_row_expectations": [expectations[item] for item in sorted(expectations)],
        "coverage": {
            "status": "pass",
            "review_entity_count": len(review_ids),
            "covered_review_entity_count": len(membership),
            "uncovered_entity_ids": [],
            "multiple_decision_type_entities": sum(
                len(group_ids) > 1 for group_ids in membership.values()
            ),
        },
        "ergonomics": ergonomics,
    }
    return {**queue_body, "queue_sha256": _canonical_hash(queue_body)}


def _verify_queue(queue: Mapping[str, Any]) -> None:
    if not isinstance(queue, Mapping) or queue.get("schema_version") != "1.0":
        raise ReviewDecisionError("Review decision queue must be schema 1.0")
    _verify_signed_payload(queue, "queue_sha256", "review decision queue")
    groups = queue.get("decision_groups")
    if not isinstance(groups, list) or not groups:
        raise ReviewDecisionError("Review decision queue has no groups")
    observed: set[str] = set()
    for group in groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("group_id"), str):
            raise ReviewDecisionError("Malformed decision group")
        if group["group_id"] in observed:
            raise ReviewDecisionError(f"Duplicate decision group: {group['group_id']}")
        observed.add(group["group_id"])
        signature = group.get("decision_group_sha256")
        body = {key: copy.deepcopy(value) for key, value in group.items() if key != "decision_group_sha256"}
        if signature != _canonical_hash(body):
            raise ReviewDecisionError(f"Decision group hash mismatch: {group['group_id']}")


def render_review_decision_queue(queue: Mapping[str, Any]) -> str:
    _verify_queue(queue)
    metrics = queue["ergonomics"]
    lines = [
        "# TFL shell 人工决策队列",
        "",
        "> 本报告只含脱敏结构。确认单位是“判断”，不是解析失败批次；同一实体可能需要多项判断。",
        "",
        f"- 待确认实体：{metrics['unique_review_entity_count']}",
        f"- 旧技术批次：{queue['legacy_technical_batch_count']}",
        f"- 实际决策组：{metrics['decision_group_count']}",
        f"- 决策成员关系：{metrics['entity_decision_memberships']}（允许同一实体重复出现）",
        f"- 信息量：{metrics['total_group_characters']} 字符 / 约 {metrics['total_screen_equivalents']} 屏",
        f"- 预计净审阅时间：{metrics['estimated_review_minutes']} 分钟",
        "- 估算口径：500 字符/分钟 + 每组 75 秒判断 + 每类差异 12 秒；不含外部追问等待。",
        "",
    ]
    for group in queue["decision_groups"]:
        lines.append(_render_group_core(group).rstrip())
        volume = group["information_volume"]
        lines.extend(
            [
                "",
                f"本组信息量：{volume['character_count']} 字符 / 约 {volume['screen_equivalents']} 屏。",
                "",
            ]
        )
    lines.extend(
        [
            "## 审计要求",
            "",
            "每组决定必须绑定本报告 SHA-256、组结构 SHA-256、审阅者标识和带时区时间戳；推翻默认决定必须写理由。",
            "",
            f"队列 SHA-256：`{queue['queue_sha256']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def render_deferred_row_expectations(
    queue: Mapping[str, Any], representative_ids: Sequence[str] | None = None
) -> str:
    _verify_queue(queue)
    all_items = {
        item["entity_id"]: item for item in queue["deferred_row_expectations"]
    }
    if representative_ids is None:
        selected = [all_items[item] for item in sorted(all_items)]
    else:
        missing = [item for item in representative_ids if item not in all_items]
        if missing:
            raise ReviewDecisionError(f"Deferred expectation not found: {missing}")
        selected = [all_items[item] for item in representative_ids]
    lines = [
        "# 延后行结果阶段契约",
        "",
        "> shell 占位行不建位置键；以下行数均为工作量提示，除非另有人工确认，不作为结果行数锁。",
        "",
    ]
    for item in selected:
        count = item["row_count_expectation"]
        estimate = count["estimated_deferred_rows"]
        estimate_text = "无法从 shell 确定" if estimate is None else f"约 {estimate} 行（非约束）"
        lines.extend(
            [
                f"## {item['entity_id']}",
                "",
                f"- 预计来源：{item['expected_from']}",
                f"- 预计行数：{estimate_text}。依据：{count['basis']}",
                "- 结果不符时硬失败：" + "；".join(item["result_arrival_behavior"]["hard_fail"]),
                "- 仅告警：" + "；".join(item["result_arrival_behavior"]["warn_only"]),
                "- 处置：新语义行在结果阶段建键；位置哈希永不允许进入冻结映射。",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _parse_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewDecisionError(f"{label} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReviewDecisionError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ReviewDecisionError(f"{label} must include a timezone")
    return value


def create_decision_record(
    queue: Mapping[str, Any],
    *,
    group_id: str,
    reviewer_role: str,
    reviewer_identifier: str,
    decided_at: str,
    outcome: str,
    resolved_inputs: Mapping[str, str] | None = None,
    override_parameters: Mapping[str, Any] | None = None,
    override_reason: str | None = None,
    decision_notes: str = "",
    template_sha256: str | None = None,
) -> dict[str, Any]:
    """Create one hash-bound, attributable decision record."""

    _verify_queue(queue)
    groups = {item["group_id"]: item for item in queue["decision_groups"]}
    if group_id not in groups:
        raise ReviewDecisionError(f"Unknown decision group: {group_id}")
    group = groups[group_id]
    if outcome not in group["allowed_outcomes"]:
        raise ReviewDecisionError(f"Outcome {outcome!r} is not allowed for {group_id}")
    if ROLE_RE.fullmatch(reviewer_role) is None:
        raise ReviewDecisionError("reviewer_role must be a role identifier")
    if not isinstance(reviewer_identifier, str) or not reviewer_identifier.strip():
        raise ReviewDecisionError("reviewer_identifier must identify who made the decision")
    _parse_timestamp(decided_at, "decided_at")
    inputs = dict(resolved_inputs or {})
    required = {
        item["field"] for item in group["required_reviewer_inputs"]
        if item["required_before_confirm"]
    }
    if outcome != "defer":
        missing = sorted(
            field for field in required if not isinstance(inputs.get(field), str) or not inputs[field].strip()
        )
        if missing:
            raise ReviewDecisionError(
                f"Decision {group_id} lacks required reviewer inputs: {missing}"
            )
    if outcome == "override_default":
        if not isinstance(override_reason, str) or not override_reason.strip():
            raise ReviewDecisionError("override_default requires a non-empty override_reason")
        if not isinstance(override_parameters, Mapping) or not override_parameters:
            raise ReviewDecisionError(
                "override_default requires structured override_parameters"
            )
    elif override_reason not in (None, ""):
        raise ReviewDecisionError("override_reason is only allowed for override_default")
    elif override_parameters not in (None, {}):
        raise ReviewDecisionError(
            "override_parameters are only allowed for override_default"
        )
    if template_sha256 is not None and SHA256_RE.fullmatch(template_sha256) is None:
        raise ReviewDecisionError("template_sha256 must be SHA-256")
    body = {
        "schema_version": "1.0",
        "record_id": "DEC-" + _canonical_hash(
            {"queue": queue["queue_sha256"], "group": group_id, "who": reviewer_identifier, "when": decided_at}
        )[:20].upper(),
        "proposal_report_sha256": queue["proposal_report_sha256"],
        "queue_sha256": queue["queue_sha256"],
        "group_id": group_id,
        "decision_group_sha256": group["decision_group_sha256"],
        "decision_type": group["decision_type"],
        "reviewer": {"role": reviewer_role, "identifier": reviewer_identifier},
        "decided_at": decided_at,
        "decision": {
            "outcome": outcome,
            "selected_action": (
                group["default_decision"]["action"]
                if outcome == "confirm_default"
                else ("manual_override" if outcome == "override_default" else "defer")
            ),
            "resolved_inputs": inputs,
            "override_parameters": (
                copy.deepcopy(dict(override_parameters))
                if outcome == "override_default"
                else None
            ),
            "notes": decision_notes,
        },
        "override_reason": override_reason if outcome == "override_default" else None,
        "basis": {
            "origin": "template_suggestion" if template_sha256 else "direct_proposal",
            "template_sha256": template_sha256,
            "template_applicability_explicitly_confirmed": bool(template_sha256),
        },
    }
    return {**body, "record_sha256": _canonical_hash(body)}


def _validate_decision_record(
    queue: Mapping[str, Any], record: Mapping[str, Any]
) -> None:
    expected = {
        "schema_version",
        "record_id",
        "proposal_report_sha256",
        "queue_sha256",
        "group_id",
        "decision_group_sha256",
        "decision_type",
        "reviewer",
        "decided_at",
        "decision",
        "override_reason",
        "basis",
        "record_sha256",
    }
    _exact_keys(record, expected, "decision record")
    _verify_signed_payload(record, "record_sha256", "decision record")
    if record.get("schema_version") != "1.0":
        raise ReviewDecisionError("Unsupported decision record schema")
    groups = {item["group_id"]: item for item in queue["decision_groups"]}
    group_id = record.get("group_id")
    if group_id not in groups:
        raise ReviewDecisionError(f"Decision record references unknown group: {group_id}")
    group = groups[group_id]
    bindings = {
        "proposal_report_sha256": queue["proposal_report_sha256"],
        "queue_sha256": queue["queue_sha256"],
        "decision_group_sha256": group["decision_group_sha256"],
        "decision_type": group["decision_type"],
    }
    for field, expected_value in bindings.items():
        if record.get(field) != expected_value:
            raise ReviewDecisionError(f"Decision record {field} is stale for {group_id}")
    reviewer = record.get("reviewer")
    if not isinstance(reviewer, Mapping) or set(reviewer) != {"role", "identifier"}:
        raise ReviewDecisionError("Malformed decision reviewer")
    if ROLE_RE.fullmatch(str(reviewer.get("role"))) is None or not str(
        reviewer.get("identifier", "")
    ).strip():
        raise ReviewDecisionError("Decision reviewer is not attributable")
    _parse_timestamp(record.get("decided_at"), "decided_at")
    decision = record.get("decision")
    if not isinstance(decision, Mapping) or set(decision) != {
        "outcome",
        "selected_action",
        "resolved_inputs",
        "override_parameters",
        "notes",
    }:
        raise ReviewDecisionError("Malformed decision content")
    outcome = decision.get("outcome")
    if outcome not in group["allowed_outcomes"]:
        raise ReviewDecisionError(f"Invalid outcome for {group_id}")
    if not isinstance(decision.get("resolved_inputs"), Mapping):
        raise ReviewDecisionError("resolved_inputs must be an object")
    required = {
        item["field"] for item in group["required_reviewer_inputs"]
        if item["required_before_confirm"]
    }
    if outcome != "defer":
        missing = sorted(
            field
            for field in required
            if not str(decision["resolved_inputs"].get(field, "")).strip()
        )
        if missing:
            raise ReviewDecisionError(f"Confirmed decision lacks inputs: {missing}")
    if outcome == "confirm_default" and decision.get("selected_action") != group[
        "default_decision"
    ]["action"]:
        raise ReviewDecisionError("confirm_default selected action differs from proposal")
    if outcome == "override_default" and not str(record.get("override_reason") or "").strip():
        raise ReviewDecisionError("Override decision lacks rationale")
    if outcome == "override_default" and (
        not isinstance(decision.get("override_parameters"), Mapping)
        or not decision["override_parameters"]
    ):
        raise ReviewDecisionError("Override decision lacks structured replacement parameters")
    if outcome != "override_default" and record.get("override_reason") is not None:
        raise ReviewDecisionError("Non-override decision cannot carry override_reason")
    if outcome != "override_default" and decision.get("override_parameters") is not None:
        raise ReviewDecisionError("Non-override decision cannot carry override_parameters")
    basis = record.get("basis")
    if not isinstance(basis, Mapping) or set(basis) != {
        "origin",
        "template_sha256",
        "template_applicability_explicitly_confirmed",
    }:
        raise ReviewDecisionError("Malformed decision basis")
    if basis.get("origin") == "template_suggestion":
        if (
            not basis.get("template_applicability_explicitly_confirmed")
            or not isinstance(basis.get("template_sha256"), str)
        ):
            raise ReviewDecisionError("Template decision lacks explicit applicability confirmation")
    elif basis.get("origin") != "direct_proposal" or basis.get("template_sha256") is not None:
        raise ReviewDecisionError("Unknown decision basis")


def build_decision_ledger(
    queue: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    _verify_queue(queue)
    by_group: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ReviewDecisionError("Decision records must be objects")
        _validate_decision_record(queue, record)
        group_id = str(record["group_id"])
        if group_id in by_group:
            raise ReviewDecisionError(f"Duplicate decision for group: {group_id}")
        by_group[group_id] = record
    expected = {item["group_id"] for item in queue["decision_groups"]}
    missing = sorted(expected - set(by_group))
    extra = sorted(set(by_group) - expected)
    deferred = sorted(
        group_id
        for group_id, record in by_group.items()
        if record["decision"]["outcome"] == "defer"
    )
    status = "complete" if not missing and not extra and not deferred else "incomplete"
    body = {
        "schema_version": "1.0",
        "status": status,
        "proposal_report_sha256": queue["proposal_report_sha256"],
        "queue_sha256": queue["queue_sha256"],
        "records": [copy.deepcopy(dict(by_group[item])) for item in sorted(by_group)],
        "coverage": {
            "required_group_count": len(expected),
            "recorded_group_count": len(by_group),
            "missing_group_ids": missing,
            "extra_group_ids": extra,
            "deferred_group_ids": deferred,
        },
    }
    return {**body, "ledger_sha256": _canonical_hash(body)}


def validate_complete_ledger(queue: Mapping[str, Any], ledger: Mapping[str, Any]) -> None:
    _verify_queue(queue)
    if not isinstance(ledger, Mapping) or ledger.get("schema_version") != "1.0":
        raise ReviewDecisionError("Decision ledger must be schema 1.0")
    _verify_signed_payload(ledger, "ledger_sha256", "decision ledger")
    rebuilt = build_decision_ledger(queue, ledger.get("records", []))
    if rebuilt != ledger:
        raise ReviewDecisionError("Decision ledger content drift")
    if ledger.get("status") != "complete":
        raise ReviewDecisionError("All decision groups must be confirmed before freeze")


def freeze_candidate_with_decisions(
    report: Mapping[str, Any],
    candidate: Mapping[str, Any],
    queue: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze a candidate only after every reviewer-centred decision is complete."""

    _verify_proposal_report(report)
    _verify_candidate(candidate)
    _verify_queue(queue)
    validate_complete_ledger(queue, ledger)
    if queue.get("proposal_report_sha256") != report.get("report_sha256"):
        raise ReviewDecisionError("Review queue is bound to another proposal report")
    if queue.get("candidate_sha256") != candidate.get("candidate_sha256"):
        raise ReviewDecisionError("Review queue is bound to another candidate")
    if report.get("candidate_sha256") != candidate.get("candidate_sha256"):
        raise ReviewDecisionError("Proposal report and candidate hashes disagree")
    if any(
        record["decision"]["outcome"] == "defer" for record in ledger["records"]
    ):
        raise ReviewDecisionError("Deferred reviewer decisions block map freeze")

    from .tfl_map import freeze_tfl_map

    latest = max(
        ledger["records"],
        key=lambda item: datetime.fromisoformat(item["decided_at"]),
    )["decided_at"]
    clean_candidate = {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if key != "input_file_sha256"
    }
    frozen = freeze_tfl_map(
        clean_candidate,
        {
            "status": "confirmed",
            "confirmation_id": "M8-REVIEW-" + ledger["ledger_sha256"][:16].upper(),
            "confirmed_by_role": "review_decision_panel",
            "confirmed_at": latest,
            "candidate_sha256": candidate["candidate_sha256"],
        },
    )
    proof_body = {
        "schema_version": "1.0",
        "status": "pass",
        "proposal_report_sha256": report["report_sha256"],
        "queue_sha256": queue["queue_sha256"],
        "ledger_sha256": ledger["ledger_sha256"],
        "candidate_sha256": candidate["candidate_sha256"],
        "frozen_sha256": frozen["frozen_sha256"],
        "confirmed_decision_group_count": len(queue["decision_groups"]),
        "decision_record_sha256": [
            item["record_sha256"] for item in ledger["records"]
        ],
        "audit_records_embedded_in_frozen_map": False,
        "audit_records_bound_by_proof_sha256": True,
    }
    return frozen, {**proof_body, "proof_sha256": _canonical_hash(proof_body)}


def export_decision_template(
    queue: Mapping[str, Any], ledger: Mapping[str, Any], *, template_id: str
) -> dict[str, Any]:
    _verify_queue(queue)
    validate_complete_ledger(queue, ledger)
    if not isinstance(template_id, str) or not template_id.strip():
        raise ReviewDecisionError("template_id must be non-empty")
    records = {item["group_id"]: item for item in ledger["records"]}
    rules = []
    for group in queue["decision_groups"]:
        record = records[group["group_id"]]
        rules.append(
            {
                "rule_id": "TPL-" + group["template_match_signature"][:16].upper(),
                "decision_type": group["decision_type"],
                "template_match_signature": group["template_match_signature"],
                "suggested_decision": copy.deepcopy(record["decision"]),
                "suggested_override_reason": record["override_reason"],
                "source_record_sha256": record["record_sha256"],
                "applicability": "requires_project_specific_confirmation",
            }
        )
    body = {
        "schema_version": "1.0",
        "template_id": template_id,
        "source": {
            "queue_sha256": queue["queue_sha256"],
            "proposal_report_sha256": queue["proposal_report_sha256"],
            "reviewer_identity_exported": False,
            "entity_ids_used_for_matching": False,
        },
        "rules": rules,
        "application_policy": {
            "auto_confirm": False,
            "human_applicability_confirmation_required": True,
            "upstream_structure_hash_must_still_match": True,
        },
    }
    return {**body, "template_sha256": _canonical_hash(body)}


def apply_decision_template(
    queue: Mapping[str, Any], template: Mapping[str, Any]
) -> dict[str, Any]:
    _verify_queue(queue)
    if not isinstance(template, Mapping) or template.get("schema_version") != "1.0":
        raise ReviewDecisionError("Decision template must be schema 1.0")
    _verify_signed_payload(template, "template_sha256", "decision template")
    rules: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for rule in template.get("rules", []):
        if not isinstance(rule, Mapping):
            raise ReviewDecisionError("Malformed template rule")
        rules[str(rule.get("template_match_signature"))].append(rule)
    suggestions = []
    for group in queue["decision_groups"]:
        matches = rules.get(group["template_match_signature"], [])
        if len(matches) > 1:
            raise ReviewDecisionError(
                f"Decision template has ambiguous rules for {group['group_id']}"
            )
        if not matches:
            continue
        rule = matches[0]
        suggestions.append(
            {
                "group_id": group["group_id"],
                "decision_type": group["decision_type"],
                "rule_id": rule["rule_id"],
                "suggested_decision": copy.deepcopy(rule["suggested_decision"]),
                "suggested_override_reason": rule["suggested_override_reason"],
                "origin": "imported_template",
                "applicability_status": "requires_human_confirmation",
                "auto_applied": False,
            }
        )
    body = {
        "schema_version": "1.0",
        "queue_sha256": queue["queue_sha256"],
        "template_sha256": template["template_sha256"],
        "matched_group_count": len(suggestions),
        "unmatched_group_count": len(queue["decision_groups"]) - len(suggestions),
        "suggestions": suggestions,
        "automatic_decision_count": 0,
        "status": "applicability_confirmation_required" if suggestions else "no_match",
    }
    return {**body, "application_sha256": _canonical_hash(body)}


def _json_diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(old) | set(new)):
            child = f"{path}/{key}"
            if key not in old:
                changes.append({"path": child, "change": "added", "old": None, "new": copy.deepcopy(new[key])})
            elif key not in new:
                changes.append({"path": child, "change": "removed", "old": copy.deepcopy(old[key]), "new": None})
            else:
                changes.extend(_json_diff(old[key], new[key], child))
        return changes
    if old != new:
        return [{"path": path or "/", "change": "changed", "old": copy.deepcopy(old), "new": copy.deepcopy(new)}]
    return []


def compare_review_versions(
    previous_queue: Mapping[str, Any],
    previous_ledger: Mapping[str, Any],
    current_queue: Mapping[str, Any],
) -> dict[str, Any]:
    """Reuse unchanged decisions and explicitly invalidate changed dependencies."""

    _verify_queue(previous_queue)
    validate_complete_ledger(previous_queue, previous_ledger)
    _verify_queue(current_queue)
    old_structures = {item["entity_id"]: item for item in previous_queue["entity_structures"]}
    new_structures = {item["entity_id"]: item for item in current_queue["entity_structures"]}
    old_membership = {
        item["entity_id"]: set(item["decision_group_ids"])
        for item in previous_queue["review_entity_membership"]
    }
    new_membership = {
        item["entity_id"]: set(item["decision_group_ids"])
        for item in current_queue["review_entity_membership"]
    }
    records = {item["group_id"]: item for item in previous_ledger["records"]}
    entities = []
    for entity_id in sorted(set(old_structures) | set(new_structures)):
        old = old_structures.get(entity_id)
        new = new_structures.get(entity_id)
        if old is None:
            status = "new_entity_requeued" if new and new["requires_review"] else "new_entity_no_review"
            differences = [{"path": "/entity", "change": "added", "old": None, "new": entity_id}]
        elif new is None:
            status = "removed_entity_prior_decision_retired"
            differences = [{"path": "/entity", "change": "removed", "old": entity_id, "new": None}]
        elif old["own_structure_sha256"] != new["own_structure_sha256"]:
            status = "changed_requeued"
            differences = _json_diff(old["own_structure"], new["own_structure"], "/own_structure")
        elif old["dependency_sha256"] != new["dependency_sha256"]:
            status = "upstream_change_invalidated_reconfirmation"
            differences = _json_diff(old["dependencies"], new["dependencies"], "/dependencies")
        elif old["requires_review"] and not new["requires_review"]:
            status = "unchanged_no_longer_requires_review"
            differences = []
        elif not old["requires_review"] and new["requires_review"]:
            status = "unchanged_newly_requeued"
            differences = []
        else:
            status = "unchanged_decision_reused" if new["requires_review"] else "unchanged_no_review"
            differences = []
        prior_group_ids = sorted(old_membership.get(entity_id, set()))
        reusable = []
        if status == "unchanged_decision_reused":
            reusable = [
                records[group_id]["record_id"]
                for group_id in prior_group_ids
                if group_id in records
            ]
        entities.append(
            {
                "entity_id": entity_id,
                "status": status,
                "previous_decision_group_ids": prior_group_ids,
                "current_decision_group_ids": sorted(new_membership.get(entity_id, set())),
                "reused_prior_record_ids": reusable,
                "differences": differences,
            }
        )
    counts: defaultdict[str, int] = defaultdict(int)
    for item in entities:
        counts[item["status"]] += 1

    group_reuse = []
    old_groups = {item["group_id"]: item for item in previous_queue["decision_groups"]}
    for group in current_queue["decision_groups"]:
        old = old_groups.get(group["group_id"])
        record = records.get(group["group_id"])
        if (
            old is not None
            and record is not None
            and old["decision_group_sha256"] == group["decision_group_sha256"]
        ):
            state = "decision_record_reused"
        else:
            state = "requires_reconfirmation"
        group_reuse.append(
            {
                "group_id": group["group_id"],
                "status": state,
                "prior_record_id": record["record_id"] if record else None,
                "previous_group_sha256": old["decision_group_sha256"] if old else None,
                "current_group_sha256": group["decision_group_sha256"],
            }
        )
    body = {
        "schema_version": "1.0",
        "previous_queue_sha256": previous_queue["queue_sha256"],
        "previous_ledger_sha256": previous_ledger["ledger_sha256"],
        "current_queue_sha256": current_queue["queue_sha256"],
        "status": "review_required" if any(
            item["status"] in {"changed_requeued", "upstream_change_invalidated_reconfirmation", "new_entity_requeued", "unchanged_newly_requeued"}
            for item in entities
        ) else "all_applicable_decisions_reused",
        "entity_status_counts": dict(sorted(counts.items())),
        "entities": entities,
        "decision_groups": group_reuse,
        "reuse_policy": {
            "unchanged_effective_structure": "reuse prior attributable decision",
            "own_structure_changed": "requeue with field-level diff",
            "upstream_dependency_changed": "invalidate and explicitly reconfirm",
            "silent_carry_forward": False,
        },
    }
    return {**body, "comparison_sha256": _canonical_hash(body)}


def render_version_comparison(report: Mapping[str, Any]) -> str:
    if not isinstance(report, Mapping):
        raise ReviewDecisionError("Version comparison must be an object")
    _verify_signed_payload(report, "comparison_sha256", "version comparison")
    labels = {
        "unchanged_decision_reused": "未变，沿用已有决定",
        "changed_requeued": "结构变化，重新入队",
        "upstream_change_invalidated_reconfirmation": "上游变化，原决定失效并重新确认",
        "new_entity_requeued": "新增实体，进入队列",
        "removed_entity_prior_decision_retired": "实体移除，旧决定退休",
        "unchanged_no_review": "未变且无需审阅",
        "new_entity_no_review": "新增但无需审阅",
        "unchanged_no_longer_requires_review": "未变且新版不再要求审阅",
        "unchanged_newly_requeued": "结构未变但新版策略要求审阅",
    }
    lines = ["# TFL shell 跨版本决策复用", ""]

    def compact(value: Any) -> str:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(rendered) <= 180:
            return rendered
        return rendered[:130] + f"… [sha256:{_canonical_hash(value)[:12]}]"

    for status, count in report["entity_status_counts"].items():
        lines.append(f"- {labels.get(status, status)}：{count}")
    lines.extend(["", "## 需关注的变化", ""])
    changed = [item for item in report["entities"] if item["differences"]]
    if not changed:
        lines.append("无。")
    for item in changed:
        lines.append(f"### {item['entity_id']} · {labels.get(item['status'], item['status'])}")
        lines.append("")
        for difference in item["differences"]:
            lines.append(
                f"- `{difference['path']}`：{difference['change']}；"
                f"上一版 {compact(difference['old'])} → 新版 {compact(difference['new'])}"
            )
        lines.append("")
    lines.append("任何结构或依赖变化都不会静默沿用旧决定。")
    return "\n".join(lines).rstrip() + "\n"
