from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.conditional_conclusions import resolve_conditional_conclusions
from csr_writer.cross_chapter import load_resolved_section
from csr_writer.io_utils import atomic_write_json, atomic_write_text
from csr_writer.resolve_placeholders import resolve
from csr_writer.section_writer import draft_section


KEYS = {
    "teae_result": "T14.3.1.2|POP=SS|ROW=ANY_TEAE|COL=ACTIVE_TOTAL",
    "trae_result": "T14.3.1.2|POP=SS|ROW=ANY_TRAE|COL=ACTIVE_TOTAL",
    "sae_count": "T14.3.1.2|POP=SS|ROW=ANY_SAE|COL=ACTIVE_TOTAL",
    "death_count": "T14.3.1.2|POP=SS|ROW=DEATH|COL=ACTIVE_TOTAL",
    "discontinuation_count": "T14.3.1.2|POP=SS|ROW=AE_DISCONTINUATION|COL=ACTIVE_TOTAL",
    "grade3plus_count": "T14.3.1.2|POP=SS|ROW=GRADE_3_PLUS|COL=ACTIVE_TOTAL",
}


def _request(*, complete: bool = True) -> dict:
    roles = list(KEYS) if complete else ["teae_result", "trae_result"]
    return {
        "schema_version": "1.1",
        "section_id": "12.2",
        "variant": "adverse_events_conditioned",
        "evidence_status": "demo_only",
        "tfl_bindings": [
            {
                "role": role,
                "key": KEYS[role],
                "source_id": "T14.3.1.2",
                "context": "脱敏合成安全性汇总单元格",
            }
            for role in roles
        ],
        "tfl_references": ["T14.3.1.2"],
        "source_fragments": [
            {
                "source_id": "SRC-SAFETY",
                "locator": "脱敏方案/安全性评价",
                "purpose": "safety_definition",
                "text": "不良事件按预设观察期、严重程度和记录类别汇总。",
            },
            {
                "source_id": "SRC-SAP",
                "locator": "脱敏统计分析计划/安全性分析集",
                "purpose": "analysis_set",
                "text": "安全性分析采用经审核的安全性分析集。",
            },
        ],
    }


def _datapack(counts: dict[str, int]) -> dict:
    source_id = "sha256:" + "a" * 64
    displays = {
        "teae_result": "33 (82.5%)",
        "trae_result": "12 (30.0%)",
        **{role: str(value) for role, value in counts.items()},
    }
    cells = {}
    for index, (role, display) in enumerate(displays.items(), start=1):
        row = KEYS[role].split("ROW=", 1)[1].split("|", 1)[0]
        cells[KEYS[role]] = {
            "v": display,
            "display_value": display,
            "numeric_value": counts.get(role),
            "type": "n_pct" if role in {"teae_result", "trae_result"} else "count",
            "row": {"id": row, "label": row},
            "column": {"id": "ACTIVE_TOTAL", "label": "ACTIVE_TOTAL"},
            "source_locator": {
                "source_id": source_id,
                "row_index": index,
                "column_index": 2,
            },
            "dimensions": {},
        }
    return {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "mapping_manifest": {"filename": "m12-synthetic.json", "sha256": "b" * 64},
        "sources": [{"source_id": source_id}],
        "tables": [
            {
                "table_id": "T14.3.1.2",
                "population": "SS",
                "source_id": source_id,
                "ingest_strategy": "cell_level",
                "dimensions": {},
            }
        ],
        "cells": cells,
    }


class M12ConditionedConclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.protocol_facts = json.loads(
            (root / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8")
        )

    def _draft(self, *, complete: bool = True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return draft_section(_request(complete=complete), temporary.name)

    def test_no_critical_events_allows_conditioned_tolerability_wording(self):
        draft = self._draft()
        datapack = _datapack(
            {
                "sae_count": 0,
                "death_count": 0,
                "discontinuation_count": 0,
                "grade3plus_count": 0,
            }
        )
        conditioned = resolve_conditional_conclusions(draft.markdown, datapack, draft.metadata)
        self.assertEqual("pass", conditioned.status)
        self.assertEqual("no_critical_events", conditioned.report["rules"][0]["branch"])
        self.assertIn("总体耐受性良好", conditioned.resolved_text)
        resolved = resolve(
            conditioned.resolved_text,
            datapack,
            self.protocol_facts,
            binding_manifest=draft.metadata,
        )
        self.assertEqual("pass", resolved.status)

    def test_sae_or_discontinuation_changes_wording_after_event_results(self):
        draft = self._draft()
        datapack = _datapack(
            {
                "sae_count": 1,
                "death_count": 0,
                "discontinuation_count": 2,
                "grade3plus_count": 1,
            }
        )
        conditioned = resolve_conditional_conclusions(draft.markdown, datapack, draft.metadata)
        self.assertEqual("important_events_present", conditioned.report["rules"][0]["branch"])
        self.assertIn("严重不良事件和死亡例数分别为{{", conditioned.resolved_text)
        self.assertIn("不作“总体耐受性良好”的概括", conditioned.resolved_text)

    def test_missing_critical_binding_suppresses_judgment_and_warns(self):
        draft = self._draft(complete=False)
        datapack = _datapack({})
        conditioned = resolve_conditional_conclusions(draft.markdown, datapack, draft.metadata)
        self.assertEqual("pass_with_warning", conditioned.status)
        self.assertEqual("critical_binding_missing", conditioned.report["rules"][0]["branch"])
        self.assertIn("数据到位前不作总体耐受性判断", conditioned.resolved_text)
        self.assertNotIn("总体耐受性良好", conditioned.resolved_text)
        self.assertEqual(1, len(conditioned.report["warnings"]))

    def test_ordinary_resolver_blocks_unexpanded_conclusion_token(self):
        draft = self._draft()
        datapack = _datapack(
            {
                "sae_count": 0,
                "death_count": 0,
                "discontinuation_count": 0,
                "grade3plus_count": 0,
            }
        )
        result = resolve(
            draft.markdown,
            datapack,
            self.protocol_facts,
            binding_manifest=draft.metadata,
        )
        self.assertEqual("blocked", result.status)
        self.assertEqual("fail", result.report["stages"]["B_residual"]["status"])

    def test_missing_datapack_cell_is_not_silently_treated_as_zero(self):
        draft = self._draft()
        datapack = _datapack(
            {
                "sae_count": 0,
                "death_count": 0,
                "discontinuation_count": 0,
                "grade3plus_count": 0,
            }
        )
        broken = copy.deepcopy(datapack)
        del broken["cells"][KEYS["sae_count"]]
        conditioned = resolve_conditional_conclusions(draft.markdown, broken, draft.metadata)
        self.assertEqual("critical_binding_missing", conditioned.report["rules"][0]["branch"])

    def test_conditioned_draft_remains_loadable_by_cross_chapter_assembly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = draft_section(_request(), root / "project")
            datapack = _datapack(
                {
                    "sae_count": 0,
                    "death_count": 0,
                    "discontinuation_count": 0,
                    "grade3plus_count": 0,
                }
            )
            conditioned = resolve_conditional_conclusions(
                draft.markdown, datapack, draft.metadata
            )
            resolved = resolve(
                conditioned.resolved_text,
                datapack,
                self.protocol_facts,
                binding_manifest=draft.metadata,
            )
            self.assertEqual(resolved.status, "pass")
            resolved_path = root / "resolved.md"
            guard_path = root / "guard.json"
            atomic_write_text(resolved_path, resolved.resolved_text or "")
            atomic_write_json(guard_path, resolved.report)
            loaded = load_resolved_section(
                draft.markdown_path,
                resolved_path,
                draft.metadata_path,
                guard_path,
            )
            self.assertEqual(loaded.section_id, "12.2")
            self.assertEqual(len(loaded.paragraphs), len(draft.metadata["paragraphs"]))


if __name__ == "__main__":
    unittest.main()
