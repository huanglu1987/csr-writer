"""M4 deterministic reverse traceability for TFL result tables."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .datapack import CELL_KEY_RE, validate_datapack
from .errors import CSRWriterError
from .io_utils import atomic_write_text
from .semantic_key import SemanticKeyError, validate_table_id
from .tfl_routing import expected_tfl_route


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _table_sort_key(table_id: str) -> tuple[int, tuple[int, ...], str]:
    return (
        {"T": 0, "F": 1, "L": 2}[table_id[0]],
        tuple(int(part) for part in table_id[1:].split(".")),
        table_id,
    )


class TraceabilityError(CSRWriterError):
    """The datapack/metadata graph is incomplete or internally inconsistent."""

    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        self.report = {
            "schema_version": "1.0",
            "status": "blocked",
            "issues": [{"code": code, "message": message, **details}],
        }
        super().__init__(f"{code}: {message}")


def _metadata_items(
    section_metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    if isinstance(section_metadata, Mapping):
        raw_items: list[Any] = [section_metadata]
    elif isinstance(section_metadata, Sequence) and not isinstance(
        section_metadata, (str, bytes)
    ):
        raw_items = list(section_metadata)
    else:
        raise TraceabilityError(
            "E-TRACE-METADATA",
            "section_metadata must be an object or list of objects",
        )

    normalized: list[tuple[str, list[Mapping[str, Any]]]] = []
    seen_instances: set[tuple[str, str | None]] = set()
    for index, metadata in enumerate(raw_items):
        if not isinstance(metadata, Mapping):
            raise TraceabilityError(
                "E-TRACE-METADATA",
                f"section_metadata[{index}] must be an object",
            )
        section = metadata.get("section") or metadata.get("section_id")
        if not isinstance(section, str) or not section.strip():
            raise TraceabilityError(
                "E-TRACE-METADATA",
                f"section_metadata[{index}] needs section or section_id",
            )
        variant = metadata.get("variant")
        if variant is not None and not isinstance(variant, str):
            raise TraceabilityError(
                "E-TRACE-METADATA",
                f"section_metadata[{index}].variant must be a string when present",
            )
        instance = (section, variant)
        if instance in seen_instances:
            raise TraceabilityError(
                "E-TRACE-METADATA",
                f"Duplicate section metadata instance: {section}/{variant or 'default'}",
                section=section,
            )
        seen_instances.add(instance)
        bindings = metadata.get("binding_manifest")
        if not isinstance(bindings, list) or any(
            not isinstance(binding, Mapping) for binding in bindings
        ):
            raise TraceabilityError(
                "E-TRACE-METADATA",
                f"Section {section} binding_manifest must be a list of objects",
                section=section,
            )
        normalized.append((section, list(bindings)))
    return sorted(normalized, key=lambda item: item[0])


def _table_index(datapack: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    tables = datapack.get("tables")
    if not isinstance(tables, list) or not tables:
        raise TraceabilityError("E-TRACE-TABLES", "datapack.tables must be a non-empty list")
    index: dict[str, Mapping[str, Any]] = {}
    for position, table in enumerate(tables):
        if not isinstance(table, Mapping):
            raise TraceabilityError(
                "E-TRACE-TABLES", f"datapack.tables[{position}] must be an object"
            )
        table_id = table.get("table_id")
        try:
            validate_table_id(table_id)
        except SemanticKeyError:
            raise TraceabilityError(
                "E-TRACE-TABLE-ID", f"Invalid table_id at tables[{position}]: {table_id!r}"
            )
        if table_id in index:
            raise TraceabilityError(
                "E-TRACE-TABLE-ID", f"Duplicate datapack table_id: {table_id}", table_id=table_id
            )
        index[table_id] = table

    cell_tables: set[str] = set()
    for key in datapack["cells"]:
        match = CELL_KEY_RE.fullmatch(key)
        if match is None:
            raise TraceabilityError("E-TRACE-CELL", f"Invalid datapack cell key: {key}")
        cell_tables.add(match.group("table"))
    unknown_cell_tables = sorted(cell_tables - set(index), key=_table_sort_key)
    empty_tables = sorted(
        (
            table_id
            for table_id in set(index) - cell_tables
            if index[table_id].get("ingest_strategy")
            not in {"metadata_only", "columns_only_deferred_rows"}
        ),
        key=_table_sort_key,
    )
    if unknown_cell_tables:
        raise TraceabilityError(
            "E-TRACE-CELL-TABLE",
            f"Cells point to table IDs absent from datapack.tables: {unknown_cell_tables}",
            table_ids=unknown_cell_tables,
        )
    if empty_tables:
        raise TraceabilityError(
            "E-TRACE-EMPTY-TABLE",
            f"datapack.tables contains non-metadata tables without mapped result cells: {empty_tables}",
            table_ids=empty_tables,
        )
    return index


def analyze_tfl_references(
    datapack: Mapping[str, Any],
    section_metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return deterministic reverse-traceability results.

    Unreferenced tables are review findings, not a structural exception.
    Conversely, an unknown table/cell reference raises ``TraceabilityError`` so
    assembly cannot silently treat a broken provenance link as coverage.
    """

    if not isinstance(datapack, Mapping):
        raise TraceabilityError("E-TRACE-DATAPACK", "datapack must be an object")
    validate_datapack(dict(datapack))
    table_index = _table_index(datapack)
    metadata = _metadata_items(section_metadata)
    references: dict[str, set[str]] = defaultdict(set)
    reference_counts: dict[str, int] = defaultdict(int)

    for section, bindings in metadata:
        section_roles: set[str] = set()
        section_keys: set[str] = set()
        for index, binding in enumerate(bindings):
            role = binding.get("role")
            key = binding.get("key")
            source_id = binding.get("source_id")
            if not all(isinstance(value, str) for value in (role, key, source_id)):
                raise TraceabilityError(
                    "E-TRACE-BINDING",
                    f"Section {section} binding {index} needs string role/key/source_id",
                    section=section,
                    binding_index=index,
                )
            if role in section_roles or key in section_keys:
                raise TraceabilityError(
                    "E-TRACE-BINDING-DUPLICATE",
                    f"Section {section} repeats a binding role or semantic key",
                    section=section,
                    role=role,
                    key=key,
                )
            section_roles.add(role)
            section_keys.add(key)
            match = CELL_KEY_RE.fullmatch(key)
            if match is None:
                raise TraceabilityError(
                    "E-TRACE-BINDING-KEY",
                    f"Section {section} role {role} has an invalid semantic key",
                    section=section,
                    role=role,
                    key=key,
                )
            table_id = match.group("table")
            if source_id != table_id:
                raise TraceabilityError(
                    "E-TRACE-BINDING-SOURCE",
                    f"Section {section} role {role} source_id disagrees with key table",
                    section=section,
                    role=role,
                    source_id=source_id,
                    table_id=table_id,
                )
            if table_id not in table_index:
                raise TraceabilityError(
                    "E-TRACE-UNKNOWN-TABLE",
                    f"Section {section} role {role} references unknown table {table_id}",
                    section=section,
                    role=role,
                    table_id=table_id,
                )
            if key not in datapack["cells"]:
                raise TraceabilityError(
                    "E-TRACE-UNKNOWN-CELL",
                    f"Section {section} role {role} references an unknown datapack cell",
                    section=section,
                    role=role,
                    key=key,
                )
            references[table_id].add(section)
            reference_counts[table_id] += 1

    metadata_only_ids = sorted(
        (
            table_id
            for table_id, table in table_index.items()
            if table.get("ingest_strategy") == "metadata_only"
        ),
        key=_table_sort_key,
    )
    deferred_row_ids = sorted(
        (
            table_id
            for table_id, table in table_index.items()
            if table.get("ingest_strategy") == "columns_only_deferred_rows"
            and not any(key.startswith(table_id + "|") for key in datapack["cells"])
        ),
        key=_table_sort_key,
    )
    all_table_ids = sorted(
        set(table_index) - set(metadata_only_ids) - set(deferred_row_ids),
        key=_table_sort_key,
    )
    referenced_ids = [table_id for table_id in all_table_ids if references[table_id]]
    unreferenced_ids = [table_id for table_id in all_table_ids if not references[table_id]]

    def table_record(table_id: str) -> dict[str, Any]:
        table = table_index[table_id]
        raw_titles = table.get("titles", [])
        titles = [str(title) for title in raw_titles] if isinstance(raw_titles, list) else []
        expected = expected_tfl_route(table_id)
        return {
            "table_id": table_id,
            "population": str(table.get("population", "")),
            "titles": titles,
            "source_id": str(table.get("source_id", "")),
            "referenced_by_sections": sorted(references[table_id]),
            "binding_reference_count": reference_counts[table_id],
            "expected_route": (
                {
                    "section_id": expected.section_id,
                    "category_id": expected.category_id,
                    "label_zh": expected.label_zh,
                }
                if expected is not None
                else None
            ),
        }

    drafted_sections = {section for section, _ in metadata}
    grouped: dict[str, dict[str, Any]] = {}
    unclassified: list[dict[str, Any]] = []
    for table_id in unreferenced_ids:
        record = table_record(table_id)
        route = record["expected_route"]
        if route is None:
            unclassified.append(record)
            continue
        category_id = route["category_id"]
        group = grouped.setdefault(
            category_id,
            {
                "category_id": category_id,
                "expected_section": route["section_id"],
                "label_zh": route["label_zh"],
                "draft_status": (
                    "drafted" if route["section_id"] in drafted_sections else "not_yet_drafted"
                ),
                "priority": "planned_section_follow_up",
                "tables": [],
            },
        )
        group["tables"].append(record)
    grouped_records = sorted(
        (
            {**group, "table_count": len(group["tables"])}
            for group in grouped.values()
        ),
        key=lambda item: (item["expected_section"], item["category_id"]),
    )

    return {
        "schema_version": "1.0",
        "status": "review_required" if unreferenced_ids else "pass",
        "datapack_sha256": _canonical_hash(datapack),
        "metadata_sha256": _canonical_hash(
            [{"section": section, "binding_manifest": bindings} for section, bindings in metadata]
        ),
        "table_count": len(all_table_ids),
        "referenced_table_count": len(referenced_ids),
        "unreferenced_table_count": len(unreferenced_ids),
        "metadata_only_table_count": len(metadata_only_ids),
        "metadata_only_tables": [table_record(table_id) for table_id in metadata_only_ids],
        "deferred_row_table_count": len(deferred_row_ids),
        "deferred_row_tables": [table_record(table_id) for table_id in deferred_row_ids],
        "referenced_tables": [table_record(table_id) for table_id in referenced_ids],
        "unreferenced_tables": [table_record(table_id) for table_id in unreferenced_ids],
        "unreferenced_by_expected_section": grouped_records,
        "unclassified_unreferenced_count": len(unclassified),
        "unclassified_unreferenced_tables": unclassified,
        "immediate_human_review_count": len(unclassified),
        "triage_contract": {
            "known_expected_section": "planned_section_follow_up",
            "unclassified": "immediate_human_review",
        },
        "issues": [
            {
                "code": (
                    "W-UNCLASSIFIED-TFL"
                    if expected_tfl_route(table_id) is None
                    else "I-EXPECTED-SECTION-NOT-REFERENCED"
                ),
                "table_id": table_id,
                "priority": (
                    "immediate_human_review"
                    if expected_tfl_route(table_id) is None
                    else "planned_section_follow_up"
                ),
                "message": (
                    "TFL has no deterministic expected CSR section"
                    if expected_tfl_route(table_id) is None
                    else "TFL is grouped under its expected CSR section for coverage follow-up"
                ),
            }
            for table_id in unreferenced_ids
        ],
    }


def _markdown_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def render_unreferenced_tfl(report: Mapping[str, Any]) -> str:
    """Render only an already-validated reverse-traceability report."""

    if report.get("schema_version") != "1.0" or report.get("status") not in {
        "pass",
        "review_required",
    }:
        raise TraceabilityError(
            "E-TRACE-REPORT", "Cannot render a blocked or malformed traceability report"
        )
    unreferenced = report.get("unreferenced_tables")
    if not isinstance(unreferenced, list):
        raise TraceabilityError(
            "E-TRACE-REPORT", "Traceability report lacks unreferenced_tables"
        )

    grouped = report.get("unreferenced_by_expected_section")
    unclassified = report.get("unclassified_unreferenced_tables")
    if not isinstance(grouped, list) or not isinstance(unclassified, list):
        raise TraceabilityError(
            "E-TRACE-REPORT",
            "Traceability report lacks expected-section grouping",
        )

    lines = [
        "# 未引用 TFL 反向溯源",
        "",
        "> 本清单将未引用结果按预期 CSR 章节分流。只有“无法归类”项需要立即人工判断；其余进入相应章节的起草或绑定队列。",
        "",
        f"- 检查表数：{report['table_count']}",
        f"- 已引用表数：{report['referenced_table_count']}",
        f"- 未引用表数：{report['unreferenced_table_count']}",
        f"- 仅登记表级元数据（不逐单元格建键）：{report.get('metadata_only_table_count', 0)}",
        f"- 等待结果 TFL 展开数据驱动行：{report.get('deferred_row_table_count', 0)}",
        f"- 需立即人工判断：{report.get('immediate_human_review_count', 0)}",
        "",
    ]
    if not unreferenced:
        lines.append("未发现有映射结果但未被章节引用的 TFL。")
        lines.append("")
        return "\n".join(lines)

    lines.extend(["## 未引用 TFL 按预期归属分类", ""])
    def route_label(group: Mapping[str, Any]) -> str:
        section = str(group.get("expected_section", ""))
        label = _markdown_escape(group.get("label_zh", ""))
        return re.sub(rf"^§{re.escape(section)}\s*", "", label).strip()

    for group in grouped:
        drafted_label = "已起草但尚未绑定" if group.get("draft_status") == "drafted" else "尚未起草"
        lines.append(
            f"- 应归 §{_markdown_escape(group.get('expected_section', ''))} "
            f"{route_label(group)}（{drafted_label}）："
            f"{group.get('table_count', 0)} 张"
        )
    lines.append(f"- ⚠ 无法归类、需人工判断：{len(unclassified)} 张")
    lines.append("")

    def append_table_rows(tables: Sequence[Mapping[str, Any]], disposition: str) -> None:
        lines.extend(
            [
                "| 表号 | 分析集 | 标题 | 来源标识 | 处置 |",
                "|---|---|---|---|---|",
            ]
        )
        for table in tables:
            title = " / ".join(table.get("titles", []))
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_escape(table.get("table_id", "")),
                        _markdown_escape(table.get("population", "")),
                        _markdown_escape(title),
                        _markdown_escape(table.get("source_id", "")),
                        disposition,
                    ]
                )
                + " |"
            )
        lines.append("")

    for group in grouped:
        lines.extend(
            [
                f"### §{_markdown_escape(group.get('expected_section', ''))} "
                f"{route_label(group)}",
                "",
            ]
        )
        append_table_rows(group.get("tables", []), "纳入该章节后续起草或绑定队列")

    lines.extend(["### ⚠ 无法归类、需人工判断", ""])
    if unclassified:
        append_table_rows(unclassified, "人工确认是否遗漏及其应引用章节")
    else:
        lines.extend(["未发现无法确定预期 CSR 章节的 TFL。", ""])
    return "\n".join(lines)


def write_unreferenced_tfl(
    datapack: Mapping[str, Any],
    section_metadata: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    output_path: str | Path,
) -> dict[str, Any]:
    """Analyze and atomically write ``unreferenced_tfl.md``."""

    report = analyze_tfl_references(datapack, section_metadata)
    atomic_write_text(output_path, render_unreferenced_tfl(report))
    return report
