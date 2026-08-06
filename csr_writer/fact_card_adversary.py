"""Deterministic fact-card adversary enumeration for forward-coverage guards.

The generator does not invent a hand-picked attack corpus.  It enumerates a
bounded grammar directly from the reviewed card: cross-field fragments,
polarity insertions, mutually exclusive cues, label/value swaps, and boundary
splices.  Every candidate is retained; callers may sort but must not suppress
the raw pass list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .constrained_narration import (
    _CJK_RUN_RE,
    _paragraph_forward_coverage,
    _value_strings,
    validate_fact_card,
)


NEGATION_AND_LIMITER_TERMS = ("不", "未", "无", "非", "除外", "以外", "反之")
SEMANTIC_OPPOSITES = (
    ("暂停", "继续"),
    ("纳入", "排除"),
    ("完成", "退出"),
    ("相关", "无关"),
    ("有", "无"),
    ("接受", "拒绝"),
)
_READABLE_CUES = (
    "既往有",
    "过敏史",
    "治疗",
    "用药",
    "事件",
    "检查",
    "严重程度",
    "研究治疗",
    "随机",
    "分配",
)
_WHOLE_VALUE_RELATION_FRAMES = (
    ("copula_wei", "{left}为{right}"),
    ("copula_shi", "{left}是{right}"),
    ("role_as", "{left}作为{right}"),
    ("coordination", "{left}与{right}"),
    ("contrast", "{left}但{right}"),
    ("sequence", "{left}后{right}"),
    ("study_adopts_both", "本研究采用{left}，并采用{right}"),
    ("assignment_by", "{left}由{right}"),
    ("purpose_for", "{left}用于{right}"),
    ("according_to", "{left}按{right}"),
    ("contains", "{left}包括{right}"),
)


@dataclass(frozen=True)
class AttackCandidate:
    strategy: str
    text: str
    fact_ids: tuple[str, ...]
    derivation: dict[str, Any]
    readability_score: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "text": self.text,
            "fact_ids": list(self.fact_ids),
            "derivation": self.derivation,
            "readability_score": self.readability_score,
        }


def _readability(text: str) -> int:
    score = 0
    if 5 <= len(text) <= 24:
        score += 4
    elif len(text) <= 36:
        score += 2
    score += sum(2 for cue in _READABLE_CUES if cue in text)
    if re.search(r"既往有.{1,8}史", text):
        score += 6
    if re.search(r"(?:接受|给予).{1,8}治疗", text):
        score += 4
    if re.search(r".{2,10}为.{1,10}", text):
        score += 2
    if text.startswith("本研究采用") and "，并采用" in text:
        score += 10
    if re.search(r".{2,18}(?:由|用于|包括).{2,24}", text):
        score += 5
    if re.search(r"(.)\1\1", text):
        score -= 3
    return score


def _fact_runs(card: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    records: list[tuple[str, str, str]] = []
    for fact in card["facts"]:
        for value in _value_strings(fact.get("value")):
            for run in _CJK_RUN_RE.findall(value):
                if len(run) >= 2:
                    records.append((str(fact["fact_id"]), "value", run))
        label = str(fact["label"])
        for run in _CJK_RUN_RE.findall(label):
            if len(run) >= 2:
                records.append((str(fact["fact_id"]), "label", run))
    return records


def _candidate(
    strategy: str,
    text: str,
    fact_ids: Sequence[str],
    derivation: Mapping[str, Any],
) -> AttackCandidate:
    return AttackCandidate(
        strategy=strategy,
        text=text,
        fact_ids=tuple(dict.fromkeys(fact_ids)),
        derivation=dict(derivation),
        readability_score=_readability(text),
    )


def enumerate_fact_card_attacks(card: Mapping[str, Any]) -> list[AttackCandidate]:
    """Enumerate the complete candidate set for the documented bounded grammar."""

    normalized = validate_fact_card(card)
    runs = _fact_runs(normalized)
    candidates: dict[tuple[str, str, tuple[str, ...]], AttackCandidate] = {}

    def add(item: AttackCandidate) -> None:
        known_texts = {
            value
            for fact in normalized["facts"]
            for value in _value_strings(fact.get("value"))
        } | {
            item["text"] for item in normalized.get("authorized_surface_forms", [])
        }
        if item.text and item.text not in known_texts:
            candidates[(item.strategy, item.text, item.fact_ids)] = item

    # A.prefix + B.middle + A.suffix catches semantically plausible in-field
    # frames populated with content taken from another field.
    value_runs = [item for item in runs if item[1] == "value" and len(item[2]) >= 4]
    for fact_a, _kind_a, value_a in value_runs:
        for fact_b, _kind_b, value_b in value_runs:
            if fact_a == fact_b:
                continue
            for prefix_len in range(2, min(4, len(value_a) - 1) + 1):
                for suffix_len in range(2, min(4, len(value_a) - prefix_len) + 1):
                    prefix = value_a[:prefix_len]
                    suffix = value_a[-suffix_len:]
                    for middle_len in range(2, min(4, len(value_b)) + 1):
                        for start in range(0, len(value_b) - middle_len + 1):
                            middle = value_b[start : start + middle_len]
                            text = prefix + middle + suffix
                            add(
                                _candidate(
                                    "cross_field_fragment_recombination",
                                    text,
                                    (fact_a, fact_b),
                                    {
                                        "frame_fact_id": fact_a,
                                        "insert_fact_id": fact_b,
                                        "parts": [prefix, middle, suffix],
                                    },
                                )
                            )

    for fact_id, kind, value in runs:
        if kind != "value":
            continue
        for term in NEGATION_AND_LIMITER_TERMS:
            for placement, text in (("prefix", term + value), ("suffix", value + term)):
                add(
                    _candidate(
                        "negation_or_limiter_insertion",
                        text,
                        (fact_id,),
                        {"source_value": value, "inserted": term, "placement": placement},
                    )
                )

    for left, right in SEMANTIC_OPPOSITES:
        left_values = [item for item in value_runs if left in item[2]]
        right_values = [item for item in value_runs if right in item[2]]
        for left_item in left_values:
            for right_item in right_values:
                if left_item[0] == right_item[0]:
                    continue
                add(
                    _candidate(
                        "semantic_reversal_pair",
                        left_item[2] + "但" + right_item[2],
                        (left_item[0], right_item[0]),
                        {"opposite_pair": [left, right]},
                    )
                )

    labels = [item for item in runs if item[1] == "label"]
    values = [item for item in runs if item[1] == "value"]
    for label_fact, _kind, label in labels:
        for value_fact, _value_kind, value in values:
            if label_fact == value_fact:
                continue
            add(
                _candidate(
                    "label_value_mismatch",
                    label + "为" + value,
                    (label_fact, value_fact),
                    {"label_fact_id": label_fact, "value_fact_id": value_fact},
                )
            )

    for fact_a, _kind_a, value_a in value_runs:
        for fact_b, _kind_b, value_b in value_runs:
            if fact_a == fact_b:
                continue
            for left_len in range(2, min(4, len(value_a)) + 1):
                for right_len in range(2, min(4, len(value_b)) + 1):
                    add(
                        _candidate(
                            "boundary_truncation_splice",
                            value_a[:left_len] + value_b[-right_len:],
                            (fact_a, fact_b),
                            {
                                "left_fact_id": fact_a,
                                "right_fact_id": fact_b,
                                "lengths": [left_len, right_len],
                            },
                        )
                    )

    return sorted(
        candidates.values(),
        key=lambda item: (-item.readability_score, item.strategy, item.text, item.fact_ids),
    )


def enumerate_whole_value_relation_attacks(
    card: Mapping[str, Any],
) -> list[AttackCandidate]:
    """Recombine complete cross-field values without consulting guard config.

    The M18 grammar was intentionally strong against substring authorization,
    but a whole-value guard needs an additional probe: two individually true
    values can still be joined into a false relation.  This corpus keeps both
    values intact and varies only their connective relation.
    """

    normalized = validate_fact_card(card)
    atoms: list[tuple[str, str]] = []
    for fact in normalized["facts"]:
        for value in _value_strings(fact.get("value")):
            if (
                value
                and "{{" not in value
                and len(value) <= 36
                and not value.endswith(("。", "！", "？", ";", "；"))
            ):
                atoms.append((str(fact["fact_id"]), value))

    known_texts = {
        value
        for fact in normalized["facts"]
        for value in _value_strings(fact.get("value"))
    } | {
        item["text"] for item in normalized.get("authorized_surface_forms", [])
    }
    candidates: dict[tuple[str, str, tuple[str, ...]], AttackCandidate] = {}
    for left_fact, left in atoms:
        for right_fact, right in atoms:
            if left_fact == right_fact:
                continue
            for relation, frame in _WHOLE_VALUE_RELATION_FRAMES:
                text = frame.format(left=left, right=right)
                if text in known_texts:
                    continue
                item = _candidate(
                    "whole_value_relation_recombination",
                    text,
                    (left_fact, right_fact),
                    {
                        "left_fact_id": left_fact,
                        "right_fact_id": right_fact,
                        "left_complete_value": left,
                        "right_complete_value": right,
                        "relation": relation,
                    },
                )
                candidates[(item.strategy, item.text, item.fact_ids)] = item
    return sorted(
        candidates.values(),
        key=lambda item: (-item.readability_score, item.text, item.fact_ids),
    )


def evaluate_attack_candidates(
    card: Mapping[str, Any],
    candidates: Sequence[AttackCandidate],
    *,
    coverage_mode: str,
) -> dict[str, Any]:
    """Return every candidate accepted by the selected coverage guard."""

    normalized = validate_fact_card(card)
    facts_by_id = {item["fact_id"]: item for item in normalized["facts"]}
    raw_passes: list[dict[str, Any]] = []
    strategy_counts: dict[str, int] = {}
    pass_counts: dict[str, int] = {}
    for candidate in candidates:
        strategy_counts[candidate.strategy] = strategy_counts.get(candidate.strategy, 0) + 1
        _checks, findings = _paragraph_forward_coverage(
            normalized,
            [{"text": candidate.text + "。", "fact_ids": list(candidate.fact_ids)}],
            facts_by_id,
            coverage_mode=coverage_mode,
        )
        if findings:
            continue
        raw_passes.append(candidate.as_dict())
        pass_counts[candidate.strategy] = pass_counts.get(candidate.strategy, 0) + 1
    return {
        "schema_version": "1.0",
        "coverage_mode": coverage_mode,
        "candidate_count": len(candidates),
        "strategy_candidate_counts": dict(sorted(strategy_counts.items())),
        "raw_pass_count": len(raw_passes),
        "strategy_raw_pass_counts": dict(sorted(pass_counts.items())),
        "raw_passes": raw_passes,
    }
