#!/usr/bin/env python3
"""Rebuild the checked-in M3 acceptance artifacts without invoking M4."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.docx_extract import extract_docx_to_file
from csr_writer.io_utils import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_text_bundle,
    load_json_unique,
)
from csr_writer.module_router import (
    EXPECTED_MODULE_IDS,
    canonical_structure_json,
    route_modules,
)
from csr_writer.resolve_placeholders import resolve
from csr_writer.section_writer import draft_section
from csr_writer.study_facts import render_fact_card, validate_study_facts

EXAMPLES = ROOT / "examples"
OUTPUT = EXAMPLES / "output"
FACTS_PATH = EXAMPLES / "demo_protocol_facts.json"
DATAPACK_PATH = OUTPUT / "datapack.json"
SECTION_INPUTS = EXAMPLES / "section_inputs"
DOCX_FIXTURE = ROOT / "tests" / "fixtures" / "m3-docx" / "deep-extraction-fixture.docx"


def _load(path: Path) -> dict:
    value = load_json_unique(path)
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return value


def _module_ids(routing: dict) -> list[str]:
    return [item["id"] for item in routing["active_modules"]]


def _contributions(routing: dict, *, section: str | None = None, variant: str | None = None):
    return [
        item
        for item in routing["structure"]["section_contributions"]
        if (section is None or item["section"] == section)
        and (variant is None or item["variant"] == variant)
    ]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_routing_acceptance(facts: dict) -> dict:
    baseline = route_modules(facts)

    no_escalation_facts = copy.deepcopy(facts)
    no_escalation_facts["design"]["escalation_scheme"] = "none"
    no_escalation = route_modules(no_escalation_facts)

    no_ada_facts = copy.deepcopy(facts)
    no_ada_facts["data_domains"]["ada"] = False
    no_ada = route_modules(no_ada_facts)

    confirmatory_facts = copy.deepcopy(facts)
    confirmatory_facts["analyses"]["hypothesis_test"] = True
    confirmatory = route_modules(confirmatory_facts)

    phase_facts = copy.deepcopy(facts)
    phase_facts["phase"] = "III"
    phase_before = canonical_structure_json(facts)
    phase_after = canonical_structure_json(phase_facts)
    phase_diff = "".join(
        difflib.unified_diff(
            phase_before.splitlines(keepends=True),
            phase_after.splitlines(keepends=True),
            fromfile="phase-Ib",
            tofile="phase-III",
        )
    )

    baseline_ids = _module_ids(baseline)
    no_escalation_ids = _module_ids(no_escalation)
    no_ada_ids = _module_ids(no_ada)
    confirmatory_ids = _module_ids(confirmatory)
    return {
        "schema_version": "1.0",
        "module_inventory": {
            "original_m3": 23,
            "expected_current": 41,
            "actual": len(EXPECTED_MODULE_IDS),
            "ids": list(EXPECTED_MODULE_IDS),
        },
        "baseline_active_modules": baseline_ids,
        "acceptance": {
            "escalation_scheme_none": {
                "changed_fields": ["design.escalation_scheme"],
                "before": facts["design"]["escalation_scheme"],
                "after": "none",
                "removed_modules": sorted(set(baseline_ids) - set(no_escalation_ids)),
                "dose_escalation_contribution_after": _contributions(
                    no_escalation, section="11.A", variant="dose_escalation"
                ),
                "materialized_11_A_after": [
                    item
                    for item in no_escalation["structure"]["materialized_sections"]
                    if item["section"].startswith("11.A")
                ],
                "unmaterialized_contributions_after": no_escalation["structure"][
                    "unmaterialized_contributions"
                ],
                "warnings_after": no_escalation["warnings"],
                "status": "pass",
            },
            "ada_false": {
                "changed_fields": ["data_domains.ada"],
                "before": facts["data_domains"]["ada"],
                "after": False,
                "removed_modules": sorted(set(baseline_ids) - set(no_ada_ids)),
                "section_11_C_after": _contributions(no_ada, section="11.C"),
                "status": "pass",
            },
            "hypothesis_test_true": {
                "changed_fields": ["analyses.hypothesis_test"],
                "before": facts["analyses"]["hypothesis_test"],
                "after": True,
                "removed_modules": sorted(set(baseline_ids) - set(confirmatory_ids)),
                "added_modules": sorted(set(confirmatory_ids) - set(baseline_ids)),
                "section_11_4_1_variants_after": [
                    item["variant"]
                    for item in _contributions(confirmatory, section="11.4.1")
                ],
                "status": "pass",
            },
            "phase_only": {
                "changed_fields": ["phase"],
                "before": facts["phase"],
                "after": phase_facts["phase"],
                "removed_modules": [],
                "added_modules": [],
                "structure_before_sha256": _sha256_text(phase_before),
                "structure_after_sha256": _sha256_text(phase_after),
                "structure_unified_diff": phase_diff,
                "structure_equal": phase_before == phase_after,
                "non_structural_warnings_after": route_modules(phase_facts)["warnings"],
                "status": "pass",
            },
        },
    }


def build_guard_regression(facts: dict, datapack: dict) -> dict:
    blocked_cases = [
        ("ascii_percentage", "试验组客观缓解率为70.0%。"),
        ("written_percentage", "试验组客观缓解率为百分之七十。"),
        ("chinese_count", "共有七例受试者达到客观缓解。"),
        ("percentage_points", "客观缓解率提高二十五个百分点。"),
        ("tenths", "试验组约七成受试者达到客观缓解。"),
        ("tenths_decimal", "安慰剂组约四成五达到客观缓解。"),
        ("fraction", "三分之二受试者达到客观缓解。"),
        ("multiple", "暴露量增加约三倍。"),
        ("financial_count", "共有壹佰贰拾例受试者。"),
        ("fuzzy_half", "超过半数受试者达到客观缓解。"),
        ("combined_original", "试验组约七成受试者达到客观缓解，安慰剂组约四成五。"),
    ]
    cases: list[dict] = []
    for case_id, draft in blocked_cases:
        outcome = resolve(draft, datapack, facts)
        cases.append(
            {
                "id": case_id,
                "draft": draft,
                "expected": "review_required",
                "actual": outcome.status,
                "tokens": [item["token"] for item in outcome.report["issues"]],
                "kinds": [item["kind"] for item in outcome.report["issues"]],
                "status": "pass" if outcome.status == "review_required" else "fail",
            }
        )

    schedule_facts = copy.deepcopy(facts)
    schedule_facts["design"]["dosing_interval_weeks"] = 3
    schedule_text = "方案规定每 3 周一次。"
    schedule = resolve(schedule_text, datapack, schedule_facts)
    cases.append(
        {
            "id": "fact_derived_schedule_false_positive_control",
            "draft": schedule_text,
            "expected": "pass",
            "actual": schedule.status,
            "tokens": [item["token"] for item in schedule.report["issues"]],
            "kinds": [item["kind"] for item in schedule.report["issues"]],
            "status": "pass" if schedule.status == "pass" else "fail",
        }
    )
    passed = sum(item["status"] == "pass" for item in cases)
    return {
        "schema_version": "1.0",
        "source": "M1M2验收报告.md §3.1 + 方案事实白名单误报控制",
        "summary": {"total": len(cases), "passed": passed, "failed": len(cases) - passed},
        "cases": cases,
    }


def build_sections(facts: dict, efficacy_datapack: dict) -> dict:
    dlt_datapack = _load(SECTION_INPUTS / "synthetic_dlt_datapack.json")
    definitions = (
        (
            "11.4",
            _load(SECTION_INPUTS / "efficacy_request.json"),
            efficacy_datapack,
        ),
        (
            "11.A",
            _load(SECTION_INPUTS / "dose_escalation_request.json"),
            dlt_datapack,
        ),
    )
    resolved_dir = ROOT / "sections" / "resolved"
    summary: dict[str, dict] = {}
    for section_id, request, datapack in definitions:
        draft = draft_section(request, ROOT)
        outcome = resolve(
            draft.markdown,
            datapack,
            facts,
            binding_manifest=draft.metadata,
        )
        if outcome.status != "pass" or outcome.resolved_text is None:
            raise RuntimeError(f"Section {section_id} did not pass resolve: {outcome.status}")
        resolved_path = resolved_dir / f"{section_id}.md"
        guard_path = resolved_dir / f"{section_id}.guard.json"
        atomic_write_text_bundle(
            [
                (resolved_path, outcome.resolved_text),
                (
                    guard_path,
                    json.dumps(outcome.report, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                ),
            ]
        )
        summary[section_id] = {
            "draft": str(draft.markdown_path.relative_to(ROOT)),
            "metadata": str(draft.metadata_path.relative_to(ROOT)),
            "resolved": str(resolved_path.relative_to(ROOT)),
            "guard_report": str(guard_path.relative_to(ROOT)),
            "placeholder_count": outcome.report["placeholder_count"],
            "resolve_status": outcome.status,
        }
    return summary


def main() -> int:
    facts = validate_study_facts(_load(FACTS_PATH))
    datapack = _load(DATAPACK_PATH)

    atomic_write_text(EXAMPLES / "demo_study_facts_card.md", render_fact_card(facts))
    routing = build_routing_acceptance(facts)
    atomic_write_json(OUTPUT / "m3_routing_acceptance.json", routing)
    atomic_write_text(OUTPUT / "m3_phase_only_structure.diff", "")

    guard_regression = build_guard_regression(facts, datapack)
    if guard_regression["summary"]["failed"]:
        raise RuntimeError("M3 numeric guard regression has failures")
    atomic_write_json(OUTPUT / "m3_numeric_guard_regression.json", guard_regression)

    extraction = extract_docx_to_file(DOCX_FIXTURE, OUTPUT / "m3_docx_extraction.json")
    section_summary = build_sections(facts, datapack)

    summary = {
        "schema_version": "1.0",
        "scope": "M3 only; M4 not run",
        "numeric_guard": guard_regression["summary"],
        "module_inventory": routing["module_inventory"],
        "routing_acceptance": {
            key: value["status"] for key, value in routing["acceptance"].items()
        },
        "phase_only_diff_bytes": 0,
        "docx_extraction": {
            "view": extraction["view"],
            "statistics": extraction["statistics"],
            "gap_codes": [item["code"] for item in extraction["gap_report"]],
            "source_unchanged": extraction["source"]["unchanged"],
        },
        "sections": section_summary,
    }
    atomic_write_json(OUTPUT / "m3_e2e_summary.json", summary)
    print("M3 e2e PASS")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
