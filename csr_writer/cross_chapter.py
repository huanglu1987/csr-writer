"""Deterministic M4 cross-chapter contracts and derived Synopsis evidence.

This module never drafts clinical prose.  It verifies already generated section
artifacts, applies explicit human review decisions, and copies confirmed body
paragraphs into a Synopsis manifest.  The copy-only design makes an independent
Synopsis writer impossible at this layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .cross_references import (
    CONCLUSION_PLACEHOLDER_RE,
    REF_PLACEHOLDER_RE,
    find_reference_ids,
)
from .errors import CSRWriterError
from .io_utils import load_json_unique, sha256_file


class CrossChapterError(CSRWriterError):
    """A section, review, claim, or Synopsis contract is invalid."""


REVIEW_MARKER = "[待医学负责人确认]"
SPACE_RE = re.compile(r"\s+")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CrossChapterError(
            f"{label} keys must be exactly {sorted(expected)}; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _split_section_markdown(text: str, label: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [block.strip() for block in re.split(r"\n\s*\n", normalized) if block.strip()]
    if not blocks or not blocks[0].startswith("# ") or "\n" in blocks[0]:
        raise CrossChapterError(f"{label} must start with one level-one heading")
    heading = blocks[0][2:].strip()
    notices: list[str] = []
    paragraphs: list[str] = []
    for block in blocks[1:]:
        if block.startswith("> "):
            notices.append(block[2:].strip())
            continue
        if block.startswith("#"):
            raise CrossChapterError(f"{label} contains an unexpected additional heading")
        if "\n" in block:
            raise CrossChapterError(f"{label} paragraph must be one physical line")
        paragraphs.append(block)
    if not paragraphs:
        raise CrossChapterError(f"{label} contains no body paragraphs")
    return heading, tuple(notices), tuple(paragraphs)


@dataclass(frozen=True)
class ResolvedParagraph:
    section_id: str
    para_id: str
    text: str
    needs_human: bool
    confidence: str
    sources: tuple[str, ...]
    placeholders: tuple[str, ...]
    references: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedSection:
    section_id: str
    heading: str
    notices: tuple[str, ...]
    evidence_status: str
    draft_sha256: str
    resolved_sha256: str
    metadata_path: Path
    reference_entities: tuple[str, ...]
    paragraphs: tuple[ResolvedParagraph, ...]


def load_resolved_section(
    draft_path: str | Path,
    resolved_path: str | Path,
    metadata_path: str | Path,
    guard_path: str | Path,
) -> ResolvedSection:
    """Load one resolved section and verify every M3 hash/guard anchor."""
    draft_file = Path(draft_path)
    resolved_file = Path(resolved_path)
    metadata_file = Path(metadata_path)
    guard_file = Path(guard_path)
    metadata = load_json_unique(metadata_file)
    guard = load_json_unique(guard_file)
    if not isinstance(metadata, dict) or not isinstance(guard, dict):
        raise CrossChapterError("Section metadata and guard report must be JSON objects")
    required_meta = {
        "schema_version",
        "section",
        "variant",
        "evidence_status",
        "writer",
        "input_sha256",
        "playbook",
        "system_prompt_sha256",
        "draft_sha256",
        "source_manifest",
        "binding_manifest",
        "paragraphs",
    }
    schema_version = metadata.get("schema_version")
    if schema_version in {"1.1", "1.2"}:
        required_meta.add("reference_manifest")
    if schema_version == "1.2":
        required_meta.add("production_authorization")
    # Semantic and reference metadata are intentionally additive and explicitly allowed.
    unknown_meta = set(metadata) - (
        required_meta
        | {
            "semantic_findings",
            "reference_manifest",
            "generation_attempts",
            "guard_retry_count",
            "prompt_audit",
            "agent_interface",
            "confirmed_body_binding_manifest",
            "style_qc",
            "production_authorization",
        }
    )
    missing_meta = required_meta - set(metadata)
    if missing_meta or unknown_meta:
        raise CrossChapterError(
            f"Section metadata contract drift: missing={sorted(missing_meta)}, "
            f"extra={sorted(unknown_meta)}"
        )
    if schema_version not in {"1.0", "1.1", "1.2"}:
        raise CrossChapterError("Unsupported section metadata schema_version")
    production_authorization = metadata.get("production_authorization")
    if schema_version == "1.2":
        expected_authorization_keys = {
            "section_id",
            "production_method",
            "narration",
            "production_profile",
            "runtime_alias_only",
            "release_scope",
            "authoring_unit",
        }
        if (
            not isinstance(production_authorization, dict)
            or set(production_authorization) != expected_authorization_keys
            or not isinstance(production_authorization.get("runtime_alias_only"), bool)
            or not isinstance(production_authorization.get("authoring_unit"), bool)
        ):
            raise CrossChapterError("Section production_authorization is malformed")
        if production_authorization["section_id"] != metadata.get("section"):
            raise CrossChapterError("Section production authorization identifies another section")
        if production_authorization["runtime_alias_only"]:
            expected_scope = {
                "demo_only": "demo_only",
                "source_gap": "source_gap_only",
            }.get(metadata.get("evidence_status"))
            if production_authorization.get("release_scope") != expected_scope:
                raise CrossChapterError(
                    "E-PRODUCTION-METHOD-RUNTIME-ALIAS-SCOPE: runtime alias escaped "
                    "demo/source-gap evidence"
                )
    raw_reference_manifest = metadata.get("reference_manifest", [])
    if not isinstance(raw_reference_manifest, list):
        raise CrossChapterError("Section reference_manifest must be a list")
    reference_entities: list[str] = []
    for index, item in enumerate(raw_reference_manifest):
        if not isinstance(item, dict) or set(item) != {
            "entity_id",
            "verified_by",
            "frozen_map_sha256",
        }:
            raise CrossChapterError(f"Malformed reference_manifest entry {index}")
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or entity_id in reference_entities:
            raise CrossChapterError(f"Invalid or duplicate reference entity: {entity_id!r}")
        if item.get("verified_by") not in {"binding_manifest", "frozen_tfl_map"}:
            raise CrossChapterError(f"Invalid reference verification source for {entity_id}")
        if item.get("verified_by") == "frozen_tfl_map" and not isinstance(
            item.get("frozen_map_sha256"), str
        ):
            raise CrossChapterError(f"Frozen-map reference lacks a map hash: {entity_id}")
        reference_entities.append(entity_id)
    section_id = metadata.get("section")
    if not isinstance(section_id, str) or not section_id:
        raise CrossChapterError("Section metadata needs a section identifier")
    if sha256_file(draft_file) != metadata.get("draft_sha256"):
        raise CrossChapterError(f"Draft hash mismatch for section {section_id}")
    if guard.get("status") != "pass":
        raise CrossChapterError(f"Numeric guards did not pass for section {section_id}")
    stages = guard.get("stages")
    if not isinstance(stages, dict) or any(
        not isinstance(stages.get(name), dict) or stages[name].get("status") != "pass"
        for name in ("A_key_existence", "B_residual", "C_bare_numbers")
    ):
        raise CrossChapterError(f"Numeric guard stage failure for section {section_id}")
    resolved_sha256 = sha256_file(resolved_file)
    if resolved_sha256 != guard.get("resolved_sha256"):
        raise CrossChapterError(f"Resolved section hash mismatch for {section_id}")

    draft_text = draft_file.read_text(encoding="utf-8")
    resolved_text = resolved_file.read_text(encoding="utf-8")
    draft_heading, draft_notices, draft_paragraphs = _split_section_markdown(
        draft_text, f"draft {section_id}"
    )
    resolved_heading, resolved_notices, resolved_paragraphs = _split_section_markdown(
        resolved_text, f"resolved {section_id}"
    )
    if draft_heading != resolved_heading or draft_notices != resolved_notices:
        raise CrossChapterError(f"Heading/notices changed while resolving section {section_id}")
    paragraph_meta = metadata.get("paragraphs")
    if not isinstance(paragraph_meta, list) or not (
        len(paragraph_meta) == len(draft_paragraphs) == len(resolved_paragraphs)
    ):
        raise CrossChapterError(f"Paragraph count drift in section {section_id}")

    paragraphs: list[ResolvedParagraph] = []
    seen_ids: set[str] = set()
    for index, (draft_text_item, resolved_text_item, raw_meta) in enumerate(
        zip(draft_paragraphs, resolved_paragraphs, paragraph_meta, strict=True), start=1
    ):
        if not isinstance(raw_meta, dict):
            raise CrossChapterError(f"Paragraph metadata {section_id}[{index}] must be an object")
        required_para = {"para_id", "sources", "confidence", "needs_human", "placeholders"}
        if schema_version in {"1.1", "1.2"}:
            required_para.add("references")
        allowed_para = required_para | {"semantic_findings", "conditional_conclusions"}
        if set(raw_meta) - allowed_para or required_para - set(raw_meta):
            raise CrossChapterError(f"Paragraph metadata contract drift at {section_id}[{index}]")
        para_id = raw_meta.get("para_id")
        if not isinstance(para_id, str) or not para_id or para_id in seen_ids:
            raise CrossChapterError(f"Invalid or duplicate para_id at {section_id}[{index}]")
        seen_ids.add(para_id)
        sources = raw_meta.get("sources")
        placeholders = raw_meta.get("placeholders")
        references = raw_meta.get("references", [])
        conditional_conclusions = raw_meta.get("conditional_conclusions", [])
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            raise CrossChapterError(f"Invalid sources for {para_id}")
        if not isinstance(placeholders, list) or not all(
            isinstance(item, str) for item in placeholders
        ):
            raise CrossChapterError(f"Invalid placeholders for {para_id}")
        if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
            raise CrossChapterError(f"Invalid references for {para_id}")
        if not isinstance(conditional_conclusions, list) or not all(
            isinstance(item, str) for item in conditional_conclusions
        ):
            raise CrossChapterError(f"Invalid conditional conclusions for {para_id}")
        observed_conclusions = [
            match.group("rule")
            for match in CONCLUSION_PLACEHOLDER_RE.finditer(draft_text_item)
        ]
        if observed_conclusions != conditional_conclusions:
            raise CrossChapterError(f"Draft conditional-conclusion drift for {para_id}")
        if CONCLUSION_PLACEHOLDER_RE.search(resolved_text_item):
            raise CrossChapterError(f"Unresolved conditional conclusion in {para_id}")
        observed_references = list(find_reference_ids(resolved_text_item))
        if observed_references != references:
            raise CrossChapterError(f"Resolved cross-reference drift for {para_id}")
        if any(entity_id not in reference_entities for entity_id in observed_references):
            raise CrossChapterError(f"Resolved paragraph {para_id} has an unconfirmed reference")
        without_references = REF_PLACEHOLDER_RE.sub("", resolved_text_item)
        if "{{" in without_references or "}}" in without_references:
            raise CrossChapterError(f"Resolved paragraph {para_id} still has value placeholders")
        paragraphs.append(
            ResolvedParagraph(
                section_id=section_id,
                para_id=para_id,
                text=resolved_text_item,
                needs_human=bool(raw_meta.get("needs_human")),
                confidence=str(raw_meta.get("confidence")),
                sources=tuple(sources),
                placeholders=tuple(placeholders),
                references=tuple(references),
            )
        )

    return ResolvedSection(
        section_id=section_id,
        heading=resolved_heading,
        notices=resolved_notices,
        evidence_status=str(metadata.get("evidence_status")),
        draft_sha256=str(metadata["draft_sha256"]),
        resolved_sha256=resolved_sha256,
        metadata_path=metadata_file,
        reference_entities=tuple(reference_entities),
        paragraphs=tuple(paragraphs),
    )


def load_constrained_resolved_section(
    markdown_path: str | Path,
    metadata_path: str | Path,
    *,
    evidence_status: str,
) -> ResolvedSection:
    """Load a certified zero-placeholder constrained narration for assembly.

    Constrained methodology and SAE artifacts have a different metadata
    contract from ordinary section-writer drafts.  This bridge does not relax
    either contract: it accepts only a release-eligible positive-coverage
    report, a passing style report, exact paragraph hashes, and no unresolved
    numeric/reference/conclusion placeholders.  Placeholder-bearing artifacts
    must still use the ordinary resolver and loader.
    """

    markdown_file = Path(markdown_path)
    metadata_file = Path(metadata_path)
    metadata = load_json_unique(metadata_file)
    if not isinstance(metadata, dict):
        raise CrossChapterError("Constrained section metadata must be a JSON object")
    required = {
        "schema_version",
        "status",
        "section_id",
        "card_id",
        "production_method",
        "narration",
        "draft_sha256",
        "source_manifest",
        "binding_manifest",
        "numeric_placeholders",
        "paragraphs",
        "forward_coverage",
        "style_qc",
        "findings",
        "card_external_entities",
    }
    missing = required - set(metadata)
    if missing:
        raise CrossChapterError(
            f"Constrained section metadata contract drift: missing={sorted(missing)}"
        )
    if (
        metadata.get("schema_version") != "1.0"
        or metadata.get("status") != "pass"
        or metadata.get("production_method") not in {"extraction", "llm_drafting"}
        or metadata.get("narration") != "constrained_llm"
    ):
        raise CrossChapterError("Constrained section is not a passing LLM-drafting artifact")
    forward = metadata.get("forward_coverage")
    if not isinstance(forward, Mapping) or (
        forward.get("status") != "pass"
        or forward.get("release_eligible") is not True
        or forward.get("coverage_mode")
        not in {"whole_declared_values", "whole_values_label_bound"}
    ):
        raise CrossChapterError(
            "E-CONSTRAINED-ASSEMBLY-COVERAGE: positive-coverage certification is absent"
        )
    style = metadata.get("style_qc")
    if not isinstance(style, Mapping) or style.get("status") != "pass" or style.get(
        "blocked"
    ) is not False:
        raise CrossChapterError("E-CONSTRAINED-ASSEMBLY-STYLE: style gate did not pass")
    if metadata.get("findings") or metadata.get("card_external_entities"):
        raise CrossChapterError("E-CONSTRAINED-ASSEMBLY-FINDINGS: guard findings remain")
    if metadata.get("numeric_placeholders") or metadata.get("binding_manifest"):
        raise CrossChapterError(
            "E-CONSTRAINED-ASSEMBLY-RESOLUTION-REQUIRED: placeholder-bearing artifact "
            "must pass the ordinary numeric resolver"
        )
    if evidence_status not in {"reviewed", "demo_only", "source_gap"}:
        raise CrossChapterError("Invalid constrained section evidence_status")

    section_id = metadata.get("section_id")
    card_id = metadata.get("card_id")
    draft_sha256 = metadata.get("draft_sha256")
    if not all(isinstance(item, str) and item for item in (section_id, card_id, draft_sha256)):
        raise CrossChapterError("Constrained section identifiers are malformed")
    heading, notices, body_paragraphs = _split_section_markdown(
        markdown_file.read_text(encoding="utf-8"), f"constrained section {section_id}"
    )
    paragraph_meta = metadata.get("paragraphs")
    if not isinstance(paragraph_meta, list) or len(paragraph_meta) != len(body_paragraphs):
        raise CrossChapterError(f"Paragraph count drift in constrained section {section_id}")
    source_manifest = metadata.get("source_manifest")
    if not isinstance(source_manifest, list) or not source_manifest:
        raise CrossChapterError("Constrained section source_manifest is missing")
    source_ids = []
    for index, item in enumerate(source_manifest):
        if not isinstance(item, Mapping) or not isinstance(item.get("source_id"), str):
            raise CrossChapterError(f"Malformed constrained source_manifest entry {index}")
        source_ids.append(str(item["source_id"]))

    paragraphs: list[ResolvedParagraph] = []
    seen_para_ids: set[str] = set()
    for index, (text, item) in enumerate(
        zip(body_paragraphs, paragraph_meta, strict=True), start=1
    ):
        if not isinstance(item, Mapping) or set(item) != {
            "para_id",
            "fact_ids",
            "text_sha256",
        }:
            raise CrossChapterError(
                f"Constrained paragraph metadata drift at {section_id}[{index}]"
            )
        para_id = item.get("para_id")
        fact_ids = item.get("fact_ids")
        if (
            not isinstance(para_id, str)
            or not para_id
            or para_id in seen_para_ids
            or not isinstance(fact_ids, list)
            or not fact_ids
            or not all(isinstance(fact_id, str) and fact_id for fact_id in fact_ids)
        ):
            raise CrossChapterError(
                f"Invalid constrained paragraph identity at {section_id}[{index}]"
            )
        seen_para_ids.add(para_id)
        if item.get("text_sha256") != _sha256_text(text):
            raise CrossChapterError(f"Constrained paragraph hash mismatch for {para_id}")
        without_references = REF_PLACEHOLDER_RE.sub("", text)
        if "{{" in without_references or "}}" in without_references:
            raise CrossChapterError(
                f"E-CONSTRAINED-ASSEMBLY-UNRESOLVED: {para_id} contains a placeholder"
            )
        references = tuple(find_reference_ids(text))
        if references:
            raise CrossChapterError(
                f"E-CONSTRAINED-ASSEMBLY-UNSCOPED-REFERENCE: {para_id} contains a reference"
            )
        needs_human = REVIEW_MARKER in text
        paragraph_sources = tuple(
            dict.fromkeys(
                (*source_ids, *(f"fact:{card_id}/{fact_id}" for fact_id in fact_ids))
            )
        )
        paragraphs.append(
            ResolvedParagraph(
                section_id=section_id,
                para_id=para_id,
                text=text,
                needs_human=needs_human,
                confidence=(
                    "inferred"
                    if needs_human
                    else "verbatim"
                    if section_id == "12.3.2"
                    else "derived"
                ),
                sources=paragraph_sources,
                placeholders=tuple(),
                references=tuple(),
            )
        )

    return ResolvedSection(
        section_id=section_id,
        heading=heading,
        notices=notices,
        evidence_status=evidence_status,
        draft_sha256=draft_sha256,
        resolved_sha256=sha256_file(markdown_file),
        metadata_path=metadata_file,
        reference_entities=tuple(),
        paragraphs=tuple(paragraphs),
    )


def apply_review_decisions(
    sections: Sequence[ResolvedSection], decisions: Mapping[str, Any]
) -> dict[str, tuple[str, ...]]:
    """Return clean paragraph text only after every pending item is explicitly resolved."""
    if not isinstance(decisions, Mapping):
        raise CrossChapterError("Review decisions must be an object")
    _require_exact_keys(decisions, {"schema_version", "sections"}, "review decisions")
    if decisions.get("schema_version") != "1.0":
        raise CrossChapterError("Unsupported review decisions schema_version")
    decision_sections = decisions.get("sections")
    if not isinstance(decision_sections, Mapping):
        raise CrossChapterError("review decisions.sections must be an object")
    expected_section_ids = {section.section_id for section in sections}
    if set(decision_sections) != expected_section_ids:
        raise CrossChapterError(
            "Review decision sections must exactly match assembled sections; "
            f"expected={sorted(expected_section_ids)}, got={sorted(decision_sections)}"
        )

    clean: dict[str, tuple[str, ...]] = {}
    unresolved: list[str] = []
    for section in sections:
        section_decision = decision_sections[section.section_id]
        if not isinstance(section_decision, Mapping):
            raise CrossChapterError(f"Review decision for {section.section_id} must be an object")
        _require_exact_keys(
            section_decision, {"draft_sha256", "paragraphs"}, f"review {section.section_id}"
        )
        if section_decision.get("draft_sha256") != section.draft_sha256:
            raise CrossChapterError(f"Stale review decision for section {section.section_id}")
        paragraph_decisions = section_decision.get("paragraphs")
        if not isinstance(paragraph_decisions, Mapping):
            raise CrossChapterError(f"review {section.section_id}.paragraphs must be an object")
        known_ids = {paragraph.para_id for paragraph in section.paragraphs}
        unknown = sorted(set(paragraph_decisions) - known_ids)
        if unknown:
            raise CrossChapterError(
                f"Review decisions reference unknown paragraphs in {section.section_id}: {unknown}"
            )
        output_paragraphs: list[str] = []
        for paragraph in section.paragraphs:
            raw_decision = paragraph_decisions.get(paragraph.para_id)
            if paragraph.needs_human:
                if raw_decision is None:
                    unresolved.append(f"{section.section_id}:{paragraph.para_id}")
                    output_paragraphs.append(paragraph.text)
                    continue
                if not isinstance(raw_decision, Mapping):
                    raise CrossChapterError(f"Decision for {paragraph.para_id} must be an object")
                _require_exact_keys(
                    raw_decision,
                    {"status", "replacement_text"},
                    f"decision {paragraph.para_id}",
                )
                replacement = raw_decision.get("replacement_text")
                if raw_decision.get("status") != "resolved" or not isinstance(
                    replacement, str
                ) or not replacement.strip():
                    unresolved.append(f"{section.section_id}:{paragraph.para_id}")
                    output_paragraphs.append(paragraph.text)
                    continue
                replacement = replacement.strip()
                if "\n" in replacement or REVIEW_MARKER in replacement or "[待确认]" in replacement:
                    raise CrossChapterError(
                        f"Resolved replacement for {paragraph.para_id} contains a review marker/newline"
                    )
                replacement_references = find_reference_ids(replacement)
                if any(entity_id not in section.reference_entities for entity_id in replacement_references):
                    raise CrossChapterError(
                        f"Resolved replacement for {paragraph.para_id} adds an unconfirmed reference"
                    )
                if "{{" in REF_PLACEHOLDER_RE.sub("", replacement):
                    raise CrossChapterError(
                        f"Resolved replacement for {paragraph.para_id} contains a value placeholder"
                    )
                output_paragraphs.append(replacement)
            else:
                if raw_decision is not None:
                    raise CrossChapterError(
                        f"Non-pending paragraph {paragraph.para_id} must not be silently rewritten"
                    )
                output_paragraphs.append(paragraph.text)
        clean[section.section_id] = tuple(output_paragraphs)
    if unresolved:
        raise CrossChapterError(
            "Clean output blocked until every pending paragraph is resolved: "
            + ", ".join(sorted(unresolved))
        )
    return clean


def derive_confirmed_body_binding_manifest(
    sections: Sequence[ResolvedSection],
    clean_paragraphs: Mapping[str, Sequence[str]],
    selected_keys: Sequence[str],
) -> dict[str, Any]:
    """Bind section-13 inputs to keys already present in reviewed body prose.

    ``clean_paragraphs`` must be the output of :func:`apply_review_decisions`;
    therefore every pending paragraph has an explicit replacement and no review
    marker remains.  A selected result key must occur in exactly one source
    paragraph, preventing section 13 from declaring an arbitrary TFL key as a
    previously reported result.
    """

    if not isinstance(selected_keys, Sequence) or isinstance(
        selected_keys, (str, bytes)
    ) or not selected_keys:
        raise CrossChapterError("Confirmed-body binding selection must be non-empty")
    keys = list(selected_keys)
    if not all(isinstance(item, str) and item for item in keys) or len(keys) != len(
        set(keys)
    ):
        raise CrossChapterError("Confirmed-body binding keys must be unique strings")

    occurrences: dict[str, list[dict[str, str]]] = {key: [] for key in keys}
    for section in sections:
        clean = clean_paragraphs.get(section.section_id)
        if clean is None or len(clean) != len(section.paragraphs):
            raise CrossChapterError(
                f"Clean reviewed body is missing for section {section.section_id}"
            )
        for index, paragraph in enumerate(section.paragraphs):
            text = str(clean[index]).strip()
            if REVIEW_MARKER in text or "[待确认]" in text:
                raise CrossChapterError(
                    f"Confirmed body still contains a review marker at {paragraph.para_id}"
                )
            for key in paragraph.placeholders:
                if key in occurrences:
                    occurrences[key].append(
                        {
                            "key": key,
                            "section_id": section.section_id,
                            "para_id": paragraph.para_id,
                            "resolved_sha256": section.resolved_sha256,
                            "confirmed_text_sha256": _sha256_text(text),
                        }
                    )

    sources: list[dict[str, str]] = []
    for key in keys:
        matches = occurrences[key]
        if len(matches) != 1:
            raise CrossChapterError(
                f"Confirmed-body key must occur exactly once before section 13: {key}; "
                f"occurrences={len(matches)}"
            )
        sources.append(matches[0])
    body = {
        "schema_version": "1.0",
        "generation_mode": "confirmed_body_only",
        "keys": keys,
        "sources": sources,
    }
    return {**body, "manifest_sha256": _sha256_json(body)}


def derive_synopsis_manifest(
    sections: Sequence[ResolvedSection],
    clean_paragraphs: Mapping[str, Sequence[str]],
    selection: Mapping[str, Sequence[str]],
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Copy selected confirmed body paragraphs into a deterministic Synopsis manifest."""
    section_map = {section.section_id: section for section in sections}
    if not isinstance(selection, Mapping) or not selection:
        raise CrossChapterError("Synopsis selection must name one or more body paragraphs")
    copied_text: list[str] = []
    records: list[dict[str, Any]] = []
    for section_id, para_ids in selection.items():
        if section_id not in section_map:
            raise CrossChapterError(f"Synopsis selects unknown section {section_id}")
        if not isinstance(para_ids, Sequence) or isinstance(para_ids, (str, bytes)) or not para_ids:
            raise CrossChapterError(f"Synopsis selection for {section_id} must be a non-empty list")
        section = section_map[section_id]
        clean = clean_paragraphs.get(section_id)
        if clean is None or len(clean) != len(section.paragraphs):
            raise CrossChapterError(f"Clean paragraph contract missing for {section_id}")
        index_by_id = {item.para_id: index for index, item in enumerate(section.paragraphs)}
        for para_id in para_ids:
            if para_id not in index_by_id:
                raise CrossChapterError(f"Synopsis selects unknown paragraph {section_id}:{para_id}")
            text = str(clean[index_by_id[para_id]])
            copied_text.append(text)
            records.append(
                {
                    "source_section": section_id,
                    "source_para_id": para_id,
                    "source_resolved_sha256": section.resolved_sha256,
                    "text_sha256": _sha256_text(text),
                }
            )
    manifest = {
        "schema_version": "1.0",
        "generation_mode": "derived_copy_only",
        "independent_drafting_allowed": False,
        "paragraphs": records,
        "synopsis_text_sha256": _sha256_text("\n\n".join(copied_text) + "\n"),
    }
    return tuple(copied_text), manifest


def verify_synopsis_copy(
    synopsis_paragraphs: Sequence[str], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Prove that every Synopsis paragraph is an exact confirmed-body copy."""
    if not isinstance(manifest, Mapping):
        raise CrossChapterError("Synopsis manifest must be an object")
    if manifest.get("generation_mode") != "derived_copy_only" or manifest.get(
        "independent_drafting_allowed"
    ) is not False:
        raise CrossChapterError("Synopsis was not declared as a copy-only derivative")
    records = manifest.get("paragraphs")
    if not isinstance(records, list) or len(records) != len(synopsis_paragraphs):
        raise CrossChapterError("Synopsis paragraph/manifest count mismatch")
    mismatches: list[str] = []
    for index, (text, record) in enumerate(zip(synopsis_paragraphs, records, strict=True)):
        if not isinstance(record, dict) or record.get("text_sha256") != _sha256_text(str(text)):
            mismatches.append(str(index))
    combined = _sha256_text("\n\n".join(str(item) for item in synopsis_paragraphs) + "\n")
    if combined != manifest.get("synopsis_text_sha256"):
        mismatches.append("combined")
    if mismatches:
        raise CrossChapterError(
            "Synopsis differs from confirmed body copy at: " + ", ".join(mismatches)
        )
    return {
        "status": "pass",
        "generation_mode": "derived_copy_only",
        "paragraph_count": len(synopsis_paragraphs),
        "independent_drafting_allowed": False,
        "synopsis_text_sha256": combined,
    }


def validate_analysis_set_consistency(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare analysis-set definitions across 9.7, 10.1, 11 and 12 metadata."""
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise CrossChapterError("Analysis-set records must be a list")
    definitions: dict[str, dict[str, list[str]]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise CrossChapterError(f"analysis_sets[{index}] must be an object")
        _require_exact_keys(record, {"section", "id", "definition"}, f"analysis_sets[{index}]")
        section, set_id, definition = (
            record.get("section"),
            record.get("id"),
            record.get("definition"),
        )
        if not all(isinstance(item, str) and item.strip() for item in (section, set_id, definition)):
            raise CrossChapterError(f"analysis_sets[{index}] fields must be non-empty strings")
        normalized = SPACE_RE.sub(" ", definition.strip())
        definitions.setdefault(set_id, {}).setdefault(normalized, []).append(section)
    issues: list[dict[str, Any]] = []
    for set_id, variants in sorted(definitions.items()):
        if len(variants) > 1:
            issues.append(
                {
                    "code": "E-ANALYSIS-SET-CONFLICT",
                    "analysis_set": set_id,
                    "variants": [
                        {"definition": definition, "sections": sorted(sections)}
                        for definition, sections in sorted(variants.items())
                    ],
                }
            )
    return {
        "status": "pass" if not issues else "fail",
        "analysis_set_count": len(definitions),
        "issues": issues,
    }


def validate_claim_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Check controlled claim stance/evidence across body, discussion and Synopsis."""
    if not isinstance(ledger, Mapping):
        raise CrossChapterError("Claim ledger must be an object")
    allowed = {"schema_version", "required_sections", "claims", "evidence_status"}
    required = {"schema_version", "required_sections", "claims"}
    if set(ledger) - allowed or required - set(ledger):
        raise CrossChapterError(
            "claim ledger keys are invalid; "
            f"missing={sorted(required - set(ledger))}, extra={sorted(set(ledger) - allowed)}"
        )
    if ledger.get("schema_version") != "1.0":
        raise CrossChapterError("Unsupported claim-ledger schema_version")
    required_sections = ledger.get("required_sections")
    claims = ledger.get("claims")
    if not isinstance(required_sections, list) or not required_sections or not all(
        isinstance(item, str) and item for item in required_sections
    ):
        raise CrossChapterError("claim ledger required_sections must be a non-empty string list")
    if not isinstance(claims, list):
        raise CrossChapterError("claim ledger claims must be a list")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            raise CrossChapterError(f"claims[{index}] must be an object")
        _require_exact_keys(
            claim, {"claim_id", "section", "stance", "evidence_keys"}, f"claims[{index}]"
        )
        if not all(
            isinstance(claim.get(field), str) and claim[field]
            for field in ("claim_id", "section", "stance")
        ):
            raise CrossChapterError(f"claims[{index}] identifiers must be strings")
        evidence = claim.get("evidence_keys")
        if not isinstance(evidence, list) or not all(isinstance(item, str) and item for item in evidence):
            raise CrossChapterError(f"claims[{index}].evidence_keys must be a string list")
        grouped.setdefault(str(claim["claim_id"]), []).append(claim)

    issues: list[dict[str, Any]] = []
    expected_sections = set(required_sections)
    for claim_id, entries in sorted(grouped.items()):
        sections = {str(item["section"]) for item in entries}
        stances = {str(item["stance"]) for item in entries}
        evidence_sets = {tuple(sorted(set(item["evidence_keys"]))) for item in entries}
        if sections != expected_sections:
            issues.append(
                {
                    "code": "E-CLAIM-COVERAGE",
                    "claim_id": claim_id,
                    "missing_sections": sorted(expected_sections - sections),
                    "extra_sections": sorted(sections - expected_sections),
                }
            )
        if len(stances) != 1:
            issues.append(
                {"code": "E-CLAIM-STANCE-CONFLICT", "claim_id": claim_id, "stances": sorted(stances)}
            )
        if len(evidence_sets) != 1:
            issues.append(
                {
                    "code": "E-CLAIM-EVIDENCE-CONFLICT",
                    "claim_id": claim_id,
                    "evidence_sets": [list(item) for item in sorted(evidence_sets)],
                }
            )
    return {"status": "pass" if not issues else "fail", "claim_count": len(grouped), "issues": issues}


def build_cross_chapter_report(
    synopsis_check: Mapping[str, Any],
    analysis_set_check: Mapping[str, Any],
    claim_check: Mapping[str, Any],
) -> dict[str, Any]:
    checks = {
        "synopsis_derived_copy": dict(synopsis_check),
        "analysis_set_consistency": dict(analysis_set_check),
        "claim_ledger_consistency": dict(claim_check),
    }
    failed = [name for name, report in checks.items() if report.get("status") != "pass"]
    return {
        "schema_version": "1.0",
        "status": "pass" if not failed else "fail",
        "checks": checks,
        "failed_checks": failed,
    }


def load_sections_from_manifest(manifest_path: str | Path) -> tuple[ResolvedSection, ...]:
    """Load a strict, path-based section manifest for CLI/e2e use."""
    manifest_file = Path(manifest_path)
    payload = load_json_unique(manifest_file)
    if not isinstance(payload, dict):
        raise CrossChapterError("Section artifact manifest must be an object")
    _require_exact_keys(payload, {"schema_version", "sections"}, "section artifact manifest")
    if payload.get("schema_version") != "1.0":
        raise CrossChapterError("Unsupported section artifact manifest schema_version")
    entries = payload.get("sections")
    if not isinstance(entries, list) or not entries:
        raise CrossChapterError("Section artifact manifest needs a non-empty sections list")
    root = manifest_file.parent
    sections: list[ResolvedSection] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise CrossChapterError(f"section manifest entry {index} must be an object")
        _require_exact_keys(
            entry,
            {"draft", "resolved", "metadata", "guard"},
            f"section manifest entry {index}",
        )
        paths = []
        for field in ("draft", "resolved", "metadata", "guard"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                raise CrossChapterError(f"section manifest entry {index}.{field} must be a path")
            paths.append((root / value).resolve())
        sections.append(load_resolved_section(*paths))
    ids = [section.section_id for section in sections]
    if len(ids) != len(set(ids)):
        raise CrossChapterError("Section artifact manifest contains duplicate section IDs")
    return tuple(sections)


def pending_paragraph_ids(sections: Iterable[ResolvedSection]) -> tuple[str, ...]:
    return tuple(
        f"{section.section_id}:{paragraph.para_id}"
        for section in sections
        for paragraph in section.paragraphs
        if paragraph.needs_human
    )
