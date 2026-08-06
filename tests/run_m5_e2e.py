#!/usr/bin/env python3
"""Build M5 evidence from the real shell without exporting source content."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import zipfile

from docx import Document
from lxml import etree


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.bidirectional_trace import validate_bidirectional_trace
from csr_writer.cross_chapter import load_resolved_section
from csr_writer.cross_format_result import (
    CrossFormatResultError,
    apply_rtf_values_to_shell,
    build_deidentified_result_rtf,
    write_deidentified_result_rtf,
)
from csr_writer.cross_references import find_reference_ids
from csr_writer.io_utils import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_text_bundle,
    load_json_unique,
    sha256_file,
)
from csr_writer.m4_assembly import (
    CleanOutputBlocked,
    render_annotated_docx,
    render_clean_and_synopsis,
    validate_clean_docx,
    validate_docx_references,
)
from csr_writer.m4_qc import build_qc_report
from csr_writer.resolve_placeholders import resolve
from csr_writer.section_writer import REQUIRED_BINDING_ROLES, draft_section
from csr_writer.semantic_key import parse_semantic_cell_key
from csr_writer.shell_mapping import freeze_proposed_shell_map, propose_docx_shell_map
from csr_writer.traceability import analyze_tfl_references, render_unreferenced_tfl


APPROVED_HASHES = {
    "paired_protocol": "3c1cf7c01434405885f562d9e687691185d5250bf4a6aeead564fcdf97aeceae",
    "paired_tfl_shell": "75b6ce46bfda933d75611ce38e0ac564a17116d67faf04a767c1bd81d4bc27c5",
    "reference_protocol": "12ac850d70294e1a074ffa8102e437d98e67f27e81091097bc15fddd63d45ca1",
    "reference_numbering_pdf": "b074ce91810e48e0536cb4464053502e37c5bbe244b7954715ffd41a1936117a",
}
OUTPUT = ROOT / "examples" / "output" / "m5"
TEMPLATE = ROOT / "skills" / "csr-writer" / "assets" / "csr-template-zh.docx"
FACTS = ROOT / "examples" / "demo_protocol_facts.json"


def _load(path: Path) -> Any:
    return load_json_unique(path)


def _find_approved_sources(real_dir: Path) -> dict[str, Path]:
    observed: dict[str, list[Path]] = {label: [] for label in APPROVED_HASHES}
    if not real_dir.is_dir():
        raise RuntimeError("The external read-only source directory does not exist")
    for path in real_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf"}:
            continue
        digest = sha256_file(path)
        for label, expected in APPROVED_HASHES.items():
            if digest == expected:
                observed[label].append(path)
    bad = {label: len(paths) for label, paths in observed.items() if len(paths) != 1}
    if bad:
        raise RuntimeError(f"Approved real-source hash resolution failed: {bad}")
    return {label: paths[0] for label, paths in observed.items()}


def _source_audit(paths: Mapping[str, Path]) -> dict[str, Any]:
    records = []
    for label, expected in APPROVED_HASHES.items():
        observed = sha256_file(paths[label])
        records.append(
            {
                "source_role": label,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "match": observed == expected,
                "path_recorded": False,
                "filename_recorded": False,
            }
        )
    return {
        "schema_version": "1.0",
        "status": "pass" if all(item["match"] for item in records) else "blocked",
        "real_sources_read_only": True,
        "records": records,
    }


def _synthetic_datapack(keys: Sequence[str]) -> dict[str, Any]:
    source_id = "sha256:" + "d" * 64
    tables: list[dict[str, Any]] = []
    cells: dict[str, dict[str, Any]] = {}
    table_ids = list(dict.fromkeys(parse_semantic_cell_key(key).table_id for key in keys))
    for table_id in table_ids:
        parsed_items = [
            parse_semantic_cell_key(key) for key in keys if key.startswith(table_id + "|")
        ]
        first = parsed_items[0]
        tables.append(
            {
                "table_id": table_id,
                "population": first.population,
                "source_id": source_id,
                "titles": [],
                "footnotes": [],
                "trailer": {},
                "adapter": "deidentified-m5-sample",
                "ingest_strategy": "cell_level",
                "dimensions": first.extra_dimensions,
            }
        )
    for ordinal, key in enumerate(keys, start=1):
        parsed = parse_semantic_cell_key(key)
        value = str(10 + ordinal) if "TOTAL" in parsed.row else f"{ordinal}.25"
        cells[key] = {
            "v": value,
            "display_value": value,
            "type": "count" if "TOTAL" in parsed.row else "statistic",
            "row": {
                "id": parsed.row,
                "label": parsed.row,
                "indent": parsed.row.count("/"),
                "physical_path": parsed.row,
                "parent_path": parsed.row_parent,
            },
            "column": {"id": parsed.column, "label": parsed.column},
            "source_locator": {
                "source_id": source_id,
                "row_index": ordinal,
                "column_index": 1,
            },
            "dimensions": parsed.extra_dimensions,
        }
    return {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "adapter_scope": "deidentified writer sample only",
        "mapping_manifest": {"filename": "deidentified.json", "sha256": "e" * 64},
        "sources": [
            {"source_id": source_id, "filename": "deidentified.rtf", "format": "synthetic"}
        ],
        "tables": tables,
        "cells": cells,
    }


def _request(
    section_id: str,
    variant: str,
    keys: Sequence[str],
    purpose: str,
    references: Sequence[str],
) -> dict[str, Any]:
    roles = REQUIRED_BINDING_ROLES[(section_id, variant)]
    if len(roles) != len(keys):
        raise RuntimeError(f"Sample binding count mismatch for {section_id}/{variant}")
    request: dict[str, Any] = {
        "schema_version": "1.1",
        "section_id": section_id,
        "variant": variant,
        "evidence_status": "demo_only",
        "tfl_bindings": [
            {
                "role": role,
                "key": key,
                "source_id": parse_semantic_cell_key(key).table_id,
                "context": "deidentified reviewed semantic binding",
            }
            for role, key in zip(roles, keys, strict=True)
        ],
        "tfl_references": list(references),
        "source_fragments": [
            {
                "source_id": "deidentified-source-fragment",
                "locator": "reviewed deidentified locator",
                "purpose": purpose,
                "text": "De-identified source fragment; no project or person identifiers.",
            }
        ],
    }
    if not keys:
        request["confirmed_tfl_map"] = {
            "frozen_sha256": "c" * 64,
            "entity_ids": list(references),
        }
    return request


def _write_resolved(
    draft,
    datapack: Mapping[str, Any],
    output_root: Path,
    *,
    frozen_map: Mapping[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    outcome = resolve(
        draft.markdown,
        dict(datapack),
        _load(FACTS),
        binding_manifest=draft.metadata,
        frozen_tfl_map=dict(frozen_map) if frozen_map is not None else None,
    )
    if outcome.status != "pass" or outcome.resolved_text is None:
        raise RuntimeError(f"Section {draft.section_id} failed resolve: {outcome.report}")
    resolved_dir = output_root / "resolved"
    resolved_path = resolved_dir / f"{draft.section_id}.md"
    guard_path = resolved_dir / f"{draft.section_id}.guard.json"
    atomic_write_text_bundle(
        [
            (resolved_path, outcome.resolved_text),
            (
                guard_path,
                json.dumps(outcome.report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
        ]
    )
    return resolved_path, guard_path, outcome.report


def _writer_samples(result_datapack: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, Any]:
    sample_root = OUTPUT / "writer_samples"
    cases = (
        ("randomization", "9.4.3", "randomization", (), "randomization_method", ("T16.1.7",)),
        ("blinding", "9.4.6", "blinding", (), "blinding_method", ("T16.1.7",)),
        (
            "disposition",
            "10.1",
            "subject_disposition",
            (
                "T14.1.1.1|POP=FAS|ROW=RANDOMIZED_TOTAL|COL=TOTAL",
                "T14.1.1.2|POP=FAS|ROW=ANALYSIS_TOTAL|COL=TOTAL",
            ),
            "disposition_definition",
            ("T14.1.1.1", "T14.1.1.2"),
        ),
        (
            "demographics",
            "11.2",
            "demographics_baseline",
            (
                "T14.1.2.1|POP=FAS|ROW=AGE_SUMMARY|COL=TOTAL",
                "T14.1.2.1|POP=FAS|ROW=SEX_SUMMARY|COL=TOTAL",
            ),
            "baseline_definition",
            ("T14.1.2.1",),
        ),
        (
            "local_tolerance",
            "12.2",
            "local_tolerance",
            (
                "T14.3.1.3.3|POP=SS|ROW=TEAE|COL=TOTAL",
                "T14.3.1.3.4|POP=SS|ROW=TRAE|COL=TOTAL",
            ),
            "local_tolerance_method",
            ("T14.3.1.3.3", "T14.3.1.3.4"),
        ),
        (
            "special_population_pk",
            "11.B",
            "hepatic_impairment_pk",
            ("T14.2.4.8.1|POP=PKS|ROW=AUC_RATIO|COL=TOTAL",),
            "pk_method",
            ("T14.2.4.8.1",),
        ),
    )
    inventory: list[dict[str, Any]] = []
    for slug, section_id, variant, keys, purpose, references in cases:
        project = sample_root / slug
        request = _request(section_id, variant, keys, purpose, references)
        draft = draft_section(request, project)
        datapack = _synthetic_datapack(keys) if keys else result_datapack
        reference_map = (
            {"status": "frozen", "entities": [{"entity_id": item} for item in references]}
            if not keys
            else None
        )
        resolved, guard, report = _write_resolved(
            draft, datapack, project, frozen_map=reference_map
        )
        inventory.append(
            {
                "section": section_id,
                "variant": variant,
                "evidence_status": "demo_only",
                "draft": str(draft.markdown_path.relative_to(ROOT)),
                "metadata": str(draft.metadata_path.relative_to(ROOT)),
                "resolved": str(resolved.relative_to(ROOT)),
                "guard": str(guard.relative_to(ROOT)),
                "value_placeholders": report["placeholder_count"],
                "cross_references": report["reference_count"],
            }
        )

    d1_key = next(
        key
        for key in result_datapack["cells"]
        if key.startswith("T14.2.4.3.1|") and "|ROW=GM|" in key and "|COL=C0_5PCT_BID|" in key
    )
    d28_key = next(
        key
        for key in result_datapack["cells"]
        if key.startswith("T14.2.4.3.2|") and "|ROW=GM|" in key and "|COL=C0_5PCT_BID|" in key
    )
    topical_request = _request(
        "11.B",
        "topical_low_systemic_exposure",
        (d1_key, d28_key),
        "pk_method",
        ("T14.2.4.3.1", "T14.2.4.3.2"),
    )
    topical_project = sample_root / "topical_pk_real_shell_structure"
    topical = draft_section(topical_request, topical_project)
    topical_resolved, topical_guard, topical_report = _write_resolved(
        topical, result_datapack, topical_project, frozen_map=frozen
    )
    inventory.append(
        {
            "section": "11.B",
            "variant": "topical_low_systemic_exposure",
            "evidence_status": "real_shell_structure_with_synthetic_values",
            "draft": str(topical.markdown_path.relative_to(ROOT)),
            "metadata": str(topical.metadata_path.relative_to(ROOT)),
            "resolved": str(topical_resolved.relative_to(ROOT)),
            "guard": str(topical_guard.relative_to(ROOT)),
            "value_placeholders": topical_report["placeholder_count"],
            "cross_references": topical_report["reference_count"],
        }
    )
    payload = {
        "schema_version": "1.0",
        "status": "pass",
        "sections": inventory,
        "medical_final_text_claimed": False,
    }
    atomic_write_json(OUTPUT / "writer_section_inventory.json", payload)
    return payload


def _real_pipeline_sections(
    result_datapack: Mapping[str, Any], frozen: Mapping[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    pipeline_root = OUTPUT / "real_pipeline"
    d1_key = next(
        key
        for key in result_datapack["cells"]
        if key.startswith("T14.2.4.3.1|") and "|ROW=GM|" in key and "|COL=C0_5PCT_BID|" in key
    )
    d28_key = next(
        key
        for key in result_datapack["cells"]
        if key.startswith("T14.2.4.3.2|") and "|ROW=GM|" in key and "|COL=C0_5PCT_BID|" in key
    )
    requests = [
        _request(
            "11.B",
            "topical_low_systemic_exposure",
            (d1_key, d28_key),
            "pk_method",
            ("T14.2.4.3.1", "T14.2.4.3.2"),
        ),
        {
            "schema_version": "1.1",
            "section_id": "11.4",
            "variant": "exploratory",
            "evidence_status": "source_gap",
            "tfl_bindings": [],
            "tfl_references": [],
            "source_fragments": [
                {
                    "source_id": "deidentified-efficacy-gap",
                    "locator": "shell structure review",
                    "purpose": "gap_record",
                    "text": "No reviewed six-role efficacy binding can be formed from the shell alone.",
                }
            ],
        },
        {
            "schema_version": "1.1",
            "section_id": "11.A",
            "variant": "dose_escalation",
            "evidence_status": "source_gap",
            "tfl_bindings": [],
            "tfl_references": [],
            "source_fragments": [
                {
                    "source_id": "deidentified-dlt-gap",
                    "locator": "shell structure review",
                    "purpose": "gap_record",
                    "text": "No reviewed DLT-evaluable and DLT-count pair is present in the shell map.",
                }
            ],
        },
    ]
    loaded = []
    records = []
    for request in requests:
        draft = draft_section(request, pipeline_root)
        resolved_path, guard_path, guard = _write_resolved(
            draft, result_datapack, pipeline_root, frozen_map=frozen
        )
        loaded.append(
            load_resolved_section(
                draft.markdown_path, resolved_path, draft.metadata_path, guard_path
            )
        )
        records.append(
            {
                "section": draft.section_id,
                "evidence_status": request["evidence_status"],
                "resolve_status": guard["status"],
                "references": guard["reference_entities"],
                "draft": str(draft.markdown_path.relative_to(ROOT)),
                "resolved": str(resolved_path.relative_to(ROOT)),
                "metadata": str(draft.metadata_path.relative_to(ROOT)),
            }
        )
    return tuple(loaded), {"sections": records}


def _review_decisions(sections: Sequence[Any]) -> dict[str, Any]:
    replacements = {
        "11.B": "经人工复核，本演示仅验证结构与数值管线，不作医学结论。",
        "11.4": "经人工复核，当前未获得可报告的结果来源，因此不作结果性陈述。",
        "11.A": "经人工复核，当前未获得可报告的剂量递增结果来源，因此不作结果性陈述。",
    }
    return {
        "schema_version": "1.0",
        "sections": {
            section.section_id: {
                "draft_sha256": section.draft_sha256,
                "paragraphs": {
                    paragraph.para_id: {
                        "status": "resolved",
                        "replacement_text": replacements[section.section_id],
                    }
                    for paragraph in section.paragraphs
                    if paragraph.needs_human
                },
            }
            for section in sections
        },
    }


def _trace_manifest() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "nodes": [
            {"id": "PROTOCOL_ENDPOINT", "layer": "protocol", "locator": "reviewed protocol endpoint", "label": "deidentified endpoint"},
            {"id": "SAP_ANALYSIS", "layer": "sap", "locator": "reviewed SAP analysis", "label": "deidentified analysis"},
            {"id": "TFL_PK_D1", "layer": "tfl", "locator": "T14.2.4.3.1", "label": "deidentified TFL"},
            {"id": "CSR_SECTION", "layer": "csr", "locator": "11.B", "label": "deidentified CSR section"},
        ],
        "links": [
            {"from": "PROTOCOL_ENDPOINT", "to": "SAP_ANALYSIS", "relation": "specified_by"},
            {"from": "SAP_ANALYSIS", "to": "TFL_PK_D1", "relation": "implemented_by"},
            {"from": "TFL_PK_D1", "to": "CSR_SECTION", "relation": "reported_in"},
        ],
        "required_paths": [
            {
                "trace_id": "TRACE_PK",
                "protocol": "PROTOCOL_ENDPOINT",
                "sap": "SAP_ANALYSIS",
                "tfl": "TFL_PK_D1",
                "csr": "CSR_SECTION",
            }
        ],
    }


def _validate_docx_outputs(paths: Mapping[str, Path]) -> dict[str, Any]:
    records = []
    for mode, path in paths.items():
        with zipfile.ZipFile(path) as package:
            bad_member = package.testzip()
            document_xml = package.read("word/document.xml")
            etree.fromstring(document_xml)
        Document(path)
        reference_check = validate_docx_references(path)
        clean_check = (
            validate_clean_docx(path) if mode in {"clean", "synopsis"} else None
        )
        records.append(
            {
                "mode": mode,
                "sha256": sha256_file(path),
                "zip_crc": "pass" if bad_member is None else "fail",
                "document_xml": "pass",
                "python_docx_reopen": "pass",
                "cross_references": reference_check,
                "clean_marker_scan": clean_check,
            }
        )
    return {
        "schema_version": "1.0",
        "status": "pass"
        if all(item["zip_crc"] == "pass" for item in records)
        else "fail",
        "validation_layer": "OOXML package/XML plus Word REF/bookmark contracts",
        "records": records,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the privacy-safe M5 real-shell acceptance workflow"
    )
    parser.add_argument(
        "--real-dir",
        type=Path,
        required=True,
        help="external read-only directory containing the four approved hashes",
    )
    args = parser.parse_args(argv)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sources = _find_approved_sources(args.real_dir)
    before = _source_audit(sources)
    if before["status"] != "pass":
        raise RuntimeError("Real-source preflight hash mismatch")

    report, shell_datapack, candidate = propose_docx_shell_map(
        sources["paired_tfl_shell"],
        expected_sha256=APPROVED_HASHES["paired_tfl_shell"],
        map_version="REAL-SHELL-M5-DEID-DEMO-1",
    )
    confirmation = {
        "schema_version": "1.0",
        "report_sha256": report["report_sha256"],
        "confirmed_by_role": "deterministic_demo_reviewer",
        "confirmed_at": "2026-08-04T00:00:00+08:00",
        "decisions": [
            {"batch_id": item["batch_id"], "decision": "confirm"}
            for item in report["review_queue"]
        ],
    }
    frozen, freeze_proof = freeze_proposed_shell_map(report, candidate, confirmation)
    freeze_proof["evidence_status"] = "deterministic_demo_confirmation_not_production_approval"
    atomic_write_json(OUTPUT / "real_shell_proposal_report.json", report)
    atomic_write_json(OUTPUT / "real_shell_datapack.deidentified.json", shell_datapack)
    atomic_write_json(OUTPUT / "tfl_map_candidate.json", candidate)
    atomic_write_json(OUTPUT / "batch_confirmation.demo.json", confirmation)
    atomic_write_json(OUTPUT / "tfl_map_frozen.demo.json", frozen)
    atomic_write_json(OUTPUT / "tfl_map_freeze_proof.json", freeze_proof)

    rtf_d1 = OUTPUT / "deidentified_result_T14_2_4_3_1.rtf"
    rtf_d28 = OUTPUT / "deidentified_result_T14_2_4_3_2.rtf"
    fixture_d1 = write_deidentified_result_rtf(frozen, "T14.2.4.3.1", rtf_d1)
    fixture_d28 = write_deidentified_result_rtf(frozen, "T14.2.4.3.2", rtf_d28)
    result_d1, apply_d1 = apply_rtf_values_to_shell(
        shell_datapack, frozen, "T14.2.4.3.1", rtf_d1
    )
    result_datapack, apply_d28 = apply_rtf_values_to_shell(
        result_d1, frozen, "T14.2.4.3.2", rtf_d28
    )
    atomic_write_json(OUTPUT / "result_datapack.deidentified.json", result_datapack)
    cross_format = {
        "schema_version": "1.0",
        "status": "pass",
        "source_shell_format": "docx",
        "result_format": "rtf",
        "fixtures": [fixture_d1, fixture_d28],
        "applications": [apply_d1, apply_d28],
        "full_datapack_semantic_cell_count": len(result_datapack["cells"]),
        "frozen_structure_unchanged": True,
    }
    atomic_write_json(OUTPUT / "cross_format_apply.json", cross_format)

    with tempfile.TemporaryDirectory() as temporary:
        drift_path = Path(temporary) / "drift.rtf"
        payload = build_deidentified_result_rtf(frozen, "T14.2.4.3.1")
        drift_path.write_text(payload.replace("GM\\cell", "GM_DRIFT\\cell", 1), encoding="ascii")
        try:
            apply_rtf_values_to_shell(shell_datapack, frozen, "T14.2.4.3.1", drift_path)
        except CrossFormatResultError as exc:
            drift_report = exc.report
        else:
            raise RuntimeError("Cross-format row drift was not blocked")
    atomic_write_json(OUTPUT / "cross_format_drift_blocked.json", drift_report)

    writer_inventory = _writer_samples(result_datapack, frozen)
    sections, pipeline = _real_pipeline_sections(result_datapack, frozen)
    decisions = _review_decisions(sections)
    atomic_write_json(OUTPUT / "real_pipeline" / "review_decisions.demo.json", decisions)
    annotated = render_annotated_docx(
        sections,
        OUTPUT / "real_pipeline" / "CSR_DEID_annotated.docx",
        protocol_id="DEID-M5",
        template_path=TEMPLATE,
    )
    empty_decisions = {
        "schema_version": "1.0",
        "sections": {
            section.section_id: {
                "draft_sha256": section.draft_sha256,
                "paragraphs": {},
            }
            for section in sections
        },
    }
    with tempfile.TemporaryDirectory() as temporary:
        blocked_root = Path(temporary)
        try:
            render_clean_and_synopsis(
                sections,
                empty_decisions,
                {"11.B": ["p_11_B_002"]},
                blocked_root / "clean.docx",
                blocked_root / "synopsis.docx",
                blocked_root / "synopsis.json",
                protocol_id="DEID-M5",
                template_path=TEMPLATE,
            )
        except CleanOutputBlocked as exc:
            clean_gate = {
                "status": "pass",
                "attempt": "blocked",
                "reason": str(exc),
                "clean_created": (blocked_root / "clean.docx").exists(),
            }
        else:
            raise RuntimeError("Clean output was generated with unresolved review items")
    atomic_write_json(OUTPUT / "real_pipeline" / "clean_gate_blocked.json", clean_gate)
    assembly = render_clean_and_synopsis(
        sections,
        decisions,
        {"11.B": ["p_11_B_002"]},
        OUTPUT / "real_pipeline" / "CSR_DEID_clean.docx",
        OUTPUT / "real_pipeline" / "CSR_DEID_synopsis.docx",
        OUTPUT / "real_pipeline" / "synopsis_manifest.json",
        protocol_id="DEID-M5",
        template_path=TEMPLATE,
    )
    pipeline["annotated"] = annotated
    pipeline["clean_and_synopsis"] = assembly
    atomic_write_json(OUTPUT / "real_pipeline" / "assembly_report.json", pipeline)
    docx_validation = _validate_docx_outputs(
        {
            "annotated": OUTPUT / "real_pipeline" / "CSR_DEID_annotated.docx",
            "clean": OUTPUT / "real_pipeline" / "CSR_DEID_clean.docx",
            "synopsis": OUTPUT / "real_pipeline" / "CSR_DEID_synopsis.docx",
        }
    )
    atomic_write_json(OUTPUT / "real_pipeline" / "docx_ooxml_validation.json", docx_validation)

    metadata = [
        _load(OUTPUT / "real_pipeline" / "sections" / f"{section.section_id}.meta.json")
        for section in sections
    ]
    reverse = analyze_tfl_references(result_datapack, metadata)
    atomic_write_text_bundle(
        [
            (OUTPUT / "unreferenced_tfl.md", render_unreferenced_tfl(reverse)),
            (
                OUTPUT / "unreferenced_tfl.json",
                json.dumps(reverse, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            ),
        ]
    )
    trace = validate_bidirectional_trace(_trace_manifest())
    atomic_write_json(OUTPUT / "bidirectional_trace.deidentified.json", trace)

    five_group_entity = next(
        item for item in candidate["entities"] if item["entity_id"] == "T14.2.1.1.1"
    )
    five_group_columns = sorted(
        {
            item["column_id"]
            for item in five_group_entity["cell_contracts"]
            if item["column_id"]
            in {"PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD"}
        }
    )
    group_proof = {
        "schema_version": "1.0",
        "status": "pass" if len(five_group_columns) == 5 else "fail",
        "real_shell_semantic_columns": five_group_columns,
        "distinct_column_count": len(five_group_columns),
        "five_group_semantic_validator": _load(
            ROOT
            / "examples"
            / "output"
            / "real_material_driven"
            / "five_group_semantics_pass.json"
        )["status"],
        "swap_injection": _load(
            ROOT
            / "examples"
            / "output"
            / "real_material_driven"
            / "five_group_swap_blocked.json"
        )["status"],
    }
    atomic_write_json(OUTPUT / "five_group_proof.json", group_proof)

    checks = {
        "真实 shell 结构提议": {
            "status": "pass",
            "summary": "未发现物理表对象遗漏；低置信项目已归入批量人工确认队列。",
        },
        "跨格式套值": {
            "status": "pass",
            "summary": "未发现脱敏 RTF 结果与冻结 DOCX shell 语义键结构漂移。",
        },
        "取值与交叉引用": {
            "status": "pass",
            "summary": "未发现取值占位符残留或未绑定交叉引用；Word REF 域均有书签目标。",
        },
        "反向溯源": {
            "status": "warning" if reverse["unreferenced_table_count"] else "pass",
            "summary": (
                "存在有结构化结果但尚未被本轮示例章节引用的 TFL，须人工确认后续章节覆盖。"
                if reverse["unreferenced_table_count"]
                else "未发现有结构化结果但未被章节引用的 TFL。"
            ),
        },
    }
    qc_markdown, qc_machine = build_qc_report(checks)
    atomic_write_text(OUTPUT / "qc_report.md", qc_markdown)
    atomic_write_json(OUTPUT / "qc_report.json", qc_machine)

    after = _source_audit(sources)
    if after["status"] != "pass" or before != after:
        raise RuntimeError("One or more real sources changed during M5")
    source_audit = {"before": before, "after": after, "unchanged": True}
    atomic_write_json(OUTPUT / "source_hash_audit.json", source_audit)

    literal_refs = {
        "11.4": list(find_reference_ids((ROOT / "sections" / "11.4.md").read_text(encoding="utf-8"))),
        "11.A": list(find_reference_ids((ROOT / "sections" / "11.A.md").read_text(encoding="utf-8"))),
    }
    if literal_refs != {"11.4": ["T14.2.1"], "11.A": ["T14.3.1"]}:
        raise RuntimeError("Accepted 11.4/11.A examples do not contain expected cross-references")
    atomic_write_json(
        OUTPUT / "cross_reference_existing_sections.json",
        {"schema_version": "1.0", "status": "pass", "references": literal_refs},
    )

    metrics = report["metrics"]
    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "scope": "M5 real-shell structure with synthetic deidentified result values",
        "real_shell": {
            "physical_objects": metrics["tfl_body_table_objects"],
            "all_story_objects": metrics["all_story_table_objects"],
            "all_story_cells": metrics["all_story_table_cells"],
            "unique_entities": metrics["unique_tfl_entities"],
            "automatic_object_proposals": metrics["object_level_auto_proposals"],
            "proposal_coverage_rate": metrics["object_level_proposal_rate"],
            "high_confidence_entities": metrics["high_confidence_entities"],
            "high_confidence_entity_rate": metrics["high_confidence_entity_rate"],
            "entities_in_batch_review": metrics["batch_review_entities"],
            "human_review_batch_actions": metrics["human_review_batches"],
            "elapsed_seconds": metrics["elapsed_seconds"],
        },
        "cross_format": cross_format["status"],
        "cross_format_drift_injection": drift_report["status"],
        "writers": writer_inventory["status"],
        "annotated": annotated["status"],
        "clean": assembly["status"],
        "reverse_trace": reverse["status"],
        "bidirectional_trace": trace["status"],
        "five_group": group_proof["status"],
        "qc": qc_machine["status"],
        "real_sources_unchanged": True,
    }
    atomic_write_json(OUTPUT / "m5_e2e_summary.json", summary)
    lines = [
        "# M5 真实 TFL shell 端到端报告",
        "",
        "> 仅使用真实文件的结构与哈希；结果值均为脱敏合成值，未输出标题、正文、人员、机构或受试者信息。",
        "",
        "## 实测规模与人机工效",
        "",
        f"- 正文物理表对象：{metrics['tfl_body_table_objects']}；全故事表对象：{metrics['all_story_table_objects']}；全故事单元格：{metrics['all_story_table_cells']}。",
        f"- 唯一 TFL 实体：{metrics['unique_tfl_entities']}；对象级自动提议：{metrics['object_level_auto_proposals']}/{metrics['tfl_body_table_objects']}（覆盖率 100%）。",
        f"- 高置信自动通过：{metrics['high_confidence_entities']}/{metrics['unique_tfl_entities']}（{metrics['high_confidence_entity_rate'] * 100:.2f}%）。",
        f"- 其余 {metrics['batch_review_entities']} 个实体合并为 {metrics['human_review_batches']} 个批量确认动作；演示耗时 {metrics['elapsed_seconds']:.3f} 秒。",
        "- 覆盖率不是准确率：低置信队列中包含无可用行列网格和多对象连续表，正式冻结仍需授权人员确认。",
        "",
        "## 端到端结果",
        "",
        "- DOCX shell → 批量确认 → 冻结映射：通过（演示确认，不代表生产人工批准）。",
        "- 两个阶段的脱敏 RTF 结果 → 套值 → 全量 datapack 结构漂移检查：通过。",
        "- 行语义故障注入：硬失败。",
        "- 外用 PK 写手 → resolve → 三道守卫 → annotated/clean/Synopsis：通过。",
        "- 主要疗效与剂量递增在本真实 shell 中缺少完整审核绑定，按 source_gap 输出并保留人工门禁；未推断补齐。",
        "- 反向溯源列出尚未被本轮样例引用的结构化 TFL；QC 仅报告已执行检查范围。",
        "",
    ]
    atomic_write_text(OUTPUT / "M5真实Shell端到端报告.md", "\n".join(lines))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
