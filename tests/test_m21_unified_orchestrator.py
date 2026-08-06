from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from csr_writer.deterministic_sections import (
    DeterministicGenerationError,
    _chapter14_bucket,
    validate_deterministic_bundle,
)
from csr_writer.io_utils import atomic_write_json
from csr_writer.unified_orchestrator import (
    UnifiedCompositionError,
    compose_unified_csr,
)


ROOT = Path(__file__).resolve().parents[1]
M20 = ROOT / "examples" / "output" / "m20" / "true_e2e_final"
FIXTURE = ROOT / "examples" / "fixtures" / "m20_unfamiliar"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class M21UnifiedOrchestratorTests(unittest.TestCase):
    def _arguments(self, root: Path) -> dict:
        context = _load(M20 / "14_deterministic" / "context.json")
        return {
            "routing": _load(M20 / "03_module_route.json"),
            "deterministic_bundle": _load(M20 / "14_deterministic" / "manifest.json"),
            "ordinary_artifacts": [
                {
                    "draft_path": M20 / "10_guarded_result/project/sections/12.3.3.md",
                    "resolved_path": M20 / "10_guarded_result/12.3.3.resolved.md",
                    "metadata_path": M20
                    / "10_guarded_result/project/sections/12.3.3.meta.json",
                    "guard_path": M20 / "10_guarded_result/resolve.pass.json",
                }
            ],
            "constrained_artifacts": [
                {
                    "markdown_path": M20 / "08_method_limited_free/9.1.md",
                    "metadata_path": M20 / "08_method_limited_free/9.1.meta.json",
                    "evidence_status": "demo_only",
                },
                {
                    "markdown_path": M20 / "09_sae_exact/12.3.2.md",
                    "metadata_path": M20 / "09_sae_exact/12.3.2.meta.json",
                    "evidence_status": "demo_only",
                },
            ],
            "review_decisions": _load(M20 / "12_review_decisions.synthetic.json"),
            "publication_reference_map": context["publication_reference_map"],
            "study_facts": _load(
                FIXTURE / "study_facts.single_arm_migraine.deidentified.json"
            ),
            "annotated_output": root / "unified.annotated.docx",
            "clean_output": root / "unified.clean.docx",
        }

    def test_complete_bundle_validation_binds_all_52_units(self) -> None:
        bundle = _load(M20 / "14_deterministic" / "manifest.json")
        report = validate_deterministic_bundle(bundle)
        self.assertEqual(52, report["unit_count"])
        self.assertFalse(report["model_called"])
        broken = copy.deepcopy(bundle)
        broken["artifacts"][0]["validation"]["status"] = "blocked"
        with self.assertRaisesRegex(
            DeterministicGenerationError,
            "E-DETERMINISTIC-BUNDLE-VALIDATION-BINDING",
        ):
            validate_deterministic_bundle(broken)

    def test_duplicate_validation_cannot_shadow_a_deterministic_unit(self) -> None:
        bundle = _load(M20 / "14_deterministic" / "manifest.json")
        broken = copy.deepcopy(bundle)
        broken["minimal_validations"][1] = copy.deepcopy(
            broken["minimal_validations"][0]
        )
        with self.assertRaisesRegex(
            DeterministicGenerationError,
            "E-DETERMINISTIC-BUNDLE-DUPLICATE-VALIDATION",
        ):
            validate_deterministic_bundle(broken)

    def test_nonstandard_result_table_requires_one_explicit_chapter14_owner(self) -> None:
        self.assertEqual("14.3.4", _chapter14_bucket("T14.3.9.1", ("12.4.3",)))
        with self.assertRaisesRegex(
            DeterministicGenerationError, "E-CHAPTER14-UNROUTED"
        ):
            _chapter14_bucket("T14.3.9.1", ("12",))

    def test_unguarded_constrained_artifact_cannot_use_composer_as_a_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._arguments(root)
            metadata = _load(M20 / "08_method_limited_free/9.1.meta.json")
            metadata["status"] = "blocked"
            tampered = root / "unguarded.meta.json"
            atomic_write_json(tampered, metadata)
            arguments["ordinary_artifacts"] = []
            arguments["constrained_artifacts"] = [
                {
                    "markdown_path": M20 / "08_method_limited_free/9.1.md",
                    "metadata_path": tampered,
                    "evidence_status": "demo_only",
                }
            ]
            with self.assertRaisesRegex(
                UnifiedCompositionError, "E-COMPOSE-GUARD-PROOF"
            ):
                compose_unified_csr(**arguments)

    def test_single_plan_renders_two_reproducible_full_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = compose_unified_csr(**self._arguments(root))
            annotated = Document(root / "unified.annotated.docx")
            clean = Document(root / "unified.clean.docx")
            annotated_text = "\n".join(item.text for item in annotated.paragraphs)
            clean_text = "\n".join(item.text for item in clean.paragraphs)
            clean_headings = {
                item.text
                for item in clean.paragraphs
                if item.style is not None and item.style.name.startswith("Heading")
            }

        self.assertEqual("pass", report["status"])
        self.assertTrue(report["reproducibility"]["annotated"]["byte_identical"])
        self.assertTrue(report["reproducibility"]["clean"]["byte_identical"])
        self.assertEqual(
            report["composition_plan"]["composition_plan_sha256"],
            report["outputs"]["annotated"]["ooxml_qc"][
                "composition_plan_sha256"
            ],
        )
        self.assertEqual(17, report["outputs"]["clean"]["render"]["omitted_official_heading_count"])
        self.assertEqual(6, report["outputs"]["clean"]["ooxml_qc"]["directory_word_field_count"])
        self.assertIn("本章以下小节因本研究设计不适用而未纳入", clean_text)
        self.assertFalse(any(item.startswith("9.4.3 ") for item in clean_headings))
        self.assertFalse(any(item.startswith("9.4.6 ") for item in clean_headings))
        self.assertIn("内部溯源", annotated_text)
        self.assertNotIn("内部溯源", clean_text)
        self.assertFalse(report["guard_bypass_available"])


if __name__ == "__main__":
    unittest.main()
