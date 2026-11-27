"""Guard-preserving orchestration of one routed CSR into two reader versions.

This module is deliberately an admission and ordering layer.  It cannot accept
free text or prebuilt ``ResolvedSection`` objects: ordinary sections must pass
the resolver bridge, constrained sections must pass the positive-coverage
bridge, and deterministic content must pass the complete 48+4 bundle gate.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from docx import Document
from lxml import etree

from .cross_chapter import (
    REVIEW_MARKER,
    ResolvedSection,
    apply_review_decisions,
    load_constrained_resolved_section,
    load_resolved_section,
)
from .deterministic_sections import (
    _canonicalize_docx_package,
    validate_deterministic_bundle,
)
from .errors import CSRWriterError
from .io_utils import atomic_write_json, load_json_unique, paths_alias, sha256_file
from .m4_assembly import validate_clean_docx, validate_docx_references
from .paths import (
    DEFAULT_FORMAT_SPEC,
    DEFAULT_STRUCTURE,
    DEFAULT_TEMPLATE,
    DEFAULT_TITLE_OVERRIDES,
)
from .project_skeleton import (
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
)


class UnifiedCompositionError(CSRWriterError):
    """A source is uncertified or the final composition violates its plan."""


ORDINARY_KEYS = {"draft_path", "resolved_path", "metadata_path", "guard_path"}
CONSTRAINED_KEYS = {"markdown_path", "metadata_path", "evidence_status"}


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise UnifiedCompositionError(
            f"E-COMPOSE-ADMISSION-CONTRACT: {label} must have exactly "
            f"{sorted(expected)}"
        )


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise UnifiedCompositionError(f"E-COMPOSE-PATH: {label} must be a path")
    result = Path(value)
    if not result.is_file():
        raise UnifiedCompositionError(f"E-COMPOSE-PATH: {label} does not exist: {result}")
    return result


def _load_admitted_sections(
    ordinary_artifacts: Sequence[Mapping[str, Any]],
    constrained_artifacts: Sequence[Mapping[str, Any]],
) -> tuple[tuple[ResolvedSection, ...], list[dict[str, Any]]]:
    if isinstance(ordinary_artifacts, (str, bytes)) or isinstance(
        constrained_artifacts, (str, bytes)
    ):
        raise UnifiedCompositionError("E-COMPOSE-ADMISSION-CONTRACT")
    sections: list[ResolvedSection] = []
    admissions: list[dict[str, Any]] = []
    for index, raw in enumerate(ordinary_artifacts):
        if not isinstance(raw, Mapping):
            raise UnifiedCompositionError("E-COMPOSE-ADMISSION-CONTRACT")
        _exact_keys(raw, ORDINARY_KEYS, f"ordinary_artifacts[{index}]")
        paths = {
            key: _path(raw[key], f"ordinary_artifacts[{index}].{key}")
            for key in ORDINARY_KEYS
        }
        try:
            section = load_resolved_section(
                paths["draft_path"],
                paths["resolved_path"],
                paths["metadata_path"],
                paths["guard_path"],
                require_condition_proof=True,
            )
        except Exception as exc:
            raise UnifiedCompositionError(
                f"E-COMPOSE-GUARD-PROOF: ordinary artifact {index} was rejected: {exc}"
            ) from exc
        guard = load_json_unique(paths["guard_path"])
        admissions.append(
            {
                "section_id": section.section_id,
                "admission_path": "ordinary_resolver_bridge",
                "evidence_status": section.evidence_status,
                "input_sha256": {
                    key: sha256_file(path) for key, path in sorted(paths.items())
                },
                "guard_status": guard.get("status"),
                "guard_stages": {
                    key: value.get("status")
                    for key, value in guard.get("stages", {}).items()
                    if isinstance(value, Mapping)
                },
            }
        )
        sections.append(section)

    for index, raw in enumerate(constrained_artifacts):
        if not isinstance(raw, Mapping):
            raise UnifiedCompositionError("E-COMPOSE-ADMISSION-CONTRACT")
        _exact_keys(raw, CONSTRAINED_KEYS, f"constrained_artifacts[{index}]")
        markdown = _path(
            raw["markdown_path"], f"constrained_artifacts[{index}].markdown_path"
        )
        metadata = _path(
            raw["metadata_path"], f"constrained_artifacts[{index}].metadata_path"
        )
        evidence_status = raw["evidence_status"]
        if not isinstance(evidence_status, str):
            raise UnifiedCompositionError("E-COMPOSE-ADMISSION-CONTRACT")
        try:
            section = load_constrained_resolved_section(
                markdown, metadata, evidence_status=evidence_status
            )
        except Exception as exc:
            raise UnifiedCompositionError(
                f"E-COMPOSE-GUARD-PROOF: constrained artifact {index} was rejected: {exc}"
            ) from exc
        guard = load_json_unique(metadata)
        admissions.append(
            {
                "section_id": section.section_id,
                "admission_path": "positive_coverage_bridge",
                "evidence_status": section.evidence_status,
                "input_sha256": {
                    "markdown_path": sha256_file(markdown),
                    "metadata_path": sha256_file(metadata),
                },
                "guard_status": guard.get("status"),
                "forward_coverage_status": guard.get("forward_coverage", {}).get(
                    "status"
                ),
                "forward_coverage_release_eligible": guard.get(
                    "forward_coverage", {}
                ).get("release_eligible"),
                "style_status": guard.get("style_qc", {}).get("status"),
            }
        )
        sections.append(section)

    if not sections:
        raise UnifiedCompositionError("E-COMPOSE-NO-GUARDED-BODY")
    ids = [section.section_id for section in sections]
    if len(ids) != len(set(ids)):
        raise UnifiedCompositionError(
            f"E-COMPOSE-DUPLICATE-SECTION: {sorted(ids)}"
        )
    return tuple(sections), admissions


def _evidence_status(value: str) -> str:
    if value == "demo_only":
        return "demo_only"
    if value == "source_gap":
        return "source_gap"
    if value == "derived_copy_only":
        return "derived_copy_only"
    if value in {"reviewed", "reviewed_project_evidence"}:
        return "reviewed_project_evidence"
    raise UnifiedCompositionError(f"E-COMPOSE-EVIDENCE-STATUS: {value!r}")


def _content_from_sections(
    sections: Sequence[ResolvedSection],
    clean_paragraphs: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for section in sections:
        replacements = clean_paragraphs.get(section.section_id) if clean_paragraphs else None
        if replacements is not None and len(replacements) != len(section.paragraphs):
            raise UnifiedCompositionError(
                f"E-COMPOSE-CLEAN-PARAGRAPH-DRIFT: {section.section_id}"
            )
        paragraphs: list[dict[str, Any]] = []
        for index, paragraph in enumerate(section.paragraphs):
            text = paragraph.text if replacements is None else str(replacements[index])
            paragraphs.append(
                {
                    "text": text,
                    "sources": list(paragraph.sources),
                    "confidence": (
                        paragraph.confidence
                        if replacements is None
                        else "derived"
                        if paragraph.needs_human
                        else paragraph.confidence
                    ),
                    "needs_human": paragraph.needs_human if replacements is None else False,
                }
            )
        content.append(
            {
                "section_id": section.section_id,
                "variant": "guard_admitted_body",
                "title": section.heading,
                "evidence_status": _evidence_status(section.evidence_status),
                "paragraphs": paragraphs,
                "reference_entities": list(section.reference_entities),
            }
        )
    return content


def _section_plan_record(section: ResolvedSection) -> dict[str, Any]:
    return {
        "section_id": section.section_id,
        "heading": section.heading,
        "draft_sha256": section.draft_sha256,
        "resolved_sha256": section.resolved_sha256,
        "metadata_sha256": sha256_file(section.metadata_path),
        "reference_entities": list(section.reference_entities),
        "paragraphs": [
            {
                "para_id": paragraph.para_id,
                "text_sha256": hashlib.sha256(paragraph.text.encode("utf-8")).hexdigest(),
                "needs_human": paragraph.needs_human,
                "sources": list(paragraph.sources),
            }
            for paragraph in section.paragraphs
        ],
    }


def _field_and_page_invariants(
    path: Path,
    *,
    expected_plan_sha256: str,
    expected_omission_count: int,
) -> dict[str, Any]:
    with zipfile.ZipFile(path) as package:
        members = package.namelist()
        xml_payloads = {
            name: package.read(name)
            for name in members
            if name.startswith("word/") and name.endswith(".xml")
        }
    instructions: list[str] = []
    visible: list[str] = []
    for payload in xml_payloads.values():
        root = etree.fromstring(payload)
        instructions.extend(
            node.text or ""
            for node in root.iter()
            if etree.QName(node).localname == "instrText"
        )
        visible.extend(
            node.text or ""
            for node in root.iter()
            if etree.QName(node).localname == "t"
        )
    toc_instructions = [item for item in instructions if item.strip().startswith("TOC ")]
    ref_instructions = [item for item in instructions if item.strip().startswith("REF ")]
    page_fields = 0
    centered_page_fields = 0
    for name, payload in xml_payloads.items():
        if not name.startswith("word/footer"):
            continue
        root = etree.fromstring(payload)
        for paragraph in root.iter():
            if etree.QName(paragraph).localname != "p":
                continue
            paragraph_instructions = [
                node.text or ""
                for node in paragraph.iter()
                if etree.QName(node).localname == "instrText"
            ]
            if not any(item.strip() == "PAGE" for item in paragraph_instructions):
                continue
            page_fields += 1
            centered = any(
                etree.QName(node).localname == "jc"
                and (node.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val") == "center")
                for node in paragraph.iter()
            )
            centered_page_fields += int(centered)
    text = "\n".join(visible)
    omission_lines = text.count("——")
    identifier = Document(path).core_properties.identifier
    update_fields = False
    with zipfile.ZipFile(path) as package:
        settings = etree.fromstring(package.read("word/settings.xml"))
        update_fields = any(
            etree.QName(node).localname == "updateFields"
            and node.get(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
            )
            in {"true", "1"}
            for node in settings.iter()
        )
    checks = {
        "six_directory_fields_preserved": len(toc_instructions) == 6,
        "word_fields_enabled_for_update": update_fields,
        "page_field_present": page_fields >= 1,
        "page_field_centered": page_fields == centered_page_fields,
        "composition_plan_bound": identifier == expected_plan_sha256,
        "no_unresolved_placeholders": "{{" not in text and "}}" not in text,
        "omission_declarations_present": (
            expected_omission_count == 0 or omission_lines >= expected_omission_count
        ),
    }
    if not all(checks.values()):
        raise UnifiedCompositionError(
            f"E-COMPOSE-OOXML-QC: {path.name} failed "
            f"{[key for key, value in checks.items() if not value]}"
        )
    return {
        "status": "pass",
        "checks": {key: "pass" for key in checks},
        "directory_word_field_count": len(toc_instructions),
        "ref_field_count": len(ref_instructions),
        "page_field_count": page_fields,
        "centered_page_field_count": centered_page_fields,
        "omission_reason_line_count": omission_lines,
        "composition_plan_sha256": identifier,
    }


def _render_pair(
    *,
    routing: Mapping[str, Any],
    annotated_content: Sequence[Mapping[str, Any]],
    clean_content: Sequence[Mapping[str, Any]],
    deterministic_bundle: Mapping[str, Any],
    publication_reference_map: Mapping[str, Any] | None,
    study_facts: Mapping[str, Any],
    annotated_output: Path,
    clean_output: Path,
    work_directory: Path,
    composition_plan_sha256: str,
    structure_path: str | Path,
    template_path: str | Path,
    format_spec_path: str | Path,
    title_overrides_path: str | Path,
) -> dict[str, Any]:
    work_directory.mkdir(parents=True, exist_ok=True)
    annotated = render_project_annotated_skeleton(
        routing,
        annotated_content,
        annotated_output,
        scaffold_path=work_directory / "annotated.scaffold.docx",
        structure_path=structure_path,
        template_path=template_path,
        format_spec_path=format_spec_path,
        omission_policy="remove",
        publication_reference_map=publication_reference_map,
        study_facts=study_facts,
        title_overrides_path=title_overrides_path,
        deterministic_bundle=deterministic_bundle,
        composition_plan_sha256=composition_plan_sha256,
    )
    clean = render_project_clean_skeleton(
        routing,
        clean_output,
        content_sections=clean_content,
        scaffold_path=work_directory / "clean.scaffold.docx",
        structure_path=structure_path,
        template_path=template_path,
        format_spec_path=format_spec_path,
        omission_policy="remove",
        publication_reference_map=publication_reference_map,
        study_facts=study_facts,
        title_overrides_path=title_overrides_path,
        deterministic_bundle=deterministic_bundle,
        composition_plan_sha256=composition_plan_sha256,
    )
    _canonicalize_docx_package(annotated_output)
    _canonicalize_docx_package(clean_output)
    expected_omissions = len(
        routing.get("structure", {}).get("section_plan", {}).get("omitted_sections", [])
    )
    annotated_qc = _field_and_page_invariants(
        annotated_output,
        expected_plan_sha256=composition_plan_sha256,
        expected_omission_count=expected_omissions,
    )
    clean_qc = _field_and_page_invariants(
        clean_output,
        expected_plan_sha256=composition_plan_sha256,
        expected_omission_count=expected_omissions,
    )
    validate_docx_references(annotated_output)
    validate_docx_references(clean_output)
    validate_clean_docx(clean_output)
    annotated["output_sha256"] = sha256_file(annotated_output)
    clean["output_sha256"] = sha256_file(clean_output)
    return {
        "annotated": {"render": annotated, "ooxml_qc": annotated_qc},
        "clean": {"render": clean, "ooxml_qc": clean_qc},
    }


def compose_unified_csr(
    *,
    routing: Mapping[str, Any],
    deterministic_bundle: Mapping[str, Any],
    ordinary_artifacts: Sequence[Mapping[str, Any]] = (),
    constrained_artifacts: Sequence[Mapping[str, Any]] = (),
    review_decisions: Mapping[str, Any],
    publication_reference_map: Mapping[str, Any] | None,
    study_facts: Mapping[str, Any],
    annotated_output: str | Path,
    clean_output: str | Path,
    composition_manifest_output: str | Path | None = None,
    structure_path: str | Path = DEFAULT_STRUCTURE,
    template_path: str | Path = DEFAULT_TEMPLATE,
    format_spec_path: str | Path = DEFAULT_FORMAT_SPEC,
    title_overrides_path: str | Path = DEFAULT_TITLE_OVERRIDES,
) -> dict[str, Any]:
    """Compose two versions from one immutable plan and verify byte reproducibility."""

    if not isinstance(routing, Mapping) or not isinstance(study_facts, Mapping):
        raise UnifiedCompositionError("E-COMPOSE-INPUT-CONTRACT")
    if publication_reference_map is not None and not isinstance(
        publication_reference_map, Mapping
    ):
        raise UnifiedCompositionError("E-COMPOSE-PUBLICATION-MAP")
    deterministic_validation = validate_deterministic_bundle(deterministic_bundle)
    sections, admissions = _load_admitted_sections(
        ordinary_artifacts, constrained_artifacts
    )
    evidence_kinds = {s.evidence_status for s in sections}
    if "demo_only" in evidence_kinds and len(evidence_kinds - {"demo_only", "source_gap"}) > 0:
        raise UnifiedCompositionError("E-COMPOSE-MIXED-DEMO-EVIDENCE")
    if "demo_only" not in evidence_kinds:
        section_plan = routing["structure"]["section_plan"]
        active = set(section_plan["retained_official_section_ids"])
        active.update(s["section_id"] for s in section_plan["formal_extensions"])
        parents = {sid.rsplit(".", 1)[0] for sid in active if "." in sid}
        covered = set(deterministic_validation["section_ids"]) | {s.section_id for s in sections}
        missing = sorted(active - parents - covered)
        if missing or "source_gap" in evidence_kinds:
            raise UnifiedCompositionError(f"E-COMPOSE-INCOMPLETE-CLEAN: {missing}")
    try:
        clean_paragraphs = apply_review_decisions(sections, review_decisions)
    except Exception as exc:
        raise UnifiedCompositionError(f"E-COMPOSE-REVIEW-GATE: {exc}") from exc
    for artifact in deterministic_bundle["artifacts"]:
        if artifact["content_kind"] != "confirmed_text_copy":
            continue
        for paragraph in artifact["content"]["paragraphs"]:
            index = paragraph["source_paragraph_index"]
            source = clean_paragraphs.get(paragraph["source_section_id"], ())
            if (type(index) is not int or not 0 <= index < len(source)
                    or source[index] != paragraph["text"]
                    or hashlib.sha256(source[index].encode("utf-8")).hexdigest()
                    != paragraph["source_text_sha256"]):
                raise UnifiedCompositionError("E-COMPOSE-SYNOPSIS-SOURCE-DRIFT")
    annotated_content = _content_from_sections(sections)
    clean_content = _content_from_sections(sections, clean_paragraphs)

    plan_body = {
        "schema_version": "1.0",
        "interface": "csr-writer-unified-composition-v1",
        "routing_sha256": _stable_hash(routing),
        "study_facts_sha256": _stable_hash(study_facts),
        "publication_reference_map_sha256": (
            _stable_hash(publication_reference_map)
            if publication_reference_map is not None
            else None
        ),
        "review_decisions_sha256": _stable_hash(review_decisions),
        "deterministic_bundle": deterministic_validation,
        "admissions": admissions,
        "guarded_sections": [_section_plan_record(section) for section in sections],
        "omitted_sections": routing.get("structure", {})
        .get("section_plan", {})
        .get("omitted_sections", []),
        "rendering": {
            "section_order_source": "ICH-E3 structure plus reviewed title overrides",
            "numbering_policy": "retain_gaps_with_visible_declarations",
            "body_rewrite_allowed": False,
            "directory_fields_remain_word_fields": True,
            "clean_and_annotated_share_plan": True,
            "structure_sha256": sha256_file(structure_path),
            "template_sha256": sha256_file(template_path),
            "format_spec_sha256": sha256_file(format_spec_path),
            "title_overrides_sha256": sha256_file(title_overrides_path),
        },
    }
    plan_sha256 = _stable_hash(plan_body)
    plan = {**plan_body, "composition_plan_sha256": plan_sha256}

    annotated_path = Path(annotated_output)
    clean_path = Path(clean_output)
    destinations = [annotated_path, clean_path]
    if composition_manifest_output is not None:
        destinations.append(Path(composition_manifest_output))
    inputs = [structure_path, template_path, format_spec_path, title_overrides_path]
    for spec in (*ordinary_artifacts, *constrained_artifacts):
        inputs.extend(path for name, path in spec.items() if name.endswith("_path"))
    for index, destination in enumerate(destinations):
        if any(paths_alias(destination, other) for other in (*destinations[:index], *inputs)):
            raise UnifiedCompositionError("E-COMPOSE-OUTPUT-ALIAS")
    annotated_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="csr-writer-compose-") as temporary:
        temporary_path = Path(temporary)
        first = _render_pair(
            routing=routing,
            annotated_content=annotated_content,
            clean_content=clean_content,
            deterministic_bundle=deterministic_bundle,
            publication_reference_map=publication_reference_map,
            study_facts=study_facts,
            annotated_output=annotated_path,
            clean_output=clean_path,
            work_directory=temporary_path / "first",
            composition_plan_sha256=plan_sha256,
            structure_path=structure_path,
            template_path=template_path,
            format_spec_path=format_spec_path,
            title_overrides_path=title_overrides_path,
        )
        verification_annotated = temporary_path / "verify.annotated.docx"
        verification_clean = temporary_path / "verify.clean.docx"
        _render_pair(
            routing=routing,
            annotated_content=annotated_content,
            clean_content=clean_content,
            deterministic_bundle=deterministic_bundle,
            publication_reference_map=publication_reference_map,
            study_facts=study_facts,
            annotated_output=verification_annotated,
            clean_output=verification_clean,
            work_directory=temporary_path / "second",
            composition_plan_sha256=plan_sha256,
            structure_path=structure_path,
            template_path=template_path,
            format_spec_path=format_spec_path,
            title_overrides_path=title_overrides_path,
        )
        reproducibility = {
            "annotated": {
                "first_sha256": sha256_file(annotated_path),
                "second_sha256": sha256_file(verification_annotated),
            },
            "clean": {
                "first_sha256": sha256_file(clean_path),
                "second_sha256": sha256_file(verification_clean),
            },
        }
    for item in reproducibility.values():
        item["byte_identical"] = item["first_sha256"] == item["second_sha256"]
    if not all(item["byte_identical"] for item in reproducibility.values()):
        raise UnifiedCompositionError(
            f"E-COMPOSE-NONDETERMINISTIC: {reproducibility}"
        )

    report = {
        "schema_version": "1.0",
        "status": "pass",
        "composition_plan": plan,
        "guarded_section_count": len(sections),
        "admission_count": len(admissions),
        "outputs": {
            "annotated": {
                "path": str(annotated_path.resolve()),
                "sha256": sha256_file(annotated_path),
                **first["annotated"],
            },
            "clean": {
                "path": str(clean_path.resolve()),
                "sha256": sha256_file(clean_path),
                **first["clean"],
            },
        },
        "reproducibility": reproducibility,
        "body_rewrite_performed": False,
        "guard_bypass_available": False,
    }
    if composition_manifest_output is not None:
        atomic_write_json(composition_manifest_output, report)
    return report

