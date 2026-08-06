"""Apply a de-identified RTF result table to a frozen DOCX-shell contract.

The DOCX shell fixes semantic keys and physical coordinates before database
lock.  The RTF result adapter is therefore deliberately not an automatic
mapper: it accepts values only when the result row/column identifiers occupy
the exact reviewed coordinates recorded in the frozen map.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .datapack import validate_datapack
from .errors import CSRWriterError
from .io_utils import sha256_file
from .row_semantics import (
    classify_shell_row_segment,
    readable_result_segment,
)
from .rtf_parser import ADAPTER_ID as RTF_ADAPTER_ID, parse_rtf_table
from .semantic_key import build_semantic_cell_key, parse_semantic_cell_key
from .tfl_map import TFLMapDriftError, check_result_against_frozen_map


class CrossFormatResultError(CSRWriterError):
    """An RTF result cannot be applied to the frozen shell without guessing."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        self.report = dict(report or {})
        super().__init__(message)


def _entity(frozen_map: Mapping[str, Any], entity_id: str) -> Mapping[str, Any]:
    matches = [item for item in frozen_map.get("entities", []) if item.get("entity_id") == entity_id]
    if len(matches) != 1:
        raise CrossFormatResultError(
            f"Frozen map must contain exactly one entity {entity_id}; observed={len(matches)}"
        )
    entity = matches[0]
    strategy = entity.get("ingest_strategy")
    if strategy == "metadata_only" or (
        strategy == "cell_level" and not entity.get("cell_contracts")
    ):
        raise CrossFormatResultError(
            f"Entity {entity_id} is metadata-only; cell values cannot be applied before review"
        )
    return entity


def _coordinate_contracts(entity: Mapping[str, Any]) -> dict[tuple[int, int], Mapping[str, Any]]:
    coordinates: dict[tuple[int, int], Mapping[str, Any]] = {}
    for index, contract in enumerate(entity.get("cell_contracts", [])):
        if not isinstance(contract, Mapping):
            raise CrossFormatResultError(f"Malformed cell contract {index}")
        row_index = contract.get("row_index")
        column_index = contract.get("column_index")
        if not isinstance(row_index, int) or not isinstance(column_index, int):
            raise CrossFormatResultError(f"Cell contract lacks integer coordinates: {contract.get('key')}")
        coordinate = (row_index, column_index)
        if coordinate in coordinates:
            raise CrossFormatResultError(f"Frozen map repeats physical coordinate {coordinate}")
        coordinates[coordinate] = contract
    return coordinates


def _structure_issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _source_with_result(
    result: dict[str, Any], parsed: Any
) -> str:
    result_source_id = f"sha256:{parsed.source_sha256}"
    if all(item.get("source_id") != result_source_id for item in result["sources"]):
        result["sources"].append(
            {
                "source_id": result_source_id,
                "filename": "deidentified-result.rtf",
                "manifest_path": "frozen-shell semantic application",
                "format": RTF_ADAPTER_ID,
                "declared_codepage": parsed.declared_codepage,
                "codec": parsed.codec,
                "decode_replacements": 0,
            }
        )
    return result_source_id


def _apply_deferred_rows(
    shell_datapack: Mapping[str, Any],
    frozen_map: Mapping[str, Any],
    entity: Mapping[str, Any],
    entity_id: str,
    parsed: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deferred = entity.get("deferred_row_contract")
    if not isinstance(deferred, Mapping):
        raise CrossFormatResultError(
            f"Entity {entity_id} lacks its frozen deferred-row contract"
        )
    frozen_columns = list(deferred.get("frozen_column_ids", []))
    physical = parsed.physical_table
    issues: list[dict[str, Any]] = []
    if physical.column_count != len(frozen_columns) + 1:
        issues.append(
            _structure_issue(
                "E-RTF-COLUMN-COUNT",
                "Result RTF columns differ from the frozen deferred-row contract",
                entity_id=entity_id,
                expected_columns=len(frozen_columns) + 1,
                observed_columns=physical.column_count,
            )
        )
    if not issues:
        header = parsed.rows[parsed.header_row_count - 1]
        observed_columns = [
            header.cells[index].strip() for index in range(1, physical.column_count)
        ]
        if observed_columns != frozen_columns:
            issues.append(
                _structure_issue(
                    "E-RTF-COLUMN-DRIFT",
                    "Result RTF column identifiers differ from the frozen deferred-row contract",
                    entity_id=entity_id,
                    expected=frozen_columns,
                    observed=observed_columns,
                )
            )

    stable_contracts_by_row: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for item in entity.get("cell_contracts", []):
        row_id = str(item.get("row_id"))
        column_id = str(item.get("column_id"))
        stable_contracts_by_row[row_id][column_id] = item
    stable_rows = set(stable_contracts_by_row)
    semantic_source = str(deferred.get("expected_semantic_source"))
    shell_stack: list[tuple[int, str]] = []
    result_stack: list[tuple[int, str]] = []
    materialized: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    seen_stable: set[str] = set()
    leaf_parents: dict[str, set[str | None]] = defaultdict(set)

    for row_index, row in enumerate(
        parsed.rows[parsed.header_row_count :], start=parsed.header_row_count
    ):
        if not any(value.strip() for value in row.cells):
            continue
        raw_label = row.cells[0]
        label = raw_label.strip()
        if not label:
            issues.append(
                _structure_issue(
                    "E-RTF-ROW-LABEL",
                    "Result RTF has a non-empty row without a semantic label",
                    entity_id=entity_id,
                    row_index=row_index,
                )
            )
            continue
        text_indent = len(raw_label) - len(raw_label.lstrip(" "))
        recorded_indent = max(
            row.indents[0] if row.indents else 0,
            row.grid[0].indent_twips if row.grid else 0,
        )
        indent = max(text_indent, recorded_indent)
        while shell_stack and shell_stack[-1][0] >= indent:
            shell_stack.pop()
        while result_stack and result_stack[-1][0] >= indent:
            result_stack.pop()

        classified = classify_shell_row_segment(label)
        shell_parent = "/".join(item[1] for item in shell_stack) or None
        shell_candidate = (
            f"{shell_parent}/{classified.segment}"
            if shell_parent
            else classified.segment
        )
        if classified.semantic and shell_candidate in stable_rows:
            if shell_candidate in seen_stable:
                issues.append(
                    _structure_issue(
                        "E-RTF-STABLE-ROW-DUPLICATE",
                        "A frozen stable row occurs more than once in the result TFL",
                        entity_id=entity_id,
                        row_id=shell_candidate,
                        row_index=row_index,
                    )
                )
                continue
            row_id = shell_candidate
            parent_path = parse_semantic_cell_key(
                next(iter(stable_contracts_by_row[row_id].values()))["key"]
            ).row_parent
            is_stable = True
            seen_stable.add(row_id)
            result_segment = row_id.rsplit("/", 1)[-1]
        else:
            result_parent = "/".join(item[1] for item in result_stack) or None
            result_segment = readable_result_segment(
                label, semantic_source, has_parent=bool(result_parent)
            )
            row_id = (
                f"{result_parent}/{result_segment}"
                if result_parent
                else result_segment
            )
            parent_path = result_parent
            is_stable = False
        if row_id in seen_rows:
            issues.append(
                _structure_issue(
                    "E-RTF-ROW-COLLISION",
                    "Result RTF repeats a semantic row inside the same parent",
                    entity_id=entity_id,
                    row_id=row_id,
                    row_index=row_index,
                )
            )
            continue
        seen_rows.add(row_id)
        leaf_parents[row_id.rsplit("/", 1)[-1]].add(parent_path)
        missing_columns = [
            frozen_columns[column_index - 1]
            for column_index in range(1, physical.column_count)
            if not row.cells[column_index].strip()
        ]
        if missing_columns:
            issues.append(
                _structure_issue(
                    "E-RTF-MISSING-VALUE",
                    "Result RTF row has blank values under frozen columns",
                    entity_id=entity_id,
                    row_id=row_id,
                    row_index=row_index,
                    missing_columns=missing_columns,
                )
            )
        materialized.append(
            {
                "row_index": row_index,
                "row_id": row_id,
                "parent_path": parent_path,
                "indent": row_id.count("/"),
                "is_stable": is_stable,
            }
        )
        shell_stack.append((indent, classified.segment))
        result_stack.append((indent, result_segment))

    missing_stable = sorted(stable_rows - seen_stable)
    for row_id in missing_stable:
        issues.append(
            _structure_issue(
                "E-RTF-STABLE-ROW-MISSING",
                "A row frozen from the shell is absent from the result TFL",
                entity_id=entity_id,
                row_id=row_id,
            )
        )
    if not materialized:
        issues.append(
            _structure_issue(
                "E-RTF-NO-RESULT-ROWS",
                "Result RTF did not materialize any semantic body rows",
                entity_id=entity_id,
            )
        )

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "blocked" if issues else "pass",
        "entity_id": entity_id,
        "format_transition": "docx-shell-to-heteromorphic-rtf-result",
        "frozen_map_sha256": frozen_map.get("frozen_sha256"),
        "rtf_sha256": parsed.source_sha256,
        "ingest_strategy": entity.get("ingest_strategy"),
        "stable_row_count": len(stable_rows),
        "materialized_row_count": len(materialized),
        "late_bound_row_count": sum(not item["is_stable"] for item in materialized),
        "cross_parent_duplicate_leaf_count": sum(
            len(parents) > 1 for parents in leaf_parents.values()
        ),
        "checks": {
            "frozen_shell_contract": "pass",
            "frozen_columns": "fail" if any(
                item["code"] in {"E-RTF-COLUMN-COUNT", "E-RTF-COLUMN-DRIFT"}
                for item in issues
            ) else "pass",
            "stable_rows": "fail" if any(
                item["code"].startswith("E-RTF-STABLE-ROW") for item in issues
            ) else "pass",
            "parent_qualified_rows": "fail" if any(
                item["code"] == "E-RTF-ROW-COLLISION" for item in issues
            ) else "pass",
            "nonblank_values": "fail" if any(
                item["code"] == "E-RTF-MISSING-VALUE" for item in issues
            ) else "pass",
        },
        "issues": issues,
    }
    if issues:
        raise CrossFormatResultError(
            "E-TFL-MAP-DRIFT: heteromorphic RTF result differs from its frozen semantic contract",
            report=report,
        )

    result = copy.deepcopy(dict(shell_datapack))
    result_source_id = _source_with_result(result, parsed)
    result["cells"] = {
        key: value
        for key, value in result["cells"].items()
        if parse_semantic_cell_key(key).table_id != entity_id
    }
    shell_cells = shell_datapack["cells"]
    dimensions = dict(entity.get("dimensions", {}))
    population = str(entity.get("population"))
    for row_record in materialized:
        row_index = row_record["row_index"]
        row_id = row_record["row_id"]
        for column_index, column_id in enumerate(frozen_columns, start=1):
            value = parsed.rows[row_index].cells[column_index].strip()
            if row_record["is_stable"]:
                contract = stable_contracts_by_row[row_id][column_id]
                key = str(contract["key"])
                cell = copy.deepcopy(shell_cells[key])
                cell["source_locator"]["row_index"] = row_index
                cell["source_locator"]["column_index"] = column_index
            else:
                key = build_semantic_cell_key(
                    entity_id, population, row_id, column_id, dimensions
                )
                cell = {
                    "v": value,
                    "display_value": value,
                    "type": "unit" if column_id == "UNIT" else "statistic",
                    "row": {
                        "id": row_id,
                        "label": row_id,
                        "indent": row_record["indent"],
                        "physical_path": row_id,
                        "parent_path": row_record["parent_path"],
                    },
                    "column": {"id": column_id, "label": column_id},
                    "source_locator": {
                        "source_id": result_source_id,
                        "row_index": row_index,
                        "column_index": column_index,
                    },
                    "dimensions": dimensions,
                }
            cell["v"] = value
            cell["display_value"] = value
            cell["source_locator"]["source_id"] = result_source_id
            result["cells"][key] = cell
    for table in result["tables"]:
        if table.get("table_id") == entity_id:
            table["source_id"] = result_source_id
            table["adapter"] = RTF_ADAPTER_ID
            break
    result["mapping_manifest"] = {
        "filename": "frozen-tfl-map.json",
        "sha256": str(frozen_map["frozen_sha256"]),
    }
    validate_datapack(result)
    try:
        drift_check = check_result_against_frozen_map(result, frozen_map)
    except TFLMapDriftError as exc:
        raise CrossFormatResultError(str(exc), report=exc.report) from exc
    report["result_datapack_sha256"] = drift_check["result_datapack_sha256"]
    report["checks"]["semantic_key_and_contract_drift"] = "pass"
    return result, report


def apply_rtf_values_to_shell(
    shell_datapack: Mapping[str, Any],
    frozen_map: Mapping[str, Any],
    entity_id: str,
    rtf_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy values from one RTF table after an exact frozen-coordinate check."""

    validate_datapack(dict(shell_datapack))
    # This also validates the frozen-map hash and proves the input shell still
    # implements the exact semantic contract being used below.
    check_result_against_frozen_map(shell_datapack, frozen_map)
    entity = _entity(frozen_map, entity_id)
    parsed = parse_rtf_table(rtf_path)
    if entity.get("ingest_strategy") in {
        "columns_only_deferred_rows",
        "mixed_stable_and_deferred_rows",
    }:
        return _apply_deferred_rows(
            shell_datapack, frozen_map, entity, entity_id, parsed
        )
    contracts = _coordinate_contracts(entity)
    physical = parsed.physical_table

    rows: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    columns: dict[int, set[str]] = defaultdict(set)
    for (row_index, column_index), contract in contracts.items():
        rows[row_index].append(contract)
        columns[column_index].add(str(contract.get("column_id")))

    issues: list[dict[str, Any]] = []
    if max(row for row, _column in contracts) >= len(parsed.rows):
        issues.append(
            _structure_issue(
                "E-RTF-ROW-COUNT",
                "Result RTF has fewer physical rows than the frozen shell contract",
                expected_last_row=max(row for row, _column in contracts),
                observed_last_row=len(parsed.rows) - 1,
            )
        )
    if max(column for _row, column in contracts) >= physical.column_count:
        issues.append(
            _structure_issue(
                "E-RTF-COLUMN-COUNT",
                "Result RTF has fewer physical columns than the frozen shell contract",
                expected_last_column=max(column for _row, column in contracts),
                observed_last_column=physical.column_count - 1,
            )
        )

    if not issues:
        header_row = parsed.rows[parsed.header_row_count - 1]
        for column_index, semantic_ids in sorted(columns.items()):
            if len(semantic_ids) != 1:
                issues.append(
                    _structure_issue(
                        "E-FROZEN-COLUMN-CONTRACT",
                        "One physical column has multiple frozen semantic IDs",
                        column_index=column_index,
                        semantic_ids=sorted(semantic_ids),
                    )
                )
                continue
            expected = next(iter(semantic_ids))
            observed = header_row.cells[column_index].strip()
            if observed != expected:
                issues.append(
                    _structure_issue(
                        "E-RTF-COLUMN-DRIFT",
                        "Result RTF column identifier differs from the frozen shell contract",
                        column_index=column_index,
                        expected=expected,
                        observed=observed,
                    )
                )
        for row_index, row_contracts in sorted(rows.items()):
            expected_ids = {str(item.get("row_id")) for item in row_contracts}
            if len(expected_ids) != 1:
                issues.append(
                    _structure_issue(
                        "E-FROZEN-ROW-CONTRACT",
                        "One physical row has multiple frozen semantic IDs",
                        row_index=row_index,
                        semantic_ids=sorted(expected_ids),
                    )
                )
                continue
            expected = next(iter(expected_ids))
            observed = parsed.rows[row_index].cells[0].strip()
            if observed != expected:
                issues.append(
                    _structure_issue(
                        "E-RTF-ROW-DRIFT",
                        "Result RTF row identifier differs from the frozen shell contract",
                        row_index=row_index,
                        expected=expected,
                        observed=observed,
                    )
                )
        for (row_index, column_index), contract in sorted(contracts.items()):
            if not parsed.rows[row_index].cells[column_index].strip():
                issues.append(
                    _structure_issue(
                        "E-RTF-MISSING-VALUE",
                        "Result RTF cell is blank at a frozen semantic coordinate",
                        key=contract.get("key"),
                        row_index=row_index,
                        column_index=column_index,
                    )
                )

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "blocked" if issues else "pass",
        "entity_id": entity_id,
        "format_transition": "docx-shell-to-rtf-result",
        "frozen_map_sha256": frozen_map.get("frozen_sha256"),
        "rtf_sha256": parsed.source_sha256,
        "semantic_cell_count": len(contracts),
        "checks": {
            "frozen_shell_contract": "pass",
            "physical_coordinates": "fail" if issues else "pass",
            "row_identifiers": "fail" if any(item["code"] == "E-RTF-ROW-DRIFT" for item in issues) else "pass",
            "column_identifiers": "fail" if any(item["code"] == "E-RTF-COLUMN-DRIFT" for item in issues) else "pass",
            "nonblank_values": "fail" if any(item["code"] == "E-RTF-MISSING-VALUE" for item in issues) else "pass",
        },
        "issues": issues,
    }
    if issues:
        raise CrossFormatResultError(
            "E-TFL-MAP-DRIFT: RTF result differs from the frozen DOCX-shell contract",
            report=report,
        )

    result = copy.deepcopy(dict(shell_datapack))
    result_source_id = f"sha256:{parsed.source_sha256}"
    if all(item.get("source_id") != result_source_id for item in result["sources"]):
        result["sources"].append(
            {
                "source_id": result_source_id,
                "filename": "deidentified-result.rtf",
                "manifest_path": "frozen-shell coordinate application",
                "format": RTF_ADAPTER_ID,
                "declared_codepage": parsed.declared_codepage,
                "codec": parsed.codec,
                "decode_replacements": 0,
            }
        )
    for (row_index, column_index), contract in contracts.items():
        key = str(contract["key"])
        value = parsed.rows[row_index].cells[column_index].strip()
        result["cells"][key]["v"] = value
        result["cells"][key]["display_value"] = value
        result["cells"][key]["source_locator"]["source_id"] = result_source_id
    for table in result["tables"]:
        if table.get("table_id") == entity_id:
            table["source_id"] = result_source_id
            table["adapter"] = RTF_ADAPTER_ID
            break
    result["mapping_manifest"] = {
        "filename": "frozen-tfl-map.json",
        "sha256": str(frozen_map["frozen_sha256"]),
    }
    validate_datapack(result)
    try:
        drift_check = check_result_against_frozen_map(result, frozen_map)
    except TFLMapDriftError as exc:
        raise CrossFormatResultError(str(exc), report=exc.report) from exc
    report["result_datapack_sha256"] = drift_check["result_datapack_sha256"]
    report["checks"]["semantic_key_and_contract_drift"] = "pass"
    return result, report


def _rtf_cell(text: str, *, bold: bool = False, indent: int = 0) -> str:
    if not text.isascii() or any(character in text for character in "{}\\"):
        raise CrossFormatResultError("De-identified RTF fixture labels must be safe ASCII")
    weight = r"\b " if bold else ""
    left = rf"\li{indent * 360}" if indent else ""
    return rf"\pard\intbl\ql{left} {weight}{text}\cell"


def _synthetic_value(cell_type: str, ordinal: int) -> str:
    if cell_type == "count":
        return str(10 + ordinal)
    if cell_type == "n_pct":
        return f"{10 + ordinal} ({20 + ordinal}.0)"
    if cell_type == "pvalue":
        return "0.042"
    if cell_type == "ci95":
        return "(1.20, 3.40)"
    if cell_type == "unit":
        return "ng/mL"
    if cell_type in {"text", "text_judgement", "status"}:
        return "DEID_RESULT"
    if cell_type == "date":
        return "2026-01-01"
    if cell_type == "composite":
        return "DEID/RESULT"
    return f"{ordinal + 1}.25"


def build_deidentified_result_rtf(
    frozen_map: Mapping[str, Any], entity_id: str
) -> str:
    """Build a value-bearing, identifier-safe RTF fixture for one frozen entity."""

    entity = _entity(frozen_map, entity_id)
    if entity.get("ingest_strategy") != "cell_level":
        raise CrossFormatResultError(
            "Use build_deidentified_heteromorphic_result_rtf for deferred-row entities"
        )
    contracts = _coordinate_contracts(entity)
    max_row = max(row for row, _column in contracts)
    max_column = max(column for _row, column in contracts)
    first_body_row = min(row for row, _column in contracts)
    if first_body_row < 1:
        raise CrossFormatResultError("Frozen result coordinates do not leave a header row")

    by_coordinate = contracts
    column_ids: dict[int, str] = {}
    for (_row_index, column_index), contract in contracts.items():
        value = str(contract["column_id"])
        previous = column_ids.setdefault(column_index, value)
        if previous != value:
            raise CrossFormatResultError(
                f"Physical column {column_index} has incompatible frozen semantic IDs"
            )
    row_ids: dict[int, str] = {}
    for (row_index, _column_index), contract in contracts.items():
        value = str(contract["row_id"])
        previous = row_ids.setdefault(row_index, value)
        if previous != value:
            raise CrossFormatResultError(
                f"Physical row {row_index} has incompatible frozen semantic IDs"
            )

    boundaries = "".join(rf"\cellx{(index + 1) * 1800}" for index in range(max_column + 1))
    lines = [r"{\rtf1\ansi\ansicpg1252\deff0", rf"\pard\qc\b DEIDENTIFIED {entity_id}\par"]
    for row_index in range(first_body_row):
        labels = [f"ROW_LEVEL_{row_index + 1}"]
        for column_index in range(1, max_column + 1):
            labels.append(
                column_ids.get(column_index, f"PAD_COL_{column_index}")
                if row_index == first_body_row - 1
                else f"LEVEL_{row_index + 1}_COL_{column_index}"
            )
        lines.append(r"\trowd" + boundaries + "".join(_rtf_cell(value, bold=True) for value in labels) + r"\row")
    ordinal = 0
    for row_index in range(first_body_row, max_row + 1):
        labels = [row_ids.get(row_index, "")]
        for column_index in range(1, max_column + 1):
            contract = by_coordinate.get((row_index, column_index))
            if contract is None:
                labels.append("")
            else:
                labels.append(_synthetic_value(str(contract.get("type")), ordinal))
                ordinal += 1
        lines.append(r"\trowd" + boundaries + "".join(_rtf_cell(value) for value in labels) + r"\row")
    lines.extend([r"\pard\ql Program: deidentified_result.sas   Output: RESULT\par", "}"])
    return "\n".join(lines) + "\n"


def build_deidentified_heteromorphic_result_rtf(
    frozen_map: Mapping[str, Any],
    entity_id: str,
    rows: list[tuple[int, str]],
) -> str:
    """Build an ASCII RTF whose real result rows need not match shell positions."""

    entity = _entity(frozen_map, entity_id)
    if entity.get("ingest_strategy") not in {
        "columns_only_deferred_rows",
        "mixed_stable_and_deferred_rows",
    }:
        raise CrossFormatResultError("Heteromorphic fixtures require a deferred-row entity")
    deferred = entity.get("deferred_row_contract")
    if not isinstance(deferred, Mapping):
        raise CrossFormatResultError("Deferred-row entity lacks its frozen column contract")
    columns = list(deferred.get("frozen_column_ids", []))
    if not rows or not columns:
        raise CrossFormatResultError("Heteromorphic fixture needs rows and frozen columns")
    for indent, label in rows:
        if not isinstance(indent, int) or indent < 0 or not isinstance(label, str) or not label:
            raise CrossFormatResultError("Heteromorphic row definitions are invalid")
        if not label.isascii() or any(character in label for character in "{}\\"):
            raise CrossFormatResultError("Heteromorphic fixture labels must be safe ASCII")
    boundaries = "".join(
        rf"\cellx{(index + 1) * 1800}" for index in range(len(columns) + 1)
    )
    lines = [
        r"{\rtf1\ansi\ansicpg1252\deff0",
        rf"\pard\qc\b DEIDENTIFIED {entity_id}\par",
        r"\trowd"
        + boundaries
        + _rtf_cell("ROW", bold=True)
        + "".join(_rtf_cell(column, bold=True) for column in columns)
        + r"\row",
    ]
    ordinal = 0
    for indent, label in rows:
        values: list[str] = []
        for _column in columns:
            values.append(_synthetic_value("statistic", ordinal))
            ordinal += 1
        lines.append(
            r"\trowd"
            + boundaries
            + _rtf_cell(label, indent=indent)
            + "".join(_rtf_cell(value) for value in values)
            + r"\row"
        )
    lines.extend([r"\pard\ql Program: deidentified_result.sas   Output: RESULT\par", "}"])
    return "\n".join(lines) + "\n"


def write_deidentified_result_rtf(
    frozen_map: Mapping[str, Any], entity_id: str, output_path: str | Path
) -> dict[str, Any]:
    """Write only synthetic values and frozen ASCII semantic identifiers."""

    output = Path(output_path)
    if output.suffix.lower() != ".rtf":
        raise CrossFormatResultError("De-identified result fixture output must end in .rtf")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_deidentified_result_rtf(frozen_map, entity_id)
    output.write_text(payload, encoding="ascii", newline="\n")
    return {
        "entity_id": entity_id,
        "sha256": sha256_file(output),
        "bytes": output.stat().st_size,
        "contains_source_titles": False,
        "contains_source_values": False,
    }
