"""Strict DOCX-result matching against a frozen DOCX-shell semantic map.

The production default is fail closed.  A diagnostic partial mode exists only
for de-identified development fixtures: matched values may be exercised by the
downstream writer while every unmatched result value and every unresolved
frozen cell remains explicit in the report.
"""

from __future__ import annotations

import copy
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .datapack import validate_datapack
from .docx_extract import W, _StyleVisibility, _parse_xml
from .docx_tfl_parser import ParsedDocxTFLTable, _parse_table
from .errors import CSRWriterError
from .io_utils import sha256_file
from .row_semantics import classify_shell_row_segment, readable_result_segment
from .semantic_key import build_semantic_cell_key, parse_semantic_cell_key
from .shell_mapping import (
    ADAPTER_ID as SHELL_ADAPTER_ID,
    TITLE_RE,
    _analysis_set,
    _column_id,
    _flatten_header_paths_with_visual_spanners,
    _stage,
    _text,
)
from .tfl_map import check_result_against_frozen_map


ADAPTER_ID = "docx-result-ooxml-frozen-match-v1"
COMPOSITE_GM_CV_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)\s*\(([+-]?\d+(?:\.\d+)?)\)\s*$"
)


class DocxResultError(CSRWriterError):
    """A DOCX result cannot be safely related to its frozen shell map."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        self.report = dict(report or {})
        super().__init__(message)


def coverage_metric_views(entity_reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return all reviewed coverage denominators without selecting a favorable one."""

    result_count = sum(int(item["result_semantic_value_count"]) for item in entity_reports)
    matched_count = sum(int(item["matched_result_value_count"]) for item in entity_reports)
    unresolved_count = sum(int(item["unresolved_frozen_cell_count"]) for item in entity_reports)
    shell_expected_count = matched_count + unresolved_count
    explicit_contract_reports = [
        item
        for item in entity_reports
        if item.get("frozen_contract_cell_count") is not None
    ]
    explicit_frozen_count = sum(
        int(item["frozen_contract_cell_count"]) for item in explicit_contract_reports
    )
    explicit_matched_count = sum(
        int(item["matched_result_value_count"]) for item in explicit_contract_reports
    )
    return {
        "result_side_matched_value_count": matched_count,
        "result_side_semantic_value_count": result_count,
        "result_side_match_rate": round(matched_count / result_count, 4)
        if result_count
        else 0.0,
        "result_oriented_match_rate": round(matched_count / result_count, 4)
        if result_count
        else 0.0,
        "shell_oriented_expected_cell_count": shell_expected_count,
        "shell_oriented_match_rate": round(matched_count / shell_expected_count, 4)
        if shell_expected_count
        else 0.0,
        "explicit_frozen_contract_entity_count": len(explicit_contract_reports),
        "explicit_frozen_contract_cell_count": explicit_frozen_count,
        "explicit_frozen_contract_matched_count": explicit_matched_count,
        "frozen_shell_side_matched_cell_count": explicit_matched_count,
        "frozen_shell_side_contract_cell_count": explicit_frozen_count,
        "frozen_shell_side_match_rate": round(
            explicit_matched_count / explicit_frozen_count, 4
        )
        if explicit_frozen_count
        else 0.0,
        "explicit_frozen_contract_match_rate": round(
            explicit_matched_count / explicit_frozen_count, 4
        )
        if explicit_frozen_count
        else 0.0,
    }


@dataclass(frozen=True)
class _ResultEntity:
    entity_id: str
    population: str
    dimensions: dict[str, str]
    parsed: ParsedDocxTFLTable


def _read_result_entities(source: Path) -> tuple[list[_ResultEntity], int, int]:
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            document = _parse_xml(archive.read("word/document.xml"), "word/document.xml")
            styles_root = (
                _parse_xml(archive.read("word/styles.xml"), "word/styles.xml")
                if "word/styles.xml" in names
                else None
            )
    except (KeyError, zipfile.BadZipFile) as exc:
        raise DocxResultError("Result TFL is not a valid DOCX package") from exc
    body = document.find(W("body"))
    if body is None:
        raise DocxResultError("Result TFL has no Word document body")
    styles = _StyleVisibility(styles_root)
    tables = body.findall(W("tbl"))
    occurrences: list[tuple[str, str, int, int]] = []
    table_count = 0
    for body_index, child in enumerate(body):
        if child.tag == W("tbl"):
            table_count += 1
            continue
        if child.tag != W("p"):
            continue
        title = _text(child, styles)
        match = TITLE_RE.match(title)
        if match is None:
            continue
        prefix = {"表": "T", "图": "F", "清单": "L"}[match.group(1)]
        occurrences.append((prefix + match.group(2), title, body_index, table_count))
    if not occurrences:
        raise DocxResultError("Result TFL contains no recognized TFL title")
    result: list[_ResultEntity] = []
    seen: set[str] = set()
    for position, (entity_id, title, _body_index, tables_before) in enumerate(occurrences):
        if entity_id in seen:
            raise DocxResultError(f"Result TFL repeats entity {entity_id}")
        seen.add(entity_id)
        next_tables_before = (
            occurrences[position + 1][3] if position + 1 < len(occurrences) else len(tables)
        )
        indexes = list(range(tables_before + 1, next_tables_before + 1))
        if len(indexes) != 1:
            raise DocxResultError(
                f"Result entity {entity_id} must own exactly one Word table; observed={len(indexes)}"
            )
        parsed = _parse_table(
            tables[indexes[0] - 1],
            table_index=indexes[0],
            styles=styles,
            allow_first_row_header=True,
            allow_ambiguous_header_paths=True,
        )
        result.append(
            _ResultEntity(
                entity_id=entity_id,
                population=_analysis_set(title),
                dimensions=_stage(title),
                parsed=parsed,
            )
        )
    physical_cell_count = sum(len(table.findall(".//" + W("tc"))) for table in tables)
    return result, len(tables), physical_cell_count


def _cell_type(column_id: str) -> str:
    if column_id.endswith("_EVENT_COUNT"):
        return "count"
    if column_id.endswith("_N_PCT"):
        return "n_pct"
    return "statistic"


def _set_matched_value(
    datapack: dict[str, Any], key: str, value: str, source_id: str, *, row_index: int, column_index: int
) -> None:
    cell = datapack["cells"][key]
    cell["v"] = value
    cell["display_value"] = value
    cell["source_locator"]["source_id"] = source_id
    cell["source_locator"]["row_index"] = row_index
    cell["source_locator"]["column_index"] = column_index
    cell["result_value_status"] = "observed_result"


def _match_ancova_row(label: str, frozen_rows: set[str]) -> tuple[str | None, str]:
    normalized = unicodedata.normalize("NFKC", label).strip()
    compact = re.sub(r"\s+", "", normalized).casefold()
    if (
        "基线" in compact
        and "较基线变化" not in compact
        and "mean" in compact
        and "sd" in compact
    ):
        candidate = "BASELINE/OBSERVED_VALUE/MEAN_SD"
        return (candidate, "matched") if candidate in frozen_rows else (None, "stable_row_absent")
    visit = re.search(r"第\s*(\d+)\s*天", normalized)
    if visit and "mean" in compact and "sd" in compact:
        candidate = f"D{visit.group(1)}/OBSERVED_VALUE/MEAN_SD"
        return (candidate, "matched") if candidate in frozen_rows else (None, "visit_not_in_frozen_shell")

    # A structurally simple result table may freeze reviewed statistics at the
    # root level (for example ROW=N or ROW=P_VALUE).  Prefer that exact semantic
    # identity before applying ANCOVA-specific nested-row suffix heuristics.
    classified = classify_shell_row_segment(normalized)
    if classified.semantic and classified.segment in frozen_rows:
        return classified.segment, "matched"

    if compact == "n":
        suffixes = ("/N",)
    elif "较基线变化" in compact and "mean" in compact and "sd" in compact:
        suffixes = ("/CHANGE_FROM_BASELINE/MEAN_SD",)
    elif "lsmean" in compact and "difference" in compact:
        suffixes = ("/LS_MEAN_DIFFERENCE_SE_A",)
    elif "lsmean" in compact:
        suffixes = ("/LS_MEAN_SE_A",)
    elif "95%ci" in compact:
        suffixes = ("/LS_MEAN_DIFFERENCE_SE_A_CI95", "_CI95")
    elif "p值" in compact or compact in {"p-value", "pvalue"}:
        suffixes = ("/P_VALUE_A",)
    else:
        return (
            (classified.segment, "matched")
            if classified.semantic and classified.segment in frozen_rows
            else (None, "stable_row_unrecognized")
        )
    candidates = sorted(
        row_id for row_id in frozen_rows if any(row_id.endswith(suffix) for suffix in suffixes)
    )
    if len(candidates) == 1:
        return candidates[0], "matched"
    if not candidates:
        return None, "stable_row_absent"
    return None, "stable_row_ambiguous_without_visit"


def _apply_ancova(
    datapack: dict[str, Any], entity: _ResultEntity, frozen_entity: Mapping[str, Any], source_id: str
) -> dict[str, Any]:
    contracts = {
        (str(item["row_id"]), str(item["column_id"])): item
        for item in frozen_entity.get("cell_contracts", [])
    }
    frozen_rows = {row_id for row_id, _column_id_value in contracts}
    paths, visual_inferences = _flatten_header_paths_with_visual_spanners(
        entity.parsed.physical_table
    )
    result_columns = [_column_id(path) for path in paths[1:]]
    if len(result_columns) != len(set(result_columns)):
        raise DocxResultError(f"Result entity {entity.entity_id} has ambiguous result columns")
    unmatched = Counter()
    matched = 0
    total = 0
    matched_keys: list[str] = []
    seen_matched_keys: set[str] = set()
    for row_index, row in enumerate(
        entity.parsed.rows[entity.parsed.header_row_count :],
        start=entity.parsed.header_row_count,
    ):
        if not any(value.strip() for value in row.cells):
            continue
        matched_row, reason = _match_ancova_row(row.cells[0], frozen_rows)
        for column_index, column_id in enumerate(result_columns, start=1):
            value = row.cells[column_index].strip()
            if not value:
                raise DocxResultError(
                    f"Result entity {entity.entity_id} has a blank ANCOVA value at row {row_index}"
                )
            total += 1
            contract = contracts.get((matched_row, column_id)) if matched_row else None
            if contract is None:
                unmatched[reason if matched_row is None else "result_column_not_in_frozen_shell"] += 1
                continue
            key = str(contract["key"])
            if key in seen_matched_keys:
                raise DocxResultError(
                    f"Result entity {entity.entity_id} maps more than one row to frozen key {key}"
                )
            seen_matched_keys.add(key)
            _set_matched_value(
                datapack, key, value, source_id, row_index=row_index, column_index=column_index
            )
            matched += 1
            matched_keys.append(key)
    frozen_unresolved = len(contracts) - matched
    return {
        "entity_id": entity.entity_id,
        "strategy": "stable_rows_exact_semantic_subset",
        "result_semantic_value_count": total,
        "matched_result_value_count": matched,
        "unmatched_result_value_count": total - matched,
        "unmatched_reasons": dict(sorted(unmatched.items())),
        "frozen_contract_cell_count": len(contracts),
        "unresolved_frozen_cell_count": frozen_unresolved,
        "matched_keys": matched_keys,
        "visual_spanner_inferences": visual_inferences,
    }


def _pk_parameter_root(label: str) -> str | None:
    compact = unicodedata.normalize("NFKC", label).strip().casefold()
    return {
        "cmax": "CMAX_UNIT",
        "auc0-t": "AUC0_UNIT",
        "auc0-inf": "AUC0_INF_UNIT",
        "tmax": "TMAX_UNIT",
        "t1/2": "T_HALF_UNIT",
    }.get(compact)


def _apply_pk(
    datapack: dict[str, Any], entity: _ResultEntity, frozen_entity: Mapping[str, Any], source_id: str
) -> dict[str, Any]:
    contracts = {
        (str(item["row_id"]), str(item["column_id"])): item
        for item in frozen_entity.get("cell_contracts", [])
    }
    paths, visual_inferences = _flatten_header_paths_with_visual_spanners(
        entity.parsed.physical_table
    )
    descriptors: list[tuple[int, str, str]] = []
    flattened: list[str] = []
    for column_index, path in enumerate(paths[2:], start=2):
        parts = [part.strip() for part in path.split("/") if part.strip()]
        if len(parts) != 2:
            raise DocxResultError(
                f"PK spanner path is not a group/statistic pair: {path!r}"
            )
        group_id = _column_id(parts[0])
        statistic = re.sub(r"\s+", " ", parts[1]).strip().casefold()
        if statistic == "n":
            role = "N"
        elif statistic in {"geo mean (cv%)", "geometric mean (cv%)"}:
            role = "GEO_MEAN_CV"
        else:
            raise DocxResultError(f"Unsupported PK spanner statistic: {parts[1]!r}")
        descriptors.append((column_index, group_id, role))
        flattened.append(f"{group_id}/{role}")
    if len(flattened) != len(set(flattened)):
        raise DocxResultError("PK spanner flattening produced duplicate semantic paths")

    unmatched = Counter()
    matched = 0
    total = 0
    matched_keys: list[str] = []
    for row_index, row in enumerate(
        entity.parsed.rows[entity.parsed.header_row_count :],
        start=entity.parsed.header_row_count,
    ):
        root = _pk_parameter_root(row.cells[0])
        for column_index, group_id, role in descriptors:
            value = row.cells[column_index].strip()
            if not value:
                raise DocxResultError(
                    f"Result entity {entity.entity_id} has a blank PK value at row {row_index}"
                )
            atoms: list[tuple[str, str]]
            if role == "N":
                atoms = [("N", value)]
            else:
                match = COMPOSITE_GM_CV_RE.fullmatch(value)
                if match is None:
                    raise DocxResultError(
                        f"PK Geo Mean (CV%) value cannot be split deterministically at row {row_index}"
                    )
                atoms = [("GM", match.group(1)), ("GCV", match.group(2))]
            for statistic, atomic_value in atoms:
                total += 1
                row_id = f"{root}/{statistic}" if root else None
                contract = contracts.get((row_id, group_id)) if row_id else None
                if contract is None:
                    unmatched["result_parameter_not_in_frozen_shell"] += 1
                    continue
                key = str(contract["key"])
                _set_matched_value(
                    datapack,
                    key,
                    atomic_value,
                    source_id,
                    row_index=row_index,
                    column_index=column_index,
                )
                matched += 1
                matched_keys.append(key)
    return {
        "entity_id": entity.entity_id,
        "strategy": "pk_spanner_transposed_to_frozen_rows",
        "spanner_flattened_columns": flattened,
        "visual_spanner_inferences": visual_inferences,
        "result_semantic_value_count": total,
        "matched_result_value_count": matched,
        "unmatched_result_value_count": total - matched,
        "unmatched_reasons": dict(sorted(unmatched.items())),
        "frozen_contract_cell_count": len(contracts),
        "unresolved_frozen_cell_count": len(contracts) - matched,
        "matched_keys": matched_keys,
    }


def _deferred_leaf_rows(entity: _ResultEntity, semantic_source: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    seen: set[str] = set()
    for row_index, row in enumerate(
        entity.parsed.rows[entity.parsed.header_row_count :],
        start=entity.parsed.header_row_count,
    ):
        if not any(value.strip() for value in row.cells):
            continue
        label = row.cells[0].strip()
        if not label:
            raise DocxResultError(
                f"Deferred result entity {entity.entity_id} has a value row without a label"
            )
        indent = max(
            row.indents[0] if row.indents else 0,
            row.grid[0].indent_twips if row.grid else 0,
        )
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if indent <= 0:
            stack.append((indent, readable_result_segment(label, semantic_source, has_parent=False)))
            continue
        if not stack:
            raise DocxResultError(
                f"Deferred result entity {entity.entity_id} has an indented row without a parent"
            )
        parent = "/".join(item[1] for item in stack)
        leaf = readable_result_segment(label, semantic_source, has_parent=True)
        row_id = f"{parent}/{leaf}"
        if row_id in seen:
            raise DocxResultError(
                f"Deferred result entity {entity.entity_id} repeats row {row_id} inside one parent"
            )
        seen.add(row_id)
        records.append(
            {
                "row_index": row_index,
                "row_id": row_id,
                "parent_path": parent,
                "row_leaf": leaf,
            }
        )
        stack.append((indent, leaf))
    if not records:
        raise DocxResultError(
            f"Deferred result entity {entity.entity_id} materialized no readable leaf rows"
        )
    return records


def _apply_deferred(
    datapack: dict[str, Any], entity: _ResultEntity, frozen_entity: Mapping[str, Any], source_id: str
) -> dict[str, Any]:
    contract = frozen_entity.get("deferred_row_contract")
    if not isinstance(contract, Mapping):
        raise DocxResultError(f"Frozen entity {entity.entity_id} lacks a deferred-row contract")
    frozen_columns = list(contract.get("frozen_column_ids", []))
    paths, visual_inferences = _flatten_header_paths_with_visual_spanners(
        entity.parsed.physical_table
    )
    result_columns = [_column_id(path) for path in paths[1:]]
    if len(result_columns) != len(set(result_columns)):
        raise DocxResultError(f"Deferred result entity {entity.entity_id} has duplicate columns")
    column_indexes = {column_id: index for index, column_id in enumerate(result_columns, start=1)}
    matching_columns = [column_id for column_id in frozen_columns if column_id in column_indexes]
    result_only_columns = [column_id for column_id in result_columns if column_id not in frozen_columns]
    missing_frozen_columns = [column_id for column_id in frozen_columns if column_id not in column_indexes]
    stable_contracts = {
        (str(item["row_id"]), str(item["column_id"])): item
        for item in frozen_entity.get("cell_contracts", [])
    }
    stable_rows = {row_id for row_id, _column_id_value in stable_contracts}
    matched_stable_keys: list[str] = []
    seen_stable_rows: set[str] = set()
    stable_result_value_count = 0
    stable_unmatched_result_count = 0
    for row_index, physical_row in enumerate(
        entity.parsed.rows[entity.parsed.header_row_count :],
        start=entity.parsed.header_row_count,
    ):
        label = physical_row.cells[0].strip()
        if not label:
            continue
        classified = classify_shell_row_segment(label)
        candidates = sorted(
            row_id
            for row_id in stable_rows
            if row_id == classified.segment or row_id.endswith("/" + classified.segment)
        )
        if not classified.semantic or not candidates:
            continue
        if len(candidates) != 1:
            raise DocxResultError(
                f"Deferred result entity {entity.entity_id} has an ambiguous stable row {label!r}"
            )
        row_id = candidates[0]
        if row_id in seen_stable_rows:
            raise DocxResultError(
                f"Deferred result entity {entity.entity_id} repeats stable row {row_id}"
            )
        seen_stable_rows.add(row_id)
        for column_id in result_columns:
            column_index = column_indexes[column_id]
            value = physical_row.cells[column_index].strip()
            if not value:
                raise DocxResultError(
                    f"Deferred result entity {entity.entity_id} has a blank stable-row value"
                )
            stable_result_value_count += 1
            contract_item = stable_contracts.get((row_id, column_id))
            if contract_item is None:
                stable_unmatched_result_count += 1
                continue
            key = str(contract_item["key"])
            _set_matched_value(
                datapack,
                key,
                value,
                source_id,
                row_index=row_index,
                column_index=column_index,
            )
            matched_stable_keys.append(key)
    rows = _deferred_leaf_rows(
        entity, str(contract.get("expected_semantic_source"))
    )
    dimensions = dict(frozen_entity.get("dimensions", {}))
    population = str(frozen_entity.get("population"))
    matched_keys: list[str] = list(matched_stable_keys)
    for row_record in rows:
        physical_row = entity.parsed.rows[row_record["row_index"]]
        for column_id in frozen_columns:
            key = build_semantic_cell_key(
                entity.entity_id,
                population,
                row_record["row_id"],
                column_id,
                dimensions,
            )
            column_index = column_indexes.get(column_id)
            value = physical_row.cells[column_index].strip() if column_index is not None else "xx"
            if column_index is not None and not value:
                raise DocxResultError(
                    f"Deferred result entity {entity.entity_id} has a blank matched value"
                )
            datapack["cells"][key] = {
                "v": value,
                "display_value": value,
                "type": _cell_type(column_id),
                "row": {
                    "id": row_record["row_id"],
                    "label": row_record["row_id"],
                    "indent": row_record["row_id"].count("/"),
                    "physical_path": row_record["row_id"],
                    "parent_path": row_record["parent_path"],
                },
                "column": {"id": column_id, "label": column_id},
                "source_locator": {
                    "source_id": source_id,
                    "row_index": row_record["row_index"],
                    "column_index": column_index if column_index is not None else -1,
                },
                "dimensions": dimensions,
                "result_value_status": (
                    "observed_result" if column_index is not None else "source_gap"
                ),
            }
            if column_index is not None:
                matched_keys.append(key)
    duplicate_leaf_parents: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        duplicate_leaf_parents[row["row_leaf"]].add(row["parent_path"])
    detected = len(rows) * len(result_columns) + stable_result_value_count
    matched = len(rows) * len(matching_columns) + len(matched_stable_keys)
    unresolved_stable = len(stable_contracts) - len(matched_stable_keys)
    result_only_count = (
        len(rows) * len(result_only_columns) + stable_unmatched_result_count
    )
    return {
        "entity_id": entity.entity_id,
        "strategy": "deferred_leaf_rows_against_frozen_columns",
        "shell_placeholder_row_count": contract.get("shell_placeholder_row_count"),
        "template_placeholder_row_count": contract.get("template_placeholder_row_count"),
        "result_leaf_row_count": len(rows),
        "result_semantic_value_count": detected,
        "matched_result_value_count": matched,
        "stable_result_row_count": len(seen_stable_rows),
        "matched_stable_cell_count": len(matched_stable_keys),
        "unmatched_result_value_count": result_only_count,
        "unmatched_reasons": (
            {"result_column_not_in_frozen_shell": result_only_count}
            if result_only_count
            else {}
        ),
        "frozen_contracted_cells_materialized": (
            len(rows) * len(frozen_columns) + len(stable_contracts)
        ),
        "unresolved_frozen_cell_count": (
            len(rows) * len(missing_frozen_columns) + unresolved_stable
        ),
        "matching_columns": matching_columns,
        "result_only_columns": result_only_columns,
        "missing_frozen_columns": missing_frozen_columns,
        "cross_parent_duplicate_leaf_count": sum(
            len(parents) > 1 for parents in duplicate_leaf_parents.values()
        ),
        "matched_keys": matched_keys,
        "visual_spanner_inferences": visual_inferences,
    }


def apply_docx_result_to_frozen_shell(
    shell_datapack: Mapping[str, Any],
    frozen_map: Mapping[str, Any],
    result_path: str | Path,
    *,
    expected_sha256: str,
    diagnostic_partial: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply only deterministically matched DOCX result values.

    When ``diagnostic_partial`` is false, any unmatched result value or missing
    frozen value raises :class:`DocxResultError`.  The opt-in partial mode is
    deliberately marked non-release-eligible and is intended for de-identified
    development fixtures only.
    """

    validate_datapack(dict(shell_datapack))
    check_result_against_frozen_map(shell_datapack, frozen_map)
    source = Path(result_path).resolve()
    before_hash = sha256_file(source)
    if before_hash != expected_sha256:
        raise DocxResultError("Result DOCX SHA-256 differs from the approved fixture hash")
    entities, physical_table_count, physical_cell_count = _read_result_entities(source)
    after_hash = sha256_file(source)
    if after_hash != before_hash:
        raise DocxResultError("Result DOCX changed during read-only ingestion")

    frozen_entities = {
        str(item["entity_id"]): item for item in frozen_map.get("entities", [])
    }
    result = copy.deepcopy(dict(shell_datapack))
    result_source_id = f"sha256:{before_hash}"
    if all(item.get("source_id") != result_source_id for item in result["sources"]):
        result["sources"].append(
            {
                "source_id": result_source_id,
                "filename": "deidentified-result.docx",
                "manifest_path": "frozen-shell diagnostic result application",
                "format": ADAPTER_ID,
                "decode_replacements": 0,
            }
        )
    for cell in result["cells"].values():
        cell.setdefault("result_value_status", "shell_placeholder")

    entity_reports: list[dict[str, Any]] = []
    unmatched_entities: list[str] = []
    observed_result_entity_ids = {entity.entity_id for entity in entities}
    for entity in entities:
        frozen_entity = frozen_entities.get(entity.entity_id)
        if frozen_entity is None:
            unmatched_entities.append(entity.entity_id)
            continue
        if entity.population != frozen_entity.get("population"):
            raise DocxResultError(
                f"Result population drift for {entity.entity_id}: "
                f"expected={frozen_entity.get('population')!r}, observed={entity.population!r}"
            )
        if entity.dimensions != frozen_entity.get("dimensions"):
            raise DocxResultError(
                f"Result dimension drift for {entity.entity_id}: "
                f"expected={frozen_entity.get('dimensions')!r}, observed={entity.dimensions!r}"
            )
        strategy = str(frozen_entity.get("ingest_strategy"))
        if strategy == "metadata_only":
            body_rows = len(entity.parsed.rows) - entity.parsed.header_row_count
            entity_report = {
                "entity_id": entity.entity_id,
                "strategy": "metadata_only",
                "result_semantic_value_count": 0,
                "matched_result_value_count": 0,
                "unmatched_result_value_count": 0,
                "unmatched_reasons": {},
                "frozen_contract_cell_count": 0,
                "unresolved_frozen_cell_count": 0,
                "body_rows_indexed_as_metadata": body_rows,
                "matched_keys": [],
                "visual_spanner_inferences": 0,
            }
        elif strategy in {"columns_only_deferred_rows", "mixed_stable_and_deferred_rows"}:
            entity_report = _apply_deferred(result, entity, frozen_entity, result_source_id)
        elif entity.entity_id == "T14.2.4.3.1":
            entity_report = _apply_pk(result, entity, frozen_entity, result_source_id)
        else:
            entity_report = _apply_ancova(result, entity, frozen_entity, result_source_id)
        entity_reports.append(entity_report)
        for table in result["tables"]:
            if table.get("table_id") == entity.entity_id:
                table["source_id"] = result_source_id
                table["adapter"] = ADAPTER_ID
                break

    missing_result_entities = sorted(
        set(frozen_entities) - observed_result_entity_ids
    )
    for entity_id in missing_result_entities:
        frozen_entity = frozen_entities[entity_id]
        frozen_contract_count = len(frozen_entity.get("cell_contracts", []))
        entity_reports.append(
            {
                "entity_id": entity_id,
                "strategy": "missing_result_entity",
                "result_semantic_value_count": 0,
                "matched_result_value_count": 0,
                "unmatched_result_value_count": 0,
                "unmatched_reasons": {"result_entity_missing": 1},
                "frozen_contract_cell_count": frozen_contract_count,
                "unresolved_frozen_cell_count": frozen_contract_count,
                "matched_keys": [],
                "visual_spanner_inferences": 0,
            }
        )

    result["mapping_manifest"] = {
        "filename": "frozen-tfl-map.json",
        "sha256": str(frozen_map["frozen_sha256"]),
    }
    validate_datapack(result)
    structure_report = check_result_against_frozen_map(result, frozen_map)

    result_count = sum(item["result_semantic_value_count"] for item in entity_reports)
    matched_count = sum(item["matched_result_value_count"] for item in entity_reports)
    unmatched_count = sum(item["unmatched_result_value_count"] for item in entity_reports)
    unresolved_count = sum(item["unresolved_frozen_cell_count"] for item in entity_reports)
    coverage_views = coverage_metric_views(entity_reports)
    deferred_detected = sum(
        item.get("result_semantic_value_count", 0)
        for item in entity_reports
        if item["strategy"] == "deferred_leaf_rows_against_frozen_columns"
    )
    deferred_materialized = sum(
        item.get("frozen_contracted_cells_materialized", 0)
        for item in entity_reports
        if item["strategy"] == "deferred_leaf_rows_against_frozen_columns"
    )
    strict_blockers = (
        unmatched_count
        + unresolved_count
        + len(unmatched_entities)
        + len(missing_result_entities)
    )
    report = {
        "schema_version": "1.0",
        "status": "pass" if strict_blockers == 0 else "diagnostic_partial",
        "release_eligible": strict_blockers == 0,
        "diagnostic_partial_mode": diagnostic_partial,
        "source_sha256": before_hash,
        "source_unchanged_after_read": before_hash == after_hash,
        "physical_table_count": physical_table_count,
        "physical_cell_count": physical_cell_count,
        "result_entity_count": len(entities),
        "matched_entity_count": len(entities) - len(unmatched_entities),
        "unmatched_entities": unmatched_entities,
        "missing_result_entities": missing_result_entities,
        "metrics": {
            "result_semantic_value_count": result_count,
            "matched_result_value_count": matched_count,
            "unmatched_result_value_count": unmatched_count,
            "unresolved_frozen_cell_count": unresolved_count,
            **coverage_views,
            "deferred_result_semantic_keys_detected": deferred_detected,
            "deferred_frozen_contracted_cells_materialized": deferred_materialized,
            "deferred_shell_placeholder_rows": sum(
                int(item.get("shell_placeholder_row_count") or 0)
                + int(item.get("template_placeholder_row_count") or 0)
                for item in entity_reports
                if item["strategy"] == "deferred_leaf_rows_against_frozen_columns"
            ),
        },
        "coverage_denominators": {
            "result_oriented": (
                "matched result values / semantic values observed in the result TFL"
            ),
            "shell_oriented_all": (
                "matched cells / (matched cells + unresolved frozen/materialized cells)"
            ),
            "shell_oriented_explicit_contracts": (
                "matched cells / frozen_contract_cell_count for entities with an explicit "
                "fixed-size contract; this is the conservative fill-readiness view"
            ),
            "reporting_policy": "all denominators must be reported together; none may be relabeled as overall coverage",
        },
        "checks": {
            "source_hash": "pass",
            "source_unchanged": "pass",
            "population_and_dimensions": "pass",
            "frozen_structure_gate": structure_report["status"],
            "strict_value_completeness": "pass" if strict_blockers == 0 else "fail",
        },
        "entity_reports": entity_reports,
    }
    if strict_blockers and not diagnostic_partial:
        raise DocxResultError(
            "E-TFL-MAP-DRIFT: DOCX result is not fully compatible with the frozen shell map",
            report=report,
        )
    return result, report
