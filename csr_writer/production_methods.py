"""Versioned ownership registry for every routed CSR authoring unit.

This module answers one question before any authoring starts: which mechanism
is allowed to produce the unit?  It deliberately keeps that policy outside the
section writer so a playbook file cannot accidentally grant LLM access.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .io_utils import load_yaml, sha256_file
from .paths import DEFAULT_METHOD_EXTRACTION_CONTRACTS, DEFAULT_PRODUCTION_METHODS


ALLOWED_PRODUCTION_METHODS = frozenset(
    {
        "deterministic_generation",
        "extraction",
        "template_slot",
        "llm_drafting",
        "mixed",
    }
)


class ProductionMethodError(ConfigurationError):
    """A unit has no unique approved producer or entered the wrong pipeline."""


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductionMethodError(f"{label} must be an object")
    return value


def _require_string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ProductionMethodError(f"{label} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ProductionMethodError(f"{label} must not be empty")
    return list(value)


def load_method_extraction_contracts(
    path: str | Path = DEFAULT_METHOD_EXTRACTION_CONTRACTS,
) -> dict[str, Any]:
    """Load and strictly validate the 32 method-section extraction contracts."""

    source = Path(path)
    payload = _require_mapping(load_yaml(source), "method extraction registry")
    if payload.get("schema_version") != "1.0":
        raise ProductionMethodError("Unsupported method extraction schema_version")
    policy = _require_mapping(payload.get("policy"), "method extraction policy")
    expected_count = policy.get("contract_count")
    if not isinstance(expected_count, int) or expected_count <= 0:
        raise ProductionMethodError("method extraction contract_count must be positive")
    defaults = _require_mapping(payload.get("defaults"), "method extraction defaults")
    narration_by_section = _require_mapping(
        policy.get("narration_by_section"), "policy.narration_by_section"
    )
    narration_lookup: dict[str, str] = {}
    for narration, raw_ids in narration_by_section.items():
        if narration not in {"verbatim_list", "template_wrap", "constrained_llm"}:
            raise ProductionMethodError(f"unsupported extraction narration: {narration}")
        for section_id in _require_string_list(
            raw_ids, f"policy.narration_by_section.{narration}"
        ):
            if section_id in narration_lookup:
                raise ProductionMethodError(
                    f"duplicate extraction narration assignment: {section_id}"
                )
            narration_lookup[section_id] = narration
    _require_string_list(
        defaults.get("summary_constraints"), "defaults.summary_constraints"
    )
    _require_string_list(
        defaults.get("validation_required"), "defaults.validation_required"
    )
    raw_contracts = payload.get("contracts")
    if not isinstance(raw_contracts, list):
        raise ProductionMethodError("method extraction contracts must be a list")

    contracts: dict[str, dict[str, Any]] = {}
    required = {
        "section_id",
        "title",
        "sources",
        "verbatim_elements",
        "summarizable_elements",
        "validation",
    }
    for index, raw in enumerate(raw_contracts):
        contract = _require_mapping(raw, f"contracts[{index}]")
        missing = required - set(contract)
        extra = set(contract) - required
        if missing or extra:
            raise ProductionMethodError(
                f"contracts[{index}] drift: missing={sorted(missing)}, extra={sorted(extra)}"
            )
        section_id = contract.get("section_id")
        title = contract.get("title")
        if not isinstance(section_id, str) or not section_id or not isinstance(title, str) or not title:
            raise ProductionMethodError(f"contracts[{index}] needs section_id and title")
        if section_id in contracts:
            raise ProductionMethodError(f"duplicate extraction contract: {section_id}")
        sources = contract.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ProductionMethodError(f"contract {section_id} needs at least one source")
        normalized_sources: list[dict[str, str]] = []
        source_keys = {"file_role", "locator_key", "canonical_locator"}
        for source_index, item in enumerate(sources):
            source_item = _require_mapping(
                item, f"contract {section_id}.sources[{source_index}]"
            )
            if set(source_item) != source_keys or not all(
                isinstance(source_item.get(key), str) and source_item[key].strip()
                for key in source_keys
            ):
                raise ProductionMethodError(
                    f"contract {section_id}.sources[{source_index}] contract drift"
                )
            normalized_sources.append(dict(source_item))
        verbatim = _require_string_list(
            contract.get("verbatim_elements"),
            f"contract {section_id}.verbatim_elements",
        )
        summarizable = _require_string_list(
            contract.get("summarizable_elements"),
            f"contract {section_id}.summarizable_elements",
            allow_empty=True,
        )
        validation = _require_mapping(
            contract.get("validation"), f"contract {section_id}.validation"
        )
        if set(validation) != {"keyword_groups", "item_count_rule", "cross_checks"}:
            raise ProductionMethodError(f"contract {section_id}.validation contract drift")
        keyword_groups = validation.get("keyword_groups")
        if not isinstance(keyword_groups, list) or not keyword_groups or not all(
            isinstance(group, list)
            and group
            and all(isinstance(item, str) and item for item in group)
            for group in keyword_groups
        ):
            raise ProductionMethodError(f"contract {section_id} needs keyword groups")
        if not isinstance(validation.get("item_count_rule"), str) or not validation[
            "item_count_rule"
        ].strip():
            raise ProductionMethodError(f"contract {section_id} needs item_count_rule")
        cross_checks = _require_string_list(
            validation.get("cross_checks"), f"contract {section_id}.cross_checks"
        )
        contracts[section_id] = {
            "section_id": section_id,
            "title": title,
            "sources": normalized_sources,
            "verbatim_elements": verbatim,
            "summarizable_elements": summarizable,
            "summary_constraints": list(defaults["summary_constraints"]),
            "validation": {
                "keyword_groups": copy.deepcopy(keyword_groups),
                "item_count_rule": validation["item_count_rule"],
                "cross_checks": cross_checks,
                "required_evidence": list(defaults["validation_required"]),
            },
            "human_confirmation_required": True,
            "llm_rewriting": (
                "constrained_only"
                if narration_lookup.get(section_id) == "constrained_llm"
                else "forbidden"
            ),
            "narration": narration_lookup.get(section_id),
            "fact_card_fields": [
                {
                    "field_id": f"V{field_index:02d}",
                    "label": label,
                    "required": True,
                    "verbatim_required": True,
                    "back_reference": "exact_string",
                }
                for field_index, label in enumerate(verbatim, start=1)
            ]
            + [
                {
                    "field_id": f"S{field_index:02d}",
                    "label": label,
                    "required": True,
                    "verbatim_required": False,
                    "back_reference": "required_terms",
                }
                for field_index, label in enumerate(summarizable, start=1)
            ],
        }
    if len(contracts) != expected_count:
        raise ProductionMethodError(
            f"method extraction contract count {len(contracts)} != {expected_count}"
        )
    missing_narration = sorted(set(contracts) - set(narration_lookup))
    extra_narration = sorted(set(narration_lookup) - set(contracts))
    if missing_narration or extra_narration:
        raise ProductionMethodError(
            "extraction narration coverage drift: "
            f"missing={missing_narration}, extra={extra_narration}"
        )
    return {
        "schema_version": "1.0",
        "config_path": str(source.resolve()),
        "config_sha256": sha256_file(source),
        "policy": dict(policy),
        "contracts": contracts,
    }


def load_production_method_registry(
    path: str | Path = DEFAULT_PRODUCTION_METHODS,
    *,
    extraction_contracts_path: str | Path = DEFAULT_METHOD_EXTRACTION_CONTRACTS,
) -> dict[str, Any]:
    """Expand profile assignments into a unique per-section registry."""

    source = Path(path)
    payload = _require_mapping(load_yaml(source), "production method registry")
    if payload.get("schema_version") != "1.0":
        raise ProductionMethodError("Unsupported production method schema_version")
    policy = _require_mapping(payload.get("policy"), "production method policy")
    declared_methods = set(
        _require_string_list(policy.get("allowed_methods"), "policy.allowed_methods")
    )
    if declared_methods != ALLOWED_PRODUCTION_METHODS:
        raise ProductionMethodError(
            "production method enum drift: "
            f"expected={sorted(ALLOWED_PRODUCTION_METHODS)}, observed={sorted(declared_methods)}"
        )
    profiles = _require_mapping(payload.get("profiles"), "production method profiles")
    normalized_profiles: dict[str, dict[str, Any]] = {}
    for profile_id, raw in profiles.items():
        if not isinstance(profile_id, str) or not profile_id:
            raise ProductionMethodError("production profile IDs must be strings")
        profile = _require_mapping(raw, f"profile {profile_id}")
        if set(profile) != {"production_method", "reason", "spec"}:
            raise ProductionMethodError(f"profile {profile_id} contract drift")
        method = profile.get("production_method")
        reason = profile.get("reason")
        spec = _require_mapping(profile.get("spec"), f"profile {profile_id}.spec")
        if method not in ALLOWED_PRODUCTION_METHODS:
            raise ProductionMethodError(f"profile {profile_id} has invalid method {method!r}")
        if not isinstance(reason, str) or not reason.strip() or not spec:
            raise ProductionMethodError(f"profile {profile_id} needs reason and spec")
        normalized_profiles[profile_id] = {
            "production_method": method,
            "production_reason": reason,
            "production_spec": copy.deepcopy(dict(spec)),
        }

    assignments: dict[str, dict[str, Any]] = {}
    raw_assignments = payload.get("assignments")
    if not isinstance(raw_assignments, list) or not raw_assignments:
        raise ProductionMethodError("production assignments must be a non-empty list")
    extraction_contracts = load_method_extraction_contracts(extraction_contracts_path)
    for index, raw in enumerate(raw_assignments):
        item = _require_mapping(raw, f"assignments[{index}]")
        if set(item) != {"profile", "section_ids"}:
            raise ProductionMethodError(f"assignments[{index}] contract drift")
        profile_id = item.get("profile")
        if profile_id not in normalized_profiles:
            raise ProductionMethodError(f"assignments[{index}] has unknown profile {profile_id!r}")
        section_ids = _require_string_list(
            item.get("section_ids"), f"assignments[{index}].section_ids"
        )
        for section_id in section_ids:
            if section_id in assignments:
                raise ProductionMethodError(f"duplicate production assignment: {section_id}")
            assignment = copy.deepcopy(normalized_profiles[profile_id])
            assignment.update(
                {
                    "section_id": section_id,
                    "production_profile": profile_id,
                }
            )
            if assignment["production_spec"].get("contract_registry") == "method-extraction-contracts.yaml":
                contract = extraction_contracts["contracts"].get(section_id)
                if contract is None:
                    raise ProductionMethodError(
                        f"method extraction unit {section_id} lacks an extraction contract"
                    )
                assignment["production_spec"]["contract_id"] = section_id
                assignment["production_spec"]["contract"] = copy.deepcopy(contract)
                assignment["narration"] = contract["narration"]
            else:
                assignment["narration"] = {
                    "title_page": "template_wrap",
                    "synopsis": "deterministic_copy",
                    "body_toc": "deterministic_generation",
                    "body_table_toc": "deterministic_generation",
                    "body_figure_toc": "deterministic_generation",
                    "chapter14_catalog": "deterministic_generation",
                    "appendix_catalog": "deterministic_generation",
                    "support_report_catalog": "deterministic_generation",
                    "abbreviations": "verbatim_list",
                    "ethics_committee": "template_wrap",
                    "ethical_conduct": "template_wrap",
                    "informed_consent": "template_wrap",
                    "investigator_management": "constrained_llm",
                    "introduction": "constrained_llm",
                    "objectives": "verbatim_list",
                    "guarded_result_drafting": "guarded_section_writer",
                    "individual_listing_reference": "deterministic_generation",
                    "sae_narrative_fact_card": "constrained_llm",
                    "chapter14_material": "deterministic_generation",
                    "reference_list": "deterministic_generation",
                    "appendix_entry": "deterministic_generation",
                }[profile_id]
            spec = assignment["production_spec"]
            source_values = (
                spec.get("data_sources")
                or spec.get("source_files")
                or spec.get("slots")
                or [spec.get("producer")]
            )
            assignment["source"] = {
                "declared": True,
                "items": copy.deepcopy(list(source_values)),
                "missing_source_policy": spec.get(
                    "missing_source_policy", spec.get("unresolved_locator_policy", "hard_fail")
                ),
            }
            assignments[section_id] = assignment

    contract_ids = set(extraction_contracts["contracts"])
    assigned_contract_ids = {
        section_id
        for section_id, item in assignments.items()
        if item["production_spec"].get("contract_registry") == "method-extraction-contracts.yaml"
    }
    if assigned_contract_ids != contract_ids:
        raise ProductionMethodError(
            "method extraction assignment drift: "
            f"missing={sorted(contract_ids - assigned_contract_ids)}, "
            f"extra={sorted(assigned_contract_ids - contract_ids)}"
        )

    runtime_aliases = set(
        _require_string_list(
            payload.get("runtime_llm_aliases"), "runtime_llm_aliases", allow_empty=True
        )
    )
    overlap = runtime_aliases & set(assignments)
    if overlap:
        raise ProductionMethodError(
            f"runtime aliases cannot bypass assigned authoring units: {sorted(overlap)}"
        )
    counts = Counter(item["production_method"] for item in assignments.values())
    return {
        "schema_version": "1.0",
        "config_path": str(source.resolve()),
        "config_sha256": sha256_file(source),
        "extraction_contract_config_sha256": extraction_contracts["config_sha256"],
        "policy": dict(policy),
        "profiles": normalized_profiles,
        "assignments": assignments,
        "runtime_llm_aliases": sorted(runtime_aliases),
        "production_method_counts": dict(sorted(counts.items())),
    }


def production_assignment(
    section_id: str,
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of one explicit authoring-unit assignment."""

    loaded = registry or load_production_method_registry()
    assignments = _require_mapping(loaded.get("assignments"), "registry.assignments")
    assignment = assignments.get(section_id)
    if not isinstance(assignment, Mapping):
        raise ProductionMethodError(
            f"E-PRODUCTION-METHOD-UNREGISTERED: routed authoring unit {section_id!r} "
            "has no approved production method"
        )
    return copy.deepcopy(dict(assignment))


def assert_llm_drafting_allowed(
    section_id: str,
    registry: Mapping[str, Any] | None = None,
    *,
    evidence_status: str | None = None,
) -> dict[str, Any]:
    """Block every assigned non-LLM unit before an envelope can be emitted."""

    loaded = registry or load_production_method_registry()
    assignments = _require_mapping(loaded.get("assignments"), "registry.assignments")
    assignment = assignments.get(section_id)
    if isinstance(assignment, Mapping):
        if assignment.get("production_method") != "llm_drafting" or assignment.get(
            "narration"
        ) != "guarded_section_writer":
            raise ProductionMethodError(
                "E-PRODUCTION-METHOD-WRITER-BLOCK: "
                f"section {section_id} is assigned to {assignment.get('production_method')}/"
                f"{assignment.get('narration')}; "
                "it must not enter the section-writer pipeline"
            )
        result = copy.deepcopy(dict(assignment))
        result["runtime_alias_only"] = False
        result["release_scope"] = "registered_authoring_unit"
        return result
    if section_id in set(loaded.get("runtime_llm_aliases", [])):
        allowed_alias_statuses = {"demo_only", "source_gap"}
        if evidence_status is not None and evidence_status not in allowed_alias_statuses:
            raise ProductionMethodError(
                "E-PRODUCTION-METHOD-RUNTIME-ALIAS-SCOPE: runtime aliases are "
                "restricted to demo_only historical replay or source_gap output"
            )
        release_scope = (
            "source_gap_only" if evidence_status == "source_gap" else "demo_only"
        )
        return {
            "section_id": section_id,
            "production_method": "llm_drafting",
            "production_profile": "runtime_alias_only",
            "production_reason": (
                "历史证据重放或容器级运行别名；不属于当前 120 个独立起草单元。"
            ),
            "production_spec": {"authoring_unit": False},
            "narration": "guarded_section_writer",
            "source": {"declared": True, "items": ["historical_evidence_replay"]},
            "runtime_alias_only": True,
            "release_scope": release_scope,
        }
    raise ProductionMethodError(
        f"E-PRODUCTION-METHOD-UNREGISTERED: writer section {section_id!r} is not registered"
    )


def audit_authoring_unit_coverage(
    section_ids: list[str],
    registry: Mapping[str, Any] | None = None,
    *,
    routed_subset: bool = False,
) -> dict[str, Any]:
    """Audit active ownership, optionally allowing unused entries in the global registry."""

    loaded = registry or load_production_method_registry()
    assignments = _require_mapping(loaded.get("assignments"), "registry.assignments")
    observed = list(section_ids)
    duplicate_units = sorted(
        section_id for section_id, count in Counter(observed).items() if count > 1
    )
    missing = sorted(set(observed) - set(assignments))
    extra = sorted(set(assignments) - set(observed))
    methods = Counter(
        assignments[section_id]["production_method"]
        for section_id in observed
        if section_id in assignments
    )
    passed = not duplicate_units and not missing and (routed_subset or not extra)
    return {
        "schema_version": "1.0",
        "status": "pass" if passed else "blocked",
        "authoring_unit_count": len(observed),
        "registered_assignment_count": len(assignments),
        "missing_assignments": missing,
        "unexpected_assignments": [] if routed_subset else extra,
        "inactive_assignments": extra if routed_subset else [],
        "duplicate_units": duplicate_units,
        "production_method_counts": dict(sorted(methods.items())),
        "config_sha256": loaded.get("config_sha256"),
        "extraction_contract_config_sha256": loaded.get(
            "extraction_contract_config_sha256"
        ),
    }
