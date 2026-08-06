#!/usr/bin/env python3
"""Build fact-only legal rewrites without importing or reading guard policy code."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _values(fact: dict) -> list[str]:
    value = fact.get("value")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    gap = fact.get("fixed_gap_sentence")
    return [gap] if isinstance(gap, str) and gap else []


def _causality_sentence(value: str) -> str:
    if value == "未评价":
        return "研究者对该事件与研究药物的关系未作评价。"
    if value == "无法评价":
        return "研究者无法评价该事件与研究药物的关系。"
    return f"研究者判断该事件与研究药物{value}。"


def _fact_sentence(fact: dict, *, value_first: bool = False) -> str:
    values = _values(fact)
    if not values:
        raise ValueError(f"fact {fact['fact_id']} has no legal source surface")
    if fact.get("entity_type") == "causality":
        return _causality_sentence(values[0])
    if fact.get("value") is None:
        return values[0]
    label = str(fact["label"])
    if len(values) > 1:
        joined = "、".join(values[:-1]) + "和" + values[-1]
        return f"{joined}均列为{label}。" if value_first else f"{label}包括{joined}。"
    value = values[0]
    if value.endswith(("。", "！", "？")):
        return value
    if len(value) >= 13:
        return value + "。"
    return f"{value}作为{label}。" if value_first else f"{label}为{value}。"


def _paragraph(texts: list[str], facts: list[dict]) -> dict:
    return {"text": "".join(texts), "fact_ids": [fact["fact_id"] for fact in facts]}


def _specific_natural_sentences(section_id: str, facts: list[dict]) -> list[str]:
    by_id = {fact["fact_id"]: fact for fact in facts}
    default = [_fact_sentence(fact) for fact in facts]
    replacements: dict[str, str] = {}
    if section_id == "9.4.3":
        replacements = {
            "V01": "本研究采用中央随机化。",
            "V02": "分配比为等比例。",
            "V03": "研究中心和基线严重程度均为分层因素。",
        }
    elif section_id == "9.4.6":
        replacements = {
            "V01": "本研究为双盲设计。",
            "V02": "受试者、研究者和疗效评价人员均为设盲对象。",
        }
    elif section_id == "9.5.3":
        replacements = {"V05": "FAS作为分析集。"}
    elif section_id == "9.1":
        replacements = {
            "V01": "本研究为随机、双盲、安慰剂对照研究。",
            "V02": "研究采用平行组设计。",
        }
    elif section_id == "9.2":
        replacements = {
            "V01": "本研究采用安慰剂对照。",
            "V02": "研究对象为目标适应证成人受试者。",
        }
    elif section_id == "9.4.1":
        replacements = {"V06": "研究治疗经皮肤局部外用。"}
    elif section_id == "6":
        replacements = {"V01": "关于总体管理职责，申办方负责研究总体管理。"}
    elif section_id == "7":
        replacements = {
            "V01": "研究适应证为目标皮肤病。",
            "V02": "研究产品为脱敏研究制剂。",
        }
    return [
        replacements.get(fact["fact_id"], default[index])
        for index, fact in enumerate(facts)
        if fact["fact_id"] in by_id
    ]


def _cases_for_card(card: dict, path: str) -> list[dict]:
    facts = list(card["facts"])
    fixed_facts = (
        [fact for fact in facts if fact.get("value") is None]
        if path == "sae_exact_surface"
        else []
    )
    mutable_facts = [fact for fact in facts if fact not in fixed_facts]
    section_id = str(card["section_id"])
    ordered = [_fact_sentence(fact) for fact in mutable_facts]
    value_first = [_fact_sentence(fact, value_first=True) for fact in mutable_facts]
    specific = _specific_natural_sentences(section_id, mutable_facts)
    punctuation = "".join(
        sentence
        if fact.get("entity_type") == "causality"
        or any(value.endswith(("。", "！", "？")) for value in _values(fact))
        else sentence.rstrip("。")
        + ("；" if index < len(mutable_facts) - 1 else "。")
        for index, (sentence, fact) in enumerate(zip(ordered, mutable_facts))
    )
    fixed_paragraphs = [
        {"text": _fact_sentence(fact), "fact_ids": [fact["fact_id"]]}
        for fact in fixed_facts
    ]
    variants = [
        (
            "ordered_label_value",
            "语序调整",
            [_paragraph(ordered, mutable_facts), *fixed_paragraphs],
        ),
        (
            "reverse_order",
            "语序调整",
            [
                _paragraph(
                    list(reversed(ordered)), list(reversed(mutable_facts))
                ),
                *fixed_paragraphs,
            ],
        ),
        (
            "one_fact_per_paragraph",
            "分段",
            [
                {"text": sentence, "fact_ids": [fact["fact_id"]]}
                for sentence, fact in zip(ordered, mutable_facts)
            ]
            + fixed_paragraphs,
        ),
        (
            "paired_paragraphs",
            "合段",
            [
                _paragraph(
                    ordered[index : index + 2],
                    mutable_facts[index : index + 2],
                )
                for index in range(0, len(mutable_facts), 2)
            ]
            + fixed_paragraphs,
        ),
        (
            "value_before_label",
            "主被动与句式变换",
            [_paragraph(value_first, mutable_facts), *fixed_paragraphs],
        ),
        (
            "section_specific_natural",
            "同义句式",
            [_paragraph(specific, mutable_facts), *fixed_paragraphs],
        ),
        (
            "punctuation_variant",
            "标点变体",
            [_paragraph([punctuation], mutable_facts), *fixed_paragraphs],
        ),
    ]
    return [
        {
            "case_id": f"HOLDOUT-{card['card_id']}-{case_id}",
            "path": path,
            "card_id": card["card_id"],
            "section_id": section_id,
            "transformation": transformation,
            "paragraphs": paragraphs,
            "legality_basis": (
                "仅重排该事实卡的完整字段值、完整标签和固定缺口句；"
                "未读取 authorized_surface_forms 或既有成文产物。"
            ),
        }
        for case_id, transformation, paragraphs in variants
    ]


def _validate_fact_only(case: dict, card: dict) -> None:
    facts = {fact["fact_id"]: fact for fact in card["facts"]}
    claimed: set[str] = set()
    body = "\n".join(paragraph["text"] for paragraph in case["paragraphs"])
    for paragraph in case["paragraphs"]:
        ids = paragraph["fact_ids"]
        if not ids or any(fact_id not in facts for fact_id in ids):
            raise ValueError(f"{case['case_id']} has an invalid fact_id")
        claimed.update(ids)
        for fact_id in ids:
            for value in _values(facts[fact_id]):
                if value not in paragraph["text"] and value not in body:
                    raise ValueError(
                        f"{case['case_id']} does not preserve complete value {fact_id}"
                    )
    if claimed != set(facts):
        raise ValueError(f"{case['case_id']} does not cover the complete fact card")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guard-bundle-sha256", required=True)
    parser.add_argument(
        "--method-card-dir",
        type=Path,
        default=ROOT / "examples/output/m19/fact_cards/method",
    )
    parser.add_argument(
        "--sae-card-dir",
        type=Path,
        default=ROOT / "examples/output/m19/fact_cards/sae_holdout_views",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "examples/output/m19/holdout/legal_rewrites.json",
    )
    args = parser.parse_args()
    if len(args.guard_bundle_sha256) != 64:
        raise ValueError("guard bundle SHA-256 is required as an opaque freeze marker")
    cards: dict[str, dict] = {}
    card_files = sorted(args.method_card_dir.glob("*.json")) + sorted(
        args.sae_card_dir.glob("*.json")
    )
    for path in card_files:
        card = json.loads(path.read_text(encoding="utf-8"))
        if card.get("authorized_surface_forms") != []:
            raise ValueError("holdout construction may only read surface-free fact views")
        cards[card["card_id"]] = card
    cases = []
    for card in cards.values():
        path = "sae_exact_surface" if card["section_id"] == "12.3.2" else "method_label_bound"
        for case in _cases_for_card(card, path):
            _validate_fact_only(case, card)
            cases.append(case)
    script_path = Path(__file__).resolve()
    payload = {
        "schema_version": "1.0",
        "status": "authored_after_guard_policy_freeze",
        "guard_bundle_sha256_opaque_marker": args.guard_bundle_sha256,
        "construction": {
            "builder_path": str(script_path.relative_to(ROOT)),
            "builder_sha256": _sha256(script_path),
            "guard_module_imported": False,
            "guard_configuration_read": False,
            "authorized_surface_forms_read": False,
            "existing_narrative_outputs_read": False,
            "input_scope": "surface-free fact cards only",
            "transformations": [
                "语序调整",
                "分段",
                "合段",
                "主被动与句式变换",
                "同义句式",
                "标点变体",
            ],
            "card_files": [
                {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path)}
                for path in card_files
            ],
        },
        "case_count": len(cases),
        "method_case_count": sum(case["path"] == "method_label_bound" for case in cases),
        "sae_case_count": sum(case["path"] == "sae_exact_surface" for case in cases),
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "case_count": len(cases),
                "method_case_count": payload["method_case_count"],
                "sae_case_count": payload["sae_case_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
