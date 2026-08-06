#!/usr/bin/env python3
"""Run the privacy-safe M8 reviewer-ergonomics acceptance workflow."""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Mapping

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.io_utils import atomic_write_json, atomic_write_text, sha256_file
from csr_writer.review_decisions import (
    apply_decision_template,
    build_decision_ledger,
    build_review_decision_queue,
    compare_review_versions,
    create_decision_record,
    export_decision_template,
    render_deferred_row_expectations,
    render_review_decision_queue,
    render_version_comparison,
)
from csr_writer.shell_mapping import propose_docx_shell_map


APPROVED_HASHES = {
    "paired_protocol": "3c1cf7c01434405885f562d9e687691185d5250bf4a6aeead564fcdf97aeceae",
    "paired_tfl_shell": "75b6ce46bfda933d75611ce38e0ac564a17116d67faf04a767c1bd81d4bc27c5",
    "reference_protocol": "12ac850d70294e1a074ffa8102e437d98e67f27e81091097bc15fddd63d45ca1",
    "reference_numbering_pdf": "b074ce91810e48e0536cb4464053502e37c5bbe244b7954715ffd41a1936117a",
}
OUTPUT = ROOT / "examples" / "output" / "m8"


def _find_sources(real_dir: Path) -> dict[str, Path]:
    matches: dict[str, list[Path]] = {role: [] for role in APPROVED_HASHES}
    if not real_dir.is_dir():
        raise RuntimeError("External read-only source directory is unavailable")
    for path in real_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf"}:
            continue
        digest = sha256_file(path)
        for role, expected in APPROVED_HASHES.items():
            if digest == expected:
                matches[role].append(path)
    invalid = {role: len(paths) for role, paths in matches.items() if len(paths) != 1}
    if invalid:
        raise RuntimeError(f"Approved-source hash resolution failed: {invalid}")
    return {role: values[0] for role, values in matches.items()}


def _source_audit(sources: Mapping[str, Path]) -> dict[str, Any]:
    records = []
    for role, expected in APPROVED_HASHES.items():
        observed = sha256_file(sources[role])
        records.append(
            {
                "source_role": role,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "match": expected == observed,
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


def _grid(document: Document, columns: list[str], rows: list[str]) -> None:
    table = document.add_table(rows=len(rows) + 1, cols=len(columns) + 1)
    table.cell(0, 0).text = "ROW"
    for column_index, column in enumerate(columns, start=1):
        table.cell(0, column_index).text = column
    for row_index, row in enumerate(rows, start=1):
        table.cell(row_index, 0).text = row
        for column_index in range(1, len(columns) + 1):
            table.cell(row_index, column_index).text = "xx"


def _layout_placeholder(document: Document, target: str) -> None:
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = f"请根据表{target}的模板输出本表。"


def _build_version_shell(path: Path, *, changed_parent: bool) -> None:
    document = Document()
    document.add_paragraph("表14.1.1.3 Deidentified deviation categories (All Enrolled Set)")
    _grid(document, ["PBO", "1.0% QD"], ["偏离类别1", "偏离类别2"])
    document.add_paragraph("表14.1.4.1 Deidentified medication rows (FAS)")
    columns = ["PBO", "1.0% QD"]
    if changed_parent:
        columns.append("0.5% BID")
    _grid(document, columns, ["ATC分类1", "药物名1"])
    document.add_paragraph("表14.1.4.2 Deidentified medication child (FAS)")
    _layout_placeholder(document, "14.1.4.1")
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)


def _default_records(queue: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = []
    for index, group in enumerate(queue["decision_groups"]):
        resolved = {
            item["field"]: "DEIDENTIFIED-DEMO-V1"
            for item in group["required_reviewer_inputs"]
        }
        records.append(
            create_decision_record(
                queue,
                group_id=group["group_id"],
                reviewer_role="lead_statistician",
                reviewer_identifier="reviewer_demo_001",
                decided_at=f"2026-08-04T{index:02d}:00:00+08:00",
                outcome="confirm_default",
                resolved_inputs=resolved,
                decision_notes="脱敏演示；不代表真实项目批准",
            )
        )
    return records


def _version_reuse_demo(root: Path) -> dict[str, Any]:
    v10_path = root / "shell_v1.0.deidentified.docx"
    v11_path = root / "shell_v1.1.deidentified.docx"
    _build_version_shell(v10_path, changed_parent=False)
    _build_version_shell(v11_path, changed_parent=True)
    report10, _shell10, candidate10 = propose_docx_shell_map(
        v10_path, expected_sha256=sha256_file(v10_path), map_version="M8-DEID-V1.0"
    )
    report11, _shell11, candidate11 = propose_docx_shell_map(
        v11_path, expected_sha256=sha256_file(v11_path), map_version="M8-DEID-V1.1"
    )
    queue10 = build_review_decision_queue(report10, candidate10)
    queue11 = build_review_decision_queue(report11, candidate11)
    ledger10 = build_decision_ledger(queue10, _default_records(queue10))
    comparison = compare_review_versions(queue10, ledger10, queue11)
    statuses = {item["entity_id"]: item["status"] for item in comparison["entities"]}
    expected = {
        "T14.1.1.3": "unchanged_decision_reused",
        "T14.1.4.1": "changed_requeued",
        "T14.1.4.2": "upstream_change_invalidated_reconfirmation",
    }
    if any(statuses.get(entity_id) != status for entity_id, status in expected.items()):
        raise RuntimeError(f"M8 version-reuse states drifted: {statuses}")
    atomic_write_json(root / "version_v1.0_queue.json", queue10)
    atomic_write_json(root / "version_v1.0_ledger.demo.json", ledger10)
    atomic_write_json(root / "version_v1.1_queue.json", queue11)
    atomic_write_json(root / "version_reuse_proof.json", comparison)
    atomic_write_text(root / "version_reuse_proof.md", render_version_comparison(comparison))
    return comparison


def _privacy_scan(root: Path) -> dict[str, Any]:
    forbidden = (
        "ZYG",
        "BV25072501",
        "脂溢性皮炎",
        "/Users/",
        "研究者姓名",
        "机构名称",
    )
    findings: list[dict[str, str]] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        payloads: list[tuple[str, str]] = []
        if path.suffix.lower() in {".json", ".md", ".txt"}:
            payloads.append((path.name, path.read_text(encoding="utf-8")))
        elif path.suffix.lower() == ".docx":
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        payloads.append((f"{path.name}:{name}", archive.read(name).decode("utf-8", errors="replace")))
        for label, text in payloads:
            scanned += 1
            for token in forbidden:
                if token in text:
                    findings.append({"artifact": label, "token": token})
    return {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "payload_count_scanned": scanned,
        "findings": findings,
        "real_source_content_written": False,
    }


def run(real_dir: Path) -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sources = _find_sources(real_dir)
    before = _source_audit(sources)
    if before["status"] != "pass":
        raise RuntimeError("Approved real-source baseline failed before M8 run")

    started = time.perf_counter()
    proposal, shell, candidate = propose_docx_shell_map(
        sources["paired_tfl_shell"],
        expected_sha256=APPROVED_HASHES["paired_tfl_shell"],
        map_version="REAL-SHELL-M8-DEID-1",
    )
    queue = build_review_decision_queue(proposal, candidate)
    elapsed = time.perf_counter() - started
    records = _default_records(queue)
    ledger = build_decision_ledger(queue, records)
    template = export_decision_template(
        queue, ledger, template_id="DEIDENTIFIED-SPONSOR-VENDOR-DEMO"
    )
    template_application = apply_decision_template(queue, template)

    atomic_write_json(OUTPUT / "real_shell_proposal_report.json", proposal)
    atomic_write_json(OUTPUT / "real_shell_datapack.deidentified.json", shell)
    atomic_write_json(OUTPUT / "tfl_map_candidate.deidentified.json", candidate)
    atomic_write_json(OUTPUT / "review_decision_queue.json", queue)
    atomic_write_text(OUTPUT / "review_decision_queue.md", render_review_decision_queue(queue))
    atomic_write_text(
        OUTPUT / "deferred_row_expectations.md", render_deferred_row_expectations(queue)
    )
    representatives = ["T14.1.3.1", "T14.1.4.2", "T14.1.1.3"]
    atomic_write_text(
        OUTPUT / "deferred_row_expectations.representatives.md",
        render_deferred_row_expectations(queue, representatives),
    )
    atomic_write_json(
        OUTPUT / "deferred_row_expectations.representatives.json",
        {
            "schema_version": "1.0",
            "entity_ids": representatives,
            "items": [
                item
                for item in queue["deferred_row_expectations"]
                if item["entity_id"] in representatives
            ],
        },
    )
    atomic_write_json(OUTPUT / "review_ergonomics.json", queue["ergonomics"])
    atomic_write_json(OUTPUT / "decision_record.example.json", records[0])
    atomic_write_json(OUTPUT / "decision_ledger.demo.json", ledger)
    atomic_write_json(OUTPUT / "decision_template.demo.json", template)
    atomic_write_json(OUTPUT / "template_application_proof.json", template_application)

    comparison = _version_reuse_demo(OUTPUT / "version_reuse")
    after = _source_audit(sources)
    atomic_write_json(
        OUTPUT / "source_hash_audit.json",
        {
            "schema_version": "1.0",
            "status": "pass" if before == after else "blocked",
            "before": before,
            "after": after,
            "hashes_identical_before_after": before == after,
        },
    )
    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "real_shell": {
            "physical_word_table_objects": proposal["metrics"]["tfl_body_table_objects"],
            "unique_tfl_entities": proposal["metrics"]["unique_tfl_entities"],
            "legacy_technical_review_batches": proposal["metrics"]["human_review_batches"],
            "review_entities": proposal["metrics"]["batch_review_entities"],
            "reviewer_decision_groups": queue["ergonomics"]["decision_group_count"],
            "overlapping_entity_decision_memberships": queue["ergonomics"]["entity_decision_memberships"],
            "generation_elapsed_seconds": round(elapsed, 3),
        },
        "ergonomics": queue["ergonomics"],
        "audit_demo": {
            "record_count": len(records),
            "ledger_status": ledger["status"],
            "real_human_approval_claimed": False,
        },
        "template_demo": {
            "matched_groups": template_application["matched_group_count"],
            "automatic_decisions": template_application["automatic_decision_count"],
            "status": template_application["status"],
        },
        "version_reuse_demo": comparison["entity_status_counts"],
        "real_source_hashes_unchanged": before == after,
    }
    atomic_write_json(OUTPUT / "m8_e2e_summary.json", summary)
    privacy = _privacy_scan(OUTPUT)
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    if privacy["status"] != "pass" or before != after:
        raise RuntimeError("M8 privacy or real-source integrity gate failed")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--real-dir", type=Path, default=Path("/Users/huanglu/Desktop/未命名文件夹")
    )
    args = parser.parse_args()
    summary = run(args.real_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
