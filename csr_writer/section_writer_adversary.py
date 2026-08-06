"""Deterministic adversarial audit for every guarded section-writer unit."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .conditional_conclusions import EXPECTED_CONCLUSION_RULES, RULE_CONTRACTS
from .section_writer import (
    PLAYBOOK_FILENAMES,
    SECTION_VARIANTS,
    DraftParagraph,
    SectionWriterError,
    _parse_model_response,
    _validate_generated_paragraphs,
)


def guarded_unit_ids(inventory_path: str | Path) -> list[str]:
    payload = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    return [
        str(row["section_id"])
        for row in payload["rows"]
        if row.get("is_authoring_unit") is True
        and row.get("narration") == "guarded_section_writer"
    ]


def _variant(section_id: str) -> str:
    conclusion_variants = [
        variant
        for (candidate, variant), _rule in EXPECTED_CONCLUSION_RULES.items()
        if candidate == section_id
    ]
    if conclusion_variants:
        return conclusion_variants[0]
    variants = SECTION_VARIANTS.get(section_id)
    return sorted(variants)[0] if variants else "audit_unwired"


def _envelope(section_id: str) -> dict[str, Any]:
    variant = _variant(section_id)
    rule = EXPECTED_CONCLUSION_RULES.get((section_id, variant))
    roles = RULE_CONTRACTS[rule].roles if rule is not None else ("audit_first", "audit_second")
    table_id = "T14.3.99" if section_id.startswith("12.") else "T14.2.99"
    return {
        "data": {
            "section_id": section_id,
            "variant": variant,
            "source_fragments": [{"source_id": "audit_source"}],
            "tfl_bindings": [
                {
                    "role": role,
                    "key": (
                        f"{table_id}|POP=SAF|ROW={role.upper()}|COL=TOTAL"
                    ),
                    "source_id": table_id,
                }
                for role in roles
            ],
            "tfl_references": [],
        }
    }


def _baseline(section_id: str, envelope: Mapping[str, Any]) -> tuple[DraftParagraph, ...]:
    rule = EXPECTED_CONCLUSION_RULES.get(
        (section_id, str(envelope["data"]["variant"]))
    )
    if rule is not None:
        return (
            DraftParagraph(
                text=f"[待医学负责人确认] {{{{@conclusion:{rule}}}}}",
                sources=("audit_source",),
                confidence="inferred",
                needs_human=True,
            ),
        )
    first, second = [item["key"] for item in envelope["data"]["tfl_bindings"][:2]]
    return (
        DraftParagraph(
            text=f"结果为 {{{{{first}}}}}，相关结果为 {{{{{second}}}}}。",
            sources=("audit_source",),
            confidence="derived",
            needs_human=False,
        ),
    )


def _outcome(
    section_id: str,
    envelope: Mapping[str, Any],
    paragraphs: tuple[DraftParagraph, ...],
) -> dict[str, Any]:
    try:
        _validate_generated_paragraphs(paragraphs, envelope)
    except SectionWriterError as exc:
        return {"status": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
    return {"status": "pass", "reason": None}


def _attacks(
    section_id: str, envelope: Mapping[str, Any], baseline: tuple[DraftParagraph, ...]
) -> dict[str, tuple[DraftParagraph, ...]]:
    first, second = [item["key"] for item in envelope["data"]["tfl_bindings"][:2]]
    rule = EXPECTED_CONCLUSION_RULES.get(
        (section_id, str(envelope["data"]["variant"]))
    )
    common = {
        "unbound_placeholder": (
            DraftParagraph(
                text="结果为 {{T14.2.99|POP=FAS|ROW=UNBOUND|COL=TOTAL}}。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "missing_binding": (
            DraftParagraph(
                text=f"结果为 {{{{{first}}}}}。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "duplicate_binding": (
            DraftParagraph(
                text=f"结果为 {{{{{first}}}}}，复核结果为 {{{{{first}}}}}。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "swapped_binding_order": (
            DraftParagraph(
                text=f"结果为 {{{{{second}}}}}，相关结果为 {{{{{first}}}}}。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "literal_arabic_number": baseline
        + (
            DraftParagraph(
                text="共纳入 12 例受试者。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "literal_chinese_number": baseline
        + (
            DraftParagraph(
                text="共纳入十二例受试者。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "literal_zero_event": baseline
        + (
            DraftParagraph(
                text="未发生严重不良事件。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "internal_process_language": baseline
        + (
            DraftParagraph(
                text="当前证据包未提供结果绑定。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "malformed_braces": (
            DraftParagraph(
                text=f"结果为 {{{{{first}}}}}，且含有 {{MALFORMED}}。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "unbound_reference": baseline
        + (
            DraftParagraph(
                text="详见 {{@ref:T14.2.99}}。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
        "unscoped_source": tuple(
            DraftParagraph(
                text=item.text,
                sources=("outside_source",),
                confidence=item.confidence,
                needs_human=item.needs_human,
            )
            for item in baseline
        ),
        "physical_paragraph_injection": (
            DraftParagraph(
                text=baseline[0].text + "\n另一段。",
                sources=("audit_source",),
                confidence=baseline[0].confidence,
                needs_human=baseline[0].needs_human,
            ),
        ),
        "forbidden_style": baseline
        + (
            DraftParagraph(
                text="受试者例数汇总为待补充。",
                sources=("audit_source",),
                confidence="derived",
                needs_human=False,
            ),
        ),
    }
    if rule is not None:
        common.update(
            {
                "sentence_driven_arbitrary_prose": baseline
                + (
                    DraftParagraph(
                        text=(
                            "[待医学负责人确认] "
                            "主要结果与整体判断一致。"
                        ),
                        sources=("audit_source",),
                        confidence="inferred",
                        needs_human=True,
                    ),
                ),
                "sentence_driven_partial_binding": baseline
                + (
                    DraftParagraph(
                        text=f"关键结果为 {{{{{first}}}}}。",
                        sources=("audit_source",),
                        confidence="derived",
                        needs_human=False,
                    ),
                ),
                "wrong_conclusion_rule": (
                    DraftParagraph(
                        text="[待医学负责人确认] {{@conclusion:WRONG_RULE}}",
                        sources=("audit_source",),
                        confidence="inferred",
                        needs_human=True,
                    ),
                ),
            }
        )
    return common


def _parser_attack_results() -> list[dict[str, Any]]:
    payloads = {
        "extra_top_level_key": {"paragraphs": [], "extra": True},
        "empty_paragraph_list": {"paragraphs": []},
        "extra_paragraph_key": {
            "paragraphs": [
                {
                    "text": "文本。",
                    "sources": ["audit_source"],
                    "confidence": "derived",
                    "needs_human": False,
                    "extra": True,
                }
            ]
        },
        "non_boolean_human_flag": {
            "paragraphs": [
                {
                    "text": "文本。",
                    "sources": ["audit_source"],
                    "confidence": "derived",
                    "needs_human": "false",
                }
            ]
        },
    }
    records = []
    for attack_id, payload in payloads.items():
        try:
            _parse_model_response(json.dumps(payload, ensure_ascii=False))
        except SectionWriterError as exc:
            records.append(
                {"attack_id": attack_id, "status": "blocked", "reason": str(exc)}
            )
        else:
            records.append({"attack_id": attack_id, "status": "pass", "reason": None})
    return records


def audit_guarded_section_writer_units(
    inventory_path: str | Path,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for section_id in guarded_unit_ids(inventory_path):
        envelope = _envelope(section_id)
        baseline = _baseline(section_id, envelope)
        baseline_outcome = _outcome(section_id, envelope, baseline)
        attacks = []
        for attack_id, paragraphs in _attacks(section_id, envelope, baseline).items():
            outcome = _outcome(section_id, envelope, paragraphs)
            attacks.append({"attack_id": attack_id, **outcome})
        records.append(
            {
                "section_id": section_id,
                "runtime_wired": section_id in PLAYBOOK_FILENAMES
                and section_id in SECTION_VARIANTS,
                "variant": envelope["data"]["variant"],
                "conditional_rule": EXPECTED_CONCLUSION_RULES.get(
                    (section_id, envelope["data"]["variant"])
                ),
                "baseline": baseline_outcome,
                "attack_count": len(attacks),
                "blocked_attack_count": sum(
                    item["status"] == "blocked" for item in attacks
                ),
                "raw_passes": [
                    item for item in attacks if item["status"] == "pass"
                ],
                "attacks": attacks,
            }
        )
    sentence_records = [
        item
        for item in records
        if item["conditional_rule"] is not None
    ]
    return {
        "schema_version": "1.0",
        "unit_count": len(records),
        "runtime_wired_count": sum(item["runtime_wired"] for item in records),
        "runtime_unwired_count": sum(not item["runtime_wired"] for item in records),
        "runtime_unwired_section_ids": [
            item["section_id"] for item in records if not item["runtime_wired"]
        ],
        "baseline_pass_count": sum(
            item["baseline"]["status"] == "pass" for item in records
        ),
        "attack_count": sum(item["attack_count"] for item in records),
        "blocked_attack_count": sum(
            item["blocked_attack_count"] for item in records
        ),
        "raw_pass_count": sum(len(item["raw_passes"]) for item in records),
        "sentence_driven": {
            "unit_count": len(sentence_records),
            "section_ids": [item["section_id"] for item in sentence_records],
            "arbitrary_prose_passed_section_ids": [
                item["section_id"]
                for item in sentence_records
                if any(
                    attack["attack_id"] == "sentence_driven_arbitrary_prose"
                    and attack["status"] == "pass"
                    for attack in item["attacks"]
                )
            ],
            "partial_binding_blocked_section_ids": [
                item["section_id"]
                for item in sentence_records
                if any(
                    attack["attack_id"] == "sentence_driven_partial_binding"
                    and attack["status"] == "blocked"
                    for attack in item["attacks"]
                )
            ],
        },
        "parser_attacks": _parser_attack_results(),
        "records": records,
    }
