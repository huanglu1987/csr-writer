"""Version-compatible semantic cell keys for deterministic TFL bindings.

The original M2 key is retained verbatim::

    T14.2.1|POP=FAS|ROW=RESULT|COL=TRT

New datasets may append reviewed dimensions in lexical order::

    T14.2.4.3.1|POP=PKS|ROW=CMAX|COL=DOSE_0P5_BID|STAGE=D1

Required dimensions stay first so existing consumers can continue to read
``table/pop/row/col``.  Extra dimensions form an ordered, extensible suffix;
the parser rejects non-canonical ordering and repeated names instead of
silently normalizing an ambiguous key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


TABLE_ID_RE = re.compile(r"^[TFL]\d+(?:\.\d+)+$")
DIMENSION_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
REQUIRED_DIMENSIONS = ("POP", "ROW", "COL")
_FORBIDDEN_VALUE_CHARS = frozenset("|={}")


class SemanticKeyError(ValueError):
    """A semantic key is malformed, ambiguous, or non-canonical."""


def validate_table_id(value: Any) -> str:
    if not isinstance(value, str) or TABLE_ID_RE.fullmatch(value) is None:
        raise SemanticKeyError(
            f"table_id must be T/F/L plus a dotted numeric identifier: {value!r}"
        )
    return value


def validate_dimension_name(value: Any) -> str:
    if not isinstance(value, str) or DIMENSION_NAME_RE.fullmatch(value) is None:
        raise SemanticKeyError(
            f"dimension name must match {DIMENSION_NAME_RE.pattern}: {value!r}"
        )
    return value


def validate_dimension_value(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SemanticKeyError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise SemanticKeyError(f"{name} cannot have surrounding whitespace: {value!r}")
    if any(character in value for character in _FORBIDDEN_VALUE_CHARS):
        raise SemanticKeyError(
            f"{name} contains a reserved key delimiter or placeholder brace: {value!r}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SemanticKeyError(f"{name} contains a control character")
    if name == "ROW":
        segments = value.split("/")
        if any(not segment or segment != segment.strip() for segment in segments):
            raise SemanticKeyError(
                f"ROW hierarchy must contain non-empty, trimmed path segments: {value!r}"
            )
    return value


@dataclass(frozen=True)
class SemanticCellKey:
    """Parsed canonical semantic key."""

    table_id: str
    dimensions: tuple[tuple[str, str], ...]

    @property
    def population(self) -> str:
        return dict(self.dimensions)["POP"]

    @property
    def row(self) -> str:
        return dict(self.dimensions)["ROW"]

    @property
    def column(self) -> str:
        return dict(self.dimensions)["COL"]

    @property
    def extra_dimensions(self) -> dict[str, str]:
        return dict(self.dimensions[len(REQUIRED_DIMENSIONS) :])

    @property
    def row_parent(self) -> str | None:
        parent, separator, _leaf = self.row.rpartition("/")
        return parent if separator else None

    @property
    def row_leaf(self) -> str:
        return self.row.rsplit("/", 1)[-1]

    def as_string(self) -> str:
        suffix = "|".join(f"{name}={value}" for name, value in self.dimensions)
        return f"{self.table_id}|{suffix}"


def parse_semantic_cell_key(value: Any) -> SemanticCellKey:
    """Parse one key and enforce its canonical dimension order."""

    if not isinstance(value, str):
        raise SemanticKeyError(f"semantic key must be a string: {value!r}")
    parts = value.split("|")
    if len(parts) < 4:
        raise SemanticKeyError("semantic key needs table plus POP/ROW/COL dimensions")
    table_id = validate_table_id(parts[0])

    pairs: list[tuple[str, str]] = []
    for part in parts[1:]:
        if part.count("=") != 1:
            raise SemanticKeyError(f"dimension must contain exactly one '=': {part!r}")
        name, raw_value = part.split("=", 1)
        name = validate_dimension_name(name)
        pairs.append((name, validate_dimension_value(raw_value, name)))

    names = [name for name, _value in pairs]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise SemanticKeyError(f"repeated semantic key dimension(s): {repeated}")
    if tuple(names[: len(REQUIRED_DIMENSIONS)]) != REQUIRED_DIMENSIONS:
        raise SemanticKeyError(
            "semantic key dimensions must start with POP, ROW, COL in that order"
        )
    extras = names[len(REQUIRED_DIMENSIONS) :]
    if extras != sorted(extras):
        raise SemanticKeyError(
            f"extra dimensions must be in lexical order: observed={extras}, expected={sorted(extras)}"
        )
    return SemanticCellKey(table_id=table_id, dimensions=tuple(pairs))


def build_semantic_cell_key(
    table_id: str,
    population: str,
    row: str,
    column: str,
    extra_dimensions: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
) -> str:
    """Build the only accepted canonical serialization of a semantic key."""

    table_id = validate_table_id(table_id)
    required = [
        ("POP", validate_dimension_value(population, "POP")),
        ("ROW", validate_dimension_value(row, "ROW")),
        ("COL", validate_dimension_value(column, "COL")),
    ]
    if extra_dimensions is None:
        raw_extras: list[tuple[str, str]] = []
    elif isinstance(extra_dimensions, Mapping):
        raw_extras = list(extra_dimensions.items())
    else:
        raw_extras = list(extra_dimensions)

    extras: list[tuple[str, str]] = []
    names: set[str] = set()
    for raw_name, raw_value in raw_extras:
        name = validate_dimension_name(raw_name)
        if name in REQUIRED_DIMENSIONS:
            raise SemanticKeyError(f"extra dimension cannot repeat required {name}")
        if name in names:
            raise SemanticKeyError(f"repeated semantic key dimension: {name}")
        names.add(name)
        extras.append((name, validate_dimension_value(raw_value, name)))
    extras.sort(key=lambda item: item[0])

    parsed = SemanticCellKey(table_id, tuple(required + extras))
    return parsed.as_string()


@dataclass(frozen=True)
class SemanticKeyMatch:
    """Small ``re.Match``-compatible view used by legacy M2-M4 callers."""

    raw: str
    parsed: SemanticCellKey

    def group(self, *groups: int | str) -> str | tuple[str, ...]:
        def one(group: int | str) -> str:
            if group in (0, "0"):
                return self.raw
            aliases = {
                "table": self.parsed.table_id,
                "pop": self.parsed.population,
                "row": self.parsed.row,
                "col": self.parsed.column,
            }
            if isinstance(group, str) and group in aliases:
                return aliases[group]
            raise IndexError(f"unknown semantic key group: {group!r}")

        if not groups:
            return self.raw
        values = tuple(one(group) for group in groups)
        return values[0] if len(values) == 1 else values

    def groupdict(self, default: Any = None) -> dict[str, str]:
        del default
        return {
            "table": self.parsed.table_id,
            "pop": self.parsed.population,
            "row": self.parsed.row,
            "col": self.parsed.column,
        }


class SemanticKeyPattern:
    """Compatibility facade retaining ``CELL_KEY_RE.fullmatch`` callers."""

    pattern = (
        "<T/F/L dotted id>|POP=<population>|ROW=<row[/child]>|COL=<column>"
        "[|<EXTRA>=<value> ... lexical order]"
    )

    def fullmatch(self, value: Any) -> SemanticKeyMatch | None:
        try:
            parsed = parse_semantic_cell_key(value)
        except SemanticKeyError:
            return None
        return SemanticKeyMatch(raw=value, parsed=parsed)


CELL_KEY_PATTERN = SemanticKeyPattern()
