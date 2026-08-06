"""Dedicated OOXML TFL table parser with explicit merge semantics.

This module intentionally does not change :mod:`csr_writer.docx_extract`.
It reuses that accepted-view text logic, but reads ``gridSpan`` and ``vMerge``
directly from OOXML so physical merge provenance is not lost through the
high-level python-docx table API.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from lxml import etree

from .docx_extract import (
    W,
    _StyleVisibility,
    _on_off_value,
    _parse_xml,
    _visible_text,
)
from .errors import ConfigurationError
from .io_utils import sha256_file
from .physical_table import (
    PhysicalCell,
    PhysicalRow,
    PhysicalTable,
    PhysicalTableError,
)


ADAPTER_ID = "docx-tfl-ooxml-physical-v1"


class DocxTFLParseError(ConfigurationError):
    """A DOCX TFL table cannot be parsed without guessing."""


@dataclass(frozen=True)
class ParsedDocxTFLTable:
    table_index: int
    physical_table: PhysicalTable

    @property
    def rows(self) -> tuple[PhysicalRow, ...]:
        return self.physical_table.rows

    @property
    def header_row_count(self) -> int:
        return self.physical_table.header_row_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_index": self.table_index,
            **self.physical_table.as_dict(),
        }


@dataclass(frozen=True)
class ParsedDocxTFLDocument:
    source_path: str
    source_sha256: str
    tables: tuple[ParsedDocxTFLTable, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": ADAPTER_ID,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "tables": [table.as_dict() for table in self.tables],
        }


@dataclass
class _CellDraft:
    text: str
    bold: bool
    indent: int
    indent_twips: int
    row_span: int = 1
    col_span: int = 1
    merged_from: tuple[int, int] | None = None


def _nearest_ancestor(element: etree._Element, tag: str) -> etree._Element | None:
    return next((ancestor for ancestor in element.iterancestors() if ancestor.tag == tag), None)


def _cell_paragraphs(cell: etree._Element) -> list[etree._Element]:
    return [
        paragraph
        for paragraph in cell.iter(W("p"))
        if _nearest_ancestor(paragraph, W("tc")) is cell
    ]


def _cell_text_and_format(
    cell: etree._Element, styles: _StyleVisibility
) -> tuple[str, bool, int, int]:
    paragraphs = _cell_paragraphs(cell)
    text_parts: list[str] = []
    text_runs: list[etree._Element] = []
    indent_twips = 0
    for paragraph in paragraphs:
        paragraph_style = styles.paragraph_style(paragraph)
        text = _visible_text(
            paragraph,
            styles,
            paragraph_style,
            skip_text_boxes=True,
        )
        if text or text_parts:
            text_parts.append(text)
        indent = paragraph.find(f"./{W('pPr')}/{W('ind')}")
        if indent is not None:
            raw_indent = indent.get(W("left")) or indent.get(W("start"))
            if raw_indent:
                try:
                    indent_twips = max(indent_twips, max(0, int(raw_indent)))
                except ValueError as exc:
                    raise DocxTFLParseError(
                        f"Non-integer paragraph indentation {raw_indent!r}"
                    ) from exc
        for run in paragraph.iter(W("r")):
            if _nearest_ancestor(run, W("tc")) is not cell:
                continue
            if any(
                ancestor.tag in {W("del"), W("moveFrom")}
                for ancestor in run.iterancestors()
                if ancestor is not cell
            ):
                continue
            if _visible_text(run, styles, paragraph_style, skip_text_boxes=True):
                text_runs.append(run)
    text = "\n".join(text_parts).rstrip()
    leading = len(text) - len(text.lstrip(" \t\u3000"))
    bold = bool(text_runs) and all(
        bool(_on_off_value(run.find(f"./{W('rPr')}/{W('b')}"))) for run in text_runs
    )
    return text, bold, leading, indent_twips


def _positive_int(element: etree._Element | None, attribute: str, *, label: str) -> int:
    if element is None:
        return 1
    raw_value = element.get(W(attribute))
    if raw_value is None:
        raise DocxTFLParseError(f"{label} is missing w:{attribute}")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise DocxTFLParseError(f"{label} is not an integer: {raw_value!r}") from exc
    if value < 1:
        raise DocxTFLParseError(f"{label} must be positive: {value}")
    return value


def _grid_width(table: etree._Element) -> int:
    grid = table.find(W("tblGrid"))
    if grid is None:
        raise DocxTFLParseError("TFL table is missing w:tblGrid; grid width is ambiguous")
    width = len(grid.findall(W("gridCol")))
    if width < 2:
        raise DocxTFLParseError(f"TFL table grid has only {width} columns")
    return width


def _row_header_flag(row: etree._Element) -> bool:
    return bool(_on_off_value(row.find(f"./{W('trPr')}/{W('tblHeader')}")))


def _vertical_mode(cell_properties: etree._Element | None) -> Literal["start", "continue"] | None:
    if cell_properties is None:
        return None
    vertical = cell_properties.find(W("vMerge"))
    if vertical is None:
        return None
    value = (vertical.get(W("val")) or "continue").casefold()
    if value == "restart":
        return "start"
    if value == "continue":
        return "continue"
    raise DocxTFLParseError(f"Unsupported w:vMerge value: {value!r}")


def _parse_table(
    table: etree._Element,
    *,
    table_index: int,
    styles: _StyleVisibility,
    allow_first_row_header: bool = False,
    allow_ambiguous_header_paths: bool = False,
) -> ParsedDocxTFLTable:
    if any(ancestor.tag == W("tbl") for ancestor in table.iterancestors()):
        raise DocxTFLParseError(
            f"Nested table at index {table_index} is outside the TFL physical-table contract"
        )
    width = _grid_width(table)
    xml_rows = table.findall(W("tr"))
    if len(xml_rows) < 2:
        raise DocxTFLParseError(f"TFL table {table_index} needs header and body rows")

    rows: list[list[_CellDraft]] = []
    marked_headers: list[bool] = []
    active_vertical: dict[int, tuple[int, int]] = {}
    vertical_started: set[tuple[int, int]] = set()
    vertical_continued: set[tuple[int, int]] = set()

    for row_index, xml_row in enumerate(xml_rows):
        row_properties = xml_row.find(W("trPr"))
        for offset_name in ("gridBefore", "gridAfter"):
            offset = row_properties.find(W(offset_name)) if row_properties is not None else None
            if offset is not None and _positive_int(offset, "val", label=f"w:{offset_name}"):
                raise DocxTFLParseError(
                    f"TFL table {table_index} row {row_index} uses w:{offset_name}; "
                    "ragged grids are ambiguous"
                )

        row: list[_CellDraft] = []
        next_active: dict[int, tuple[int, int]] = {}
        column_index = 0
        for xml_cell in xml_row.findall(W("tc")):
            cell_properties = xml_cell.find(W("tcPr"))
            span = _positive_int(
                cell_properties.find(W("gridSpan")) if cell_properties is not None else None,
                "val",
                label="w:gridSpan",
            )
            if column_index + span > width:
                raise DocxTFLParseError(
                    f"TFL table {table_index} row {row_index} cell span escapes grid"
                )
            text, bold, indent, indent_twips = _cell_text_and_format(xml_cell, styles)
            vertical = _vertical_mode(cell_properties)
            if vertical == "continue":
                anchor_coordinate = active_vertical.get(column_index)
                if anchor_coordinate is None:
                    raise DocxTFLParseError(
                        "Irregular merged table cells: orphan w:vMerge continuation at "
                        f"table {table_index}, row {row_index}, column {column_index}"
                    )
                anchor = rows[anchor_coordinate[0]][anchor_coordinate[1]]
                if span != anchor.col_span:
                    raise DocxTFLParseError(
                        "Irregular merged table cells: w:vMerge continuation changes gridSpan at "
                        f"table {table_index}, row {row_index}, column {column_index}"
                    )
                if text.strip():
                    raise DocxTFLParseError(
                        "Ambiguous merged table cells: w:vMerge continuation contains text at "
                        f"table {table_index}, row {row_index}, column {column_index}"
                    )
                for _ in range(span):
                    row.append(
                        _CellDraft(
                            text="",
                            bold=anchor.bold,
                            indent=0,
                            indent_twips=0,
                            merged_from=anchor_coordinate,
                        )
                    )
                anchor.row_span += 1
                vertical_continued.add(anchor_coordinate)
                next_active[column_index] = anchor_coordinate
                column_index += span
                continue

            anchor_coordinate = (row_index, column_index)
            anchor = _CellDraft(
                text=text,
                bold=bold,
                indent=indent,
                indent_twips=indent_twips,
                col_span=span,
            )
            row.append(anchor)
            for _ in range(1, span):
                row.append(
                    _CellDraft(
                        text="",
                        bold=bold,
                        indent=0,
                        indent_twips=0,
                        merged_from=anchor_coordinate,
                    )
                )
            if vertical == "start":
                vertical_started.add(anchor_coordinate)
                next_active[column_index] = anchor_coordinate
            column_index += span

        if column_index != width:
            raise DocxTFLParseError(
                f"TFL table {table_index} row {row_index} fills {column_index} columns; expected {width}"
            )
        rows.append(row)
        marked_headers.append(_row_header_flag(xml_row))
        active_vertical = next_active

    missing_vertical = sorted(vertical_started - vertical_continued)
    if missing_vertical:
        raise DocxTFLParseError(
            "Irregular merged table cells: w:vMerge restart has no continuation at "
            + ", ".join(f"row {row}, column {column}" for row, column in missing_vertical)
        )

    header_row_count = 0
    for marked in marked_headers:
        if not marked:
            break
        header_row_count += 1
    if any(marked_headers[header_row_count:]):
        raise DocxTFLParseError(
            f"TFL table {table_index} has non-contiguous w:tblHeader rows"
        )
    if header_row_count == 0:
        for row in rows:
            visible_anchors = [
                cell for cell in row if cell.merged_from is None and cell.text.strip()
            ]
            if not visible_anchors or not all(cell.bold for cell in visible_anchors):
                break
            header_row_count += 1
    if header_row_count == 0 and allow_first_row_header:
        first_visible = [
            cell for cell in rows[0] if cell.merged_from is None and cell.text.strip()
        ]
        later_has_content = any(
            any(cell.text.strip() for cell in row if cell.merged_from is None)
            for row in rows[1:]
        )
        if len(first_visible) >= 2 and later_has_content:
            header_row_count = 1
    if not 1 <= header_row_count < len(rows):
        raise DocxTFLParseError(
            f"TFL table {table_index} needs a leading header block and at least one body row"
        )

    physical_rows = tuple(
        PhysicalRow(
            grid=tuple(
                PhysicalCell(
                    text=cell.text,
                    bold=cell.bold,
                    indent=cell.indent,
                    indent_twips=cell.indent_twips,
                    row_span=cell.row_span,
                    col_span=cell.col_span,
                    merged_from=cell.merged_from,
                )
                for cell in row
            ),
            is_header=row_index < header_row_count,
        )
        for row_index, row in enumerate(rows)
    )
    try:
        physical_table = PhysicalTable(physical_rows, header_row_count)
        if not allow_ambiguous_header_paths:
            physical_table.flatten_header_paths()
    except PhysicalTableError as exc:
        raise DocxTFLParseError(str(exc)) from exc
    return ParsedDocxTFLTable(table_index=table_index, physical_table=physical_table)


def parse_docx_tfl_tables(
    path: str | Path,
    *,
    allow_first_row_header: bool = False,
    allow_ambiguous_header_paths: bool = False,
    selected_table_indexes: Sequence[int] | None = None,
) -> ParsedDocxTFLDocument:
    """Parse all top-level body tables and verify the source stayed unchanged."""
    source = Path(path).resolve()
    if source.suffix.lower() != ".docx" or not source.is_file():
        raise DocxTFLParseError(f"DOCX TFL input is not a .docx file: {source}")
    before_hash = sha256_file(source)
    before_size = source.stat().st_size
    try:
        with zipfile.ZipFile(source) as archive:
            names = [item.filename for item in archive.infolist()]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise DocxTFLParseError(
                    f"DOCX package contains duplicate part names: {duplicates!r}"
                )
            if "word/document.xml" not in names:
                raise DocxTFLParseError("DOCX package is missing word/document.xml")
            document_root = _parse_xml(
                archive.read("word/document.xml"), "word/document.xml"
            )
            styles_root = (
                _parse_xml(archive.read("word/styles.xml"), "word/styles.xml")
                if "word/styles.xml" in names
                else None
            )
    except zipfile.BadZipFile as exc:
        raise DocxTFLParseError(f"Input is not a valid DOCX ZIP package: {source}") from exc

    styles = _StyleVisibility(styles_root)
    all_tables = list(document_root.iter(W("tbl")))
    top_level_tables = [
        table for table in all_tables if not any(a.tag == W("tbl") for a in table.iterancestors())
    ]
    if len(top_level_tables) != len(all_tables):
        raise DocxTFLParseError("Nested DOCX tables are outside the TFL physical-table contract")
    if not top_level_tables:
        raise DocxTFLParseError("DOCX TFL contains no body tables")
    if selected_table_indexes is None:
        selected = set(range(1, len(top_level_tables) + 1))
    else:
        selected = set(selected_table_indexes)
        if len(selected) != len(tuple(selected_table_indexes)) or any(
            not isinstance(index, int) or not 1 <= index <= len(top_level_tables)
            for index in selected
        ):
            raise DocxTFLParseError("selected_table_indexes contains duplicates or out-of-range values")
    tables = tuple(
        _parse_table(
            table,
            table_index=index,
            styles=styles,
            allow_first_row_header=allow_first_row_header,
            allow_ambiguous_header_paths=allow_ambiguous_header_paths,
        )
        for index, table in enumerate(top_level_tables, start=1)
        if index in selected
    )

    after_hash = sha256_file(source)
    after_size = source.stat().st_size
    if before_hash != after_hash or before_size != after_size:
        raise DocxTFLParseError(
            "DOCX TFL source changed while it was being parsed; output is untrusted"
        )
    return ParsedDocxTFLDocument(
        source_path=str(source),
        source_sha256=before_hash,
        tables=tables,
    )
