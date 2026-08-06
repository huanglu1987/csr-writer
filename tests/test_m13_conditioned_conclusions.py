from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.conditional_conclusions import (
    ConditionalConclusionError,
    resolve_conditional_conclusions,
)
from csr_writer.cross_chapter import (
    CrossChapterError,
    ResolvedParagraph,
    ResolvedSection,
    derive_confirmed_body_binding_manifest,
)
from csr_writer.resolve_placeholders import resolve
from csr_writer.section_writer import SectionWriterError, draft_section


ROOT = Path(__file__).resolve().parents[1]

PK_KEYS = {
    "blq_ratio": "T14.2.4.3.1|POP=PKPS|ROW=BLQ_RATIO|COL=ACTIVE_TOTAL",
    "quantifiable_timepoint_count": "T14.2.4.3.1|POP=PKPS|ROW=QUANTIFIABLE_TIMEPOINT_COUNT|COL=ACTIVE_TOTAL",
    "cmax_calculable": "T14.2.4.3.1|POP=PKPS|ROW=CMAX_CALCULABLE|COL=ACTIVE_TOTAL",
    "auc_calculable": "T14.2.4.3.1|POP=PKPS|ROW=AUC_CALCULABLE|COL=ACTIVE_TOTAL",
    "lloq": "T14.2.4.3.1|POP=PKPS|ROW=LLOQ|COL=ACTIVE_TOTAL",
    "noncalculable_reason": "T14.2.4.3.1|POP=PKPS|ROW=NONCALCULABLE_REASON|COL=ACTIVE_TOTAL",
    "sampling_coverage_complete": "T14.2.4.3.1|POP=PKPS|ROW=SAMPLING_COVERAGE_COMPLETE|COL=ACTIVE_TOTAL",
}
EFFICACY_KEYS = {
    "primary_endpoint_result": "T14.2.2.1.1|POP=FAS|ROW=PRIMARY_ENDPOINT|COL=ACTIVE_TOTAL",
    "analysis_set": "T14.2.2.1.1|POP=FAS|ROW=ANALYSIS_SET|COL=ACTIVE_TOTAL",
    "prespecified": "T14.2.2.1.1|POP=FAS|ROW=PRESPECIFIED|COL=ACTIVE_TOTAL",
    "hypothesis_test": "T14.2.2.1.1|POP=FAS|ROW=HYPOTHESIS_TEST|COL=ACTIVE_TOTAL",
}
OVERALL_KEYS = {
    "primary_endpoint_result": EFFICACY_KEYS["primary_endpoint_result"],
    "pk_key_finding": PK_KEYS["cmax_calculable"],
    "teae_result": "T14.3.1.2|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL",
    "sae_count": "T14.3.1.2|POP=SS|ROW=ANY_SAE|COL=ACTIVE_TOTAL",
    "death_count": "T14.3.1.2|POP=SS|ROW=DEATH|COL=ACTIVE_TOTAL",
    "discontinuation_count": "T14.3.1.2|POP=SS|ROW=AE_DISCONTINUATION|COL=ACTIVE_TOTAL",
}


def _confirmed_manifest(keys: list[str]) -> dict:
    sources = [
        {
            "key": key,
            "section_id": "DEID-BODY",
            "para_id": f"p_{index:03d}",
            "resolved_sha256": hashlib.sha256(f"resolved-{index}".encode()).hexdigest(),
            "confirmed_text_sha256": hashlib.sha256(f"text-{index}".encode()).hexdigest(),
        }
        for index, key in enumerate(keys, start=1)
    ]
    body = {
        "schema_version": "1.0",
        "generation_mode": "confirmed_body_only",
        "keys": keys,
        "sources": sources,
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        **body,
        "manifest_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _request(section: str, *, omitted_roles: set[str] | None = None) -> dict:
    omitted = omitted_roles or set()
    if section == "11.5.10":
        keys = PK_KEYS
        variant = "pk_conclusion_conditioned"
        source = {
            "source_id": "SRC-PK",
            "locator": "脱敏方案/PK评价",
            "purpose": "pk_method",
            "text": "PK 结论应结合可定量性、参数可计算性和采样覆盖。",
        }
    elif section == "11.6.7":
        keys = EFFICACY_KEYS
        variant = "exploratory_efficacy_conclusion_conditioned"
        source = {
            "source_id": "SRC-EFF",
            "locator": "脱敏方案/探索性终点",
            "purpose": "endpoint_definition",
            "text": "该终点用于探索性评价。",
        }
    elif section == "13":
        keys = OVERALL_KEYS
        variant = "overall_conclusion_conditioned"
        source = {
            "source_id": "SRC-BODY",
            "locator": "已确认正文/结果节",
            "purpose": "confirmed_body_excerpt",
            "text": "本节仅综合已确认正文中的主要终点、PK 和安全性结果。",
        }
    else:  # pragma: no cover - test helper contract
        raise AssertionError(section)
    bindings = [
        {
            "role": role,
            "key": key,
            "source_id": key.split("|", 1)[0],
            "context": "脱敏合成已确认正文结果",
        }
        for role, key in keys.items()
        if role not in omitted
    ]
    request = {
        "schema_version": "1.1",
        "section_id": section,
        "variant": variant,
        "evidence_status": "demo_only",
        "tfl_bindings": bindings,
        "tfl_references": list(
            dict.fromkeys(item["source_id"] for item in bindings)
        ),
        "source_fragments": [source],
    }
    if section == "13":
        request["confirmed_body_binding_manifest"] = _confirmed_manifest(
            [item["key"] for item in bindings]
        )
    return request


def _cell(key: str, display: str, cell_type: str, index: int, **extra) -> dict:
    row = key.split("ROW=", 1)[1].split("|", 1)[0]
    col = key.split("COL=", 1)[1].split("|", 1)[0]
    return {
        "v": display,
        "display_value": display,
        "type": cell_type,
        "row": {"id": row, "label": row},
        "column": {"id": col, "label": col},
        "source_locator": {
            "source_id": "sha256:" + "a" * 64,
            "row_index": index,
            "column_index": 2,
        },
        "dimensions": {},
        **extra,
    }


def _datapack(
    *,
    pk_limited: bool = False,
    efficacy_hypothesis_test: bool = False,
    overall_events: bool = False,
    omit_keys: set[str] | None = None,
) -> dict:
    omitted = omit_keys or set()
    values = {
        PK_KEYS["blq_ratio"]: _cell(
            PK_KEYS["blq_ratio"],
            "68.0%" if pk_limited else "18.0%",
            "ratio",
            1,
            numeric_value=68.0 if pk_limited else 18.0,
        ),
        PK_KEYS["quantifiable_timepoint_count"]: _cell(
            PK_KEYS["quantifiable_timepoint_count"], "8", "count", 2, numeric_value=8
        ),
        PK_KEYS["cmax_calculable"]: _cell(
            PK_KEYS["cmax_calculable"],
            "不可计算" if pk_limited else "可计算",
            "text_judgement",
            3,
            boolean_value=not pk_limited,
        ),
        PK_KEYS["auc_calculable"]: _cell(
            PK_KEYS["auc_calculable"],
            "不可计算" if pk_limited else "可计算",
            "text_judgement",
            4,
            boolean_value=not pk_limited,
        ),
        PK_KEYS["lloq"]: _cell(PK_KEYS["lloq"], "0.1 ng/mL", "text", 5),
        PK_KEYS["noncalculable_reason"]: _cell(
            PK_KEYS["noncalculable_reason"],
            "可定量浓度不足" if pk_limited else "不适用",
            "text_judgement",
            6,
        ),
        PK_KEYS["sampling_coverage_complete"]: _cell(
            PK_KEYS["sampling_coverage_complete"],
            "不完整" if pk_limited else "完整",
            "text_judgement",
            7,
            boolean_value=not pk_limited,
        ),
        EFFICACY_KEYS["primary_endpoint_result"]: _cell(
            EFFICACY_KEYS["primary_endpoint_result"], "24 (60.0%)", "n_pct", 8
        ),
        EFFICACY_KEYS["analysis_set"]: _cell(
            EFFICACY_KEYS["analysis_set"], "FAS", "text", 9
        ),
        EFFICACY_KEYS["prespecified"]: _cell(
            EFFICACY_KEYS["prespecified"], "预设", "text_judgement", 10, boolean_value=True
        ),
        EFFICACY_KEYS["hypothesis_test"]: _cell(
            EFFICACY_KEYS["hypothesis_test"],
            "已实施" if efficacy_hypothesis_test else "未实施",
            "text_judgement",
            11,
            boolean_value=efficacy_hypothesis_test,
        ),
        OVERALL_KEYS["teae_result"]: _cell(
            OVERALL_KEYS["teae_result"], "33 (82.5%)", "n_pct", 12
        ),
        OVERALL_KEYS["sae_count"]: _cell(
            OVERALL_KEYS["sae_count"], "1" if overall_events else "0", "count", 13,
            numeric_value=1 if overall_events else 0,
        ),
        OVERALL_KEYS["death_count"]: _cell(
            OVERALL_KEYS["death_count"], "0", "count", 14, numeric_value=0
        ),
        OVERALL_KEYS["discontinuation_count"]: _cell(
            OVERALL_KEYS["discontinuation_count"],
            "2" if overall_events else "0",
            "count",
            15,
            numeric_value=2 if overall_events else 0,
        ),
    }
    cells = {key: value for key, value in values.items() if key not in omitted}
    source_id = "sha256:" + "a" * 64
    table_specs = (
        ("T14.2.2.1.1", "FAS"),
        ("T14.2.4.3.1", "PKPS"),
        ("T14.3.1.2", "SS"),
    )
    return {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "mapping_manifest": {"filename": "m13-synthetic.json", "sha256": "b" * 64},
        "sources": [{"source_id": source_id}],
        "tables": [
            {
                "table_id": table_id,
                "population": population,
                "source_id": source_id,
                "ingest_strategy": "cell_level",
                "dimensions": {},
            }
            for table_id, population in table_specs
        ],
        "cells": cells,
    }


class M13ConditionedConclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol_facts = json.loads(
            (ROOT / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8")
        )

    def _draft(self, section: str, *, omitted_roles: set[str] | None = None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return draft_section(_request(section, omitted_roles=omitted_roles), temporary.name)

    def _condition_and_resolve(self, draft, datapack):
        conditioned = resolve_conditional_conclusions(
            draft.markdown, datapack, draft.metadata
        )
        resolved = resolve(
            conditioned.resolved_text,
            datapack,
            self.protocol_facts,
            binding_manifest=draft.metadata,
        )
        self.assertEqual("pass", resolved.status, resolved.report)
        return conditioned, resolved.resolved_text

    def test_pk_conclusion_normal_limited_and_missing(self) -> None:
        normal, normal_text = self._condition_and_resolve(
            self._draft("11.5.10"), _datapack()
        )
        self.assertEqual("exposure_characterizable", normal.report["rules"][0]["branch"])
        self.assertIn("可用于描述观察到的系统暴露特征", normal_text)

        limited, limited_text = self._condition_and_resolve(
            self._draft("11.5.10"), _datapack(pk_limited=True)
        )
        self.assertEqual("quantifiability_limited", limited.report["rules"][0]["branch"])
        self.assertIn("不得据此表述为“系统暴露低”", limited_text)

        missing_role = "sampling_coverage_complete"
        missing, missing_text = self._condition_and_resolve(
            self._draft("11.5.10", omitted_roles={missing_role}),
            _datapack(omit_keys={PK_KEYS[missing_role]}),
        )
        self.assertEqual("critical_binding_missing", missing.report["rules"][0]["branch"])
        self.assertIn("当前不作系统暴露判断", missing_text)

    def test_exploratory_efficacy_normal_hypothesis_and_missing(self) -> None:
        normal, normal_text = self._condition_and_resolve(
            self._draft("11.6.7"), _datapack()
        )
        self.assertEqual("prespecified_descriptive", normal.report["rules"][0]["branch"])
        self.assertIn("不用于确证性结论", normal_text)

        tested, tested_text = self._condition_and_resolve(
            self._draft("11.6.7"), _datapack(efficacy_hypothesis_test=True)
        )
        self.assertEqual("prespecified_hypothesis_test", tested.report["rules"][0]["branch"])
        self.assertNotIn("疗效优于", tested_text)

        missing_role = "hypothesis_test"
        missing, missing_text = self._condition_and_resolve(
            self._draft("11.6.7", omitted_roles={missing_role}),
            _datapack(omit_keys={EFFICACY_KEYS[missing_role]}),
        )
        self.assertEqual("critical_binding_missing", missing.report["rules"][0]["branch"])
        self.assertIn("当前不作疗效判断", missing_text)

    def test_overall_conclusion_normal_events_and_missing(self) -> None:
        normal, normal_text = self._condition_and_resolve(
            self._draft("13"), _datapack()
        )
        self.assertEqual(
            "confirmed_body_complete_no_critical_events",
            normal.report["rules"][0]["branch"],
        )
        self.assertIn("总体获益风险判断仍待医学负责人确认", normal_text)

        events, events_text = self._condition_and_resolve(
            self._draft("13"), _datapack(overall_events=True)
        )
        self.assertEqual("important_safety_events_present", events.report["rules"][0]["branch"])
        self.assertIn("不作有利的获益风险表述", events_text)

        missing_role = "pk_key_finding"
        missing, missing_text = self._condition_and_resolve(
            self._draft("13", omitted_roles={missing_role}),
            _datapack(omit_keys={OVERALL_KEYS[missing_role]}),
        )
        self.assertEqual("critical_binding_missing", missing.report["rules"][0]["branch"])
        self.assertIn("当前不作总体获益风险判断", missing_text)

    def test_section_13_cannot_add_a_key_absent_from_confirmed_body(self) -> None:
        request = _request("13")
        request["confirmed_body_binding_manifest"] = _confirmed_manifest(
            request["confirmed_body_binding_manifest"]["keys"][:-1]
        )
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(SectionWriterError):
            draft_section(request, temporary)

    def test_tampered_confirmed_body_manifest_is_rejected_at_expansion(self) -> None:
        draft = self._draft("13")
        metadata = copy.deepcopy(draft.metadata)
        metadata["confirmed_body_binding_manifest"]["manifest_sha256"] = "0" * 64
        with self.assertRaises(ConditionalConclusionError):
            resolve_conditional_conclusions(draft.markdown, _datapack(), metadata)

    def test_confirmed_body_manifest_is_derived_from_reviewed_paragraphs(self) -> None:
        key = OVERALL_KEYS["primary_endpoint_result"]
        paragraph = ResolvedParagraph(
            section_id="11.6.7",
            para_id="p_11_6_7_001",
            text="主要终点结果已确认。",
            needs_human=False,
            confidence="derived",
            sources=("T14.2.2.1.1",),
            placeholders=(key,),
            references=("T14.2.2.1.1",),
        )
        section = ResolvedSection(
            section_id="11.6.7",
            heading="探索性疗效结论",
            notices=(),
            evidence_status="demo_only",
            draft_sha256="a" * 64,
            resolved_sha256="b" * 64,
            metadata_path=Path("11.6.7.meta.json"),
            reference_entities=("T14.2.2.1.1",),
            paragraphs=(paragraph,),
        )
        manifest = derive_confirmed_body_binding_manifest(
            [section], {"11.6.7": ("主要终点结果已确认。",)}, [key]
        )
        self.assertEqual("confirmed_body_only", manifest["generation_mode"])
        self.assertEqual("p_11_6_7_001", manifest["sources"][0]["para_id"])
        with self.assertRaises(CrossChapterError):
            derive_confirmed_body_binding_manifest(
                [section], {"11.6.7": ("主要终点结果已确认。",)}, ["absent-key"]
            )


if __name__ == "__main__":
    unittest.main()
