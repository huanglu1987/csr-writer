"""M2 reviewed semantic mapping and deterministic datapack construction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import DatapackError, SemanticMappingError
from .io_utils import atomic_write_json_bundle, load_data, sha256_file
from .physical_table import PhysicalTableError
from .rtf_parser import ADAPTER_ID, ParsedTable, parse_rtf_table
from .row_semantics import row_id_is_semantic
from .semantic_key import (
    CELL_KEY_PATTERN,
    SemanticKeyError,
    build_semantic_cell_key,
    parse_semantic_cell_key,
    validate_dimension_name,
    validate_dimension_value,
    validate_table_id,
)


ALLOWED_CELL_TYPES = frozenset(
    {
        "unit",
        "count",
        "n_pct",
        "mean_sd",
        "geomean_cv",
        "median_range",
        "ratio",
        "difference",
        "ci95",
        "pvalue",
        "statistic",
        "text",
        "date",
        "text_judgement",
        "composite",
        "status",
    }
)
# Compatibility name retained for M2-M4 callers that use
# ``CELL_KEY_RE.fullmatch(...).group('table'|'pop'|'row'|'col')``.
CELL_KEY_RE = CELL_KEY_PATTERN
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_INGEST_STRATEGIES = frozenset(
    {
        "cell_level",
        "metadata_only",
        "columns_only_deferred_rows",
        "mixed_stable_and_deferred_rows",
    }
)

DEFERRED_ROW_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "detection",
        "materialize_at",
        "shell_placeholder_row_count",
        "template_placeholder_row_count",
        "stable_row_count",
        "frozen_column_ids",
        "expected_semantic_source",
    }
)


def _validate_table_id(value: Any) -> str:
    try:
        return validate_table_id(value)
    except SemanticKeyError as exc:
        raise SemanticMappingError(f"Invalid table_id: {value!r}: {exc}") from exc


def _validate_dimension_value(value: Any, name: str, label: str) -> str:
    try:
        return validate_dimension_value(value, name)
    except SemanticKeyError as exc:
        raise SemanticMappingError(f"Invalid {label}: {value!r}: {exc}") from exc


def _validate_extra_dimensions(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SemanticMappingError("dimensions must be a key/value object")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        try:
            name = validate_dimension_name(raw_name)
        except SemanticKeyError as exc:
            raise SemanticMappingError(f"Invalid extra dimension name: {raw_name!r}") from exc
        if name in {"POP", "ROW", "COL"}:
            raise SemanticMappingError(f"dimensions cannot repeat required dimension {name}")
        normalized[name] = _validate_dimension_value(raw_value, name, f"{name} value")
    return {name: normalized[name] for name in sorted(normalized)}


def _mapping_for_source(source_spec: dict[str, Any]) -> dict[str, Any]:
    mapping = source_spec.get("mapping")
    if not isinstance(mapping, dict):
        raise SemanticMappingError(
            "Every source needs an explicit reviewed mapping; automatic normalized IDs are not release-safe"
        )
    for field in ("columns", "rows", "types"):
        if not isinstance(mapping.get(field), dict):
            raise SemanticMappingError(f"Reviewed mapping is missing {field}")
    overrides = mapping.get("cell_column_overrides", {})
    if not isinstance(overrides, dict):
        raise SemanticMappingError("cell_column_overrides must be a mapping")
    return mapping


def _validate_signature(parsed: ParsedTable, source_spec: dict[str, Any]) -> None:
    expected_titles = source_spec.get("expected_titles")
    if expected_titles is not None and list(parsed.titles) != expected_titles:
        raise SemanticMappingError(
            f"Title signature drift for {source_spec.get('path')}: "
            f"expected {expected_titles!r}, got {list(parsed.titles)!r}"
        )


def _physical_row_records(parsed: ParsedTable) -> list[dict[str, Any]]:
    """Return non-empty body rows with an indentation-derived parent path."""

    records: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    for row_index, physical_row in enumerate(
        parsed.rows[parsed.header_row_count :], start=parsed.header_row_count
    ):
        if not any(value.strip() for value in physical_row.cells):
            continue
        raw_label = physical_row.cells[0]
        label = raw_label.strip()
        if not label:
            raise SemanticMappingError(
                f"Non-empty physical row {row_index} has no row label"
            )
        text_indent = len(raw_label) - len(raw_label.lstrip(" "))
        recorded_indent = max(
            physical_row.indents[0] if physical_row.indents else 0,
            physical_row.grid[0].indent_twips if physical_row.grid else 0,
        )
        indent = max(text_indent, recorded_indent)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent_path = "/".join(item[1] for item in stack)
        path = f"{parent_path}/{label}" if parent_path else label
        records.append(
            {
                "row_index": row_index,
                "physical_row": physical_row,
                "raw_label": raw_label,
                "label": label,
                "indent": indent,
                "physical_path": path,
                "parent_path": parent_path or None,
            }
        )
        stack.append((indent, label))
    return records


def _metadata_only_table(
    parsed: ParsedTable,
    source_spec: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    table_id = _validate_table_id(source_spec.get("table_id"))
    population = _validate_dimension_value(
        source_spec.get("population"), "POP", "population"
    )
    _validate_signature(parsed, source_spec)
    reason = source_spec.get("metadata_only_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SemanticMappingError(
            f"metadata_only source {table_id} needs a non-empty metadata_only_reason"
        )
    dimensions = _validate_extra_dimensions(source_spec.get("dimensions"))
    return {
        "table": {
            "table_id": table_id,
            "population": population,
            "source_id": source_id,
            "titles": list(parsed.titles),
            "footnotes": list(parsed.footnotes),
            "trailer": dict(parsed.trailer),
            "adapter": ADAPTER_ID,
            "ingest_strategy": "metadata_only",
            "metadata_only_reason": reason.strip(),
            "dimensions": dimensions,
            "physical_row_count": max(len(parsed.rows) - parsed.header_row_count, 0),
        },
        "cells": {},
    }


def _map_table(parsed: ParsedTable, source_spec: dict[str, Any], source_id: str) -> dict[str, Any]:
    if source_spec.get("ingest_strategy") == "metadata_only":
        return _metadata_only_table(parsed, source_spec, source_id)
    if source_spec.get("ingest_strategy", "cell_level") != "cell_level":
        raise SemanticMappingError(
            "ingest_strategy must be cell_level or metadata_only"
        )
    table_id = _validate_table_id(source_spec.get("table_id"))
    population = _validate_dimension_value(
        source_spec.get("population"), "POP", "population"
    )
    dimensions = _validate_extra_dimensions(source_spec.get("dimensions"))
    mapping = _mapping_for_source(source_spec)
    _validate_signature(parsed, source_spec)

    try:
        physical_header_paths = parsed.physical_table.flatten_header_paths()
    except PhysicalTableError as exc:
        raise SemanticMappingError(f"Cannot flatten reviewed header in {table_id}: {exc}") from exc
    physical_columns = [value.strip() for value in physical_header_paths[1:]]
    if not all(isinstance(label, str) for label in mapping["columns"]):
        raise SemanticMappingError(f"Column mapping labels must be strings in {table_id}")
    configured_columns = set(mapping["columns"])
    observed_columns = set(physical_columns)
    if configured_columns != observed_columns:
        raise SemanticMappingError(
            f"Column mapping coverage drift in {table_id}: "
            f"missing={sorted(configured_columns - observed_columns)!r}, "
            f"unreviewed={sorted(observed_columns - configured_columns)!r}"
        )
    mapped_columns: list[tuple[str, str]] = []
    for label in physical_columns:
        mapped_columns.append(
            (
                label,
                _validate_dimension_value(
                    mapping["columns"][label], "COL", "column id"
                ),
            )
        )
    column_ids = [item[1] for item in mapped_columns]
    if len(column_ids) != len(set(column_ids)):
        raise SemanticMappingError(f"Column semantic ID collision in {table_id}: {column_ids}")

    if not all(isinstance(label, str) for label in mapping["rows"]):
        raise SemanticMappingError(f"Row mapping labels must be strings in {table_id}")
    row_records = _physical_row_records(parsed)
    label_counts: dict[str, int] = {}
    path_counts: dict[str, int] = {}
    for record in row_records:
        label_counts[record["label"]] = label_counts.get(record["label"], 0) + 1
        path_counts[record["physical_path"]] = (
            path_counts.get(record["physical_path"], 0) + 1
        )
    repeated_in_parent = sorted(path for path, count in path_counts.items() if count > 1)
    if repeated_in_parent:
        raise SemanticMappingError(
            f"Duplicate row label within the same parent in {table_id}: {repeated_in_parent}"
        )

    configured_rows = set(mapping["rows"])
    used_mapping_keys: set[str] = set()
    for record in row_records:
        path = record["physical_path"]
        label = record["label"]
        if path in mapping["rows"]:
            mapping_key = path
        elif label_counts[label] == 1 and label in mapping["rows"]:
            # Backward-compatible flat mapping for a globally unique label.
            mapping_key = label
        elif label_counts[label] > 1:
            raise SemanticMappingError(
                f"Repeated row label {label!r} in {table_id} needs reviewed parent paths; "
                f"missing mapping for {path!r}"
            )
        else:
            raise SemanticMappingError(
                f"Row mapping coverage drift in {table_id}: unreviewed={path!r}"
            )
        record["mapping_key"] = mapping_key
        used_mapping_keys.add(mapping_key)
    if configured_rows != used_mapping_keys:
        raise SemanticMappingError(
            f"Row mapping coverage drift in {table_id}: "
            f"unused={sorted(configured_rows - used_mapping_keys)!r}, "
            f"unreviewed={sorted(used_mapping_keys - configured_rows)!r}"
        )

    reviewed_row_ids: dict[str, str] = {}
    row_labels_by_id: dict[str, str] = {}
    for mapping_key, raw_row_id in mapping["rows"].items():
        row_id = _validate_dimension_value(raw_row_id, "ROW", "row id")
        physical_record = next(
            record for record in row_records if record["mapping_key"] == mapping_key
        )
        if label_counts[physical_record["label"]] > 1 and "/" not in row_id:
            raise SemanticMappingError(
                f"Repeated leaf {physical_record['label']!r} in {table_id} requires "
                f"a parent-qualified semantic ROW path, got {row_id!r}"
            )
        if row_id in row_labels_by_id:
            raise SemanticMappingError(
                f"Semantic row collision {row_id} in {table_id}: "
                f"{row_labels_by_id[row_id]!r} and {mapping_key!r}"
            )
        reviewed_row_ids[mapping_key] = row_id
        row_labels_by_id[row_id] = mapping_key
    if not all(isinstance(row_id, str) for row_id in mapping["types"]):
        raise SemanticMappingError(f"Type mapping row IDs must be strings in {table_id}")
    configured_types = set(mapping["types"])
    expected_types = set(row_labels_by_id)
    if configured_types != expected_types:
        raise SemanticMappingError(
            f"Type mapping coverage drift in {table_id}: "
            f"missing={sorted(expected_types - configured_types)!r}, "
            f"unused={sorted(configured_types - expected_types)!r}"
        )
    for row_id, row_type in mapping["types"].items():
        _validate_dimension_value(row_id, "ROW", "type row id")
        if not isinstance(row_type, str) or row_type not in ALLOWED_CELL_TYPES:
            raise SemanticMappingError(
                f"Missing or invalid type for {table_id} row {row_id}: {row_type!r}"
            )

    overrides = mapping.get("cell_column_overrides", {})
    allowed_override_keys = {
        f"{row_id}|{column_id}"
        for row_id in row_labels_by_id
        for column_id in column_ids
    }
    for override_key, raw_target in overrides.items():
        if not isinstance(override_key, str) or override_key not in allowed_override_keys:
            raise SemanticMappingError(
                f"Unknown cell_column_overrides key in {table_id}: {override_key!r}"
            )
        source_column_id = override_key.rsplit("|", 1)[1]
        target_column_id = _validate_dimension_value(
            raw_target, "COL", "cell column id"
        )
        if (source_column_id == "UNIT") != (target_column_id == "UNIT"):
            raise SemanticMappingError(
                f"Physical UNIT column cannot be remapped in {table_id}: {override_key} -> {target_column_id}"
            )
    for row_id in row_labels_by_id:
        effective_columns = [
            _validate_dimension_value(
                overrides.get(f"{row_id}|{column_id}", column_id),
                "COL",
                "cell column id",
            )
            for column_id in column_ids
        ]
        if len(effective_columns) != len(set(effective_columns)):
            raise SemanticMappingError(
                f"Cell column semantic ID collision in {table_id} row {row_id}: {effective_columns}"
            )

    cells: dict[str, dict[str, Any]] = {}
    locators: set[tuple[int, int]] = set()
    row_ids_seen: dict[str, str] = {}
    for record in row_records:
        row_index = record["row_index"]
        physical_row = record["physical_row"]
        label = record["label"]
        indent = record["indent"]
        mapping_key = record["mapping_key"]
        row_id = reviewed_row_ids[mapping_key]
        if row_id in row_ids_seen:
            raise SemanticMappingError(
                f"Semantic row collision {row_id} in {table_id}: "
                f"{row_ids_seen[row_id]!r} and {label!r}"
            )
        row_ids_seen[row_id] = label
        row_type = mapping["types"].get(row_id)
        if row_type not in ALLOWED_CELL_TYPES:
            raise SemanticMappingError(f"Missing or invalid type for {table_id} row {row_id}: {row_type!r}")

        for column_index, value in enumerate(physical_row.cells[1:], start=1):
            display_value = value.strip()
            if not display_value:
                continue
            physical_column_id = mapped_columns[column_index - 1][1]
            override_key = f"{row_id}|{physical_column_id}"
            column_id = mapping.get("cell_column_overrides", {}).get(
                override_key, physical_column_id
            )
            column_id = _validate_dimension_value(
                column_id, "COL", "cell column id"
            )
            cell_type = "unit" if column_id == "UNIT" else row_type
            if (column_id == "UNIT") != (cell_type == "unit"):
                raise SemanticMappingError(
                    f"COL=UNIT iff type=unit invariant failed for {table_id} {row_id}"
                )
            try:
                key = build_semantic_cell_key(
                    table_id,
                    population,
                    row_id,
                    column_id,
                    dimensions,
                )
            except SemanticKeyError as exc:
                raise SemanticMappingError(
                    f"Cannot build semantic cell key in {table_id}: {exc}"
                ) from exc
            if key in cells:
                previous = cells[key]["source_locator"]
                raise SemanticMappingError(
                    f"Semantic cell collision {key}: {previous} and row={row_index}, col={column_index}"
                )
            locator = (row_index, column_index)
            if locator in locators:
                raise SemanticMappingError(f"Duplicate physical source locator in {table_id}: {locator}")
            locators.add(locator)
            cells[key] = {
                "v": display_value,
                "display_value": display_value,
                "type": cell_type,
                "row": {
                    "id": row_id,
                    "label": label,
                    "indent": indent,
                    "physical_path": record["physical_path"],
                    "parent_path": record["parent_path"],
                },
                "column": {
                    "id": column_id,
                    "label": mapped_columns[column_index - 1][0],
                },
                "source_locator": {
                    "source_id": source_id,
                    "row_index": row_index,
                    "column_index": column_index,
                },
                "dimensions": dimensions,
            }

    return {
        "table": {
            "table_id": table_id,
            "population": population,
            "source_id": source_id,
            "titles": list(parsed.titles),
            "footnotes": list(parsed.footnotes),
            "trailer": dict(parsed.trailer),
            "adapter": ADAPTER_ID,
            "ingest_strategy": "cell_level",
            "dimensions": dimensions,
        },
        "cells": cells,
    }


def _validate_deferred_row_contract(table_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != DEFERRED_ROW_CONTRACT_KEYS:
        raise DatapackError(
            f"Table {table_id} deferred_row_contract is missing or malformed"
        )
    if (
        value.get("schema_version") != "1.0"
        or value.get("detection") != "nonsemantic_row_id"
        or value.get("materialize_at") != "result_tfl"
    ):
        raise DatapackError(f"Table {table_id} deferred-row policy drift")
    for field in (
        "shell_placeholder_row_count",
        "template_placeholder_row_count",
        "stable_row_count",
    ):
        if not isinstance(value.get(field), int) or value[field] < 0:
            raise DatapackError(f"Table {table_id} {field} must be a non-negative integer")
    if value["shell_placeholder_row_count"] + value["template_placeholder_row_count"] < 1:
        raise DatapackError(f"Table {table_id} deferred contract records no placeholder rows")
    columns = value.get("frozen_column_ids")
    if (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(item, str) and item for item in columns)
        or len(columns) != len(set(columns))
    ):
        raise DatapackError(f"Table {table_id} frozen deferred-row columns are invalid")
    source = value.get("expected_semantic_source")
    if not isinstance(source, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", source):
        raise DatapackError(f"Table {table_id} expected row semantic source is invalid")
    return value


def validate_datapack(datapack: dict[str, Any]) -> None:
    if not isinstance(datapack, dict):
        raise DatapackError("Datapack JSON must be an object")
    if datapack.get("schema_version") != "1.0":
        raise DatapackError("Unsupported datapack schema_version")
    mapping_manifest = datapack.get("mapping_manifest")
    if not isinstance(mapping_manifest, dict):
        raise DatapackError("mapping_manifest must be an object")
    if not isinstance(mapping_manifest.get("filename"), str) or not SHA256_RE.fullmatch(
        str(mapping_manifest.get("sha256", ""))
    ):
        raise DatapackError("mapping_manifest needs a filename and lowercase SHA-256")
    sources = datapack.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DatapackError("Datapack sources must be a non-empty list")
    if not all(isinstance(item, dict) for item in sources):
        raise DatapackError("Every datapack source must be an object")
    for item in sources:
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", source_id):
            raise DatapackError(f"Invalid datapack source_id: {source_id!r}")
    source_ids = {item.get("source_id") for item in sources}

    tables = datapack.get("tables")
    if not isinstance(tables, list) or not tables:
        raise DatapackError("Datapack tables must be a non-empty list")
    table_index: dict[str, dict[str, Any]] = {}
    for position, table in enumerate(tables):
        if not isinstance(table, dict):
            raise DatapackError(f"Datapack table {position} must be an object")
        try:
            table_id = validate_table_id(table.get("table_id"))
            population = validate_dimension_value(table.get("population"), "POP")
        except SemanticKeyError as exc:
            raise DatapackError(f"Invalid datapack table {position}: {exc}") from exc
        if table_id in table_index:
            raise DatapackError(f"Duplicate datapack table_id: {table_id}")
        if table.get("source_id") not in source_ids:
            raise DatapackError(f"Table {table_id} references an unknown source")
        strategy = table.get("ingest_strategy", "cell_level")
        if strategy not in ALLOWED_INGEST_STRATEGIES:
            raise DatapackError(f"Table {table_id} has invalid ingest_strategy {strategy!r}")
        if strategy == "metadata_only" and not isinstance(
            table.get("metadata_only_reason"), str
        ):
            raise DatapackError(f"Table {table_id} metadata_only_reason is required")
        if strategy in {
            "columns_only_deferred_rows",
            "mixed_stable_and_deferred_rows",
        }:
            deferred = _validate_deferred_row_contract(
                table_id, table.get("deferred_row_contract")
            )
            layout = table.get("layout_reference")
            expected_layout_keys = {
                "inherited_from",
                "pattern_id",
                "columns",
                "rows",
                "inherited_column_ids",
                "row_classifier_ids",
            }
            if layout is not None:
                if not isinstance(layout, dict) or set(layout) != expected_layout_keys:
                    raise DatapackError(
                        f"Table {table_id} deferred-row layout contract is malformed"
                    )
                inherited_columns = layout.get("inherited_column_ids")
                if (
                    layout.get("columns") != "inherited"
                    or layout.get("rows") != "deferred_until_result"
                    or not isinstance(inherited_columns, list)
                    or inherited_columns != deferred["frozen_column_ids"]
                ):
                    raise DatapackError(
                        f"Table {table_id} deferred-row layout contract has invalid columns/rows"
                    )
            if strategy == "columns_only_deferred_rows" and deferred["stable_row_count"] != 0:
                raise DatapackError(
                    f"Table {table_id} columns-only contract cannot declare stable rows"
                )
            if strategy == "mixed_stable_and_deferred_rows" and deferred["stable_row_count"] < 1:
                raise DatapackError(
                    f"Table {table_id} mixed-row contract needs stable rows"
                )
        elif table.get("deferred_row_contract") is not None:
            raise DatapackError(
                f"Table {table_id} cannot carry a deferred contract under {strategy}"
            )
        raw_dimensions = table.get("dimensions", {})
        try:
            dimensions = _validate_extra_dimensions(raw_dimensions)
        except SemanticMappingError as exc:
            raise DatapackError(f"Table {table_id} dimensions are invalid: {exc}") from exc
        table_index[table_id] = {
            "population": population,
            "strategy": strategy,
            "dimensions": dimensions,
            "deferred_row_contract": table.get("deferred_row_contract"),
        }

    cells = datapack.get("cells")
    if not isinstance(cells, dict):
        raise DatapackError("Datapack cells must be a mapping")
    if not cells and any(
        table["strategy"] in {"cell_level", "mixed_stable_and_deferred_rows"}
        for table in table_index.values()
    ):
        raise DatapackError("A stable-row datapack must contain mapped cells")
    cell_tables: set[str] = set()
    for key, cell in cells.items():
        if not isinstance(key, str):
            raise DatapackError(f"Datapack cell keys must be strings: {key!r}")
        try:
            parsed_key = parse_semantic_cell_key(key)
        except SemanticKeyError as exc:
            raise DatapackError(f"Invalid datapack cell key: {key}: {exc}") from exc
        table_record = table_index.get(parsed_key.table_id)
        if table_record is None:
            raise DatapackError(
                f"Cell {key} points to a table absent from datapack.tables"
            )
        if table_record["strategy"] == "metadata_only":
            raise DatapackError(f"metadata_only table {parsed_key.table_id} cannot have cell keys")
        if not row_id_is_semantic(parsed_key.row):
            raise DatapackError(
                f"Cell {key} freezes an opaque or positional shell row identifier"
            )
        deferred = table_record.get("deferred_row_contract")
        if deferred is not None and parsed_key.column not in deferred["frozen_column_ids"]:
            raise DatapackError(
                f"Cell {key} escapes the frozen deferred-row column contract"
            )
        if parsed_key.population != table_record["population"]:
            raise DatapackError(f"Cell {key} population disagrees with table metadata")
        if parsed_key.extra_dimensions != table_record["dimensions"]:
            raise DatapackError(f"Cell {key} dimensions disagree with table metadata")
        cell_tables.add(parsed_key.table_id)
        if not isinstance(cell, dict) or not isinstance(cell.get("v"), str):
            raise DatapackError(f"Cell {key} must have string v")
        if cell.get("v") != cell.get("display_value"):
            raise DatapackError(f"Cell {key} v/display_value mismatch")
        if not isinstance(cell.get("type"), str) or cell.get("type") not in ALLOWED_CELL_TYPES:
            raise DatapackError(f"Cell {key} has invalid type {cell.get('type')!r}")
        if (parsed_key.column == "UNIT") != (cell.get("type") == "unit"):
            raise DatapackError(f"Cell {key} violates COL=UNIT iff type=unit")
        row = cell.get("row")
        column = cell.get("column")
        if not isinstance(row, dict) or not isinstance(column, dict):
            raise DatapackError(f"Cell {key} row/column metadata must be objects")
        if row.get("id") != parsed_key.row or column.get("id") != parsed_key.column:
            raise DatapackError(f"Cell {key} key and embedded semantic IDs disagree")
        if cell.get("dimensions", {}) != parsed_key.extra_dimensions:
            raise DatapackError(f"Cell {key} embedded dimensions disagree with its key")
        source_locator = cell.get("source_locator")
        if not isinstance(source_locator, dict):
            raise DatapackError(f"Cell {key} source_locator must be an object")
        if source_locator.get("source_id") not in source_ids:
            raise DatapackError(f"Cell {key} references an unknown source")

    empty_cell_level = sorted(
        table_id
        for table_id, table in table_index.items()
        if table["strategy"] in {"cell_level", "mixed_stable_and_deferred_rows"}
        and table_id not in cell_tables
    )
    if empty_cell_level:
        raise DatapackError(
            f"cell_level tables without mapped cells: {empty_cell_level}"
        )


def build_datapack(manifest_path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_file = Path(manifest_path)
    manifest_sha256 = sha256_file(manifest_file)
    manifest = load_data(manifest_file)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise SemanticMappingError("TFL manifest must declare schema_version: '1.0'")
    source_specs = manifest.get("sources")
    if not isinstance(source_specs, list) or not source_specs:
        raise SemanticMappingError("TFL manifest needs a non-empty sources list")

    sources: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    combined_cells: dict[str, dict[str, Any]] = {}
    parsed_tables: list[dict[str, Any]] = []
    table_ids: set[str] = set()
    for source_spec in source_specs:
        if not isinstance(source_spec, dict) or not isinstance(source_spec.get("path"), str):
            raise SemanticMappingError("Each manifest source needs a path")
        source_path = (manifest_file.parent / source_spec["path"]).resolve()
        parsed = parse_rtf_table(source_path)
        parsed_tables.append(parsed.as_dict())
        source_id = f"sha256:{parsed.source_sha256}"
        sources.append(
            {
                "source_id": source_id,
                "filename": source_path.name,
                "manifest_path": source_spec["path"],
                "format": ADAPTER_ID,
                "declared_codepage": parsed.declared_codepage,
                "codec": parsed.codec,
                "decode_replacements": 0,
            }
        )
        mapped = _map_table(parsed, source_spec, source_id)
        table_id = mapped["table"]["table_id"]
        if table_id in table_ids:
            raise SemanticMappingError(f"Duplicate table_id in manifest: {table_id}")
        table_ids.add(table_id)
        tables.append(mapped["table"])
        for key, cell in mapped["cells"].items():
            if key in combined_cells:
                raise SemanticMappingError(f"Cross-table semantic cell collision: {key}")
            combined_cells[key] = cell

    datapack = {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "adapter_scope": "reviewed TFL mappings; physical adapter recorded per source",
        "listing_policy": {
            "default": "cell_level",
            "subject_level_wide_listing": "metadata_only",
            "rationale": (
                "Participant-level wide listings are indexed for coverage and provenance "
                "but are not expanded into subject-cell semantic keys. This limits privacy "
                "exposure and prevents an unbounded datapack; aggregate result tables remain "
                "cell_level."
            ),
        },
        "mapping_manifest": {
            "filename": manifest_file.name,
            "sha256": manifest_sha256,
        },
        "sources": sources,
        "tables": tables,
        "cells": combined_cells,
    }
    if sha256_file(manifest_file) != manifest_sha256:
        raise SemanticMappingError(f"Mapping manifest changed while being read: {manifest_file}")
    validate_datapack(datapack)
    return datapack, {
        "schema_version": "1.0",
        "mapping_manifest": dict(datapack["mapping_manifest"]),
        "tables": parsed_tables,
    }


def build_datapack_to_files(
    manifest_path: str | Path,
    output_path: str | Path,
    parsed_output_path: str | Path | None = None,
) -> dict[str, Any]:
    datapack, parsed = build_datapack(manifest_path)
    outputs: list[tuple[str | Path, Any]] = [(output_path, datapack)]
    if parsed_output_path is not None:
        outputs.append((parsed_output_path, parsed))
    atomic_write_json_bundle(outputs)
    return datapack
