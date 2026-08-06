"""Loss-aware SAS ODS RTF physical-table adapter.

The adapter remains intentionally narrow (one RTF document and one physical
table), but it now represents horizontal and vertical merged cells instead of
silently flattening or rejecting them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import RTFParseError
from .io_utils import sha256_file
from .physical_table import (
    PhysicalCell,
    PhysicalRow,
    PhysicalTable,
    PhysicalTableError,
)


ADAPTER_ID = "sas-ods-rtf-physical-v2"
CODEPAGES = {936: "gbk", 1252: "cp1252", 950: "big5", 65001: "utf-8"}
TRAILER_RE = re.compile(r"^Program:\s*(?P<program>.*?)\s+Output:\s*(?P<output>.+)$", re.I)
_CELL_MARKER_RE = re.compile(r"\\cell\b")
_CELLX_RE = re.compile(r"\\cellx-?\d+\b")


@dataclass(frozen=True)
class ParsedTable:
    source_path: str
    source_sha256: str
    declared_codepage: int
    codec: str
    titles: tuple[str, ...]
    rows: tuple[PhysicalRow, ...]
    header_row_count: int
    footnotes: tuple[str, ...]
    trailer: dict[str, str]

    @property
    def physical_table(self) -> PhysicalTable:
        return PhysicalTable(self.rows, self.header_row_count)

    def as_dict(self) -> dict[str, Any]:
        table = self.physical_table
        return {
            "adapter": ADAPTER_ID,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "declared_codepage": self.declared_codepage,
            "codec": self.codec,
            "decode_replacements": 0,
            "titles": list(self.titles),
            "header_row_count": self.header_row_count,
            "flattened_header_paths": list(table.flatten_header_paths()),
            "rows": [row.as_dict() for row in self.rows],
            "footnotes": list(self.footnotes),
            "trailer": dict(self.trailer),
        }


@dataclass
class _CellDraft:
    text: str
    bold: bool
    indent: int
    indent_twips: int
    horizontal: Literal["start", "continue"] | None
    vertical: Literal["start", "continue"] | None
    row_span: int = 1
    col_span: int = 1
    merged_from: tuple[int, int] | None = None


def _decode_hex_escapes(text: str, codec: str) -> str:
    def decode_run(match: re.Match[str]) -> str:
        byte_values = bytes(
            int(value, 16) for value in re.findall(r"\\'([0-9a-fA-F]{2})", match.group(0))
        )
        try:
            return byte_values.decode(codec, errors="strict")
        except UnicodeDecodeError as exc:
            raise RTFParseError(
                f"Invalid {codec} byte sequence in RTF hex escapes at byte {exc.start}"
            ) from exc

    return re.sub(r"(?:\\'[0-9a-fA-F]{2})+", decode_run, text)


def _strip_known_destination_groups(text: str) -> str:
    # Deliberately limited to the simple groups present in this adapter's supported form.
    return re.sub(
        r"\{\\(?:fonttbl|colortbl|stylesheet|info|\*)[^{}]*(?:\{[^{}]*\})*[^{}]*\}",
        "",
        text,
    )


def _clean_rtf_text(chunk: str, *, keep_indent: bool) -> str:
    chunk = chunk.replace(r"\{", "{").replace(r"\}", "}")
    chunk = chunk.replace(r"\\", "\x00")
    chunk = re.sub(r"\\tab\b\s?", "\t", chunk)
    chunk = re.sub(r"\\(?:line|softline)\b\s?", " ", chunk)
    chunk = chunk.replace(r"\~", "\u00a0").replace(r"\-", "\u00ad")
    chunk = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", chunk)
    chunk = re.sub(r"\\[^a-zA-Z]", "", chunk)
    chunk = chunk.replace("{", "").replace("}", "").replace("\x00", "\\")
    chunk = chunk.replace("\r", "").replace("\n", "")
    return chunk.rstrip() if keep_indent else chunk.strip()


def _plain_segments(chunk: str) -> list[str]:
    values: list[str] = []
    for segment in re.split(r"\\par\b", chunk):
        value = _clean_rtf_text(segment, keep_indent=False)
        if value:
            values.append(value)
    return values


def _merge_flag(segment: str, start: str, continuation: str) -> Literal["start", "continue"] | None:
    has_start = re.search(rf"\\{start}\b", segment) is not None
    has_continuation = re.search(rf"\\{continuation}\b", segment) is not None
    if has_start and has_continuation:
        raise RTFParseError(
            f"Ambiguous merged table cells: both \\{start} and \\{continuation} are set"
        )
    if has_start:
        return "start"
    if has_continuation:
        return "continue"
    return None


def _parse_table_row(raw_row: str, row_index: int) -> tuple[str, list[_CellDraft]]:
    table_start = raw_row.rfind(r"\trowd")
    if table_start < 0:
        raise RTFParseError(f"Physical row {row_index} has cells but no \\trowd descriptor")
    prefix = raw_row[:table_start]
    block = raw_row[table_start:]
    first_cell = _CELL_MARKER_RE.search(block)
    if first_cell is None:
        raise RTFParseError(f"Physical row {row_index} has no \\cell terminators")
    descriptor = block[: first_cell.start()]
    cellx_matches = list(_CELLX_RE.finditer(descriptor))
    if len(cellx_matches) < 2:
        raise RTFParseError(
            f"Physical row {row_index} needs at least two \\cellx boundaries"
        )
    content = block[cellx_matches[-1].end() :]
    if _CELLX_RE.search(content):
        raise RTFParseError(f"Physical row {row_index} interleaves descriptors and content")

    properties: list[tuple[Literal["start", "continue"] | None, Literal["start", "continue"] | None]] = []
    cursor = 0
    for match in cellx_matches:
        segment = descriptor[cursor : match.start()]
        properties.append(
            (
                _merge_flag(segment, "clmgf", "clmrg"),
                _merge_flag(segment, "clvmgf", "clvmrg"),
            )
        )
        cursor = match.end()

    pieces = _CELL_MARKER_RE.split(content)
    if len(pieces) - 1 != len(properties):
        raise RTFParseError(
            f"Physical row {row_index} has {len(pieces) - 1} cell values but "
            f"{len(properties)} cell descriptors"
        )
    if _clean_rtf_text(pieces[-1], keep_indent=False):
        raise RTFParseError(f"Unexpected content after the final cell in row {row_index}")

    cells: list[_CellDraft] = []
    for column_index, raw_cell in enumerate(pieces[:-1]):
        text = _clean_rtf_text(raw_cell, keep_indent=True)
        leading = re.match(r"[ \t\u3000]*", text)
        indent = len(leading.group(0)) if leading is not None else 0
        left_indent = re.search(r"\\li(-?\d+)\b", raw_cell)
        indent_twips = max(0, int(left_indent.group(1))) if left_indent else 0
        bold = re.search(r"\\b(?!0)\b", raw_cell) is not None
        horizontal, vertical = properties[column_index]
        cells.append(
            _CellDraft(
                text=text,
                bold=bold,
                indent=indent,
                indent_twips=indent_twips,
                horizontal=horizontal,
                vertical=vertical,
            )
        )
    return prefix, cells


def _resolve_horizontal_merges(rows: list[list[_CellDraft]]) -> None:
    for row_index, row in enumerate(rows):
        column_index = 0
        while column_index < len(row):
            cell = row[column_index]
            if cell.horizontal == "continue":
                raise RTFParseError(
                    f"Irregular merged table cells: orphan \\clmrg at row {row_index}, column {column_index}"
                )
            if cell.horizontal != "start":
                column_index += 1
                continue
            end = column_index + 1
            while end < len(row) and row[end].horizontal == "continue":
                if row[end].text.strip():
                    raise RTFParseError(
                        "Ambiguous merged table cells: horizontal continuation contains text "
                        f"at row {row_index}, column {end}"
                    )
                if row[end].vertical not in {None, cell.vertical}:
                    raise RTFParseError(
                        "Irregular merged table cells: horizontal span has conflicting vertical flags "
                        f"at row {row_index}, column {end}"
                    )
                end += 1
            if end == column_index + 1:
                raise RTFParseError(
                    f"Irregular merged table cells: \\clmgf has no continuation at row {row_index}, column {column_index}"
                )
            cell.col_span = end - column_index
            for continuation_column in range(column_index + 1, end):
                continuation = row[continuation_column]
                continuation.bold = cell.bold
                continuation.indent = 0
                continuation.indent_twips = 0
                continuation.merged_from = (row_index, column_index)
            column_index = end


def _resolve_vertical_merges(rows: list[list[_CellDraft]]) -> None:
    active: dict[int, tuple[int, int]] = {}
    started: set[tuple[int, int]] = set()
    continued: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        next_active: dict[int, tuple[int, int]] = {}
        for column_index, cell in enumerate(row):
            # Same-row horizontal continuation cells are governed by their anchor.
            if cell.merged_from is not None and cell.merged_from[0] == row_index:
                continue
            if cell.vertical == "start":
                coordinate = (row_index, column_index)
                started.add(coordinate)
                next_active[column_index] = coordinate
                continue
            if cell.vertical != "continue":
                continue
            anchor_coordinate = active.get(column_index)
            if anchor_coordinate is None:
                raise RTFParseError(
                    f"Irregular merged table cells: orphan \\clvmrg at row {row_index}, column {column_index}"
                )
            anchor = rows[anchor_coordinate[0]][anchor_coordinate[1]]
            if cell.col_span != anchor.col_span:
                raise RTFParseError(
                    "Irregular merged table cells: vertical continuation changes horizontal span "
                    f"at row {row_index}, column {column_index}"
                )
            for span_column in range(column_index, column_index + cell.col_span):
                current = row[span_column]
                if current.text.strip():
                    raise RTFParseError(
                        "Ambiguous merged table cells: vertical continuation contains text "
                        f"at row {row_index}, column {span_column}"
                    )
                current.bold = anchor.bold
                current.indent = 0
                current.indent_twips = 0
                current.merged_from = anchor_coordinate
            anchor.row_span += 1
            continued.add(anchor_coordinate)
            next_active[column_index] = anchor_coordinate
        active = next_active
    missing = sorted(started - continued)
    if missing:
        raise RTFParseError(
            "Irregular merged table cells: vertical merge anchor has no continuation at "
            + ", ".join(f"row {row}, column {column}" for row, column in missing)
        )


def _leading_header_count(rows: list[list[_CellDraft]]) -> int:
    count = 0
    for row in rows:
        visible_anchors = [cell for cell in row if cell.merged_from is None and cell.text.strip()]
        if not visible_anchors or not all(cell.bold for cell in visible_anchors):
            break
        count += 1
    if count == 0:
        raise RTFParseError("At least one leading physical header row must be fully bold")
    if count == len(rows):
        raise RTFParseError("RTF table has header rows but no body rows")
    return count


def _finalize_rows(rows: list[list[_CellDraft]], header_row_count: int) -> tuple[PhysicalRow, ...]:
    physical_rows: list[PhysicalRow] = []
    for row_index, row in enumerate(rows):
        physical_rows.append(
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
        )
    return tuple(physical_rows)


def parse_rtf_table(path: str | Path) -> ParsedTable:
    source = Path(path)
    if source.suffix.lower() != ".rtf":
        raise RTFParseError(f"Adapter {ADAPTER_ID} accepts .rtf only: {source}")
    if not source.is_file():
        raise RTFParseError(f"RTF input does not exist: {source}")

    before_hash = sha256_file(source)
    raw_bytes = source.read_bytes()
    raw = raw_bytes.decode("latin-1")
    if len(re.findall(r"\{\\rtf\d+", raw, flags=re.I)) != 1:
        raise RTFParseError(
            "This adapter rejects concatenated or multiple RTF documents/tables"
        )
    codepage_match = re.search(r"\\ansicpg(\d+)", raw)
    if codepage_match is None:
        raise RTFParseError("RTF is missing an explicit \\ansicpg declaration")
    codepage = int(codepage_match.group(1))
    if codepage not in CODEPAGES:
        raise RTFParseError(f"Unsupported RTF codepage {codepage}; no silent codec fallback")
    codec = CODEPAGES[codepage]
    if re.search(r"\\u-?\d+", raw):
        raise RTFParseError(
            "This adapter rejects RTF Unicode controls; a future adapter must implement \\ucN fallback semantics"
        )

    body = _decode_hex_escapes(_strip_known_destination_groups(raw), codec)
    if "\ufffd" in body:
        raise RTFParseError("Decoded RTF contains U+FFFD replacement characters")
    chunks = re.split(r"\\row\b", body)
    if len(chunks) < 2:
        raise RTFParseError("No physical \\row markers were found")

    titles: list[str] = []
    draft_rows: list[list[_CellDraft]] = []
    for row_index, raw_row in enumerate(chunks[:-1]):
        if _CELL_MARKER_RE.search(raw_row) is None:
            if draft_rows:
                raise RTFParseError("Non-table paragraph occurred between physical table rows")
            titles.extend(_plain_segments(raw_row))
            continue
        prefix, row = _parse_table_row(raw_row, row_index)
        titles.extend(_plain_segments(prefix))
        draft_rows.append(row)

    if not draft_rows:
        raise RTFParseError("No non-empty table rows were parsed")
    width = len(draft_rows[0])
    for row_index, row in enumerate(draft_rows):
        if len(row) != width:
            raise RTFParseError(
                f"Row {row_index} has {len(row)} cells; expected fixed grid width {width}"
            )

    _resolve_horizontal_merges(draft_rows)
    _resolve_vertical_merges(draft_rows)
    header_row_count = _leading_header_count(draft_rows)
    rows = _finalize_rows(draft_rows, header_row_count)
    try:
        physical_table = PhysicalTable(rows, header_row_count)
        # Flatten now so ambiguous spanners fail at ingestion, not later mapping.
        physical_table.flatten_header_paths()
    except PhysicalTableError as exc:
        raise RTFParseError(str(exc)) from exc

    tail_segments = _plain_segments(chunks[-1])
    footnotes: list[str] = []
    trailer: dict[str, str] = {}
    for value in tail_segments:
        match = TRAILER_RE.match(value)
        if match:
            if trailer:
                raise RTFParseError("Multiple Program/Output trailers are not supported")
            trailer = {"program": match.group("program"), "output_raw": match.group("output")}
        else:
            footnotes.append(value)

    after_hash = sha256_file(source)
    if after_hash != before_hash:
        raise RTFParseError(f"Input changed while being parsed: {source}")
    return ParsedTable(
        source_path=str(source),
        source_sha256=before_hash,
        declared_codepage=codepage,
        codec=codec,
        titles=tuple(titles),
        rows=rows,
        header_row_count=header_row_count,
        footnotes=tuple(footnotes),
        trailer=trailer,
    )
