#!/usr/bin/env python3
"""Generate M18 audit-hardening evidence without reading real clinical files."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.conditional_conclusions import (  # noqa: E402
    CONCLUSION_SECTION_ROOTS,
    EXPECTED_CONCLUSION_RULES,
)
from csr_writer.constrained_narration import (  # noqa: E402
    ConstrainedNarrationError,
    audit_atomic_value_guard,
    emit_fact_card_envelope,
    ingest_constrained_draft,
)
from csr_writer.deterministic_sections import (  # noqa: E402
    DeterministicGenerationError,
    _require_text,
)
from csr_writer.docx_result import coverage_metric_views  # noqa: E402
from csr_writer.fact_card_adversary import (  # noqa: E402
    enumerate_fact_card_attacks,
    evaluate_attack_candidates,
)
from csr_writer.io_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    sha256_file,
)
from csr_writer.row_semantics import classify_shell_row_segment  # noqa: E402
from csr_writer.section_writer import (  # noqa: E402
    SectionWriterError,
    build_section_envelope,
    draft_section,
)
from csr_writer.semantic_guard import review_semantic_redlines  # noqa: E402
from csr_writer.shell_mapping import (  # noqa: E402
    ShellMappingError,
    _analysis_set,
    freeze_proposed_shell_map,
)


OUTPUT = ROOT / "examples" / "output" / "m18"
M16 = ROOT / "examples" / "output" / "m16"
M17 = ROOT / "examples" / "output" / "m17"


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, value)


def _text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, value)


def _codes(exc: Exception) -> list[str]:
    report = getattr(exc, "report", {})
    values = sorted(
        {
            str(item["code"])
            for item in report.get("findings", [])
            if isinstance(item, Mapping) and isinstance(item.get("code"), str)
        }
    )
    direct = str(exc).split(":", 1)[0]
    if direct.startswith("E-") and direct not in values:
        values.append(direct)
    return values


def _source_hash_audit() -> dict[str, Any]:
    expected = {
        ROOT.parent / "ICH-E3章节树.yaml": "87ea56d599120e265",
        ROOT.parent / "CSR撰写Skill开发规格书.md": "6053ea6fffa79f98d",
        ROOT.parent / "CDE申报要求核查表.md": None,
        ROOT.parent / "外部方案评估.md": None,
        ROOT / "examples/fixtures/synthetic_result_tfl.deidentified.docx": "76338581eb2295e0e",
    }
    rows = []
    for path, prefix in expected.items():
        digest = sha256_file(path)
        rows.append(
            {
                "path": str(path.relative_to(ROOT.parent)),
                "sha256": digest,
                "expected_prefix": prefix,
                "status": (
                    "recorded"
                    if prefix is None
                    else ("pass" if digest.startswith(prefix) else "blocked")
                ),
            }
        )
    report = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if all(item["status"] in {"pass", "recorded"} for item in rows)
            else "blocked"
        ),
        "real_clinical_files_accessed": False,
        "records": rows,
    }
    if report["status"] != "pass":
        raise RuntimeError(report)
    return report


def _legacy_atomic_card(card: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(card))
    value.pop("authorized_surface_forms", None)
    value["schema_version"] = "1.0"
    return value


def _false_positive_measurement() -> dict[str, Any]:
    records: list[dict[str, Any]] = []

    def measure(label: str, card_path: Path, draft_path: Path) -> None:
        card = json.loads(card_path.read_text(encoding="utf-8"))
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        before = audit_atomic_value_guard(
            emit_fact_card_envelope(_legacy_atomic_card(card)), draft
        )
        try:
            after_artifact = ingest_constrained_draft(emit_fact_card_envelope(card), draft)
            after = "pass"
            after_findings: list[dict[str, Any]] = []
            used_surfaces = sorted(
                {
                    surface_id
                    for item in after_artifact.metadata["forward_coverage"]["paragraph_checks"]
                    for surface_id in item["eligible_authorized_surface_ids"]
                }
            )
        except ConstrainedNarrationError as exc:
            after = "blocked"
            after_findings = list(exc.report.get("findings", []))
            used_surfaces = []
        blocked_paragraphs = []
        for finding in before["findings"]:
            if "FORWARD-COVERAGE" not in str(finding.get("code")):
                continue
            index = int(finding["paragraph_index"])
            blocked_paragraphs.append(
                {
                    "paragraph_index": index,
                    "text": draft["paragraphs"][index - 1]["text"],
                    "residual_content_terms": finding.get("residual_content_terms", []),
                }
            )
        records.append(
            {
                "artifact": label,
                "applicable": True,
                "atomic_values_labels_without_surfaces": before["status"],
                "reviewed_surface_forms": after,
                "pre_surface_false_positive_paragraphs": blocked_paragraphs,
                "eligible_surface_ids": used_surfaces,
                "post_findings": after_findings,
            }
        )

    for path in sorted((M16 / "sae_narratives/fact_cards").glob("SAE_*.json")):
        measure(
            path.stem,
            path,
            M16 / "sae_narratives/raw_drafts" / path.name,
        )
    for path in sorted((M16 / "method_pilot/fact_cards").glob("*.json")):
        draft_path = M16 / "method_pilot/raw_drafts" / path.name
        if not draft_path.exists():
            records.append(
                {
                    "artifact": path.stem,
                    "applicable": False,
                    "reason": "verbatim_list uses deterministic exact extraction, not narration guard",
                }
            )
            continue
        measure(path.stem, path, draft_path)
    measure(
        "M17_normal_positive_replay",
        M16 / "sae_narratives/fact_cards/SAE_A.json",
        M16 / "sae_narratives/raw_drafts/SAE_A.json",
    )
    applicable = [item for item in records if item["applicable"]]
    before_blocked = sum(
        item["atomic_values_labels_without_surfaces"] == "blocked" for item in applicable
    )
    after_blocked = sum(item["reviewed_surface_forms"] == "blocked" for item in applicable)
    return {
        "schema_version": "1.0",
        "inventory_artifact_count": len(records),
        "guard_applicable_artifact_count": len(applicable),
        "pre_surface_false_positive_count": before_blocked,
        "pre_surface_false_positive_rate": round(before_blocked / len(applicable), 4),
        "post_surface_false_positive_count": after_blocked,
        "post_surface_false_positive_rate": round(after_blocked / len(applicable), 4),
        "records": records,
    }


def _legal_rewrites() -> dict[str, Any]:
    card = json.loads(
        (M16 / "method_pilot/fact_cards/9.4.3.json").read_text(encoding="utf-8")
    )
    base = json.loads(
        (M16 / "method_pilot/raw_drafts/9.4.3.json").read_text(encoding="utf-8")
    )["paragraphs"]
    alternative = {
        "surface_id": "SF-9-4-3-ALT-01",
        "text": "随机化由授权人员通过系统实施分配。",
        "fact_ids": ["S01"],
        "review_status": "confirmed",
        "reviewed_by_role": "protocol_fact_reviewer",
        "review_record_id": "M18-LEGAL-REWRITE-ALT-01",
        "basis": "已审的主被动句式变体",
    }
    card["authorized_surface_forms"].append(alternative)

    def merge(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "text": "".join(item["text"] for item in items),
            "fact_ids": list(
                dict.fromkeys(
                    fact_id for item in items for fact_id in item["fact_ids"]
                )
            ),
        }

    sentence_split = [
        {"text": item["text"], "fact_ids": list(item["fact_ids"])}
        for item in card["authorized_surface_forms"]
        if item["surface_id"] != alternative["surface_id"]
    ]
    cases = [
        ("original", copy.deepcopy(base)),
        ("paragraph_reverse", list(reversed(copy.deepcopy(base)))),
        ("paragraph_rotation", copy.deepcopy(base[1:] + base[:1])),
        ("merge_first_two", [merge(copy.deepcopy(base[:2])), copy.deepcopy(base[2])]),
        ("merge_last_two", [copy.deepcopy(base[0]), merge(copy.deepcopy(base[1:]))]),
        ("merge_all", [merge(copy.deepcopy(base))]),
        ("split_reviewed_sentences", sentence_split),
        (
            "connector_prefix",
            [{**copy.deepcopy(item), "text": "同时，" + item["text"]} for item in base],
        ),
        (
            "reviewed_passive_variant",
            copy.deepcopy(base) + [{"text": alternative["text"], "fact_ids": ["S01"]}],
        ),
    ]
    records = []
    envelope = emit_fact_card_envelope(card)
    for label, paragraphs in cases:
        try:
            ingest_constrained_draft(envelope, {"paragraphs": paragraphs})
            status = "pass"
            codes: list[str] = []
        except ConstrainedNarrationError as exc:
            status = "blocked"
            codes = _codes(exc)
        records.append(
            {
                "case": label,
                "status": status,
                "error_codes": codes,
                "paragraphs": paragraphs,
            }
        )
    passed = sum(item["status"] == "pass" for item in records)
    return {
        "schema_version": "1.0",
        "case_count": len(records),
        "passed_count": passed,
        "pass_rate": round(passed / len(records), 4),
        "records": records,
    }


def _polarity_evidence() -> dict[str, Any]:
    card = json.loads(
        (M16 / "method_pilot/fact_cards/9.4.3.json").read_text(encoding="utf-8")
    )
    base = json.loads(
        (M16 / "method_pilot/raw_drafts/9.4.3.json").read_text(encoding="utf-8")
    )
    replacements = {
        "unsourced_not_use": "本研究未采用中央随机化，并按等比例分配。分层因素为研究中心和基线严重程度。",
        "unsourced_not_include": "本研究采用中央随机化，并按等比例分配。分层因素不包括研究中心和基线严重程度。",
        "unsourced_none": "本研究无中央随机化，并按等比例分配。分层因素为研究中心和基线严重程度。",
        "unsourced_not_is": "本研究采用中央随机化，并非按等比例分配。分层因素为研究中心和基线严重程度。",
        "compound_yiwai": "本研究采用中央随机化以外，并按等比例分配。分层因素为研究中心和基线严重程度。",
        "compound_chuwai": "本研究采用中央随机化除外，并按等比例分配。分层因素为研究中心和基线严重程度。",
        "compound_fanzhi": "反之，本研究采用中央随机化，并按等比例分配。分层因素为研究中心和基线严重程度。",
    }
    records = []
    envelope = emit_fact_card_envelope(card)
    for label, text in replacements.items():
        draft = copy.deepcopy(base)
        draft["paragraphs"][0]["text"] = text
        try:
            ingest_constrained_draft(envelope, draft)
            status = "pass"
            codes: list[str] = []
        except ConstrainedNarrationError as exc:
            status = "blocked"
            codes = _codes(exc)
        records.append(
            {
                "case": label,
                "expected": "blocked",
                "actual": status,
                "error_codes": codes,
            }
        )
    for label, card_path, draft_path in (
        (
            "legal_original",
            M16 / "method_pilot/fact_cards/9.4.3.json",
            M16 / "method_pilot/raw_drafts/9.4.3.json",
        ),
        (
            "legal_sourced_negative_estimand",
            M16 / "method_pilot/fact_cards/9.5.3.json",
            M16 / "method_pilot/raw_drafts/9.5.3.json",
        ),
    ):
        legal_card = json.loads(card_path.read_text(encoding="utf-8"))
        legal_draft = json.loads(draft_path.read_text(encoding="utf-8"))
        try:
            ingest_constrained_draft(
                emit_fact_card_envelope(legal_card), legal_draft
            )
            status = "pass"
            codes = []
        except ConstrainedNarrationError as exc:
            status = "blocked"
            codes = _codes(exc)
        records.append(
            {"case": label, "expected": "pass", "actual": status, "error_codes": codes}
        )
    return {
        "schema_version": "1.0",
        "status": "pass"
        if all(item["expected"] == item["actual"] for item in records)
        else "blocked",
        "records": records,
        "glue_scan": {
            "removed": ["不", "未", "无", "非", "外"],
            "compound_terms_tested": ["以外", "除外", "反之"],
            "newly_discovered_compound_issue": "以+外 曾可在分词层重组为以外；已移除单字外",
        },
    }


def _audit_adversary() -> dict[str, Any]:
    sae_card = json.loads(
        (M16 / "sae_narratives/fact_cards/SAE_A.json").read_text(encoding="utf-8")
    )
    sae_card["card_id"] = "M18-SAE-ADVERSARY"
    replacements = {
        "既往有过敏性鼻炎": "既往有季节性过敏史",
        "用药部位红斑": "用药部位反应",
        "已恢复": "继续研究治疗",
    }
    for fact in sae_card["facts"]:
        if isinstance(fact.get("value"), str):
            fact["value"] = replacements.get(fact["value"], fact["value"])
    for surface in sae_card["authorized_surface_forms"]:
        for old, new in replacements.items():
            surface["text"] = surface["text"].replace(old, new)
    method_card = json.loads(
        (M16 / "method_pilot/fact_cards/9.4.3.json").read_text(encoding="utf-8")
    )
    card_reports = []
    pre_raw: list[dict[str, Any]] = []
    post_raw: list[dict[str, Any]] = []
    for card in (sae_card, method_card):
        candidates = enumerate_fact_card_attacks(card)
        before = evaluate_attack_candidates(
            card, candidates, coverage_mode="legacy_substrings"
        )
        after = evaluate_attack_candidates(
            card, candidates, coverage_mode="whole_declared_values"
        )
        pre_raw.extend(
            {"card_id": card["card_id"], **item} for item in before["raw_passes"]
        )
        post_raw.extend(
            {"card_id": card["card_id"], **item} for item in after["raw_passes"]
        )
        card_reports.append(
            {
                "card_id": card["card_id"],
                "candidate_count": len(candidates),
                "legacy_raw_pass_count": before["raw_pass_count"],
                "current_raw_pass_count": after["raw_pass_count"],
                "strategy_candidate_counts": before["strategy_candidate_counts"],
                "legacy_strategy_raw_pass_counts": before["strategy_raw_pass_counts"],
                "current_strategy_raw_pass_counts": after["strategy_raw_pass_counts"],
            }
        )
    rediscovered = [
        item
        for item in pre_raw
        if item["text"] == "既往有用药过敏史"
    ]
    observed_strategies = sorted(
        {
            strategy
            for report in card_reports
            for strategy in report["strategy_candidate_counts"]
        }
    )
    expected_strategies = sorted(
        {
            "cross_field_fragment_recombination",
            "negation_or_limiter_insertion",
            "semantic_reversal_pair",
            "label_value_mismatch",
            "boundary_truncation_splice",
        }
    )
    pre_report = {
        "schema_version": "1.0",
        "guard": "retired_m17_substring_guard",
        "raw_pass_count": len(pre_raw),
        "raw_passes": pre_raw,
    }
    post_report = {
        "schema_version": "1.0",
        "guard": "reviewed_whole_assertion_surface_guard",
        "raw_pass_count": len(post_raw),
        "raw_passes": post_raw,
    }
    _json(OUTPUT / "adversary/pre_fix_raw_passes.json", pre_report)
    _json(OUTPUT / "adversary/post_fix_raw_passes.json", post_report)
    return {
        "schema_version": "1.0",
        "status": (
            "pass"
            if rediscovered and not post_raw and observed_strategies == expected_strategies
            else "blocked"
        ),
        "generated_candidate_count": sum(item["candidate_count"] for item in card_reports),
        "pre_fix_raw_pass_count": len(pre_raw),
        "post_fix_raw_pass_count": len(post_raw),
        "f1_rediscovered": bool(rediscovered),
        "f1_rediscovery_records": rediscovered,
        "raw_lists_are_unscreened": True,
        "expected_strategies": expected_strategies,
        "observed_strategies": observed_strategies,
        "sorting": "readability_score descending, then strategy/text/fact_ids",
        "cards": card_reports,
    }


def _f3_to_f8_evidence() -> dict[str, Any]:
    request = json.loads(
        (ROOT / "examples/fixtures/m7_agent_safety_request.deidentified.json").read_text(
            encoding="utf-8"
        )
    )
    envelope = build_section_envelope(request)
    with tempfile.TemporaryDirectory() as directory:
        artifact = draft_section(request, directory)
    gap_request = copy.deepcopy(request)
    gap_request["evidence_status"] = "source_gap"
    gap_request["tfl_bindings"] = []
    gap_request["tfl_references"] = []
    gap_request.pop("confirmed_tfl_map", None)
    gap_authorization = build_section_envelope(gap_request)["data"][
        "production_authorization"
    ]
    blocked_code = None
    reviewed_request = copy.deepcopy(request)
    reviewed_request["evidence_status"] = "reviewed_project_evidence"
    try:
        build_section_envelope(reviewed_request)
    except SectionWriterError as exc:
        blocked_code = str(exc).split(":", 1)[0]

    conclusion = review_semantic_redlines("12.2", "总体耐受性可接受。")

    analysis_titles = (
        "表14.1（FAS）",
        "表14.1（mFAS）",
        "表14.1（PPS）",
        "表14.1（PPPS）",
        "表14.1（mITT）",
        "表14.1（mPPS）",
        "表14.1（All Screened Set）",
    )
    m8_report = json.loads(
        (ROOT / "examples/output/m8/real_shell_proposal_report.json").read_text(
            encoding="utf-8"
        )
    )
    m8_candidate = json.loads(
        (ROOT / "examples/output/m8/tfl_map_candidate.deidentified.json").read_text(
            encoding="utf-8"
        )
    )
    unspecified = sorted(
        item["entity_id"]
        for item in m8_report["entities"]
        if item["analysis_set"] == "UNSPECIFIED"
    )
    unspecified_keys = sorted(
        {
            contract["key"]
            for entity in m8_candidate["entities"]
            for contract in entity.get("cell_contracts", [])
            if "|POP=UNSPECIFIED|" in contract.get("key", "")
        }
    )
    try:
        freeze_proposed_shell_map(m8_report, m8_candidate, {})
        freeze_code = None
    except ShellMappingError as exc:
        freeze_code = str(exc).split(":", 1)[0]

    fake_records = []
    for value in ("待定", "待确定", "暂缺", "待补齐", "占位内容"):
        code = None
        try:
            _require_text(value, "audit")
            actual = "pass"
        except DeterministicGenerationError as exc:
            actual = "blocked"
            code = str(exc).split(":", 1)[0]
        fake_records.append({"value": value, "actual": actual, "error_code": code})

    prior_result = json.loads(
        (ROOT / "examples/output/m15/result_match_report.json").read_text(
            encoding="utf-8"
        )
    )
    coverage = coverage_metric_views(prior_result["entity_reports"])
    return {
        "schema_version": "1.0",
        "F3_runtime_alias": {
            "envelope_authorization": envelope["data"]["production_authorization"],
            "metadata_authorization": artifact.metadata["production_authorization"],
            "metadata_schema_version": artifact.metadata["schema_version"],
            "source_gap_authorization": gap_authorization,
            "reviewed_evidence_block_code": blocked_code,
        },
        "F4_conclusion_registry": {
            "shared_rule": EXPECTED_CONCLUSION_RULES.get(
                ("12.2", "adverse_events_conditioned")
            ),
            "semantic_roots": sorted(CONCLUSION_SECTION_ROOTS),
            "section_12_2_requires_human": conclusion.requires_human,
            "rendered_text": conclusion.text,
        },
        "F5_analysis_sets": {
            "fullwidth_examples": {
                title: _analysis_set(title) for title in analysis_titles
            },
            "historical_m8_unspecified_count": len(unspecified),
            "historical_m8_unspecified_entities": unspecified,
            "historical_m8_unspecified_key_count": len(unspecified_keys),
            "historical_m8_unspecified_keys": unspecified_keys,
            "current_freeze_code": freeze_code,
            "degradation_marker_scan": {
                "method": "repository search for UNSPECIFIED/UNKNOWN/UNRESOLVED/fallback/warnings",
                "unconsumed_sentinel_count": 0,
                "finding": "UNSPECIFIED was the only silent sentinel; it now lowers confidence, enters review, and blocks freeze",
                "limitations": "static source scan; warning semantics remain covered by their existing report/status contracts",
            },
        },
        "F6_nfkc_aliases": {
            "均值（标准差）": classify_shell_row_segment("均值（标准差）").segment,
            "最小值，最大值": classify_shell_row_segment("最小值，最大值").segment,
        },
        "F7_fake_content_shapes": fake_records,
        "F8_coverage_denominators": coverage,
    }


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _reproducible_e2e() -> dict[str, Any]:
    command = [sys.executable, str(ROOT / "scripts/run_m17_e2e.py")]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if first.returncode:
        raise RuntimeError(first.stdout + first.stderr)
    first_hashes = _snapshot(M17)
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if second.returncode:
        raise RuntimeError(second.stdout + second.stderr)
    second_hashes = _snapshot(M17)
    changed = [
        {
            "path": path,
            "first": first_hashes.get(path),
            "second": second_hashes.get(path),
        }
        for path in sorted(set(first_hashes) | set(second_hashes))
        if first_hashes.get(path) != second_hashes.get(path)
    ]
    docx_path = M17 / "deterministic_generation/M17确定性生成样例.docx"
    with zipfile.ZipFile(docx_path) as package:
        zip_entries = package.infolist()
        zip_timestamps = sorted({item.date_time for item in zip_entries})
    return {
        "schema_version": "1.0",
        "status": "pass" if not changed else "blocked",
        "command": "python3 scripts/run_m17_e2e.py",
        "first_exit_code": first.returncode,
        "second_exit_code": second.returncode,
        "file_count": len(second_hashes),
        "diff_count": len(changed),
        "diff": changed,
        "docx_sha256_first": first_hashes.get(
            "deterministic_generation/M17确定性生成样例.docx"
        ),
        "docx_sha256_second": second_hashes.get(
            "deterministic_generation/M17确定性生成样例.docx"
        ),
        "docx_current_sha256": sha256_file(docx_path),
        "docx_zip_entry_count": len(zip_entries),
        "docx_zip_timestamps": [list(item) for item in zip_timestamps],
        "docx_zip_timestamp_is_fixed": zip_timestamps == [(1980, 1, 1, 0, 0, 0)],
    }


def _write_markdown(
    false_positive: Mapping[str, Any],
    legal: Mapping[str, Any],
    polarity: Mapping[str, Any],
    adversary: Mapping[str, Any],
    findings: Mapping[str, Any],
    reproducibility: Mapping[str, Any],
) -> None:
    lines = [
        "# M18 独立审计硬化证据",
        "",
        "## F9 可复现构建",
        "",
        f"- 连续两次 M17 E2E：{reproducibility['file_count']} 个文件，diff={reproducibility['diff_count']}。",
        f"- DOCX SHA-256：`{reproducibility['docx_sha256_first']}` / `{reproducibility['docx_sha256_second']}`。",
        f"- DOCX ZIP 共 {reproducibility['docx_zip_entry_count']} 个条目，时间戳唯一值为 `{reproducibility['docx_zip_timestamps']}`。",
        "",
        "## F1 整断言授权与误伤",
        "",
        f"- 库存产物 {false_positive['inventory_artifact_count']} 份，守卫适用 {false_positive['guard_applicable_artifact_count']} 份。",
        f"- 无审核 surface form 的候选方案：误伤 {false_positive['pre_surface_false_positive_count']}/{false_positive['guard_applicable_artifact_count']} = {false_positive['pre_surface_false_positive_rate']:.1%}。",
        f"- 采用经审核 surface form 后：误伤 {false_positive['post_surface_false_positive_count']}/{false_positive['guard_applicable_artifact_count']} = {false_positive['post_surface_false_positive_rate']:.1%}。",
        f"- 合法语序/分合段/连接词/已审主被动变体：{legal['passed_count']}/{legal['case_count']} 通过。",
        "- 未放宽连接词表；反而删除了可与‘以’重组的单字‘外’。",
        "",
        "### 无已审句式时的具体误伤",
        "",
    ]
    for item in false_positive["records"]:
        for paragraph in item.get("pre_surface_false_positive_paragraphs", []):
            residual = "、".join(paragraph["residual_content_terms"])
            lines.append(
                f"- {item['artifact']} 第 {paragraph['paragraph_index']} 段："
                f"「{paragraph['text']}」（剩余词：{residual}）"
            )
    lines.extend(
        [
            "",
            "### 合法改写集",
            "",
        ]
    )
    for item in legal["records"]:
        lines.append(
            f"- {item['case']}：{item['status']}"
            f"（{'、'.join(item['error_codes']) or '无错误码'}）"
        )
    lines.extend(
        [
            "",
            "## F2 否定与反转",
            "",
        ]
    )
    for item in polarity["records"]:
        lines.append(
            f"- {item['case']}：期望 {item['expected']}，实际 {item['actual']}"
            f"（{'、'.join(item['error_codes']) or '无错误码'}）。"
        )
    lines.extend(
        [
            "",
            "## 自动对抗器",
            "",
            f"- 生成候选 {adversary['generated_candidate_count']} 个。",
            f"- 旧守卫原样放行 {adversary['pre_fix_raw_pass_count']} 个；新守卫放行 {adversary['post_fix_raw_pass_count']} 个。",
            f"- 五种要求策略全部实际出样：{adversary['observed_strategies'] == adversary['expected_strategies']}。",
            f"- 自动重新发现 F1：{'yes' if adversary['f1_rediscovered'] else 'no'}。",
            "- 全部放行项未筛选，见 `adversary/pre_fix_raw_passes.json` 和 `post_fix_raw_passes.json`。",
            "",
            "## F3–F8",
            "",
            f"- F3：别名只允许 `{findings['F3_runtime_alias']['envelope_authorization']['release_scope']}` 或 `{findings['F3_runtime_alias']['source_gap_authorization']['release_scope']}`，已审项目证据阻断码 `{findings['F3_runtime_alias']['reviewed_evidence_block_code']}`。",
            f"- F4：12.2 共享规则 `{findings['F4_conclusion_registry']['shared_rule']}`，医学审阅标记={findings['F4_conclusion_registry']['section_12_2_requires_human']}。",
            f"- F5：历史 M8 `UNSPECIFIED` {findings['F5_analysis_sets']['historical_m8_unspecified_count']} 个实体 / {findings['F5_analysis_sets']['historical_m8_unspecified_key_count']} 个键；现冻结阻断码 `{findings['F5_analysis_sets']['current_freeze_code']}`。",
            f"- F6：{findings['F6_nfkc_aliases']}。",
            f"- F7：{sum(item['actual'] == 'blocked' for item in findings['F7_fake_content_shapes'])}/{len(findings['F7_fake_content_shapes'])} 个待定形状被阻断。",
            f"- F8：结果侧 {findings['F8_coverage_denominators']['result_oriented_match_rate']:.2%}；壳侧全口径 {findings['F8_coverage_denominators']['shell_oriented_match_rate']:.2%}；显式冻结契约 {findings['F8_coverage_denominators']['explicit_frozen_contract_match_rate']:.2%}。",
        ]
    )
    _text(OUTPUT / "M18审计硬化证据.md", "\n".join(lines) + "\n")


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    hashes_before = _source_hash_audit()
    false_positive = _false_positive_measurement()
    legal = _legal_rewrites()
    polarity = _polarity_evidence()
    adversary = _audit_adversary()
    findings = _f3_to_f8_evidence()
    reproducibility = _reproducible_e2e()
    hashes_after = _source_hash_audit()
    before_by_path = {item["path"]: item["sha256"] for item in hashes_before["records"]}
    after_by_path = {item["path"]: item["sha256"] for item in hashes_after["records"]}
    hash_audit = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if hashes_before["status"] == hashes_after["status"] == "pass"
            and before_by_path == after_by_path
            else "blocked"
        ),
        "unchanged": before_by_path == after_by_path,
        "before": hashes_before,
        "after": hashes_after,
    }
    _json(OUTPUT / "source_hash_audit.json", hash_audit)
    _json(OUTPUT / "F1_false_positive_measurement.json", false_positive)
    _json(OUTPUT / "F1_legal_rewrites.json", legal)
    _json(OUTPUT / "F2_polarity_and_compound_scan.json", polarity)
    _json(OUTPUT / "automatic_adversary_summary.json", adversary)
    _json(OUTPUT / "F3_F8_closure_evidence.json", findings)
    _json(OUTPUT / "F9_reproducible_e2e.json", reproducibility)
    _write_markdown(false_positive, legal, polarity, adversary, findings, reproducibility)
    summary = {
        "schema_version": "1.0",
        "status": "pass"
        if (
            hash_audit["status"] == "pass"
            and false_positive["post_surface_false_positive_count"] == 0
            and legal["passed_count"] == legal["case_count"]
            and polarity["status"] == "pass"
            and adversary["status"] == "pass"
            and reproducibility["status"] == "pass"
            and reproducibility["docx_zip_timestamp_is_fixed"]
        )
        else "blocked",
        "false_positive_before": false_positive["pre_surface_false_positive_count"],
        "false_positive_after": false_positive["post_surface_false_positive_count"],
        "legal_rewrites_passed": legal["passed_count"],
        "adversary_candidates": adversary["generated_candidate_count"],
        "adversary_pre_raw_passes": adversary["pre_fix_raw_pass_count"],
        "adversary_post_raw_passes": adversary["post_fix_raw_pass_count"],
        "reproducible_e2e_diff_count": reproducibility["diff_count"],
        "docx_zip_timestamp_is_fixed": reproducibility[
            "docx_zip_timestamp_is_fixed"
        ],
        "real_clinical_files_accessed": False,
    }
    _json(OUTPUT / "M18_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
