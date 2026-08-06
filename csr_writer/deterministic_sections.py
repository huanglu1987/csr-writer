"""Zero-LLM producers for the 48 deterministic and four slot-template units.

The production-method registry is the ownership boundary.  This module never
falls back to prose generation: a missing reviewed input blocks the unit, and
every produced unit carries its own machine-checkable validation record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .cross_references import bookmark_name
from .errors import ConfigurationError
from .io_utils import sha256_file
from .paths import DEFAULT_TEMPLATE
from .production_methods import load_production_method_registry
from .publication_refs import validate_publication_reference_map


class DeterministicGenerationError(ConfigurationError):
    """A deterministic unit cannot be generated from its reviewed sources."""


DETERMINISTIC_BUNDLE_VERSION = "1.0"
DIRECTORY_FIELDS = {
    "3.1": ' TOC \\o "1-5" \\h \\z \\u ',
    "3.2": ' TOC \\c "Table" \\h \\z ',
    "3.3": ' TOC \\c "Figure" \\h \\z ',
    "3.4": ' TOC \\f "TFL" \\h \\z ',
    "3.5": ' TOC \\f "APP" \\h \\z ',
    "3.6": ' TOC \\f "SUP" \\h \\z ',
}

APPENDIX_HEADERS = (
    "附录编号",
    "文件名称",
    "本次是否提交",
    "提交位置",
    "未提交理由/是否可按要求提供",
)

_FORBIDDEN_FAKE_CONTENT_RE = re.compile(
    r"(?:TBD|TODO|PLACEHOLDER|INPUT REQUIRED|"
    r"占位(?:文字|内容|符)?|"
    r"待(?:补(?:充|齐|录入)?|填(?:写|充)?|确定|定)|"
    r"暂缺)",
    re.I,
)

_REPRODUCIBLE_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeterministicGenerationError(f"E-DETERMINISTIC-INPUT: {label} must be an object")
    return value


def _require_list(value: Any, label: str, *, allow_empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise DeterministicGenerationError(
            f"E-DETERMINISTIC-INPUT: {label} must be a"
            f" {'possibly empty' if allow_empty else 'non-empty'} list"
        )
    return list(value)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeterministicGenerationError(
            f"E-DETERMINISTIC-MISSING-SOURCE: {label} must be confirmed non-empty text"
        )
    text = value.strip()
    if _FORBIDDEN_FAKE_CONTENT_RE.search(text):
        raise DeterministicGenerationError(
            f"E-DETERMINISTIC-FAKE-CONTENT: {label} contains placeholder-like text"
        )
    return text


def _appendix_rows(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    if plan.get("status") != "pass":
        raise DeterministicGenerationError("E-APPENDIX-PLAN: appendix status plan is not passed")
    rows = plan.get("entries", plan.get("rows"))
    normalized = _require_list(rows, "appendix_status_plan.entries")
    if len(normalized) != 24:
        raise DeterministicGenerationError(
            f"E-APPENDIX-PLAN: expected 24 status rows, observed {len(normalized)}"
        )
    required = {
        "category",
        "category_title",
        "appendix_id",
        "file_name",
        "submitted",
        "submission_location",
        "reason_or_on_request",
    }
    clean: list[dict[str, Any]] = []
    for index, raw in enumerate(normalized):
        item = _require_mapping(raw, f"appendix row {index}")
        missing = required - set(item)
        if missing:
            raise DeterministicGenerationError(
                f"E-APPENDIX-PLAN: row {index} lacks {sorted(missing)}"
            )
        for field in required:
            _require_text(item[field], f"appendix row {index}.{field}")
        clean.append(copy.deepcopy(dict(item)))
    if len({item["category_title"] for item in clean}) != 4:
        raise DeterministicGenerationError("E-APPENDIX-PLAN: four categories are required")
    return clean


def _accepted_entity_ids(payload: Any) -> set[str]:
    if isinstance(payload, Mapping):
        raw = payload.get("entity_ids", payload.get("entities"))
    else:
        raw = payload
    values = _require_list(raw, "accepted_result_tfl entities")
    result: set[str] = set()
    for index, item in enumerate(values):
        entity = item.get("entity_id") if isinstance(item, Mapping) else item
        result.add(_require_text(entity, f"accepted_result_tfl[{index}].entity_id"))
    return result


def _frozen_entity_ids(payload: Mapping[str, Any]) -> set[str]:
    if payload.get("status") != "frozen":
        raise DeterministicGenerationError("E-TFL-MAP-NOT-FROZEN: tfl_map must be frozen")
    rows = _require_list(payload.get("entities"), "frozen_tfl_map.entities")
    return {
        _require_text(item.get("entity_id"), f"frozen_tfl_map.entities[{index}].entity_id")
        for index, item in enumerate(rows)
        if isinstance(item, Mapping)
    }


def _chapter14_bucket(
    entity_id: str, ich_e3_sections: Sequence[str] = ()
) -> str:
    numeric = entity_id[1:] if entity_id[:1] in {"T", "F", "L"} else entity_id
    if numeric.startswith("14.1.1"):
        return "14.1.1"
    if numeric.startswith("14.1.2"):
        return "14.1.2"
    if numeric.startswith(("14.1.3", "14.1.4")):
        return "14.1.3"
    if numeric.startswith("14.2"):
        return "14.2"
    for section_id in ("14.3.1", "14.3.2", "14.3.3", "14.3.4"):
        if numeric.startswith(section_id):
            return section_id
    # A reviewed result-only table can use a non-E3 source number.  In that
    # case its chapter-14 owner must come from the explicit frozen route; the
    # generator never infers an owner from the title or the unfamiliar number.
    direct = {
        section_id
        for route in ich_e3_sections
        for section_id in ("14.1.1", "14.1.2", "14.1.3", "14.2", "14.3.1", "14.3.2", "14.3.3", "14.3.4")
        if route == section_id or route.startswith(section_id + ".")
    }
    if len(direct) == 1:
        return next(iter(direct))
    result_routes = set(ich_e3_sections)
    if result_routes and all(route == "12.4" or route.startswith("12.4.") for route in result_routes):
        return "14.3.4"
    raise DeterministicGenerationError(
        f"E-CHAPTER14-UNROUTED: publication entity {entity_id} has no explicit "
        f"chapter-14 owner; routes={sorted(ich_e3_sections)}"
    )


def _validation(section_id: str, checks: Mapping[str, Any]) -> dict[str, Any]:
    failed = [name for name, passed in checks.items() if passed is not True]
    return {
        "section_id": section_id,
        "status": "pass" if not failed else "blocked",
        "checks": dict(checks),
        "failed_checks": failed,
        "model_called": False,
    }


def _unit(
    assignment: Mapping[str, Any],
    title: str,
    content_kind: str,
    content: Mapping[str, Any],
    checks: Mapping[str, Any],
) -> dict[str, Any]:
    validation = _validation(str(assignment["section_id"]), checks)
    if validation["status"] != "pass":
        raise DeterministicGenerationError(
            f"E-DETERMINISTIC-VALIDATION: {assignment['section_id']} failed "
            f"{validation['failed_checks']}"
        )
    return {
        "section_id": assignment["section_id"],
        "title": title,
        "production_method": assignment["production_method"],
        "production_profile": assignment["production_profile"],
        "content_kind": content_kind,
        "content": copy.deepcopy(dict(content)),
        "validation": validation,
    }


def _title_page(
    assignment: Mapping[str, Any], title: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    identifiers = _require_mapping(
        _require_mapping(context.get("study_facts"), "study_facts").get("study_identifiers"),
        "study_facts.study_identifiers",
    )
    sponsor = _require_mapping(context.get("sponsor_metadata"), "sponsor_metadata")
    version = _require_mapping(context.get("report_version_and_date"), "report_version_and_date")
    signatories = _require_list(
        context.get("approved_signatory_register"), "approved_signatory_register"
    )
    required_ids = {f"CDE-{index}" for index in range(1, 8)}
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(signatories):
        item = dict(_require_mapping(raw, f"approved_signatory_register[{index}]"))
        control_id = _require_text(item.get("control_id"), f"signatory {index}.control_id")
        if control_id in by_id:
            raise DeterministicGenerationError(f"E-TITLE-SIGNATORY: duplicate {control_id}")
        for field in ("label", "value", "evidence_status", "location"):
            _require_text(item.get(field), f"signatory {control_id}.{field}")
        by_id[control_id] = item
    if set(by_id) != required_ids:
        raise DeterministicGenerationError(
            f"E-TITLE-SIGNATORY: expected CDE-1..CDE-7, observed {sorted(by_id)}"
        )
    for control_id in (f"CDE-{index}" for index in range(1, 7)):
        if by_id[control_id]["location"] != "标题页":
            raise DeterministicGenerationError(
                f"E-TITLE-SIGNATORY: {control_id} must be located on 标题页"
            )
    if by_id["CDE-7"]["location"] != "16.1.5":
        raise DeterministicGenerationError(
            "E-TITLE-SIGNATORY: CDE-7 sponsor medical officer signature belongs to 16.1.5"
        )
    fields = {
        "study_title": _require_text(identifiers.get("study_title"), "study title"),
        "protocol_id": _require_text(identifiers.get("protocol_id"), "protocol ID"),
        "report_version": _require_text(version.get("version"), "report version"),
        "report_date": _require_text(version.get("date"), "report date"),
        "sponsor": _require_text(sponsor.get("sponsor"), "sponsor"),
        "confidentiality": _require_text(version.get("confidentiality"), "confidentiality"),
    }
    return _unit(
        assignment,
        title,
        "title_page_template",
        {
            "fields": fields,
            "title_page_controls": [by_id[f"CDE-{index}"] for index in range(1, 7)],
            "annex_ii_control": by_id["CDE-7"],
            "basis": "CDE 2025年第14号§3.2/第15号§3.4.2；第7项归16.1.5",
        },
        {
            "all_required_slots_present": True,
            "six_title_page_controls_present": len(by_id) == 7,
            "annex_ii_control_routed_to_16_1_5": by_id["CDE-7"]["location"] == "16.1.5",
        },
    )


def _synopsis(
    assignment: Mapping[str, Any], title: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    body = _require_mapping(context.get("confirmed_clean_body"), "confirmed_clean_body")
    manifest = _require_mapping(
        context.get("synopsis_selection_manifest"), "synopsis_selection_manifest"
    )
    selections = _require_list(manifest.get("selection"), "synopsis_selection_manifest.selection")
    copied: list[dict[str, Any]] = []
    for index, raw in enumerate(selections):
        item = _require_mapping(raw, f"synopsis selection {index}")
        source_id = _require_text(item.get("source_section_id"), "synopsis source section")
        source_paragraphs = _require_list(body.get(source_id), f"confirmed_clean_body.{source_id}")
        indices = _require_list(item.get("paragraph_indices"), "synopsis paragraph indices")
        for paragraph_index in indices:
            if not isinstance(paragraph_index, int) or paragraph_index < 0 or paragraph_index >= len(source_paragraphs):
                raise DeterministicGenerationError(
                    f"E-SYNOPSIS-SELECTION: invalid paragraph index {paragraph_index} for {source_id}"
                )
            text = _require_text(source_paragraphs[paragraph_index], "confirmed synopsis source")
            copied.append(
                {
                    "source_section_id": source_id,
                    "source_paragraph_index": paragraph_index,
                    "text": text,
                    "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
    return _unit(
        assignment,
        title,
        "confirmed_text_copy",
        {"paragraphs": copied},
        {"selection_nonempty": bool(copied), "all_text_copied_verbatim": True},
    )


def _directory(
    assignment: Mapping[str, Any], title: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    section_id = str(assignment["section_id"])
    source_checks = {
        "3.1": "document_heading_styles",
        "3.2": "caption_style_table",
        "3.3": "caption_style_figure",
        "3.4": "publication_reference_map",
        "3.5": "appendix_status_plan",
        "3.6": "confirmed_submission_registry",
    }
    source_key = source_checks[section_id]
    source = context.get(source_key)
    if source_key in {"document_heading_styles", "caption_style_table", "caption_style_figure"}:
        _require_text(source, source_key)
    elif source_key == "publication_reference_map":
        validate_publication_reference_map(_require_mapping(source, source_key))
    elif source_key == "appendix_status_plan":
        _appendix_rows(_require_mapping(source, source_key))
    else:
        registry = _require_mapping(source, source_key)
        _require_list(registry.get("entries"), f"{source_key}.entries")
    return _unit(
        assignment,
        title,
        "word_field",
        {"instruction": DIRECTORY_FIELDS[section_id], "source_key": source_key},
        {"word_field_instruction_present": True, "source_validated": True},
    )


def _ethics_template(
    assignment: Mapping[str, Any], title: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    section_id = str(assignment["section_id"])
    if section_id == "5.1":
        committee = _require_mapping(context.get("committee_register"), "committee_register")
        committees = _require_list(committee.get("committees"), "committee_register.committees")
        approval_identifier = _require_text(context.get("approval_identifier"), "approval_identifier")
        approval_date = _require_text(context.get("approval_date"), "approval_date")
        protocol_version = _require_text(context.get("protocol_version"), "protocol_version")
        text = (
            f"本研究方案{protocol_version}已由{_require_text(committees[0], 'committee')}审查并批准，"
            f"批件编号为{approval_identifier}，批准日期为{approval_date}。"
        )
        slots = ["committee_register", "approval_identifier", "approval_date", "protocol_version"]
    elif section_id == "5.2":
        ethics = _require_text(context.get("applicable_ethics_standard"), "applicable_ethics_standard")
        gcp = _require_text(context.get("gcp_standard"), "gcp_standard")
        deviations = _require_text(
            context.get("confirmed_ethics_deviations"), "confirmed_ethics_deviations"
        )
        text = f"本研究按照{ethics}和{gcp}开展。经确认的伦理相关偏离情况为：{deviations}。"
        slots = ["applicable_ethics_standard", "gcp_standard", "confirmed_ethics_deviations"]
    else:
        versions = _require_list(context.get("consent_form_versions"), "consent_form_versions")
        process = _require_text(context.get("consent_process_register"), "consent_process_register")
        waiver = _require_text(context.get("waiver_if_any"), "waiver_if_any")
        version_text = "、".join(_require_text(item, "consent form version") for item in versions)
        text = f"研究使用的知情同意文件版本为{version_text}。{process}。豁免情况：{waiver}。"
        slots = ["consent_form_versions", "consent_process_register", "waiver_if_any"]
    return _unit(
        assignment,
        title,
        "fixed_template_with_slots",
        {"text": text, "filled_slots": slots},
        {"all_slots_present": True, "fixed_template_used": True},
    )


def _individual_reference(
    assignment: Mapping[str, Any], title: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    targets = _require_mapping(
        context.get("individual_listing_targets"), "individual_listing_targets"
    )
    target = _require_mapping(
        targets.get(assignment["section_id"]),
        f"individual_listing_targets.{assignment['section_id']}",
    )
    display = _require_text(target.get("display"), "individual listing display")
    bookmark = _require_text(target.get("bookmark"), "individual listing bookmark")
    target_type = target.get("target_type", "appendix")
    if target_type == "appendix":
        appendix_match = re.search(r"(16(?:\.\d+)+)", display)
        if appendix_match is None:
            raise DeterministicGenerationError(
                f"E-INDIVIDUAL-LISTING-TARGET: {assignment['section_id']} lacks an appendix ID"
            )
        appendix_id = appendix_match.group(1)
        appendix_ids = {
            row["appendix_id"]
            for row in _appendix_rows(
                _require_mapping(context.get("appendix_status_plan"), "appendix_status_plan")
            )
        }
        expected_bookmark = f"_Ref_Sec_{appendix_id.replace('.', '_')}"
        target_confirmed = appendix_id in appendix_ids
        target_check = "target_retained_in_appendix_status_plan"
    elif target_type == "publication":
        entity_id = _require_text(target.get("entity_id"), "individual listing entity_id")
        publication = validate_publication_reference_map(
            _require_mapping(
                context.get("publication_reference_map"), "publication_reference_map"
            )
        )
        publication_ids = {
            item["publication_entity_id"] for item in publication["entries"]
        }
        expected_bookmark = bookmark_name(entity_id)
        target_confirmed = entity_id in publication_ids
        target_check = "target_retained_in_publication_reference_map"
    else:
        raise DeterministicGenerationError(
            f"E-INDIVIDUAL-LISTING-TARGET: unsupported target_type {target_type!r}"
        )
    if not target_confirmed or bookmark != expected_bookmark:
        raise DeterministicGenerationError(
            f"E-INDIVIDUAL-LISTING-TARGET: expected confirmed {expected_bookmark}"
        )
    prefix_by_section = {
        "12.3.1.1": "死亡列表详见",
        "12.3.1.2": "其他严重不良事件列表详见",
        "12.3.1.3": "其他重要不良事件列表详见",
    }
    return _unit(
        assignment,
        title,
        "word_ref",
        {
            "prefix": prefix_by_section.get(
                str(assignment["section_id"]), "相关个体数据详见"
            ),
            "display": display,
            "bookmark": bookmark,
            "suffix": "。",
        },
        {
            "confirmed_target_present": True,
            target_check: True,
            "word_ref_not_literal_reference": True,
        },
    )


def _chapter14_sources(context: Mapping[str, Any]) -> tuple[dict[str, list[dict[str, str]]], int]:
    frozen = _require_mapping(context.get("frozen_tfl_map"), "frozen_tfl_map")
    frozen_ids = _frozen_entity_ids(frozen)
    frozen_entities = {
        str(item["entity_id"]): item
        for item in _require_list(frozen.get("entities"), "frozen_tfl_map.entities")
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }
    accepted_ids = _accepted_entity_ids(context.get("accepted_result_tfl"))
    publication = validate_publication_reference_map(
        _require_mapping(context.get("publication_reference_map"), "publication_reference_map")
    )
    grouped: dict[str, list[dict[str, str]]] = {key: [] for key in (
        "14.1.1", "14.1.2", "14.1.3", "14.2", "14.3.1", "14.3.2", "14.3.3", "14.3.4"
    )}
    for entry in publication["entries"]:
        source = entry["source_entity_id"]
        if source not in frozen_ids:
            raise DeterministicGenerationError(
                f"E-CHAPTER14-FROZEN-MISMATCH: {source} is absent from frozen tfl_map"
            )
        if source not in accepted_ids:
            raise DeterministicGenerationError(
                f"E-CHAPTER14-RESULT-MISSING: {source} is absent from accepted result TFL"
            )
        entity = frozen_entities[source]
        grouped[
            _chapter14_bucket(source, tuple(entity.get("ich_e3_sections", ())))
        ].append(dict(entry))
    missing_units = sorted(section_id for section_id, rows in grouped.items() if not rows)
    if missing_units:
        raise DeterministicGenerationError(
            f"E-CHAPTER14-EMPTY-UNIT: no confirmed entity for {missing_units}"
        )
    return grouped, len(publication["entries"])


def _chapter14(
    assignment: Mapping[str, Any],
    title: str,
    grouped: Mapping[str, list[dict[str, str]]],
) -> dict[str, Any]:
    rows = copy.deepcopy(grouped[str(assignment["section_id"])])
    return _unit(
        assignment,
        title,
        "chapter14_entities",
        {"entries": rows},
        {
            "entity_count_positive": bool(rows),
            "frozen_publication_result_three_way_match": True,
            "bookmark_per_entity": all(entry.get("publication_entity_id") for entry in rows),
        },
    )


def _references(
    assignment: Mapping[str, Any], title: str, context: Mapping[str, Any]
) -> dict[str, Any]:
    ledger = _require_mapping(context.get("confirmed_claim_ledger"), "confirmed_claim_ledger")
    bibliography = _require_mapping(context.get("bibliography_registry"), "bibliography_registry")
    claims = _require_list(ledger.get("claims"), "confirmed_claim_ledger.claims")
    sources = _require_mapping(bibliography.get("sources"), "bibliography_registry.sources")
    cited_ids: list[str] = []
    for index, claim in enumerate(claims):
        source_id = _require_text(
            _require_mapping(claim, f"claim {index}").get("source_id"), f"claim {index}.source_id"
        )
        if source_id not in sources:
            raise DeterministicGenerationError(
                f"E-REFERENCE-UNREGISTERED: claim source {source_id} is not in bibliography registry"
            )
        if source_id not in cited_ids:
            cited_ids.append(source_id)
    entries = [
        {"source_id": source_id, "citation": _require_text(sources[source_id], source_id)}
        for source_id in cited_ids
    ]
    return _unit(
        assignment,
        title,
        "reference_list",
        {"entries": entries},
        {"cited_sources_only": bool(entries), "all_sources_registered": True},
    )


def _appendix_unit(
    assignment: Mapping[str, Any],
    title: str,
    appendix_rows: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    section_id = str(assignment["section_id"])
    rows = [copy.deepcopy(dict(row)) for row in appendix_rows if row["appendix_id"] == section_id]
    if section_id == "16.0":
        rows = [copy.deepcopy(dict(row)) for row in appendix_rows]
    if not rows:
        raise DeterministicGenerationError(
            f"E-APPENDIX-ENTRY-MISSING: appendix status plan lacks {section_id}"
        )
    extra: dict[str, Any] = {}
    if section_id == "16.1.5":
        signatories = _require_list(
            context.get("approved_signatory_register"), "approved_signatory_register"
        )
        medical = [
            dict(item)
            for item in signatories
            if isinstance(item, Mapping) and item.get("control_id") == "CDE-7"
        ]
        if len(medical) != 1 or medical[0].get("location") != "16.1.5":
            raise DeterministicGenerationError(
                "E-ANNEX-II-SIGNATURE: sponsor medical officer signature is missing from 16.1.5"
            )
        extra["annex_ii_signature"] = medical[0]
    return _unit(
        assignment,
        title,
        "appendix_status_table" if section_id == "16.0" else "appendix_status_entry",
        {"rows": rows, **extra},
        {
            "status_entry_present": True,
            "five_columns_present": all(all(header_key in row for header_key in (
                "appendix_id", "file_name", "submitted", "submission_location", "reason_or_on_request"
            )) for row in rows),
            "all_24_rows_present" if section_id == "16.0" else "entry_preserved": (
                len(rows) == 24 if section_id == "16.0" else True
            ),
        },
    )


def build_deterministic_bundle(
    context: Mapping[str, Any],
    *,
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate every zero-LLM or template-slot authoring unit."""

    context = _require_mapping(context, "deterministic generation context")
    loaded = registry or load_production_method_registry()
    assignments = _require_mapping(loaded.get("assignments"), "production assignments")
    target_assignments = {
        section_id: assignment
        for section_id, assignment in assignments.items()
        if assignment.get("production_method") in {"deterministic_generation", "template_slot"}
    }
    counts = Counter(item["production_method"] for item in target_assignments.values())
    if counts != Counter({"deterministic_generation": 48, "template_slot": 4}):
        raise DeterministicGenerationError(
            f"E-PRODUCTION-COUNT-DRIFT: expected 48+4, observed {dict(counts)}"
        )
    titles = _require_mapping(context.get("unit_titles"), "unit_titles")
    appendix_rows = _appendix_rows(
        _require_mapping(context.get("appendix_status_plan"), "appendix_status_plan")
    )
    submission_registry = _require_mapping(
        context.get("confirmed_submission_registry"), "confirmed_submission_registry"
    )
    support_report_entries = _require_list(
        submission_registry.get("entries"), "confirmed_submission_registry.entries"
    )
    for index, raw in enumerate(support_report_entries):
        item = _require_mapping(raw, f"confirmed_submission_registry.entries[{index}]")
        for field in ("report_id", "title", "submission_location", "status"):
            _require_text(item.get(field), f"support report {index}.{field}")
    grouped14, publication_count = _chapter14_sources(context)
    artifacts: list[dict[str, Any]] = []
    for section_id, assignment in target_assignments.items():
        title = _require_text(titles.get(section_id), f"unit title {section_id}")
        profile = assignment["production_profile"]
        if profile == "title_page":
            artifact = _title_page(assignment, title, context)
        elif profile == "synopsis":
            artifact = _synopsis(assignment, title, context)
        elif section_id in DIRECTORY_FIELDS:
            artifact = _directory(assignment, title, context)
        elif profile in {"ethics_committee", "ethical_conduct", "informed_consent"}:
            artifact = _ethics_template(assignment, title, context)
        elif profile == "individual_listing_reference":
            artifact = _individual_reference(assignment, title, context)
        elif profile == "chapter14_material":
            artifact = _chapter14(assignment, title, grouped14)
        elif profile == "reference_list":
            artifact = _references(assignment, title, context)
        elif profile == "appendix_entry":
            artifact = _appendix_unit(assignment, title, appendix_rows, context)
        else:
            raise DeterministicGenerationError(
                f"E-DETERMINISTIC-PRODUCER-MISSING: no producer for {section_id}/{profile}"
            )
        artifacts.append(artifact)

    artifacts.sort(key=lambda item: _section_sort_key(item["section_id"]))
    validations = [item["validation"] for item in artifacts]
    if len(artifacts) != 52 or any(item["status"] != "pass" for item in validations):
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE: incomplete or failed bundle")
    return {
        "schema_version": DETERMINISTIC_BUNDLE_VERSION,
        "status": "pass",
        "model_called": False,
        "producer_counts": dict(sorted(counts.items())),
        "unit_count": len(artifacts),
        "publication_entity_count": publication_count,
        "appendix_status_entry_count": len(appendix_rows),
        "support_report_entries": copy.deepcopy(support_report_entries),
        "context_sha256": _stable_hash(context),
        "production_registry_sha256": loaded.get("config_sha256"),
        "artifacts": artifacts,
        "minimal_validations": validations,
    }


def validate_deterministic_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete zero-LLM bundle before unified composition.

    The unified assembler intentionally accepts the bundle as data rather than
    calling individual producers again.  This gate therefore rechecks the
    ownership boundary, every per-unit validation, unique section identities,
    and a stable content hash before any artifact can enter the report.
    """

    if not isinstance(bundle, Mapping):
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-CONTRACT")
    if bundle.get("status") != "pass" or bundle.get("model_called") is not False:
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-NOT-RELEASABLE")
    if bundle.get("unit_count") != 52:
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-COUNT")
    if bundle.get("producer_counts") != {
        "deterministic_generation": 48,
        "template_slot": 4,
    }:
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-OWNER-COUNT")

    artifacts = _require_list(bundle.get("artifacts"), "bundle.artifacts")
    validations = _require_list(
        bundle.get("minimal_validations"), "bundle.minimal_validations"
    )
    if len(artifacts) != 52 or len(validations) != 52:
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-COUNT")
    section_ids: list[str] = []
    validation_by_section: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(validations):
        validation = _require_mapping(raw, f"bundle.minimal_validations[{index}]")
        section_id = _require_text(
            validation.get("section_id"), f"bundle validation {index}.section_id"
        )
        if section_id in validation_by_section:
            raise DeterministicGenerationError(
                f"E-DETERMINISTIC-BUNDLE-DUPLICATE-VALIDATION: {section_id}"
            )
        if (
            validation.get("status") != "pass"
            or validation.get("model_called") is not False
            or validation.get("failed_checks") != []
        ):
            raise DeterministicGenerationError(
                f"E-DETERMINISTIC-VALIDATION: {section_id} is not releasable"
            )
        validation_by_section[section_id] = validation

    artifact_hashes: list[dict[str, str]] = []
    for index, raw in enumerate(artifacts):
        artifact = _require_mapping(raw, f"bundle.artifacts[{index}]")
        section_id = _require_text(
            artifact.get("section_id"), f"bundle artifact {index}.section_id"
        )
        if section_id in section_ids:
            raise DeterministicGenerationError(
                f"E-DETERMINISTIC-BUNDLE-DUPLICATE: {section_id}"
            )
        section_ids.append(section_id)
        if artifact.get("production_method") not in {
            "deterministic_generation",
            "template_slot",
        }:
            raise DeterministicGenerationError(
                f"E-DETERMINISTIC-BUNDLE-OWNER: {section_id}"
            )
        if artifact.get("validation") != validation_by_section.get(section_id):
            raise DeterministicGenerationError(
                f"E-DETERMINISTIC-BUNDLE-VALIDATION-BINDING: {section_id}"
            )
        _require_text(artifact.get("title"), f"bundle artifact {section_id}.title")
        _require_text(
            artifact.get("content_kind"), f"bundle artifact {section_id}.content_kind"
        )
        _require_mapping(
            artifact.get("content"), f"bundle artifact {section_id}.content"
        )
        artifact_hashes.append(
            {"section_id": section_id, "artifact_sha256": _stable_hash(artifact)}
        )

    return {
        "schema_version": "1.0",
        "status": "pass",
        "unit_count": len(artifacts),
        "model_called": False,
        "section_ids": section_ids,
        "artifact_hashes": artifact_hashes,
        "bundle_sha256": _stable_hash(
            {
                "schema_version": bundle.get("schema_version"),
                "status": bundle.get("status"),
                "model_called": bundle.get("model_called"),
                "producer_counts": bundle.get("producer_counts"),
                "unit_count": bundle.get("unit_count"),
                "context_sha256": bundle.get("context_sha256"),
                "production_registry_sha256": bundle.get(
                    "production_registry_sha256"
                ),
                "artifacts": artifacts,
                "minimal_validations": validations,
            }
        ),
    }


def _section_sort_key(section_id: str) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part for part in section_id.split("."))


def _clear_document_body(document: Document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _add_complex_field(paragraph, instruction: str) -> None:
    for kind, text in (("begin", None), (None, instruction), ("separate", None), ("end", None)):
        run = OxmlElement("w:r")
        if kind is None:
            node = OxmlElement("w:instrText")
            node.set(qn("xml:space"), "preserve")
            node.text = text
        else:
            node = OxmlElement("w:fldChar")
            node.set(qn("w:fldCharType"), kind)
        run.append(node)
        paragraph._p.append(run)


def _add_tc_field(paragraph, text: str, category: str) -> None:
    escaped = text.replace('"', "'")
    _add_complex_field(paragraph, f' TC "{escaped}" \\f "{category}" \\l "1" ')


def _add_ref_field(paragraph, bookmark: str, display: str) -> None:
    begin = OxmlElement("w:r")
    begin_node = OxmlElement("w:fldChar")
    begin_node.set(qn("w:fldCharType"), "begin")
    begin.append(begin_node)
    paragraph._p.append(begin)
    instruction = OxmlElement("w:r")
    instruction_node = OxmlElement("w:instrText")
    instruction_node.set(qn("xml:space"), "preserve")
    instruction_node.text = f" REF {bookmark} \\h "
    instruction.append(instruction_node)
    paragraph._p.append(instruction)
    separate = OxmlElement("w:r")
    separate_node = OxmlElement("w:fldChar")
    separate_node.set(qn("w:fldCharType"), "separate")
    separate.append(separate_node)
    paragraph._p.append(separate)
    paragraph.add_run(display)
    end = OxmlElement("w:r")
    end_node = OxmlElement("w:fldChar")
    end_node.set(qn("w:fldCharType"), "end")
    end.append(end_node)
    paragraph._p.append(end)


def _add_bookmark(paragraph, name: str, bookmark_id: int, text: str) -> None:
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    paragraph._p.append(start)
    paragraph.add_run(text)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.append(end)


def _set_cell(cell, text: Any, *, bold: bool = False) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    run = paragraph.add_run(str(text))
    run.bold = bold


def _add_appendix_table(document: Document, rows: Sequence[Mapping[str, Any]]):
    table = document.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, header in zip(table.rows[0].cells, APPENDIX_HEADERS, strict=True):
        _set_cell(cell, header, bold=True)
    last_category = None
    for row in rows:
        if row["category_title"] != last_category:
            cells = table.add_row().cells
            merged = cells[0]
            for cell in cells[1:]:
                merged = merged.merge(cell)
            _set_cell(merged, row["category_title"], bold=True)
            last_category = row["category_title"]
        cells = table.add_row().cells
        values = (
            row["appendix_id"], row["file_name"], row["submitted"],
            row["submission_location"], row["reason_or_on_request"],
        )
        for cell, value in zip(cells, values, strict=True):
            _set_cell(cell, value)
    return table


def _add_heading(document: Document, section_id: str, title: str, bookmark_id: int):
    level = min(len(section_id.split(".")), 5)
    paragraph = document.add_paragraph(style=f"Heading {level}")
    _add_bookmark(paragraph, f"_Ref_Sec_{section_id.replace('.', '_')}", bookmark_id, f"{section_id} {title}")
    return paragraph


def _enable_field_updates(document: Document) -> None:
    settings = document.settings.element
    existing = settings.find(qn("w:updateFields"))
    if existing is None:
        existing = OxmlElement("w:updateFields")
        settings.append(existing)
    existing.set(qn("w:val"), "true")


def _canonicalize_docx_package(path: Path) -> None:
    """Rewrite a DOCX with stable member order and ZIP metadata.

    ``python-docx`` delegates ZIP timestamps to the wall clock.  OOXML member
    bytes are stable, but the package hash is not.  Repacking every member with
    fixed metadata makes the file-level SHA-256 reproducible from identical
    inputs while leaving the XML payload unchanged.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.canonical.", suffix=".docx", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(path, "r") as source:
            members = {item.filename: source.read(item.filename) for item in source.infolist()}
            archive_comment = bytes(source.comment)
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as target:
            target.comment = archive_comment
            for name in sorted(members):
                info = zipfile.ZipInfo(name, date_time=_REPRODUCIBLE_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0o600 << 16
                target.writestr(info, members[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def render_deterministic_bundle_docx(
    bundle: Mapping[str, Any],
    output_path: str | Path,
    *,
    template_path: str | Path = DEFAULT_TEMPLATE,
) -> dict[str, Any]:
    """Render the validated bundle without invoking or simulating an LLM."""

    if bundle.get("status") != "pass" or bundle.get("model_called") is not False:
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-NOT-RELEASABLE")
    artifacts = _require_list(bundle.get("artifacts"), "bundle.artifacts")
    if len(artifacts) != 52:
        raise DeterministicGenerationError("E-DETERMINISTIC-BUNDLE-COUNT")
    document = Document(str(template_path))
    _clear_document_body(document)
    _enable_field_updates(document)
    bookmark_id = 1
    toc_fields = 0
    tfl_entries = 0
    appendix_rows = 0
    title_controls = 0
    for artifact in artifacts:
        section_id = artifact["section_id"]
        title = artifact["title"]
        heading = _add_heading(document, section_id, title, bookmark_id)
        bookmark_id += 1
        kind = artifact["content_kind"]
        content = artifact["content"]
        if kind == "title_page_template":
            fields = content["fields"]
            document.add_paragraph(fields["study_title"], style="Title")
            document.add_paragraph(f"方案编号：{fields['protocol_id']}")
            document.add_paragraph(f"报告版本/日期：{fields['report_version']} / {fields['report_date']}")
            document.add_paragraph(f"申办方：{fields['sponsor']}")
            document.add_paragraph(fields["confidentiality"])
            table = document.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            for cell, value in zip(table.rows[0].cells, ("形式审查项", "已确认内容", "证据状态"), strict=True):
                _set_cell(cell, value, bold=True)
            for control in content["title_page_controls"]:
                cells = table.add_row().cells
                for cell, value in zip(cells, (control["label"], control["value"], control["evidence_status"]), strict=True):
                    _set_cell(cell, value)
                title_controls += 1
        elif kind == "confirmed_text_copy":
            for item in content["paragraphs"]:
                document.add_paragraph(item["text"])
        elif kind == "word_field":
            paragraph = document.add_paragraph(style="CSR TOC")
            _add_complex_field(paragraph, content["instruction"])
            toc_fields += 1
        elif kind == "fixed_template_with_slots":
            document.add_paragraph(content["text"])
        elif kind == "word_ref":
            paragraph = document.add_paragraph(content["prefix"])
            _add_ref_field(paragraph, content["bookmark"], content["display"])
            paragraph.add_run(content["suffix"])
        elif kind == "chapter14_entities":
            table = document.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            for cell, value in zip(table.rows[0].cells, ("发表编号", "标题", "正文归属"), strict=True):
                _set_cell(cell, value, bold=True)
            for entry in content["entries"]:
                cells = table.add_row().cells
                publication = entry["publication_entity_id"]
                paragraph = cells[0].paragraphs[0]
                _add_bookmark(paragraph, bookmark_name(publication), bookmark_id, publication)
                bookmark_id += 1
                _add_tc_field(paragraph, f"{publication} {entry['title']}", "TFL")
                _set_cell(cells[1], entry["title"])
                _set_cell(cells[2], entry["expected_section"])
                tfl_entries += 1
        elif kind == "reference_list":
            for index, entry in enumerate(content["entries"], start=1):
                document.add_paragraph(f"[{index}] {entry['citation']}")
        elif kind in {"appendix_status_table", "appendix_status_entry"}:
            rows = content["rows"]
            if kind == "appendix_status_table":
                _add_appendix_table(document, rows)
                appendix_rows = len(rows)
            else:
                for row in rows:
                    paragraph = document.add_paragraph(
                        f"提交状态：{row['submitted']}；提交位置：{row['submission_location']}；"
                        f"说明：{row['reason_or_on_request']}"
                    )
                    _add_tc_field(
                        paragraph, f"{row['appendix_id']} {row['file_name']}", "APP"
                    )
                if "annex_ii_signature" in content:
                    signature = content["annex_ii_signature"]
                    document.add_paragraph(
                        f"{signature['label']}：{signature['value']}（{signature['evidence_status']}）"
                    )
        else:
            raise DeterministicGenerationError(f"E-DOCX-RENDERER-MISSING: {kind}")

        if section_id == "3.6":
            for report in _require_list(
                bundle.get("support_report_entries", []),
                "bundle.support_report_entries",
                allow_empty=True,
            ):
                _add_tc_field(heading, _require_text(report.get("title"), "support report title"), "SUP")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".docx", dir=destination.parent
    )
    os.close(descriptor)
    try:
        document.save(temporary)
        os.replace(temporary, destination)
        _canonicalize_docx_package(destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {
        "schema_version": "1.0",
        "status": "pass",
        "output_path": str(destination.resolve()),
        "output_sha256": sha256_file(destination),
        "unit_count": len(artifacts),
        "directory_word_field_count": toc_fields,
        "chapter14_entity_count": tfl_entries,
        "appendix_status_row_count": appendix_rows,
        "title_page_control_count": title_controls,
        "model_called": False,
    }
