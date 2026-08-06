"""Pre-lock TFL shell mapping, human freeze, and post-lock drift gate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .datapack import validate_datapack
from .errors import CSRWriterError, DatapackError
from .semantic_key import parse_semantic_cell_key


E3_SECTION_RE = re.compile(r"^\d+(?:\.(?:\d+|[A-Z]))*$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class TFLMapError(CSRWriterError):
    """A shell mapping or confirmation contract is invalid."""


class TFLMapDriftError(TFLMapError):
    """A result datapack differs structurally from the confirmed shell map."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        super().__init__("E-TFL-MAP-DRIFT: result TFL structure differs from frozen shell map")


def migrate_legacy_unspecified_population_candidate(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Retire legacy UNSPECIFIED keys without guessing a replacement population.

    The returned registry is deliberately not a frozen TFL map.  A retired
    entity must be rebuilt from its source shell and re-confirmed before it can
    re-enter an active mapping.
    """

    if not isinstance(candidate, Mapping) or not isinstance(candidate.get("entities"), list):
        raise TFLMapError("legacy migration input must contain entities")
    before_sha256 = _canonical_hash(candidate)
    active_entities: list[dict[str, Any]] = []
    retired_entities: list[dict[str, Any]] = []
    active_key_count = 0
    retired_key_count = 0
    for raw in candidate["entities"]:
        if not isinstance(raw, Mapping):
            raise TFLMapError("legacy migration entity must be an object")
        entity = copy.deepcopy(dict(raw))
        keys = entity.get("semantic_keys")
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise TFLMapError("legacy migration entity semantic_keys are malformed")
        unspecified = entity.get("population") == "UNSPECIFIED" or any(
            "|POP=UNSPECIFIED|" in key for key in keys
        )
        if not unspecified:
            active_entities.append(entity)
            active_key_count += len(keys)
            continue
        retired_key_count += len(keys)
        retired_entities.append(
            {
                "entity_id": entity.get("entity_id"),
                "source_structure_sha256": entity.get("structure_sha256"),
                "retired_population": entity.get("population"),
                "retired_keys": list(keys),
                "replacement_keys": [],
                "migration_action": "reparse_source_shell_then_human_confirm",
                "status": "needs_reparse_and_human_confirmation",
                "blocking_code": "E-ANALYSIS-SET-UNSPECIFIED-MIGRATION-PENDING",
                "reason": (
                    "The legacy artifact has no reviewed table-level population. "
                    "Row labels alone cannot establish the missing POP dimension."
                ),
            }
        )
    body = {
        "schema_version": "1.0",
        "interface": "csr-writer-tfl-map-migration-v1",
        "source_candidate_sha256": before_sha256,
        "status": "blocked_pending_reparse" if retired_entities else "no_migration_needed",
        "release_eligible": not retired_entities,
        "before": {
            "entity_count": len(candidate["entities"]),
            "semantic_key_count": active_key_count + retired_key_count,
            "unspecified_entity_count": len(retired_entities),
            "unspecified_key_count": retired_key_count,
        },
        "after": {
            "active_entity_count": len(active_entities),
            "active_semantic_key_count": active_key_count,
            "active_unspecified_key_count": sum(
                "|POP=UNSPECIFIED|" in key
                for entity in active_entities
                for key in entity["semantic_keys"]
            ),
            "pending_reparse_entity_count": len(retired_entities),
            "pending_replacement_key_count": 0,
        },
        "active_entities": active_entities,
        "retired_entities": retired_entities,
    }
    return {**body, "migration_sha256": _canonical_hash(body)}


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise TFLMapError(
            f"{label} keys must be exactly {sorted(expected)}; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _table_kind(table_id: str) -> str:
    return {"T": "table", "F": "figure", "L": "listing"}[table_id[0]]


def _cell_contract(key: str, cell: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_semantic_cell_key(key)
    row = cell.get("row")
    column = cell.get("column")
    locator = cell.get("source_locator")
    if not isinstance(row, Mapping) or not isinstance(column, Mapping) or not isinstance(locator, Mapping):
        raise TFLMapError(f"Malformed datapack cell contract: {key}")
    return {
        "key": key,
        "type": cell.get("type"),
        "row_id": parsed.row,
        "row_label": row.get("label"),
        "row_indent": row.get("indent"),
        "row_parent_path": row.get("parent_path"),
        "column_id": parsed.column,
        "column_label": column.get("label"),
        "dimensions": dict(parsed.extra_dimensions),
        "row_index": locator.get("row_index"),
        "column_index": locator.get("column_index"),
    }


def _entity_contract(table: Mapping[str, Any], cells: Mapping[str, Any]) -> dict[str, Any]:
    table_id = table["table_id"]
    semantic_keys = sorted(
        key for key in cells if parse_semantic_cell_key(key).table_id == table_id
    )
    contracts = [_cell_contract(key, cells[key]) for key in semantic_keys]
    layout_reference = copy.deepcopy(table.get("layout_reference"))
    deferred_row_contract = copy.deepcopy(table.get("deferred_row_contract"))
    return {
        "entity_id": table_id,
        "entity_kind": _table_kind(table_id),
        "population": table.get("population"),
        "titles": copy.deepcopy(table.get("titles", [])),
        "ingest_strategy": table.get("ingest_strategy", "cell_level"),
        "dimensions": copy.deepcopy(table.get("dimensions", {})),
        "layout_reference": layout_reference,
        "deferred_row_contract": deferred_row_contract,
        "semantic_keys": semantic_keys,
        "cell_contracts": contracts,
        "structure_sha256": _canonical_hash(
            {
                "population": table.get("population"),
                "titles": table.get("titles", []),
                "ingest_strategy": table.get("ingest_strategy", "cell_level"),
                "dimensions": table.get("dimensions", {}),
                "layout_reference": layout_reference,
                "deferred_row_contract": deferred_row_contract,
                "cell_contracts": contracts,
            }
        ),
    }


def _semantic_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Compare frozen stable rows by semantics, not result-stage row position."""

    return {
        key: copy.deepcopy(contract.get(key))
        for key in (
            "key",
            "type",
            "row_id",
            "row_label",
            "row_indent",
            "row_parent_path",
            "column_id",
            "column_label",
            "dimensions",
        )
    }


def build_tfl_map_candidate(
    shell_datapack: Mapping[str, Any], route_config: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a value-free mapping candidate from a pre-lock TFL shell."""

    if not isinstance(shell_datapack, Mapping) or not isinstance(route_config, Mapping):
        raise TFLMapError("shell_datapack and route_config must be objects")
    validate_datapack(dict(shell_datapack))
    _exact_keys(
        route_config,
        {"schema_version", "map_version", "numbering_system", "routes"},
        "route_config",
    )
    if route_config.get("schema_version") != "1.0":
        raise TFLMapError("Unsupported route_config schema_version")
    map_version = route_config.get("map_version")
    if not isinstance(map_version, str) or not map_version.strip():
        raise TFLMapError("map_version must be a non-empty string")
    numbering = route_config.get("numbering_system")
    if not isinstance(numbering, Mapping):
        raise TFLMapError("numbering_system must be an object")
    _exact_keys(numbering, {"id", "description"}, "numbering_system")
    if not all(isinstance(numbering.get(key), str) and numbering[key].strip() for key in numbering):
        raise TFLMapError("numbering_system fields must be non-empty strings")

    raw_routes = route_config.get("routes")
    if not isinstance(raw_routes, list):
        raise TFLMapError("routes must be a list")
    routes: dict[str, list[str]] = {}
    for index, raw in enumerate(raw_routes):
        if not isinstance(raw, Mapping):
            raise TFLMapError(f"routes[{index}] must be an object")
        _exact_keys(raw, {"entity_id", "ich_e3_sections"}, f"routes[{index}]")
        entity_id = raw.get("entity_id")
        sections = raw.get("ich_e3_sections")
        if not isinstance(entity_id, str) or entity_id in routes:
            raise TFLMapError(f"Invalid or duplicate route entity_id: {entity_id!r}")
        if not isinstance(sections, list) or not sections or not all(
            isinstance(section, str) and E3_SECTION_RE.fullmatch(section) for section in sections
        ):
            raise TFLMapError(f"Invalid ICH E3 section mapping for {entity_id}")
        if len(sections) != len(set(sections)):
            raise TFLMapError(f"Duplicate ICH E3 section mapping for {entity_id}")
        routes[entity_id] = list(sections)

    table_index = {table["table_id"]: table for table in shell_datapack["tables"]}
    if set(routes) != set(table_index):
        raise TFLMapError(
            "Explicit TFL-number routes must exactly cover shell entities; "
            f"missing={sorted(set(table_index) - set(routes))}, "
            f"extra={sorted(set(routes) - set(table_index))}"
        )
    entities: list[dict[str, Any]] = []
    for entity_id in sorted(table_index):
        entity = _entity_contract(table_index[entity_id], shell_datapack["cells"])
        entity["ich_e3_sections"] = routes[entity_id]
        entities.append(entity)

    candidate_body = {
        "schema_version": "1.0",
        "map_version": map_version,
        "status": "awaiting_human_confirmation",
        "numbering_system": dict(numbering),
        "shell_datapack_sha256": _canonical_hash(shell_datapack),
        "entities": entities,
    }
    return {
        **candidate_body,
        "candidate_sha256": _canonical_hash(candidate_body),
    }


def freeze_tfl_map(
    candidate: Mapping[str, Any], confirmation: Mapping[str, Any]
) -> dict[str, Any]:
    """Freeze only an exactly hashed candidate with explicit human confirmation."""

    if not isinstance(candidate, Mapping) or not isinstance(confirmation, Mapping):
        raise TFLMapError("candidate and confirmation must be objects")
    required_candidate = {
        "schema_version",
        "map_version",
        "status",
        "numbering_system",
        "shell_datapack_sha256",
        "entities",
        "candidate_sha256",
    }
    _exact_keys(candidate, required_candidate, "candidate")
    body = {key: copy.deepcopy(value) for key, value in candidate.items() if key != "candidate_sha256"}
    if candidate.get("schema_version") != "1.0" or candidate.get("status") != "awaiting_human_confirmation":
        raise TFLMapError("Only a schema 1.0 awaiting-confirmation candidate can be frozen")
    if candidate.get("candidate_sha256") != _canonical_hash(body):
        raise TFLMapError("Candidate hash mismatch; regenerate and reconfirm")
    _exact_keys(
        confirmation,
        {"status", "confirmation_id", "confirmed_by_role", "confirmed_at", "candidate_sha256"},
        "confirmation",
    )
    if confirmation.get("status") != "confirmed":
        raise TFLMapError("Human confirmation status must be confirmed")
    if confirmation.get("candidate_sha256") != candidate["candidate_sha256"]:
        raise TFLMapError("Human confirmation is bound to a different candidate hash")
    for field in ("confirmation_id", "confirmed_by_role", "confirmed_at"):
        if not isinstance(confirmation.get(field), str) or not confirmation[field].strip():
            raise TFLMapError(f"confirmation.{field} must be a non-empty string")
    if ROLE_RE.fullmatch(confirmation["confirmed_by_role"]) is None:
        raise TFLMapError("confirmed_by_role must be a role id, not a reviewer name")
    frozen_body = {
        **copy.deepcopy(body),
        "status": "frozen",
        "confirmation": dict(confirmation),
    }
    return {**frozen_body, "frozen_sha256": _canonical_hash(frozen_body)}


def _validate_frozen_map(frozen: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "map_version",
        "status",
        "numbering_system",
        "shell_datapack_sha256",
        "entities",
        "confirmation",
        "frozen_sha256",
    }
    _exact_keys(frozen, expected, "frozen_map")
    if frozen.get("schema_version") != "1.0" or frozen.get("status") != "frozen":
        raise TFLMapError("TFL map is not frozen")
    body = {key: copy.deepcopy(value) for key, value in frozen.items() if key != "frozen_sha256"}
    if frozen.get("frozen_sha256") != _canonical_hash(body):
        raise TFLMapError("Frozen map hash mismatch")


def check_result_against_frozen_map(
    result_datapack: Mapping[str, Any], frozen: Mapping[str, Any]
) -> dict[str, Any]:
    """Accept value changes only; every semantic structure drift hard-fails."""

    if not isinstance(result_datapack, Mapping) or not isinstance(frozen, Mapping):
        raise TFLMapError("result_datapack and frozen map must be objects")
    _validate_frozen_map(frozen)
    try:
        validate_datapack(dict(result_datapack))
    except DatapackError as exc:
        raise TFLMapDriftError(
            {
                "schema_version": "1.0",
                "status": "blocked",
                "map_version": frozen["map_version"],
                "frozen_map_sha256": frozen["frozen_sha256"],
                "checks": {
                    "entity_set": "unknown",
                    "semantic_key_set": "fail",
                    "cell_and_table_contracts": "fail",
                    "deferred_rows_materialized_against_frozen_columns": "fail",
                    "values_may_change": "pass",
                },
                "late_bound_row_entities": [],
                "drift": {"missing_entities": [], "extra_entities": [], "changed_entities": []},
                "issues": [
                    {
                        "code": "E-TFL-MAP-DRIFT",
                        "message": str(exc),
                    }
                ],
            }
        ) from exc
    frozen_entities = {item["entity_id"]: item for item in frozen["entities"]}
    result_tables = {item["table_id"]: item for item in result_datapack["tables"]}
    result_entities = {
        entity_id: _entity_contract(table, result_datapack["cells"])
        for entity_id, table in result_tables.items()
    }
    missing_entities = sorted(set(frozen_entities) - set(result_entities))
    extra_entities = sorted(set(result_entities) - set(frozen_entities))
    changed_entities: list[dict[str, Any]] = []
    late_bound_row_entities: list[dict[str, Any]] = []
    for entity_id in sorted(set(frozen_entities) & set(result_entities)):
        expected = {
            key: frozen_entities[entity_id][key]
            for key in (
                "entity_kind",
                "population",
                "titles",
                "ingest_strategy",
                "dimensions",
                "layout_reference",
                "deferred_row_contract",
                "semantic_keys",
                "cell_contracts",
                "structure_sha256",
            )
        }
        observed = {
            key: result_entities[entity_id][key]
            for key in (
                "entity_kind",
                "population",
                "titles",
                "ingest_strategy",
                "dimensions",
                "layout_reference",
                "deferred_row_contract",
                "semantic_keys",
                "cell_contracts",
                "structure_sha256",
            )
        }
        if expected["ingest_strategy"] in {
            "columns_only_deferred_rows",
            "mixed_stable_and_deferred_rows",
        }:
            fixed_fields = (
                "entity_kind",
                "population",
                "titles",
                "ingest_strategy",
                "dimensions",
                "layout_reference",
                "deferred_row_contract",
            )
            fixed_changed = any(expected[key] != observed[key] for key in fixed_fields)
            expected_columns = set(
                (expected.get("deferred_row_contract") or {}).get(
                    "frozen_column_ids", []
                )
            )
            observed_columns = {
                item["column_id"] for item in observed["cell_contracts"]
            }
            column_changed = bool(observed_columns) and observed_columns != expected_columns
            expected_contracts = {
                item["key"]: _semantic_contract(item)
                for item in expected["cell_contracts"]
            }
            observed_contracts = {
                item["key"]: _semantic_contract(item)
                for item in observed["cell_contracts"]
            }
            missing_stable = sorted(set(expected_contracts) - set(observed_contracts))
            changed_stable = sorted(
                key
                for key in set(expected_contracts) & set(observed_contracts)
                if expected_contracts[key] != observed_contracts[key]
            )
            if fixed_changed or column_changed or missing_stable or changed_stable:
                changed_entities.append(
                    {
                        "entity_id": entity_id,
                        "missing_keys": missing_stable,
                        "extra_keys": sorted(
                            set(observed["semantic_keys"]) - set(expected_contracts)
                        ),
                        "changed_cell_contract_keys": changed_stable,
                        "table_contract_changed": fixed_changed,
                        "deferred_row_column_contract_changed": column_changed,
                    }
                )
            elif len(observed["cell_contracts"]) > len(expected_contracts):
                late_bound_row_entities.append(
                    {
                        "entity_id": entity_id,
                        "row_count": len(
                            {item["row_id"] for item in observed["cell_contracts"]}
                        ),
                        "column_count": len(observed_columns),
                        "semantic_cell_count": len(observed["cell_contracts"]),
                        "stable_semantic_cell_count": len(expected_contracts),
                        "late_bound_semantic_cell_count": max(
                            len(observed["cell_contracts"]) - len(expected_contracts), 0
                        ),
                    }
                )
            continue
        expected_keys = set(expected["semantic_keys"])
        observed_keys = set(observed["semantic_keys"])
        expected_contracts = {
            item["key"]: _semantic_contract(item) for item in expected["cell_contracts"]
        }
        observed_contracts = {
            item["key"]: _semantic_contract(item) for item in observed["cell_contracts"]
        }
        changed_keys = sorted(
            key
            for key in expected_keys & observed_keys
            if expected_contracts[key] != observed_contracts[key]
        )
        fixed_changed = any(
            expected[key] != observed[key]
            for key in (
                "entity_kind",
                "population",
                "titles",
                "ingest_strategy",
                "dimensions",
                "layout_reference",
                "deferred_row_contract",
            )
        )
        if expected_keys != observed_keys or changed_keys or fixed_changed:
            changed_entities.append(
                {
                    "entity_id": entity_id,
                    "missing_keys": sorted(expected_keys - observed_keys),
                    "extra_keys": sorted(observed_keys - expected_keys),
                    "changed_cell_contract_keys": changed_keys,
                    "table_contract_changed": fixed_changed,
                }
            )
    report = {
        "schema_version": "1.0",
        "status": "blocked" if missing_entities or extra_entities or changed_entities else "pass",
        "map_version": frozen["map_version"],
        "frozen_map_sha256": frozen["frozen_sha256"],
        "result_datapack_sha256": _canonical_hash(result_datapack),
        "checks": {
            "entity_set": "fail" if missing_entities or extra_entities else "pass",
            "semantic_key_set": "fail" if changed_entities else "pass",
            "cell_and_table_contracts": "fail" if changed_entities else "pass",
            "deferred_rows_materialized_against_frozen_columns": "pass",
            "values_may_change": "pass",
        },
        "late_bound_row_entities": late_bound_row_entities,
        "drift": {
            "missing_entities": missing_entities,
            "extra_entities": extra_entities,
            "changed_entities": changed_entities,
        },
        "issues": [],
    }
    if report["status"] != "pass":
        report["issues"] = [
            {
                "code": "E-TFL-MAP-DRIFT",
                "message": "Result TFL semantic structure differs from the confirmed shell map",
            }
        ]
        raise TFLMapDriftError(report)
    return report
