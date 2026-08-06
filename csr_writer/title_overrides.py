"""Auditable localized-title overlays for the immutable ICH E3 source tree."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .io_utils import load_yaml, sha256_file
from .paths import DEFAULT_STRUCTURE, DEFAULT_TITLE_OVERRIDES
from .structure import flatten_sections, load_chapter_tree


_OVERRIDE_KEYS = {
    "section_id",
    "official_en",
    "official_zh",
    "localized_zh",
    "reason",
}


def load_title_overrides(
    path: str | Path = DEFAULT_TITLE_OVERRIDES,
    *,
    structure_path: str | Path = DEFAULT_STRUCTURE,
) -> dict[str, Any]:
    """Load and cross-check every overlay against the untouched official source."""

    payload = load_yaml(path)
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "overrides"}:
        raise ConfigurationError("title-overrides.yaml has an invalid root contract")
    if payload["schema_version"] != "1.0" or not isinstance(payload["overrides"], list):
        raise ConfigurationError("Unsupported title-overrides.yaml schema")
    nodes = {
        node.id: node
        for node in flatten_sections(
            load_chapter_tree(structure_path).select_layers(
                ("ich-official", "industry-convention")
            )
        )
    }
    by_id: dict[str, dict[str, str]] = {}
    records: list[dict[str, str]] = []
    for index, raw in enumerate(payload["overrides"]):
        if not isinstance(raw, Mapping) or set(raw) != _OVERRIDE_KEYS:
            raise ConfigurationError(f"Invalid title override at index {index}")
        record = {key: str(raw[key]).strip() for key in _OVERRIDE_KEYS}
        if not all(record.values()):
            raise ConfigurationError(f"Title override {index} contains an empty field")
        section_id = record["section_id"]
        if section_id in by_id:
            raise ConfigurationError(f"Duplicate title override for section {section_id}")
        node = nodes.get(section_id)
        if node is None:
            raise ConfigurationError(f"Title override targets unknown section {section_id}")
        if record["official_en"] != node.en or record["official_zh"] != node.zh:
            raise ConfigurationError(
                f"Title override {section_id} no longer matches the immutable ICH source"
            )
        by_id[section_id] = record
        records.append(record)
    return {
        "schema_version": "1.0",
        "config_sha256": sha256_file(path),
        "structure_sha256": sha256_file(structure_path),
        "by_section_id": by_id,
        "records": records,
    }


def localized_title(
    section_id: str,
    official_title: str,
    overrides: Mapping[str, Any],
) -> tuple[str, dict[str, str] | None]:
    """Return the rendered title and its full provenance, if overridden."""

    raw = overrides.get("by_section_id", {}).get(section_id)
    if raw is None:
        return official_title, None
    record = dict(raw)
    return str(record["localized_zh"]), record
