"""Deterministic TFL cross-reference tokens shared by drafting and OOXML assembly."""

from __future__ import annotations

import re
from typing import Any

from .semantic_key import validate_table_id


ENTITY_NUMBER_PATTERN = r"\d+(?:\.\d+)+(?:-\d+)?"
ASCII_ENTITY_RE = re.compile(rf"^[TFL]{ENTITY_NUMBER_PATTERN}$")
SOURCE_ENTITY_RE = re.compile(
    rf"^\s*(?P<prefix>表|图|清单|T|F|L)\s*(?P<number>{ENTITY_NUMBER_PATTERN})\s*$",
    re.IGNORECASE,
)
REF_PLACEHOLDER_RE = re.compile(
    rf"\{{\{{@ref:(?P<entity>[TFL]{ENTITY_NUMBER_PATTERN})\}}\}}"
)
CONCLUSION_PLACEHOLDER_RE = re.compile(
    r"\{\{@conclusion:(?P<rule>[A-Z][A-Z0-9_]+)\}\}"
)
ANY_PLACEHOLDER_RE = re.compile(r"\{\{(?P<body>[^{}]+)\}\}")

_PREFIX_TO_ASCII = {
    "表": "T",
    "图": "F",
    "清单": "L",
    "T": "T",
    "F": "F",
    "L": "L",
}
_PREFIX_TO_DISPLAY = {"T": "表", "F": "图", "L": "清单"}
_PREFIX_TO_BOOKMARK = {"T": "Tab", "F": "Fig", "L": "Lst"}


class CrossReferenceError(ValueError):
    """A TFL entity identifier or reference token is malformed."""


def normalize_entity_id(value: Any) -> str:
    """Convert reviewed Chinese/ASCII table prefixes to one canonical ASCII ID."""

    if not isinstance(value, str):
        raise CrossReferenceError(f"TFL entity identifier must be a string: {value!r}")
    match = SOURCE_ENTITY_RE.fullmatch(value)
    if match is None:
        raise CrossReferenceError(f"Unsupported TFL entity identifier: {value!r}")
    prefix = match.group("prefix")
    prefix = prefix.upper() if len(prefix) == 1 else prefix
    entity_id = _PREFIX_TO_ASCII[prefix] + match.group("number")
    if "-" in entity_id:
        if ASCII_ENTITY_RE.fullmatch(entity_id) is None:
            raise CrossReferenceError(f"Unsupported publication entity identifier: {value!r}")
        return entity_id
    try:
        return validate_table_id(entity_id)
    except ValueError as exc:
        raise CrossReferenceError(str(exc)) from exc


def make_reference_placeholder(entity_id: str) -> str:
    return f"{{{{@ref:{normalize_entity_id(entity_id)}}}}}"


def find_reference_ids(text: str) -> tuple[str, ...]:
    if not isinstance(text, str):
        raise CrossReferenceError("Reference-bearing text must be a string")
    return tuple(match.group("entity") for match in REF_PLACEHOLDER_RE.finditer(text))


def display_reference(entity_id: str) -> str:
    normalized = normalize_entity_id(entity_id)
    return f"{_PREFIX_TO_DISPLAY[normalized[0]]} {normalized[1:]}"


def bookmark_name(entity_id: str) -> str:
    normalized = normalize_entity_id(entity_id)
    suffix = normalized[1:].replace(".", "_").replace("-", "_")
    return f"_Ref_{_PREFIX_TO_BOOKMARK[normalized[0]]}_{suffix}"


def validate_reference_syntax(text: str) -> list[dict[str, str]]:
    """Reject unknown ``@`` placeholders while leaving value keys to Guard A."""

    issues: list[dict[str, str]] = []
    for match in ANY_PLACEHOLDER_RE.finditer(text):
        body = match.group("body")
        if (
            body.startswith("@")
            and REF_PLACEHOLDER_RE.fullmatch(match.group(0)) is None
            and CONCLUSION_PLACEHOLDER_RE.fullmatch(match.group(0)) is None
        ):
            issues.append(
                {
                    "code": "E-REF-SYNTAX",
                    "message": f"Unsupported cross-reference placeholder: {match.group(0)}",
                }
            )
    return issues
