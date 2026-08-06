from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.resolve_placeholders import resolve
from csr_writer.section_writer import REQUIRED_BINDING_ROLES, SectionWriterError, draft_section
from csr_writer.semantic_key import parse_semantic_cell_key


ROOT = Path(__file__).resolve().parents[1]
FACTS = json.loads((ROOT / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8"))


CASES = (
    ("9.4.3", "randomization", (), "randomization_method", ("T14.1.1.1",)),
    ("9.4.6", "blinding", (), "blinding_method", ("T14.1.1.1",)),
    (
        "10.1",
        "subject_disposition",
        (
            "T14.1.1.2|POP=FAS|ROW=RANDOMIZED|COL=TOTAL",
            "T14.1.1.2|POP=FAS|ROW=ANALYSIS_SET|COL=TOTAL",
        ),
        "disposition_definition",
        ("T14.1.1.2",),
    ),
    (
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
        "11.B",
        "topical_low_systemic_exposure",
        (
            "T14.2.4.3.1|POP=PKS|ROW=CMAX|COL=C0_5PCT_BID|STAGE=D1",
            "T14.2.4.3.2|POP=PKS|ROW=CMAX|COL=C0_5PCT_BID|STAGE=D28",
        ),
        "pk_method",
        ("T14.2.4.3.1", "T14.2.4.3.2"),
    ),
    (
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
        "11.B",
        "hepatic_impairment_pk",
        ("T14.2.4.8.1|POP=PKS|ROW=AUC_RATIO|COL=TOTAL",),
        "pk_method",
        ("T14.2.4.8.1",),
    ),
)


def _request(section_id, variant, keys, purpose, references):
    roles = REQUIRED_BINDING_ROLES[(section_id, variant)]
    bindings = [
        {
            "role": role,
            "key": key,
            "source_id": parse_semantic_cell_key(key).table_id,
            "context": "deidentified reviewed binding",
        }
        for role, key in zip(roles, keys, strict=True)
    ]
    payload = {
        "schema_version": "1.1",
        "section_id": section_id,
        "variant": variant,
        "evidence_status": "demo_only",
        "tfl_bindings": bindings,
        "tfl_references": list(references),
        "source_fragments": [
            {
                "source_id": "deidentified-source",
                "locator": "reviewed locator",
                "purpose": purpose,
                "text": "De-identified source content; no project identifiers.",
            }
        ],
    }
    if not bindings:
        payload["confirmed_tfl_map"] = {
            "frozen_sha256": "c" * 64,
            "entity_ids": list(references),
        }
    return payload


def _datapack(keys):
    if not keys:
        return json.loads((ROOT / "examples" / "output" / "datapack.json").read_text())
    source_id = "sha256:" + "d" * 64
    table_ids = list(dict.fromkeys(parse_semantic_cell_key(key).table_id for key in keys))
    tables = []
    cells = {}
    for table_id in table_ids:
        parsed_for_table = [parse_semantic_cell_key(key) for key in keys if key.startswith(table_id + "|")]
        first = parsed_for_table[0]
        tables.append(
            {
                "table_id": table_id,
                "population": first.population,
                "source_id": source_id,
                "titles": [],
                "footnotes": [],
                "trailer": {},
                "adapter": "deidentified-test",
                "ingest_strategy": "cell_level",
                "dimensions": first.extra_dimensions,
            }
        )
    for index, key in enumerate(keys, start=1):
        parsed = parse_semantic_cell_key(key)
        cells[key] = {
            "v": f"{index}.0",
            "display_value": f"{index}.0",
            "type": "statistic",
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
                "row_index": index,
                "column_index": 1,
            },
            "dimensions": parsed.extra_dimensions,
        }
    return {
        "schema_version": "1.0",
        "semantic_key_version": "2.0",
        "adapter_scope": "deidentified section coverage test",
        "mapping_manifest": {"filename": "deidentified.json", "sha256": "e" * 64},
        "sources": [{"source_id": source_id, "filename": "deidentified.rtf", "format": "test"}],
        "tables": tables,
        "cells": cells,
    }


class M5SectionCoverageTests(unittest.TestCase):
    def test_every_requested_section_drafts_and_resolves(self):
        for section_id, variant, keys, purpose, references in CASES:
            with self.subTest(section_id=section_id, variant=variant):
                request = _request(section_id, variant, keys, purpose, references)
                if section_id in {"9.4.3", "9.4.6", "10.1"}:
                    with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
                        SectionWriterError, "E-PRODUCTION-METHOD-WRITER-BLOCK"
                    ):
                        draft_section(request, temporary)
                    continue
                with tempfile.TemporaryDirectory() as temporary:
                    draft = draft_section(request, temporary)
                    frozen = (
                        {
                            "status": "frozen",
                            "entities": [
                                {"entity_id": entity_id} for entity_id in references
                            ],
                        }
                        if not keys
                        else None
                    )
                    outcome = resolve(
                        draft.markdown,
                        _datapack(keys),
                        FACTS,
                        binding_manifest=draft.metadata,
                        frozen_tfl_map=frozen,
                    )
                self.assertEqual("pass", outcome.status, outcome.report)
                self.assertEqual(len(keys), outcome.report["placeholder_count"])
                self.assertEqual(len(references), outcome.report["reference_count"])
                self.assertEqual(references, tuple(draft.metadata["reference_manifest"][index]["entity_id"] for index in range(len(references))))

    def test_source_gap_is_explicit_and_never_inferred(self):
        request = {
            "schema_version": "1.1",
            "section_id": "11.B",
            "variant": "food_effect",
            "evidence_status": "source_gap",
            "tfl_bindings": [],
            "tfl_references": [],
            "source_fragments": [
                {
                    "source_id": "deidentified-gap-record",
                    "locator": "reviewed locator",
                    "purpose": "gap_record",
                    "text": "Required source not supplied.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            draft = draft_section(request, temporary)
        self.assertIn("结果内容保持空缺", draft.markdown)
        self.assertIn("未进行推断", draft.markdown)
        self.assertTrue(draft.metadata["paragraphs"][0]["needs_human"])


if __name__ == "__main__":
    unittest.main()
