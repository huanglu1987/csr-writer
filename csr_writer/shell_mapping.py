"""Privacy-safe, confidence-scored mapping proposals from a real DOCX TFL shell.

The scanner reads the source in place, records only de-identified structure,
and separates 175 physical Word table objects from unique TFL entities.  It
never writes source titles, cell labels, or source paths to its outputs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import etree

from .cross_references import normalize_entity_id
from .datapack import validate_datapack
from .docx_extract import W, _StyleVisibility, _parse_xml, _visible_text
from .docx_tfl_parser import DocxTFLParseError, _parse_table
from .errors import CSRWriterError
from .io_utils import load_json_unique, sha256_file
from .row_semantics import (
    classify_shell_row_segment,
    expected_result_row_source,
    row_id_is_semantic,
)
from .semantic_key import build_semantic_cell_key, parse_semantic_cell_key
from .tfl_map import build_tfl_map_candidate, freeze_tfl_map
from .tfl_routing import expected_tfl_sections


ADAPTER_ID = "docx-shell-ooxml-auto-v2"
TITLE_RE = re.compile(r"^\s*(表|图|清单)\s*(\d+(?:\.\d+)+)")
ANALYSIS_SET_RE = re.compile(
    r"[\(\[（]\s*(?P<name>[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+){0,7})"
    r"\s*[\)\]）]\s*$",
    re.IGNORECASE,
)
PARENTHESIZED_ANALYSIS_SET_RE = re.compile(
    r"[\(\[（]\s*(?P<name>[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+){0,7})"
    r"\s*[\)\]）]",
    re.IGNORECASE,
)
DOSE_RE = re.compile(
    r"(?P<dose>\d+(?:\.\d+)?)\s*%\s*(?P<frequency>BID|QD|TID|QID)",
    re.IGNORECASE,
)
SAFE_ASCII_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-/]*$")

DEFAULT_LAYOUT_REFERENCE_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "csr-writer"
    / "references"
    / "layout-reference-patterns.json"
)


class ShellMappingError(CSRWriterError):
    """The real shell cannot be mapped without hidden assumptions."""


@dataclass(frozen=True)
class _TitleOccurrence:
    entity_id: str
    title: str
    body_index: int
    tables_before: int


@dataclass(frozen=True)
class _CompiledLayoutConfig:
    patterns: tuple[tuple[str, re.Pattern[str]], ...]
    data_driven_row_patterns: tuple[tuple[str, re.Pattern[str]], ...]
    sha256: str


def _load_layout_reference_config(
    value: Mapping[str, Any] | str | Path | None,
) -> _CompiledLayoutConfig:
    if value is None:
        payload: Any = load_json_unique(DEFAULT_LAYOUT_REFERENCE_CONFIG_PATH)
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = load_json_unique(value)
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "patterns",
        "data_driven_row_patterns",
    }:
        raise ShellMappingError("Layout-reference configuration contract drift")
    if payload.get("schema_version") != "1.0":
        raise ShellMappingError("Unsupported layout-reference configuration schema")

    def compile_group(field: str, *, require_target: bool) -> tuple[tuple[str, re.Pattern[str]], ...]:
        raw_items = payload.get(field)
        if not isinstance(raw_items, list) or not raw_items:
            raise ShellMappingError(f"Layout-reference configuration {field} must be non-empty")
        compiled: list[tuple[str, re.Pattern[str]]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping) or set(raw) != {"id", "regex"}:
                raise ShellMappingError(f"Malformed {field}[{index}]")
            pattern_id = raw.get("id")
            expression = raw.get("regex")
            if (
                not isinstance(pattern_id, str)
                or re.fullmatch(r"[a-z][a-z0-9_-]*", pattern_id) is None
                or pattern_id in seen_ids
                or not isinstance(expression, str)
                or not expression
            ):
                raise ShellMappingError(f"Invalid {field}[{index}]")
            try:
                pattern = re.compile(expression, re.IGNORECASE)
            except re.error as exc:
                raise ShellMappingError(f"Invalid regex in {field}[{index}]") from exc
            if require_target and "target" not in pattern.groupindex:
                raise ShellMappingError(f"{field}[{index}] regex needs named group 'target'")
            seen_ids.add(pattern_id)
            compiled.append((pattern_id, pattern))
        return tuple(compiled)

    canonical = {
        "schema_version": "1.0",
        "patterns": [dict(item) for item in payload["patterns"]],
        "data_driven_row_patterns": [
            dict(item) for item in payload["data_driven_row_patterns"]
        ],
    }
    return _CompiledLayoutConfig(
        patterns=compile_group("patterns", require_target=True),
        data_driven_row_patterns=compile_group(
            "data_driven_row_patterns", require_target=False
        ),
        sha256=_canonical_hash(canonical),
    )


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(paragraph: etree._Element, styles: _StyleVisibility) -> str:
    return _visible_text(
        paragraph,
        styles,
        styles.paragraph_style(paragraph),
        skip_text_boxes=False,
    ).strip()


_KNOWN_ANALYSIS_SET_CANONICAL = {
    "fas": "FAS",
    "mfas": "mFAS",
    "ss": "SS",
    "pks": "PKS",
    "pkcs": "PKCS",
    "pkps": "PKPS",
    "bes": "BES",
    "pps": "PPS",
    "ppps": "PPPS",
    "mitt": "mITT",
    "mpps": "mPPS",
    "itt": "ITT",
    "all screened set": "All Screened Set",
    "all enrolled set": "All Enrolled Set",
}


def _analysis_set_details(title: str) -> tuple[str, bool]:
    """Parse a trailing analysis-set marker without treating the registry as an enum.

    Known aliases are canonicalised.  Other safe identifiers and English full names
    remain usable but are returned as unrecognised, so the proposal layer can force
    an explicit review instead of silently inventing a canonical name.
    """

    normalized = unicodedata.normalize("NFKC", title)
    # A known analysis-set marker may be followed by stage or design text in a
    # shell title.  Only registry-backed names are accepted away from the title
    # end; an unfamiliar parenthetical remains a safe trailing-marker proposal
    # that requires confirmation.
    for candidate in PARENTHESIZED_ANALYSIS_SET_RE.finditer(normalized):
        value = re.sub(r"\s+", " ", candidate.group("name")).strip()
        canonical = _KNOWN_ANALYSIS_SET_CANONICAL.get(value.casefold())
        if canonical is not None:
            return canonical, True

    match = ANALYSIS_SET_RE.search(normalized)
    if match is None:
        return "UNSPECIFIED", False
    value = re.sub(r"\s+", " ", match.group("name")).strip()
    canonical = _KNOWN_ANALYSIS_SET_CANONICAL.get(value.casefold())
    return (canonical, True) if canonical is not None else (value, False)


def _analysis_set(title: str) -> str:
    return _analysis_set_details(title)[0]


def _route_sections(entity_id: str) -> list[str]:
    return expected_tfl_sections(entity_id)


def _stage(title: str) -> dict[str, str]:
    normalized = unicodedata.normalize("NFKC", title).upper()
    if re.search(r"(?:\bD1\b|单次给药)", normalized):
        return {"STAGE": "D1"}
    if re.search(r"(?:\bD28\b|稳态)", normalized):
        return {"STAGE": "D28"}
    return {}


def _stable_id(prefix: str, text: str, length: int = 10) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length].upper()
    return f"{prefix}_{digest}"


def _column_id(label: str) -> str:
    normalized = unicodedata.normalize("NFKC", label).strip()
    normalized = re.sub(r"(?:\(\s*[Nn]\s*=\s*(?:\d+|[xX]+)\s*\)|\b[Nn]\s*=\s*(?:\d+|[xX]+)\b)(?=\s*(?:/|$))", "", normalized).strip()
    compact = re.sub(r"\s+", "", normalized)
    lowered = compact.casefold()
    statistic_suffix: str | None = None
    if re.search(r"(?:例数)?n\(%\)(?:$|/)", lowered):
        statistic_suffix = "N_PCT"
    elif "例次" in compact or re.search(r"(?:^|/)EVENTS?(?:$|/)", normalized, re.I):
        statistic_suffix = "EVENT_COUNT"

    def with_statistic(base: str) -> str:
        return f"{base}_{statistic_suffix}" if statistic_suffix else base

    if "安慰剂" in compact or "placebo" in lowered or re.search(r"\bPBO\b", normalized, re.I):
        return with_statistic("PBO")
    dose = DOSE_RE.search(normalized)
    if dose is not None:
        value = dose.group("dose").replace(".", "_")
        return with_statistic(f"C{value}PCT_{dose.group('frequency').upper()}")
    if ("试验药" in compact or "active" in lowered) and "total" in lowered:
        return with_statistic("ACTIVE_TOTAL")
    if (
        compact in {"Total", "TOTAL", "合计", "总计"}
        or lowered.endswith("/total")
        or re.match(r"^total(?:\(|/)", lowered)
    ):
        return with_statistic("TOTAL")
    if compact in {"单位", "Unit", "UNIT"}:
        return "UNIT"
    if ("例数" in compact or re.search(r"\bN\b", normalized)) and re.search(
        r"(?:受试制剂|\bT\b)$", normalized, re.I
    ):
        return "N_T"
    if ("例数" in compact or re.search(r"\bN\b", normalized)) and re.search(
        r"(?:参比制剂|\bR\b)$", normalized, re.I
    ):
        return "N_R"
    safe = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").upper()
    if re.search(r"[\u3400-\u9fff]", normalized):
        return _stable_id("COL", normalized)
    if safe and SAFE_ASCII_RE.fullmatch(safe):
        return safe[:48]
    return _stable_id("COL", normalized)


def _flatten_header_paths_with_visual_spanners(physical) -> tuple[tuple[str, ...], int]:
    """Flatten headers and conservatively normalize blank visual spanners.

    Some statistical DOCX producers render a two-column spanner by placing the
    group label in the first cell and leaving the adjacent top-level cell blank,
    without emitting ``w:gridSpan``.  A blank is inherited from the cell to its
    left only when the current column has a non-empty lower-level header.  This
    keeps ordinary blank descriptor columns untouched and records every inferred
    continuation for review.
    """

    effective: list[list[str]] = []
    inferred = 0
    for row_index in range(physical.header_row_count):
        values: list[str] = []
        for column_index in range(physical.column_count):
            _anchor_coordinate, anchor = physical._anchor_at(row_index, column_index)
            values.append(anchor.text.strip())
        effective.append(values)

    for row_index in range(physical.header_row_count - 1):
        for column_index in range(1, physical.column_count):
            if effective[row_index][column_index]:
                continue
            lower_has_label = any(
                effective[lower_index][column_index]
                for lower_index in range(row_index + 1, physical.header_row_count)
            )
            left_label = effective[row_index][column_index - 1]
            if lower_has_label and left_label:
                effective[row_index][column_index] = left_label
                inferred += 1

    paths: list[str] = []
    for column_index in range(physical.column_count):
        components: list[str] = []
        for row_index in range(physical.header_row_count):
            label = effective[row_index][column_index]
            if label and (not components or components[-1] != label):
                components.append(label)
        paths.append(" / ".join(components) or f"COLUMN_POSITION_{column_index}")
    return tuple(paths), inferred


def _row_segment(label: str) -> str:
    return classify_shell_row_segment(label).segment


def _deferred_row_contract(
    entity_id: str,
    *,
    frozen_column_ids: Sequence[str],
    shell_placeholder_row_count: int,
    template_placeholder_row_count: int,
    stable_row_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "detection": "nonsemantic_row_id",
        "materialize_at": "result_tfl",
        "shell_placeholder_row_count": shell_placeholder_row_count,
        "template_placeholder_row_count": template_placeholder_row_count,
        "stable_row_count": stable_row_count,
        "frozen_column_ids": list(frozen_column_ids),
        "expected_semantic_source": expected_result_row_source(entity_id),
    }


def _cell_type(row_id: str, column_id: str) -> str:
    if column_id == "UNIT":
        return "unit"
    leaf = row_id.rsplit("/", 1)[-1]
    if leaf == "P_VALUE":
        return "pvalue"
    if "CI" in leaf:
        return "ci95"
    if leaf in {"N", "COUNT"}:
        return "count"
    if "PCT" in leaf or "RATE" in leaf:
        return "n_pct"
    return "statistic"


def _table_shape(table: etree._Element) -> dict[str, int]:
    rows = table.findall(W("tr"))
    grid = table.find(W("tblGrid"))
    return {
        "rows": len(rows),
        "grid_columns": len(grid.findall(W("gridCol"))) if grid is not None else 0,
        "physical_cells": sum(len(row.findall(W("tc"))) for row in rows),
        "horizontal_merges": len(list(table.iter(W("gridSpan")))),
        "vertical_merges": len(list(table.iter(W("vMerge")))),
    }


def _build_cell_level_table(
    parsed,
    *,
    entity_id: str,
    population: str,
    dimensions: Mapping[str, str],
    source_id: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    physical = parsed.physical_table
    header_paths, visual_spanner_inferences = _flatten_header_paths_with_visual_spanners(
        physical
    )
    header_path_ambiguities = len(header_paths) - len(set(header_paths))
    raw_columns = list(header_paths[1:])
    column_ids = [_column_id(label) for label in raw_columns]
    if not raw_columns:
        raise ShellMappingError("Automatic column proposal found no result columns")
    seen_columns: Counter[str] = Counter()
    for index, column_id in enumerate(column_ids):
        seen_columns[column_id] += 1
        if seen_columns[column_id] > 1:
            column_ids[index] = f"{column_id}_C{index + 1}"
            header_path_ambiguities += 1

    row_ids_seen: set[str] = set()
    row_records: list[tuple[int, str, str | None, bool, str]] = []
    ambiguity_count = 0
    structural_group_hash_count = 0
    indexed_rows = list(
        enumerate(
            physical.rows[physical.header_row_count :],
            start=physical.header_row_count,
        )
    )
    logical_blocks: list[list[tuple[int, Any]]] = []
    current_block: list[tuple[int, Any]] = []
    block_has_value_row = False
    for row_index, row in indexed_rows:
        if not any(value.strip() for value in row.cells):
            if current_block:
                logical_blocks.append(current_block)
                current_block = []
                block_has_value_row = False
            continue
        has_result_values = any(value.strip() for value in row.cells[1:])
        is_structural = bool(row.cells[0].strip()) and not has_result_values
        if is_structural and block_has_value_row:
            logical_blocks.append(current_block)
            current_block = []
            block_has_value_row = False
        current_block.append((row_index, row))
        block_has_value_row = block_has_value_row or has_result_values
    if current_block:
        logical_blocks.append(current_block)

    persistent_stage: str | None = None
    stage_re = re.compile(r"^(?:D|DAY|WEEK|VISIT)_?\d+$", re.I)
    for block in logical_blocks:
        structural_segments: list[str] = []
        value_start = 0
        for position, (_row_index, row) in enumerate(block):
            if any(value.strip() for value in row.cells[1:]):
                value_start = position
                break
            classified = classify_shell_row_segment(row.cells[0].strip())
            segment = classified.segment
            if not classified.semantic:
                segment = "STRUCT_" + segment.removeprefix("ROW_")
                structural_group_hash_count += 1
            structural_segments.append(segment)
            value_start = position + 1
        stage_positions = [
            position
            for position, segment in enumerate(structural_segments)
            if stage_re.fullmatch(segment)
        ]
        if stage_positions:
            stage_position = stage_positions[-1]
            persistent_stage = structural_segments[stage_position]
            structural_segments = structural_segments[stage_position:]
        elif persistent_stage is not None and (
            not structural_segments or structural_segments[0] != persistent_stage
        ):
            structural_segments.insert(0, persistent_stage)

        stack: list[tuple[int, str, bool]] = []
        previous_value_segment: str | None = None
        for row_index, row in block[value_start:]:
            if not any(value.strip() for value in row.cells[1:]):
                continue
            raw_label = row.cells[0].strip()
            if raw_label:
                classified = classify_shell_row_segment(raw_label)
                segment = classified.segment
                segment_semantic = classified.semantic
                semantic_origin = classified.origin
                if (
                    segment == "CI95"
                    and previous_value_segment is not None
                    and any(
                        token in previous_value_segment
                        for token in ("LS_MEAN", "DIFFERENCE")
                    )
                ):
                    segment = previous_value_segment + "_CI95"
                    semantic_origin = "context_qualified_statistic"
            else:
                segment = f"ROW_POSITION_{row_index}"
                segment_semantic = False
                semantic_origin = "blank_position"
                ambiguity_count += 1
            indent = max(
                row.indents[0] if row.indents else 0,
                row.grid[0].indent_twips if row.grid else 0,
            )
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent_parts = structural_segments + [item[1] for item in stack]
            parent = "/".join(parent_parts) or None
            row_id = f"{parent}/{segment}" if parent else segment
            is_semantic = segment_semantic and all(item[2] for item in stack)
            stack_segment = segment
            if row_id in row_ids_seen:
                parent = f"POSITION_BLOCK_{row_index}"
                row_id = f"{parent}/{segment}"
                stack_segment = row_id
                is_semantic = False
                semantic_origin = "duplicate_position_disambiguation"
                ambiguity_count += 1
            row_ids_seen.add(row_id)
            if is_semantic != row_id_is_semantic(row_id):
                raise ShellMappingError("Internal row-semantic classification drift")
            row_records.append(
                (row_index, row_id, parent, is_semantic, semantic_origin)
            )
            stack.append((indent, stack_segment, is_semantic))
            previous_value_segment = segment
    if not row_records:
        raise ShellMappingError("Automatic proposal found no body rows")

    cells: dict[str, dict[str, Any]] = {}
    stable_records = [item for item in row_records if item[3]]
    placeholder_records = [item for item in row_records if not item[3]]
    for row_index, row_id, parent, _is_semantic, _origin in stable_records:
        for column_index, column_id in enumerate(column_ids, start=1):
            key = build_semantic_cell_key(
                entity_id,
                population,
                row_id,
                column_id,
                dimensions,
            )
            cells[key] = {
                "v": "xx",
                "display_value": "xx",
                "type": _cell_type(row_id, column_id),
                "row": {
                    "id": row_id,
                    "label": row_id,
                    "indent": row_id.count("/"),
                    "physical_path": row_id,
                    "parent_path": parent,
                },
                "column": {"id": column_id, "label": column_id},
                "source_locator": {
                    "source_id": source_id,
                    "row_index": row_index,
                    "column_index": column_index,
                },
                "dimensions": dict(dimensions),
            }
    if stable_records and placeholder_records:
        ingest_strategy = "mixed_stable_and_deferred_rows"
    elif placeholder_records:
        ingest_strategy = "columns_only_deferred_rows"
    else:
        ingest_strategy = "cell_level"
    table = {
        "table_id": entity_id,
        "population": population,
        "source_id": source_id,
        "titles": [],
        "footnotes": [],
        "trailer": {},
        "adapter": ADAPTER_ID,
        "ingest_strategy": ingest_strategy,
        "dimensions": dict(dimensions),
    }
    if placeholder_records:
        table["deferred_row_contract"] = _deferred_row_contract(
            entity_id,
            frozen_column_ids=column_ids,
            shell_placeholder_row_count=len(placeholder_records),
            template_placeholder_row_count=0,
            stable_row_count=len(stable_records),
        )
    proof = {
        "header_row_count": physical.header_row_count,
        "semantic_row_count": len(stable_records),
        "placeholder_row_count": len(placeholder_records),
        "semantic_column_count": len(column_ids),
        "semantic_cell_count": len(cells),
        "column_ids": column_ids,
        "row_ids_sha256": _canonical_hash([item[1] for item in stable_records]),
        "placeholder_row_ids_sha256": (
            _canonical_hash([item[1] for item in placeholder_records])
            if placeholder_records
            else None
        ),
        "expected_row_semantic_source": (
            expected_result_row_source(entity_id) if placeholder_records else None
        ),
        "automatic_row_ambiguities": ambiguity_count,
        "stable_structural_group_hashes": structural_group_hash_count,
        "automatic_header_ambiguities": header_path_ambiguities,
        "visual_spanner_inferences": visual_spanner_inferences,
    }
    return table, cells, proof


def _metadata_table(
    entity_id: str,
    population: str,
    source_id: str,
    dimensions: Mapping[str, str],
    reason: str,
) -> dict[str, Any]:
    return {
        "table_id": entity_id,
        "population": population,
        "source_id": source_id,
        "titles": [],
        "footnotes": [],
        "trailer": {},
        "adapter": ADAPTER_ID,
        "ingest_strategy": "metadata_only",
        "metadata_only_reason": reason,
        "dimensions": dict(dimensions),
    }


def _block_texts(
    body: etree._Element,
    *,
    start_index: int,
    end_index: int,
    styles: _StyleVisibility,
) -> tuple[str, ...]:
    texts: list[str] = []
    for child in body[start_index + 1 : end_index]:
        if child.tag == W("p"):
            value = _text(child, styles)
            if value:
                texts.append(value)
        elif child.tag == W("tbl"):
            for paragraph in child.iter(W("p")):
                value = _text(paragraph, styles)
                if value:
                    texts.append(value)
    return tuple(texts)


def _layout_matches(
    texts: Sequence[str], config: _CompiledLayoutConfig
) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for text_value in texts:
        for pattern_id, pattern in config.patterns:
            for match in pattern.finditer(text_value):
                target_number = match.group("target")
                matches.append(
                    {
                        "pattern_id": pattern_id,
                        "target": normalize_entity_id("T" + target_number),
                    }
                )
    return matches


def _is_data_driven_layout(
    title: str,
    parsed: Any,
    config: _CompiledLayoutConfig,
) -> tuple[bool, list[str]]:
    physical = parsed.physical_table
    evidence = [title]
    for row in physical.rows:
        if row.cells:
            evidence.append(row.cells[0])
    joined = "\n".join(evidence)
    matched = [
        pattern_id
        for pattern_id, pattern in config.data_driven_row_patterns
        if pattern.search(joined)
    ]
    return bool(matched), matched


def _inherit_layout(
    *,
    entity_id: str,
    population: str,
    dimensions: Mapping[str, str],
    source_id: str,
    target_entity_id: str,
    target_table: Mapping[str, Any],
    target_cells: Mapping[str, Mapping[str, Any]],
    target_proof: Mapping[str, Any],
    pattern_id: str,
    row_classifier_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    column_ids = list(target_proof.get("column_ids", []))
    if not column_ids:
        raise ShellMappingError(
            f"Layout-reference target {target_entity_id} has no inheritable columns"
        )
    target_deferred = target_table.get("deferred_row_contract")
    data_driven_rows = isinstance(target_deferred, Mapping)
    layout_reference = {
        "inherited_from": target_entity_id,
        "pattern_id": pattern_id,
        "columns": "inherited",
        "rows": "deferred_until_result" if data_driven_rows else "inherited_statistics",
        "inherited_column_ids": column_ids,
        "row_classifier_ids": list(row_classifier_ids),
    }
    table = {
        "table_id": entity_id,
        "population": population,
        "source_id": source_id,
        "titles": [],
        "footnotes": [],
        "trailer": {},
        "adapter": ADAPTER_ID,
        "ingest_strategy": "cell_level",
        "dimensions": dict(dimensions),
        "layout_reference": layout_reference,
    }
    if data_driven_rows:
        stable_rows = {
            parse_semantic_cell_key(key).row for key in target_cells
        }
        table["ingest_strategy"] = (
            "mixed_stable_and_deferred_rows"
            if target_cells
            else "columns_only_deferred_rows"
        )
        table["deferred_row_contract"] = _deferred_row_contract(
            entity_id,
            frozen_column_ids=column_ids,
            shell_placeholder_row_count=0,
            template_placeholder_row_count=int(
                target_deferred.get("shell_placeholder_row_count", 0)
            )
            + int(target_deferred.get("template_placeholder_row_count", 0)),
            stable_row_count=len(stable_rows),
        )
    if data_driven_rows and not target_cells:
        proof = {
            "header_row_count": target_proof.get("header_row_count", 0),
            "semantic_row_count": 0,
            "placeholder_row_count": int(
                table["deferred_row_contract"]["template_placeholder_row_count"]
            ),
            "semantic_column_count": len(column_ids),
            "semantic_cell_count": 0,
            "column_ids": column_ids,
            "row_ids_sha256": None,
            "automatic_row_ambiguities": 0,
            "automatic_header_ambiguities": target_proof.get(
                "automatic_header_ambiguities", 0
            ),
            "layout_inheritance": layout_reference,
        }
        return table, {}, proof

    inherited_cells: dict[str, dict[str, Any]] = {}
    for target_key, target_cell in target_cells.items():
        parsed_key = parse_semantic_cell_key(target_key)
        key = build_semantic_cell_key(
            entity_id,
            population,
            parsed_key.row,
            parsed_key.column,
            dimensions,
        )
        cell = copy.deepcopy(dict(target_cell))
        cell["v"] = "xx"
        cell["display_value"] = "xx"
        cell["dimensions"] = dict(dimensions)
        locator = dict(cell["source_locator"])
        locator["layout_inherited_from"] = target_entity_id
        cell["source_locator"] = locator
        inherited_cells[key] = cell
    if not inherited_cells:
        raise ShellMappingError(
            f"Layout-reference target {target_entity_id} has no inheritable statistic rows"
        )
    proof = {
        **dict(target_proof),
        "semantic_cell_count": len(inherited_cells),
        "layout_inheritance": layout_reference,
    }
    return table, inherited_cells, proof


def propose_docx_shell_map(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    map_version: str = "REAL-SHELL-M6-DEID-1",
    layout_reference_config: Mapping[str, Any] | str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return proposal report, privacy-safe shell datapack, and value-free map candidate."""

    started = time.perf_counter()
    source = Path(path).resolve()
    if source.suffix.lower() != ".docx" or not source.is_file():
        raise ShellMappingError("Real TFL shell input must be an existing DOCX")
    before_hash = sha256_file(source)
    if expected_sha256 is not None and before_hash != expected_sha256:
        raise ShellMappingError("Real TFL shell SHA-256 differs from the approved baseline")

    try:
        with zipfile.ZipFile(source) as archive:
            document = _parse_xml(archive.read("word/document.xml"), "word/document.xml")
            styles_root = (
                _parse_xml(archive.read("word/styles.xml"), "word/styles.xml")
                if "word/styles.xml" in archive.namelist()
                else None
            )
            ancillary_story_roots = [
                _parse_xml(archive.read(name), name)
                for name in archive.namelist()
                if re.fullmatch(r"word/(?:header|footer)\d*\.xml", name)
            ]
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ShellMappingError("Real TFL shell is not a valid OOXML package") from exc
    styles = _StyleVisibility(styles_root)
    body = document.find(W("body"))
    if body is None:
        raise ShellMappingError("Real TFL shell lacks a document body")
    tables = body.findall(W("tbl"))
    ancillary_tables = [
        table for root in ancillary_story_roots for table in root.iter(W("tbl"))
    ]
    ancillary_cells = sum(
        len(row.findall(W("tc")))
        for table in ancillary_tables
        for row in table.findall(W("tr"))
    )

    occurrences: list[_TitleOccurrence] = []
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
        occurrences.append(
            _TitleOccurrence(
                entity_id=normalize_entity_id(match.group(1) + match.group(2)),
                title=title,
                body_index=body_index,
                tables_before=table_count,
            )
        )
    if not occurrences:
        raise ShellMappingError("No TFL entity titles were found in the real shell")
    final_occurrence: dict[str, _TitleOccurrence] = {}
    for item in occurrences:
        final_occurrence[item.entity_id] = item
    actual_titles = sorted(final_occurrence.values(), key=lambda item: item.body_index)

    layout_config = _load_layout_reference_config(layout_reference_config)
    source_id = f"sha256:{before_hash}"
    datapack_tables: list[dict[str, Any]] = []
    datapack_cells: dict[str, dict[str, Any]] = {}
    entity_proposals: list[dict[str, Any]] = []
    review_buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    object_roles: dict[int, str] = {
        index: "front_matter" for index in range(1, actual_titles[0].tables_before + 1)
    }

    blocks: list[dict[str, Any]] = []
    own_grids: dict[str, dict[str, Any]] = {}
    layout_reference_occurrences = 0
    for position, title_record in enumerate(actual_titles):
        next_tables_before = (
            actual_titles[position + 1].tables_before
            if position + 1 < len(actual_titles)
            else len(tables)
        )
        table_indexes = list(
            range(title_record.tables_before + 1, next_tables_before + 1)
        )
        for offset, table_index in enumerate(table_indexes):
            object_roles[table_index] = "entity_main" if offset == 0 else "entity_attachment"

        entity_id = title_record.entity_id
        population, recognized_analysis_set = _analysis_set_details(title_record.title)
        dimensions = _stage(title_record.title)
        kind = {"T": "table", "F": "figure", "L": "listing"}[entity_id[0]]
        shape = _table_shape(tables[table_indexes[0] - 1]) if table_indexes else None
        metadata_reason: str | None = None
        if kind in {"figure", "listing"}:
            metadata_reason = "non-tabular TFL entity; table-level provenance only"
        elif entity_id == "T14.1.1.1":
            metadata_reason = "subject-level wide disposition listing; privacy-minimizing metadata only"
        elif not table_indexes:
            metadata_reason = "entity has no Word table object in the shell"
        elif len(table_indexes) != 1:
            metadata_reason = "entity block contains multiple Word table objects"
        elif shape is None or shape["grid_columns"] < 2 or shape["rows"] < 2:
            metadata_reason = "shell contains no usable row-by-column grid"

        parsed = None
        parse_error: str | None = None
        if metadata_reason is None:
            try:
                parsed = _parse_table(
                    tables[table_indexes[0] - 1],
                    table_index=table_indexes[0],
                    styles=styles,
                    allow_first_row_header=True,
                    allow_ambiguous_header_paths=True,
                )
                table_payload, cell_payload, proof = _build_cell_level_table(
                    parsed,
                    entity_id=entity_id,
                    population=population,
                    dimensions=dimensions,
                    source_id=source_id,
                )
                own_grids[entity_id] = {
                    "table": table_payload,
                    "cells": cell_payload,
                    "proof": proof,
                    "parsed": parsed,
                }
            except (DocxTFLParseError, ShellMappingError, ValueError) as exc:
                metadata_reason = "automatic physical-to-semantic proposal was ambiguous"
                parse_error = str(exc)

        next_body_index = (
            actual_titles[position + 1].body_index
            if position + 1 < len(actual_titles)
            else len(body)
        )
        block_matches = _layout_matches(
            _block_texts(
                body,
                start_index=title_record.body_index,
                end_index=next_body_index,
                styles=styles,
            ),
            layout_config,
        )
        layout_reference_occurrences += len(block_matches)
        blocks.append(
            {
                "title_record": title_record,
                "table_indexes": table_indexes,
                "population": population,
                "recognized_analysis_set": recognized_analysis_set,
                "dimensions": dimensions,
                "kind": kind,
                "shape": shape,
                "metadata_reason": metadata_reason,
                "parse_error": parse_error,
                "layout_matches": block_matches,
            }
        )

    # Only a one-column/no-grid placeholder is eligible for layout inheritance.
    # Notes inside genuine grids or multi-object entities remain ordinary content.
    layout_references: dict[str, dict[str, str]] = {}
    for block in blocks:
        if block["metadata_reason"] != "shell contains no usable row-by-column grid":
            continue
        matches = block["layout_matches"]
        targets = {item["target"] for item in matches}
        if len(targets) > 1:
            raise ShellMappingError(
                f"Layout-reference placeholder {block['title_record'].entity_id} names multiple targets"
            )
        if not targets:
            continue
        target = next(iter(targets))
        pattern_ids = {item["pattern_id"] for item in matches if item["target"] == target}
        layout_references[block["title_record"].entity_id] = {
            "target": target,
            "pattern_id": sorted(pattern_ids)[0],
        }

    # Detect cycles before target validation so a self/indirect cycle never degrades to metadata-only.
    for start in layout_references:
        path_seen: set[str] = set()
        cursor = start
        while cursor in layout_references:
            if cursor in path_seen:
                raise ShellMappingError(f"Layout-reference cycle detected from {start}")
            path_seen.add(cursor)
            cursor = layout_references[cursor]["target"]
    for entity_id, reference in layout_references.items():
        target = reference["target"]
        if target not in final_occurrence:
            raise ShellMappingError(
                f"Layout-reference target does not exist: {entity_id} -> {target}"
            )
        if target in layout_references or target not in own_grids:
            raise ShellMappingError(
                f"Layout-reference target has no independent usable grid: {entity_id} -> {target}"
            )

    inherited_grid_entities = 0
    mixed_inheritance_entities = 0
    for block in blocks:
        title_record = block["title_record"]
        entity_id = title_record.entity_id
        population = block["population"]
        recognized_analysis_set = block["recognized_analysis_set"]
        dimensions = block["dimensions"]
        kind = block["kind"]
        routes = _route_sections(entity_id)
        confidence = "high"
        reasons: list[str] = []
        proof: dict[str, Any]

        if entity_id in own_grids:
            grid = own_grids[entity_id]
            table_payload = copy.deepcopy(grid["table"])
            cell_payload = copy.deepcopy(grid["cells"])
            proof = copy.deepcopy(grid["proof"])
            if table_payload.get("deferred_row_contract") is not None:
                confidence = "medium"
                reasons.append(
                    "one or more shell rows lacked readable semantic IDs and were deferred"
                )
            if proof["automatic_row_ambiguities"]:
                confidence = "medium"
                reasons.append("one or more row paths required positional disambiguation")
            if proof["automatic_header_ambiguities"]:
                confidence = "medium"
                reasons.append("one or more header paths required positional disambiguation")
        elif entity_id in layout_references:
            reference = layout_references[entity_id]
            target_id = reference["target"]
            target_grid = own_grids[target_id]
            target_title = final_occurrence[target_id].title
            _title_classifier_match, classifier_ids = _is_data_driven_layout(
                target_title,
                target_grid["parsed"],
                layout_config,
            )
            data_driven_rows = target_grid["table"].get("deferred_row_contract") is not None
            table_payload, cell_payload, proof = _inherit_layout(
                entity_id=entity_id,
                population=population,
                dimensions=dimensions,
                source_id=source_id,
                target_entity_id=target_id,
                target_table=target_grid["table"],
                target_cells=target_grid["cells"],
                target_proof=target_grid["proof"],
                pattern_id=reference["pattern_id"],
                row_classifier_ids=classifier_ids,
            )
            inherited_grid_entities += 1
            if data_driven_rows:
                mixed_inheritance_entities += 1
                confidence = "medium"
            reasons.append(
                "layout reference resolved; columns inherited and rows "
                + ("deferred as data-driven" if data_driven_rows else "inherited as statistics")
            )
        else:
            metadata_reason = block["metadata_reason"] or (
                "automatic physical-to-semantic proposal was ambiguous"
            )
            table_payload = _metadata_table(
                entity_id,
                population,
                source_id,
                dimensions,
                metadata_reason,
            )
            cell_payload = {}
            if kind == "table" and entity_id != "T14.1.1.1":
                confidence = "low"
                if block["parse_error"]:
                    reasons.append(block["parse_error"])
                reasons.append(metadata_reason)
            proof = {
                "semantic_row_count": 0,
                "semantic_column_count": 0,
                "semantic_cell_count": 0,
            }

        datapack_tables.append(table_payload)
        for key, value in cell_payload.items():
            if key in datapack_cells:
                raise ShellMappingError(f"Cross-entity semantic key collision: {key}")
            datapack_cells[key] = value
        if not routes:
            confidence = "low"
            reasons.append("ICH E3 route could not be proposed from the configured numbering profile")
        if population == "UNSPECIFIED":
            confidence = "low"
            reasons.insert(
                0,
                "analysis set was not parsed from the title; POP=UNSPECIFIED requires revision",
            )
        elif not recognized_analysis_set:
            confidence = "low"
            reasons.insert(
                0,
                "analysis set used a safe non-registry title marker; exact spelling requires confirmation",
            )

        if confidence != "high":
            reason_key = reasons[0] if reasons else "medium_confidence"
            # One batch confirms one deterministic policy across all matching tables.
            # The queue still lists every affected entity, so the action is auditable
            # without forcing a reviewer to repeat the same decision per table.
            bucket = (reason_key, "POLICY_GROUP")
            review_buckets[bucket].append(entity_id)
        entity_proposals.append(
            {
                "entity_id": entity_id,
                "entity_kind": kind,
                "analysis_set": population,
                "dimensions": dict(dimensions),
                "ich_e3_sections": routes,
                "confidence": confidence,
                "title_sha256": hashlib.sha256(
                    title_record.title.encode("utf-8")
                ).hexdigest(),
                "title_character_count": len(title_record.title),
                "physical_table_indexes": block["table_indexes"],
                "physical_shape": block["shape"],
                "ingest_strategy": table_payload["ingest_strategy"],
                "layout_reference": copy.deepcopy(
                    table_payload.get("layout_reference")
                ),
                "deferred_row_contract": copy.deepcopy(
                    table_payload.get("deferred_row_contract")
                ),
                "reasons": reasons,
                "semantic_proof": proof,
                "batch_id": None,
            }
        )

    review_queue: list[dict[str, Any]] = []
    entity_to_batch: dict[str, str] = {}
    for index, ((reason, family), entity_ids) in enumerate(
        sorted(review_buckets.items()), start=1
    ):
        batch_id = f"BATCH-{index:03d}"
        for entity_id in entity_ids:
            entity_to_batch[entity_id] = batch_id
        review_queue.append(
            {
                "batch_id": batch_id,
                "family": family,
                "reason": reason,
                "entity_count": len(entity_ids),
                "entity_ids": sorted(entity_ids),
                "allowed_decisions": ["confirm", "revise", "defer"],
            }
        )
    for item in entity_proposals:
        item["batch_id"] = entity_to_batch.get(item["entity_id"])

    shell_datapack = {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "adapter_scope": "privacy-safe real DOCX shell proposal",
        "listing_policy": {
            "default": "cell_level",
            "subject_level_wide_listing": "metadata_only",
            "layout_reference_data_driven_rows": "columns_only_deferred_rows",
            "mixed_placeholder_rows": "mixed_stable_and_deferred_rows",
            "rationale": (
                "subject-level cells and non-semantic shell placeholder rows are not "
                "materialized into frozen result keys"
            ),
        },
        "mapping_manifest": {
            "filename": "deidentified-real-shell-proposal.json",
            "sha256": _canonical_hash(
                {
                    "map_version": map_version,
                    "entity_ids": [item["entity_id"] for item in entity_proposals],
                }
            ),
        },
        "sources": [
            {
                "source_id": source_id,
                "filename": "real-shell.docx",
                "manifest_path": "read-only external source",
                "format": ADAPTER_ID,
                "decode_replacements": 0,
            }
        ],
        "tables": datapack_tables,
        "cells": datapack_cells,
    }
    validate_datapack(shell_datapack)
    route_config = {
        "schema_version": "1.0",
        "map_version": map_version,
        "numbering_system": {
            "id": "configured-ich-14x",
            "description": "explicit ICH 14.x to CSR section profile",
        },
        "routes": [
            {
                "entity_id": item["entity_id"],
                "ich_e3_sections": item["ich_e3_sections"],
            }
            for item in entity_proposals
        ],
    }
    if any(not item["ich_e3_sections"] for item in entity_proposals):
        raise ShellMappingError("One or more real-shell entities lack an explicit ICH route")
    candidate = build_tfl_map_candidate(shell_datapack, route_config)

    elapsed = time.perf_counter() - started
    physical_object_count = len(tables)
    high_entities = sum(item["confidence"] == "high" for item in entity_proposals)
    review_entities = len(entity_proposals) - high_entities
    role_counts = Counter(object_roles.values())
    report_body = {
        "schema_version": "1.0",
        "status": "review_required" if review_queue else "pass",
        "privacy": {
            "source_path_written": False,
            "source_titles_written": False,
            "source_cell_labels_written": False,
            "subject_identifiers_written": False,
        },
        "source_sha256": before_hash,
        "metrics": {
            "tfl_body_table_objects": physical_object_count,
            "all_story_table_objects": physical_object_count + len(ancillary_tables),
            "tfl_body_physical_cells": sum(
                shape["physical_cells"] for shape in map(_table_shape, tables)
            ),
            "all_story_table_cells": sum(
                shape["physical_cells"] for shape in map(_table_shape, tables)
            )
            + ancillary_cells,
            "unique_tfl_entities": len(entity_proposals),
            "table_entities": sum(item["entity_kind"] == "table" for item in entity_proposals),
            "figure_entities": sum(item["entity_kind"] == "figure" for item in entity_proposals),
            "listing_entities": sum(item["entity_kind"] == "listing" for item in entity_proposals),
            "object_level_auto_proposals": physical_object_count,
            "object_level_proposal_rate": 1.0,
            "high_confidence_entities": high_entities,
            "batch_review_entities": review_entities,
            "human_review_batches": len(review_queue),
            "high_confidence_entity_rate": round(high_entities / len(entity_proposals), 4),
            "unresolved_analysis_set_entities": sum(
                item["analysis_set"] == "UNSPECIFIED" for item in entity_proposals
            ),
            "cell_level_entities": sum(
                item["ingest_strategy"] == "cell_level" for item in entity_proposals
            ),
            "metadata_only_entities": sum(
                item["ingest_strategy"] == "metadata_only" for item in entity_proposals
            ),
            "columns_only_deferred_rows_entities": sum(
                item["ingest_strategy"] == "columns_only_deferred_rows"
                for item in entity_proposals
            ),
            "mixed_stable_and_deferred_rows_entities": sum(
                item["ingest_strategy"] == "mixed_stable_and_deferred_rows"
                for item in entity_proposals
            ),
            "deferred_shell_placeholder_rows": sum(
                int(item["semantic_proof"].get("placeholder_row_count", 0))
                for item in entity_proposals
            ),
            "layout_reference_occurrences_detected": layout_reference_occurrences,
            "layout_reference_entities": len(layout_references),
            "layout_inherited_grid_entities": inherited_grid_entities,
            "layout_mixed_column_inherited_row_deferred_entities": mixed_inheritance_entities,
            "layout_full_grid_inherited_entities": (
                inherited_grid_entities - mixed_inheritance_entities
            ),
            "object_roles": dict(sorted(role_counts.items())),
            "elapsed_seconds": round(elapsed, 3),
        },
        "layout_reference_config": {
            "sha256": layout_config.sha256,
            "pattern_ids": [item[0] for item in layout_config.patterns],
            "data_driven_row_classifier_ids": [
                item[0] for item in layout_config.data_driven_row_patterns
            ],
        },
        "prefix_normalization": {
            "表": "T",
            "图": "F",
            "清单": "L",
            "ascii_key_layer": True,
        },
        "entities": entity_proposals,
        "review_queue": review_queue,
        "candidate_sha256": candidate["candidate_sha256"],
    }
    report = {**report_body, "report_sha256": _canonical_hash(report_body)}
    after_hash = sha256_file(source)
    if before_hash != after_hash:
        raise ShellMappingError("Real TFL shell changed during read-only mapping")
    return report, shell_datapack, candidate


def validate_batch_confirmation(
    report: Mapping[str, Any], confirmation: Mapping[str, Any]
) -> dict[str, Any]:
    """Require one decision per ambiguity batch before a candidate can be frozen."""

    if not isinstance(report, Mapping) or not isinstance(confirmation, Mapping):
        raise ShellMappingError("Proposal report and batch confirmation must be objects")
    expected_report_hash = report.get("report_sha256")
    if not isinstance(expected_report_hash, str):
        raise ShellMappingError("Proposal report lacks its canonical hash")
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    if expected_report_hash != _canonical_hash(body):
        raise ShellMappingError("Proposal report hash mismatch")
    required = {"schema_version", "report_sha256", "confirmed_by_role", "confirmed_at", "decisions"}
    if set(confirmation) != required or confirmation.get("schema_version") != "1.0":
        raise ShellMappingError("Batch confirmation contract drift")
    if confirmation.get("report_sha256") != expected_report_hash:
        raise ShellMappingError("Batch confirmation is bound to another proposal report")
    decisions = confirmation.get("decisions")
    if not isinstance(decisions, list):
        raise ShellMappingError("Batch confirmation decisions must be a list")
    expected_batches = {item["batch_id"] for item in report.get("review_queue", [])}
    observed: dict[str, str] = {}
    for index, item in enumerate(decisions):
        if not isinstance(item, Mapping) or set(item) != {"batch_id", "decision"}:
            raise ShellMappingError(f"Malformed batch decision {index}")
        batch_id = item.get("batch_id")
        decision = item.get("decision")
        if not isinstance(batch_id, str) or batch_id in observed:
            raise ShellMappingError(f"Invalid or duplicate batch decision: {batch_id!r}")
        if decision not in {"confirm", "defer"}:
            raise ShellMappingError("Only confirmed or explicitly deferred batches can be recorded")
        observed[batch_id] = str(decision)
    if set(observed) != expected_batches:
        raise ShellMappingError(
            f"Batch decisions must exactly cover the queue; missing={sorted(expected_batches-set(observed))}, "
            f"extra={sorted(set(observed)-expected_batches)}"
        )
    if any(decision != "confirm" for decision in observed.values()):
        raise ShellMappingError("TFL map freeze is blocked while any ambiguity batch is deferred")
    return {
        "status": "pass",
        "confirmed_batch_count": len(observed),
        "confirmed_entity_count": sum(
            item["entity_count"] for item in report.get("review_queue", [])
        ),
        "confirmed_by_role": confirmation.get("confirmed_by_role"),
        "confirmed_at": confirmation.get("confirmed_at"),
    }


def freeze_proposed_shell_map(
    report: Mapping[str, Any],
    candidate: Mapping[str, Any],
    batch_confirmation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze the value-free candidate only after all batch decisions are confirmed."""

    unresolved = sorted(
        item["entity_id"]
        for item in report.get("entities", [])
        if item.get("analysis_set") == "UNSPECIFIED"
    )
    if unresolved:
        raise ShellMappingError(
            "E-ANALYSIS-SET-UNSPECIFIED: map freeze requires an explicit analysis "
            f"set revision for {unresolved}"
        )
    proof = validate_batch_confirmation(report, batch_confirmation)
    if candidate.get("candidate_sha256") != report.get("candidate_sha256"):
        raise ShellMappingError("Proposal report and value-free candidate hashes disagree")
    role = batch_confirmation.get("confirmed_by_role")
    confirmed_at = batch_confirmation.get("confirmed_at")
    if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", role):
        raise ShellMappingError("confirmed_by_role must be a role identifier, not a name")
    if not isinstance(confirmed_at, str) or not confirmed_at.strip():
        raise ShellMappingError("confirmed_at must be a non-empty timestamp")
    frozen = freeze_tfl_map(
        candidate,
        {
            "status": "confirmed",
            "confirmation_id": "SHELL-BATCH-" + str(report["report_sha256"])[:16],
            "confirmed_by_role": role,
            "confirmed_at": confirmed_at,
            "candidate_sha256": candidate["candidate_sha256"],
        },
    )
    return frozen, {
        **proof,
        "status": "pass",
        "candidate_sha256": candidate["candidate_sha256"],
        "frozen_sha256": frozen["frozen_sha256"],
    }
