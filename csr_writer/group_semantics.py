"""M4 deterministic treatment-group semantic validation.

Numeric placeholder guards prove that a value came from a reviewed datapack
cell.  They cannot, by themselves, prove that an experimental-group sentence
was bound to an experimental-group column.  This module closes that gap with a
small, explicit contract and two independent population anchors:

* section 10.1 randomized counts; and
* section 10.3 analysis-set counts (legacy 11.1 anchors remain replay-compatible).

The validator is deliberately fail-closed.  A malformed contract, missing
anchor, unknown binding, or role/column mismatch raises ``GroupSemanticError``
instead of producing a release-looking report.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .datapack import CELL_KEY_RE, validate_datapack
from .errors import CSRWriterError


GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
COLUMN_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]*$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
INTEGER_COUNT_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")

LEGACY_REQUIRED_GROUPS = frozenset({"experimental", "control"})
ANALYSIS_SET_ANCHOR_PREFIXES = ("T10.3", "T11.1")
LEGACY_REQUIRED_ROLE_GROUPS = {
    "experimental_result": "experimental",
    "control_result": "control",
}
CONTRACT_KEYS = frozenset({"schema_version", "groups", "binding_roles"})
GROUP_KEYS = frozenset({"column", "randomized_anchor", "analysis_set_anchor"})


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class GroupSemanticError(CSRWriterError):
    """A treatment-group contract or binding failed closed."""

    def __init__(self, issues: Sequence[Mapping[str, Any]], report: Mapping[str, Any]):
        self.issues = tuple(dict(issue) for issue in issues)
        self.report = dict(report)
        detail = "; ".join(
            f"{issue.get('code', 'E-GROUP')}: {issue.get('message', '')}"
            for issue in self.issues
        )
        super().__init__(detail or "Treatment-group semantic validation failed")


def _blocked(
    code: str,
    message: str,
    *,
    contract: Mapping[str, Any] | None = None,
    datapack: Mapping[str, Any] | None = None,
    **details: Any,
) -> GroupSemanticError:
    issue = {"code": code, "message": message, **details}
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "blocked",
        "issues": [issue],
    }
    if contract is not None:
        report["contract_sha256"] = _canonical_hash(contract)
    if datapack is not None:
        report["datapack_sha256"] = _canonical_hash(datapack)
    return GroupSemanticError([issue], report)


def _require_exact_keys(
    payload: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    observed = set(payload)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(repr(key) for key in observed - expected)
        raise _blocked(
            "E-GROUP-CONTRACT-SHAPE",
            f"{label} keys must be exactly {sorted(expected)}; "
            f"missing={missing}, extra={extra}",
        )


def _parse_binding_key(key: Any, label: str, contract: Mapping[str, Any]) -> re.Match[str]:
    if not isinstance(key, str):
        raise _blocked(
            "E-GROUP-KEY",
            f"{label} must be a semantic datapack key",
            contract=contract,
            value=key,
        )
    match = CELL_KEY_RE.fullmatch(key)
    if match is None:
        raise _blocked(
            "E-GROUP-KEY",
            f"{label} is not a valid semantic datapack key: {key}",
            contract=contract,
            key=key,
        )
    return match


def _normalize_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        raise _blocked("E-GROUP-CONTRACT", "Group contract must be an object")
    _require_exact_keys(contract, CONTRACT_KEYS, "group contract")
    schema_version = contract.get("schema_version")
    if schema_version not in {"1.0", "2.0"}:
        raise _blocked(
            "E-GROUP-CONTRACT-VERSION",
            "Unsupported group contract schema_version",
            contract=contract,
        )

    raw_groups = contract.get("groups")
    if not isinstance(raw_groups, Mapping):
        raise _blocked("E-GROUP-CONTRACT", "groups must be an object", contract=contract)
    group_names = set(raw_groups)
    if schema_version == "1.0" and not LEGACY_REQUIRED_GROUPS.issubset(group_names):
        raise _blocked(
            "E-GROUP-CONTRACT",
            f"groups must include {sorted(LEGACY_REQUIRED_GROUPS)}",
            contract=contract,
            missing_groups=sorted(LEGACY_REQUIRED_GROUPS - group_names),
        )
    if schema_version == "2.0" and len(group_names) < 2:
        raise _blocked(
            "E-GROUP-CONTRACT",
            "A generic group contract needs at least two reviewed groups",
            contract=contract,
        )

    invalid_group_ids = [key for key in raw_groups if not isinstance(key, str)]
    if invalid_group_ids:
        raise _blocked(
            "E-GROUP-ID",
            f"Group ids must be strings: {[repr(key) for key in invalid_group_ids]}",
            contract=contract,
        )

    groups: dict[str, dict[str, str]] = {}
    columns: dict[str, str] = {}
    for group_id in sorted(raw_groups):
        raw_group = raw_groups[group_id]
        if not isinstance(group_id, str) or GROUP_ID_RE.fullmatch(group_id) is None:
            raise _blocked(
                "E-GROUP-ID",
                f"Invalid group id: {group_id!r}",
                contract=contract,
            )
        if not isinstance(raw_group, Mapping):
            raise _blocked(
                "E-GROUP-CONTRACT",
                f"groups.{group_id} must be an object",
                contract=contract,
            )
        _require_exact_keys(raw_group, GROUP_KEYS, f"groups.{group_id}")
        if not all(isinstance(raw_group.get(field), str) for field in GROUP_KEYS):
            raise _blocked(
                "E-GROUP-CONTRACT",
                f"groups.{group_id} fields must be strings",
                contract=contract,
            )
        column = raw_group["column"]
        if COLUMN_ID_RE.fullmatch(column) is None:
            raise _blocked(
                "E-GROUP-COLUMN",
                f"Invalid semantic column for {group_id}: {column!r}",
                contract=contract,
            )
        if column in columns:
            raise _blocked(
                "E-GROUP-COLUMN",
                f"Groups {columns[column]} and {group_id} share semantic column {column}",
                contract=contract,
            )
        columns[column] = group_id
        groups[group_id] = {field: raw_group[field] for field in sorted(GROUP_KEYS)}

    raw_roles = contract.get("binding_roles")
    if not isinstance(raw_roles, Mapping) or not raw_roles:
        raise _blocked(
            "E-GROUP-CONTRACT",
            "binding_roles must be a non-empty role-to-group object",
            contract=contract,
        )
    invalid_roles = [key for key in raw_roles if not isinstance(key, str)]
    if invalid_roles:
        raise _blocked(
            "E-GROUP-ROLE",
            f"Binding roles must be strings: {[repr(key) for key in invalid_roles]}",
            contract=contract,
        )

    roles: dict[str, str] = {}
    for role in sorted(raw_roles):
        group_id = raw_roles[role]
        if not isinstance(role, str) or ROLE_RE.fullmatch(role) is None:
            raise _blocked(
                "E-GROUP-ROLE",
                f"Invalid binding role: {role!r}",
                contract=contract,
            )
        if not isinstance(group_id, str) or group_id not in groups:
            raise _blocked(
                "E-GROUP-ROLE",
                f"Role {role} points to undefined group {group_id!r}",
                contract=contract,
            )
        roles[role] = group_id

    if schema_version == "1.0":
        for role, expected_group in LEGACY_REQUIRED_ROLE_GROUPS.items():
            if roles.get(role) != expected_group:
                raise _blocked(
                    "E-GROUP-ROLE",
                    f"binding_roles.{role} must map to {expected_group}",
                    contract=contract,
                )
        required_result_roles = dict(LEGACY_REQUIRED_ROLE_GROUPS)
    else:
        required_result_roles: dict[str, str] = {}
        for group_id in groups:
            candidates = sorted(
                role
                for role, target in roles.items()
                if target == group_id and role.endswith("_result")
            )
            if not candidates:
                raise _blocked(
                    "E-GROUP-ROLE",
                    f"Group {group_id} must have at least one mapped *_result role",
                    contract=contract,
                )
            for role in candidates:
                required_result_roles[role] = group_id

    return {
        "schema_version": schema_version,
        "groups": groups,
        "binding_roles": roles,
        "required_result_roles": required_result_roles,
    }


def _normalize_metadata(
    section_metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    if isinstance(section_metadata, Mapping):
        raw_items: list[Any] = [section_metadata]
    elif isinstance(section_metadata, Sequence) and not isinstance(
        section_metadata, (str, bytes)
    ):
        raw_items = list(section_metadata)
    else:
        raise _blocked(
            "E-GROUP-METADATA",
            "section_metadata must be an object or a list of objects",
            contract=contract,
        )
    if not raw_items:
        raise _blocked(
            "E-GROUP-METADATA",
            "At least one section metadata object is required",
            contract=contract,
        )

    normalized: list[tuple[str, list[Mapping[str, Any]]]] = []
    section_ids: set[str] = set()
    for index, metadata in enumerate(raw_items):
        if not isinstance(metadata, Mapping):
            raise _blocked(
                "E-GROUP-METADATA",
                f"section_metadata[{index}] must be an object",
                contract=contract,
            )
        section = metadata.get("section") or metadata.get("section_id")
        if not isinstance(section, str) or not section.strip():
            raise _blocked(
                "E-GROUP-METADATA",
                f"section_metadata[{index}] needs section or section_id",
                contract=contract,
            )
        if section in section_ids:
            raise _blocked(
                "E-GROUP-METADATA",
                f"Duplicate section metadata: {section}",
                contract=contract,
            )
        section_ids.add(section)
        bindings = metadata.get("binding_manifest")
        if not isinstance(bindings, list):
            raise _blocked(
                "E-GROUP-METADATA",
                f"Section {section} binding_manifest must be a list",
                contract=contract,
            )
        if any(not isinstance(binding, Mapping) for binding in bindings):
            raise _blocked(
                "E-GROUP-METADATA",
                f"Section {section} contains a non-object binding",
                contract=contract,
            )
        normalized.append((section, list(bindings)))
    return sorted(normalized, key=lambda item: item[0])


def _count_from_anchor(
    datapack: Mapping[str, Any],
    key: str,
    *,
    label: str,
    contract: Mapping[str, Any],
) -> int:
    cells = datapack["cells"]
    if key not in cells:
        raise _blocked(
            "E-GROUP-ANCHOR-MISSING",
            f"Missing {label} cell: {key}",
            contract=contract,
            datapack=datapack,
            key=key,
        )
    cell = cells[key]
    if cell.get("type") != "count":
        raise _blocked(
            "E-GROUP-ANCHOR-TYPE",
            f"{label} must bind a count cell: {key}",
            contract=contract,
            datapack=datapack,
            key=key,
            observed_type=cell.get("type"),
        )
    value = cell.get("v")
    if not isinstance(value, str) or INTEGER_COUNT_RE.fullmatch(value) is None:
        raise _blocked(
            "E-GROUP-ANCHOR-VALUE",
            f"{label} must be a non-negative integer count: {key}",
            contract=contract,
            datapack=datapack,
            key=key,
            observed_value=value,
        )
    return int(value)


def validate_group_semantics(
    datapack: Mapping[str, Any],
    section_metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate group roles against explicit columns and two count anchors.

    Returns a deterministic pass report.  Every finding raises
    :class:`GroupSemanticError`; callers cannot accidentally ignore a blocked
    report and continue assembly.
    """

    normalized_contract = _normalize_contract(contract)
    if not isinstance(datapack, Mapping):
        raise _blocked(
            "E-GROUP-DATAPACK",
            "datapack must be an object",
            contract=normalized_contract,
        )
    validate_datapack(dict(datapack))
    metadata = _normalize_metadata(section_metadata, normalized_contract)

    group_results: dict[str, dict[str, Any]] = {}
    for group_id, group in normalized_contract["groups"].items():
        randomized_match = _parse_binding_key(
            group["randomized_anchor"],
            f"groups.{group_id}.randomized_anchor",
            normalized_contract,
        )
        analysis_match = _parse_binding_key(
            group["analysis_set_anchor"],
            f"groups.{group_id}.analysis_set_anchor",
            normalized_contract,
        )
        if not randomized_match.group("table").startswith("T10.1"):
            raise _blocked(
                "E-GROUP-ANCHOR-SECTION",
                f"{group_id} randomized_anchor must be a section 10.1 anchor",
                contract=normalized_contract,
                datapack=datapack,
                key=group["randomized_anchor"],
            )
        if not analysis_match.group("table").startswith(ANALYSIS_SET_ANCHOR_PREFIXES):
            raise _blocked(
                "E-GROUP-ANCHOR-SECTION",
                f"{group_id} analysis_set_anchor must be a section 10.3 anchor "
                "(legacy 11.1 replay is accepted)",
                contract=normalized_contract,
                datapack=datapack,
                key=group["analysis_set_anchor"],
            )
        anchor_columns = {
            randomized_match.group("col"),
            analysis_match.group("col"),
            group["column"],
        }
        if len(anchor_columns) != 1:
            raise _blocked(
                "E-GROUP-ANCHOR-COLUMN",
                f"{group_id} contract column and 10.1/10.3 anchor columns disagree",
                contract=normalized_contract,
                datapack=datapack,
                columns=sorted(anchor_columns),
            )
        randomized_count = _count_from_anchor(
            datapack,
            group["randomized_anchor"],
            label=f"{group_id} randomized_anchor",
            contract=normalized_contract,
        )
        analysis_count = _count_from_anchor(
            datapack,
            group["analysis_set_anchor"],
            label=f"{group_id} analysis_set_anchor",
            contract=normalized_contract,
        )
        if randomized_count < analysis_count:
            raise _blocked(
                "E-GROUP-COUNT-ORDER",
                f"{group_id} analysis-set count exceeds randomized count",
                contract=normalized_contract,
                datapack=datapack,
                randomized_count=randomized_count,
                analysis_set_count=analysis_count,
            )
        group_results[group_id] = {
            "column": group["column"],
            "randomized_anchor": group["randomized_anchor"],
            "randomized_count": randomized_count,
            "analysis_set_anchor": group["analysis_set_anchor"],
            "analysis_set_count": analysis_count,
        }

    checked_bindings: list[dict[str, str]] = []
    observed_roles: set[str] = set()
    for section, bindings in metadata:
        section_roles: set[str] = set()
        section_keys: set[str] = set()
        for binding_index, binding in enumerate(bindings):
            role = binding.get("role")
            key = binding.get("key")
            source_id = binding.get("source_id")
            if not all(isinstance(value, str) for value in (role, key, source_id)):
                raise _blocked(
                    "E-GROUP-BINDING",
                    f"Section {section} binding {binding_index} needs string role/key/source_id",
                    contract=normalized_contract,
                    datapack=datapack,
                )
            if role in section_roles or key in section_keys:
                raise _blocked(
                    "E-GROUP-BINDING-DUPLICATE",
                    f"Section {section} repeats a binding role or semantic key",
                    contract=normalized_contract,
                    datapack=datapack,
                    section=section,
                    role=role,
                    key=key,
                )
            section_roles.add(role)
            section_keys.add(key)
            match = _parse_binding_key(key, f"Section {section} role {role}", normalized_contract)
            table_id = match.group("table")
            if source_id != table_id:
                raise _blocked(
                    "E-GROUP-BINDING-SOURCE",
                    f"Section {section} role {role} source_id disagrees with key table",
                    contract=normalized_contract,
                    datapack=datapack,
                    source_id=source_id,
                    table_id=table_id,
                )
            if key not in datapack["cells"]:
                raise _blocked(
                    "E-GROUP-BINDING-UNKNOWN",
                    f"Section {section} role {role} references an unknown datapack cell",
                    contract=normalized_contract,
                    datapack=datapack,
                    key=key,
                )

            expected_group = normalized_contract["binding_roles"].get(role)
            group_like_role = any(
                role.startswith(f"{group_id}_")
                for group_id in normalized_contract["groups"]
            )
            if expected_group is None:
                if group_like_role:
                    raise _blocked(
                        "E-GROUP-ROLE-UNMAPPED",
                        f"Group-bearing role {role} is absent from binding_roles",
                        contract=normalized_contract,
                        datapack=datapack,
                        section=section,
                    )
                continue

            observed_roles.add(role)
            expected_column = normalized_contract["groups"][expected_group]["column"]
            observed_column = match.group("col")
            if observed_column != expected_column:
                raise _blocked(
                    "E-GROUP-ROLE-COLUMN",
                    f"Section {section} role {role} is bound to {observed_column}; "
                    f"contract requires {expected_column}",
                    contract=normalized_contract,
                    datapack=datapack,
                    section=section,
                    role=role,
                    expected_group=expected_group,
                    expected_column=expected_column,
                    observed_column=observed_column,
                    key=key,
                )
            checked_bindings.append(
                {
                    "section": section,
                    "role": role,
                    "group": expected_group,
                    "column": observed_column,
                    "key": key,
                }
            )

    required_result_roles = set(normalized_contract["required_result_roles"])
    missing_core_roles = sorted(required_result_roles - observed_roles)
    if missing_core_roles:
        raise _blocked(
            "E-GROUP-ROLE-MISSING",
            f"Section metadata does not exercise required group result roles: {missing_core_roles}",
            contract=normalized_contract,
            datapack=datapack,
            missing_roles=missing_core_roles,
        )

    return {
        "schema_version": normalized_contract["schema_version"],
        "status": "pass",
        "contract_sha256": _canonical_hash(normalized_contract),
        "datapack_sha256": _canonical_hash(datapack),
        "metadata_sha256": _canonical_hash(
            [{"section": section, "binding_manifest": bindings} for section, bindings in metadata]
        ),
        "groups": group_results,
        "sections_checked": [section for section, _ in metadata],
        "binding_count_checked": len(checked_bindings),
        "bindings": sorted(
            checked_bindings,
            key=lambda item: (item["section"], item["role"], item["key"]),
        ),
        "checks": {
            "explicit_role_to_column": "pass",
            "randomized_vs_analysis_set_column": "pass",
            "randomized_count_gte_analysis_set_count": "pass",
        },
        "anchor_section_policy": {
            "randomized": "10.1",
            "analysis_set": "10.3",
            "legacy_analysis_set_replay": "11.1",
        },
        "issues": [],
    }
