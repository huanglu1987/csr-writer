"""Separate source-TFL semantic IDs from CSR publication/display IDs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cross_references import REF_PLACEHOLDER_RE, normalize_entity_id
from .errors import ConfigurationError


PUBLICATION_MAP_KEYS = {"schema_version", "entries"}
PUBLICATION_ENTRY_KEYS = {
    "source_entity_id",
    "publication_entity_id",
    "title",
    "expected_section",
}


def validate_publication_reference_map(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != PUBLICATION_MAP_KEYS:
        raise ConfigurationError("publication reference map contract drift")
    if payload.get("schema_version") != "1.0" or not isinstance(payload.get("entries"), list):
        raise ConfigurationError("unsupported publication reference map")
    sources: set[str] = set()
    publications: set[str] = set()
    entries: list[dict[str, str]] = []
    for index, raw in enumerate(payload["entries"]):
        if not isinstance(raw, Mapping) or set(raw) != PUBLICATION_ENTRY_KEYS:
            raise ConfigurationError(f"publication map entry {index} contract drift")
        source = normalize_entity_id(raw["source_entity_id"])
        publication = normalize_entity_id(raw["publication_entity_id"])
        if "-" in source or "-" not in publication:
            raise ConfigurationError(
                f"publication map entry {index} must map dotted source ID to hyphenated CSR ID"
            )
        if source[0] != publication[0]:
            raise ConfigurationError(f"publication map entry {index} changes T/F/L entity kind")
        title = raw["title"]
        expected_section = raw["expected_section"]
        if not isinstance(title, str) or not title.strip() or not isinstance(expected_section, str):
            raise ConfigurationError(f"publication map entry {index} lacks title/expected section")
        if source in sources or publication in publications:
            raise ConfigurationError(f"publication reference IDs must be one-to-one: entry {index}")
        sources.add(source)
        publications.add(publication)
        entries.append(
            {
                "source_entity_id": source,
                "publication_entity_id": publication,
                "title": title.strip(),
                "expected_section": expected_section.strip(),
            }
        )
    return {"schema_version": "1.0", "entries": entries}


def publication_aliases(payload: Mapping[str, Any]) -> dict[str, str]:
    validated = validate_publication_reference_map(payload)
    return {
        entry["source_entity_id"]: entry["publication_entity_id"]
        for entry in validated["entries"]
    }


def rewrite_reference_text(text: str, aliases: Mapping[str, str], *, require_mapped: bool) -> str:
    def replace(match):
        source = match.group("entity")
        target = aliases.get(source)
        if target is None:
            if require_mapped:
                raise ConfigurationError(f"source reference lacks publication mapping: {source}")
            return match.group(0)
        return f"{{{{@ref:{target}}}}}"

    return REF_PLACEHOLDER_RE.sub(replace, text)


def rewrite_content_references(
    content_sections: Sequence[Mapping[str, Any]],
    publication_map: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validated = validate_publication_reference_map(publication_map)
    aliases = publication_aliases(validated)
    rewritten: list[dict[str, Any]] = []
    for item in content_sections:
        clean = dict(item)
        references = []
        for source in item.get("reference_entities", []):
            normalized = normalize_entity_id(source)
            if normalized not in aliases:
                raise ConfigurationError(
                    f"content reference lacks publication mapping: {normalized}"
                )
            references.append(aliases[normalized])
        clean["reference_entities"] = references
        clean["paragraphs"] = [
            {
                **dict(paragraph),
                "text": rewrite_reference_text(
                    str(paragraph["text"]), aliases, require_mapped=True
                ),
            }
            for paragraph in item.get("paragraphs", [])
        ]
        rewritten.append(clean)
    return rewritten
