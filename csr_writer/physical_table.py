"""Shared, loss-aware physical table model for TFL adapters.

The model is deliberately physical rather than semantic.  It records the
top-left merge anchor, spans, and indentation without guessing CSR row or
column identifiers.  Reviewed semantic mapping is a separate step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import CSRWriterError


SEMANTIC_HEADER_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class PhysicalTableError(CSRWriterError):
    """A physical table cannot be represented without guessing."""


@dataclass(frozen=True)
class PhysicalCell:
    """One grid position in a rectangular physical table.

    Merge continuation positions remain present in the grid and point to the
    ultimate top-left anchor through ``merged_from``.  Only anchors may carry
    non-empty text or spans greater than one.
    """

    text: str
    bold: bool
    indent: int = 0
    indent_twips: int = 0
    row_span: int = 1
    col_span: int = 1
    merged_from: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bold": self.bold,
            "indent": self.indent,
            "indent_twips": self.indent_twips,
            "row_span": self.row_span,
            "col_span": self.col_span,
            "merged_from": list(self.merged_from) if self.merged_from else None,
        }


@dataclass(frozen=True)
class PhysicalRow:
    grid: tuple[PhysicalCell, ...]
    is_header: bool = False

    @property
    def cells(self) -> tuple[str, ...]:
        """Legacy compatibility view used by the M2 flat-table mapper."""
        return tuple(cell.text for cell in self.grid)

    @property
    def bold(self) -> tuple[bool, ...]:
        """Legacy compatibility view used by the M2 flat-table mapper."""
        return tuple(cell.bold for cell in self.grid)

    @property
    def indents(self) -> tuple[int, ...]:
        return tuple(cell.indent for cell in self.grid)

    @property
    def merged_from(self) -> tuple[tuple[int, int] | None, ...]:
        return tuple(cell.merged_from for cell in self.grid)

    def as_dict(self) -> dict[str, Any]:
        return {
            # Keep the original flat fields stable for existing parsed output.
            "cells": list(self.cells),
            "bold": list(self.bold),
            "indents": list(self.indents),
            "merged_from": [
                list(value) if value is not None else None for value in self.merged_from
            ],
            "is_header": self.is_header,
            "cell_details": [cell.as_dict() for cell in self.grid],
        }


@dataclass(frozen=True)
class PhysicalTable:
    rows: tuple[PhysicalRow, ...]
    header_row_count: int

    def __post_init__(self) -> None:
        if not self.rows:
            raise PhysicalTableError("Physical table has no rows")
        width = len(self.rows[0].grid)
        if width < 2:
            raise PhysicalTableError("Physical table needs at least two grid columns")
        if not 1 <= self.header_row_count < len(self.rows):
            raise PhysicalTableError(
                "Physical table needs at least one header row and one body row"
            )
        for row_index, row in enumerate(self.rows):
            if len(row.grid) != width:
                raise PhysicalTableError(
                    f"Row {row_index} has {len(row.grid)} grid cells; expected {width}"
                )
            if row.is_header != (row_index < self.header_row_count):
                raise PhysicalTableError(
                    f"Header rows must form one leading block; row {row_index} is inconsistent"
                )
            for column_index, cell in enumerate(row.grid):
                if cell.indent < 0 or cell.indent_twips < 0:
                    raise PhysicalTableError(
                        f"Negative indentation at row {row_index}, column {column_index}"
                    )
                if cell.merged_from is None:
                    if cell.row_span < 1 or cell.col_span < 1:
                        raise PhysicalTableError(
                            f"Invalid merge span at row {row_index}, column {column_index}"
                        )
                    if row_index + cell.row_span > len(self.rows):
                        raise PhysicalTableError(
                            f"Row span escapes table at row {row_index}, column {column_index}"
                        )
                    if column_index + cell.col_span > width:
                        raise PhysicalTableError(
                            f"Column span escapes table at row {row_index}, column {column_index}"
                        )
                    continue
                if cell.text.strip():
                    raise PhysicalTableError(
                        f"Merged continuation contains text at row {row_index}, column {column_index}"
                    )
                if cell.row_span != 1 or cell.col_span != 1:
                    raise PhysicalTableError(
                        f"Merged continuation carries a span at row {row_index}, column {column_index}"
                    )
                anchor_row, anchor_column = cell.merged_from
                if not (0 <= anchor_row <= row_index and 0 <= anchor_column < width):
                    raise PhysicalTableError(
                        f"Invalid merge anchor at row {row_index}, column {column_index}"
                    )
                if (anchor_row, anchor_column) == (row_index, column_index):
                    raise PhysicalTableError(
                        f"Merge continuation points to itself at row {row_index}, column {column_index}"
                    )
                if anchor_row == row_index and anchor_column > column_index:
                    raise PhysicalTableError(
                        f"Merge continuation points forward at row {row_index}, column {column_index}"
                    )
                anchor = self.rows[anchor_row].grid[anchor_column]
                if anchor.merged_from is not None:
                    raise PhysicalTableError(
                        f"Merge continuation does not point to an anchor at row {row_index}, column {column_index}"
                    )
                if not (
                    anchor_row <= row_index < anchor_row + anchor.row_span
                    and anchor_column <= column_index < anchor_column + anchor.col_span
                ):
                    raise PhysicalTableError(
                        f"Merge anchor span does not cover row {row_index}, column {column_index}"
                    )
        for anchor_row, row in enumerate(self.rows):
            for anchor_column, anchor in enumerate(row.grid):
                if anchor.merged_from is not None:
                    continue
                for covered_row in range(anchor_row, anchor_row + anchor.row_span):
                    for covered_column in range(
                        anchor_column, anchor_column + anchor.col_span
                    ):
                        if (covered_row, covered_column) == (
                            anchor_row,
                            anchor_column,
                        ):
                            continue
                        continuation = self.rows[covered_row].grid[covered_column]
                        if continuation.merged_from != (anchor_row, anchor_column):
                            raise PhysicalTableError(
                                "Merge anchor rectangle is not fully represented at "
                                f"row {covered_row}, column {covered_column}"
                            )

    @property
    def column_count(self) -> int:
        return len(self.rows[0].grid)

    def _anchor_at(self, row_index: int, column_index: int) -> tuple[tuple[int, int], PhysicalCell]:
        cell = self.rows[row_index].grid[column_index]
        coordinate = cell.merged_from or (row_index, column_index)
        return coordinate, self.rows[coordinate[0]].grid[coordinate[1]]

    def flatten_header_paths(self) -> tuple[str, ...]:
        """Flatten multi-row spanners into unambiguous physical header paths."""
        paths: list[str] = []
        for column_index in range(self.column_count):
            components: list[str] = []
            previous_anchor: tuple[int, int] | None = None
            for row_index in range(self.header_row_count):
                anchor_coordinate, anchor = self._anchor_at(row_index, column_index)
                label = anchor.text.strip()
                if label and anchor_coordinate != previous_anchor:
                    components.append(label)
                previous_anchor = anchor_coordinate
            path = " / ".join(components)
            if not path:
                raise PhysicalTableError(
                    f"Header column {column_index} is blank after merge expansion"
                )
            paths.append(path)
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        if duplicates:
            raise PhysicalTableError(
                "Header flattening is ambiguous; duplicate physical paths: "
                + ", ".join(repr(value) for value in duplicates)
            )
        return tuple(paths)

    def reviewed_header_ids(self, mapping: Mapping[str, str]) -> tuple[str, ...]:
        """Apply an exact, human-reviewed mapping to flattened header paths."""
        paths = self.flatten_header_paths()
        configured = set(mapping)
        observed = set(paths)
        if configured != observed:
            raise PhysicalTableError(
                "Reviewed header mapping drift: "
                f"missing={sorted(observed - configured)!r}, "
                f"unused={sorted(configured - observed)!r}"
            )
        semantic_ids: list[str] = []
        for path in paths:
            semantic_id = mapping[path]
            if not isinstance(semantic_id, str) or not SEMANTIC_HEADER_ID_RE.fullmatch(
                semantic_id
            ):
                raise PhysicalTableError(
                    f"Invalid reviewed header ID for {path!r}: {semantic_id!r}"
                )
            semantic_ids.append(semantic_id)
        if len(semantic_ids) != len(set(semantic_ids)):
            raise PhysicalTableError(
                f"Reviewed header semantic ID collision: {semantic_ids!r}"
            )
        return tuple(semantic_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "header_row_count": self.header_row_count,
            "column_count": self.column_count,
            "flattened_header_paths": list(self.flatten_header_paths()),
            "rows": [row.as_dict() for row in self.rows],
        }
