#!/usr/bin/env python3
"""Prepare de-identified fact-only inputs after the M19 guard policy is frozen."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/output/m16/method_pilot/fact_cards"
SAE_SOURCE = ROOT / "examples/output/m16/sae_narratives/fact_cards"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(source_id: str, file_role: str, locator: str) -> dict:
    return {
        "source_id": source_id,
        "file_role": file_role,
        "version": "deidentified-synthetic-v1",
        "locator": locator,
        "content_sha256": _hash(source_id + "|" + file_role + "|" + locator),
    }


def _fact(fact_id: str, label: str, value: str, source_id: str, entity_type: str) -> dict:
    return {
        "fact_id": fact_id,
        "label": label,
        "zone": "method",
        "value": value,
        "value_kind": "verbatim_text",
        "source_kind": "protocol",
        "source_id": source_id,
        "source_locator": "脱敏合成来源定位",
        "required": True,
        "verbatim_required": True,
        "required_terms": [],
        "entity_type": entity_type,
        "missing_behavior": "hard_fail",
        "fixed_gap_sentence": None,
    }


def _policy() -> dict:
    return {
        "outside_fact_content": "hard_fail",
        "database_numeric_policy": "placeholders_only",
        "missing_required_fact": "hard_fail",
        "medical_review_marker_required": False,
        "controlled_causality_terms": [
            "明确相关",
            "很可能有关",
            "可能有关",
            "不太可能有关",
            "无关",
            "未评价",
            "无法评价",
        ],
    }


def _card_6() -> dict:
    protocol = _source("m19_protocol_admin", "protocol", "脱敏方案 研究管理章节")
    register = _source(
        "m19_role_register",
        "confirmed_site_and_role_register",
        "脱敏并经确认的职责登记",
    )
    return {
        "schema_version": "1.1",
        "card_id": "M19-METHOD-6-UNFAMILIAR",
        "section_id": "6",
        "title": "6 研究者和研究管理结构",
        "production_method": "extraction",
        "narration": "constrained_llm",
        "source_manifest": [protocol, register],
        "facts": [
            _fact("V01", "总体管理职责", "申办方负责研究总体管理", protocol["source_id"], "governance_role"),
            _fact("V02", "安全性医学审阅职责", "医学监查人员负责安全性医学审阅", register["source_id"], "medical_monitoring_role"),
            _fact("V03", "统计职责", "独立统计团队负责随机化与统计分析", register["source_id"], "statistics_role"),
            _fact("S01", "协作关系", "各职能依据经确认的职责分工开展研究管理", protocol["source_id"], "governance_relationship"),
        ],
        "authorized_surface_forms": [],
        "controlled_entities": [],
        "policy": _policy(),
    }


def _card_7() -> dict:
    protocol = _source("m19_protocol_intro", "protocol", "脱敏方案 研究背景章节")
    brochure = _source("m19_brochure", "investigator_brochure", "脱敏研究者手册相关章节")
    return {
        "schema_version": "1.1",
        "card_id": "M19-METHOD-7-UNFAMILIAR",
        "section_id": "7",
        "title": "7 简介",
        "production_method": "extraction",
        "narration": "constrained_llm",
        "source_manifest": [protocol, brochure],
        "facts": [
            _fact("V01", "研究适应证", "目标皮肤病", protocol["source_id"], "indication"),
            _fact("V02", "研究产品", "脱敏研究制剂", brochure["source_id"], "investigational_product"),
            _fact("V03", "研究背景限定", "现有治疗仍存在未满足的临床需求", protocol["source_id"], "rationale_scope"),
            _fact("S01", "开发背景", "本研究用于评价脱敏研究制剂在目标人群中的临床特征", protocol["source_id"], "development_context"),
            _fact("S02", "研究依据", "研究设计依据现有非临床和早期临床资料确定", brochure["source_id"], "study_rationale"),
        ],
        "authorized_surface_forms": [],
        "controlled_entities": [],
        "policy": _policy(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "examples/output/m19/fact_cards/method",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cards = {"6": _card_6(), "7": _card_7()}
    for section_id in ("9.1", "9.2", "9.4.1", "9.4.3", "9.4.6", "9.5.3"):
        card = json.loads((SOURCE / f"{section_id}.json").read_text(encoding="utf-8"))
        card["authorized_surface_forms"] = []
        cards[section_id] = card
    manifest = []
    for section_id, card in sorted(cards.items()):
        path = args.output_dir / f"{section_id}.json"
        text = json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(text, encoding="utf-8")
        manifest.append(
            {
                "section_id": section_id,
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "authorized_surface_form_count": 0,
            }
        )
    manifest_path = args.output_dir.parent / "method_card_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"schema_version": "1.0", "card_count": len(manifest), "cards": manifest},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sae_output = args.output_dir.parent / "sae_holdout_views"
    sae_output.mkdir(parents=True, exist_ok=True)
    sae_manifest = []
    for source in sorted(SAE_SOURCE.glob("SAE_*.json")):
        card = json.loads(source.read_text(encoding="utf-8"))
        card["authorized_surface_forms"] = []
        path = sae_output / source.name
        text = json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(text, encoding="utf-8")
        sae_manifest.append(
            {
                "card_id": card["card_id"],
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "authorized_surface_form_count": 0,
            }
        )
    sae_manifest_path = args.output_dir.parent / "sae_holdout_card_manifest.json"
    sae_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "card_count": len(sae_manifest),
                "cards": sae_manifest,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "method_card_count": len(manifest),
                "method_manifest": str(manifest_path),
                "sae_holdout_card_count": len(sae_manifest),
                "sae_manifest": str(sae_manifest_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
