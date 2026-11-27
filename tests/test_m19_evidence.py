from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from csr_writer.io_utils import sha256_file


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "examples/output/m19"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class M19EvidenceTests(unittest.TestCase):
    def test_guard_bundle_was_frozen_before_independent_holdout(self) -> None:
        frozen = _load(OUTPUT / "guard_policy_frozen.json")
        corpus = _load(OUTPUT / "holdout/legal_rewrites.json")
        migrations = {
            (item["path"], item["from_sha256"]): item
            for path in (ROOT / "examples/output/m20/N4历史数字更正对照.json",
                         ROOT / "skills/csr-writer/references/production-methods-migrations.json")
            for item in _load(path)["registry_migrations"]
        }
        self.assertEqual(
            frozen["guard_bundle_sha256"],
            corpus["guard_bundle_sha256_opaque_marker"],
        )
        for item in frozen["frozen_components"]:
            current_sha256 = sha256_file(ROOT / item["path"])
            if current_sha256 == item["sha256"]:
                continue
            previous = item["sha256"]
            seen = set()
            while previous != current_sha256:
                self.assertNotIn(previous, seen, "registry migration cycle")
                seen.add(previous)
                migration = migrations.get((item["path"], previous))
                self.assertIsNotNone(migration, f"undeclared mutation of frozen component: {item['path']}")
                self.assertTrue(migration["reason"])
                self.assertTrue(migration["affected_units"])
                previous = migration["to_sha256"]
        builder = ROOT / corpus["construction"]["builder_path"]
        tree = ast.parse(builder.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertLessEqual(
            imported_roots,
            {"__future__", "argparse", "hashlib", "json", "pathlib"},
        )

    def test_holdout_rates_use_non_circular_denominators(self) -> None:
        report = _load(OUTPUT / "holdout/false_positive_results.json")
        method = report["paths"]["method_label_bound"]
        sae = report["paths"]["sae_exact_surface"]
        self.assertEqual(56, method["official_holdout_count"])
        self.assertGreater(method["pass_count"], 0)
        self.assertGreater(method["blocked_count"], 0)
        self.assertGreater(sae["official_holdout_count"], 0)
        self.assertEqual(sae["official_holdout_count"], sae["blocked_count"])
        for record in report["records"]:
            if record["official_holdout_eligible"]:
                self.assertEqual([], record["accidental_exact_reviewed_surface_overlap"])

    def test_adversary_retains_every_raw_pass_and_no_threshold(self) -> None:
        report = _load(OUTPUT / "adversary/automatic_adversary_summary.json")
        self.assertFalse(report["threshold_selected_by_implementation"])
        for item in report["paths"].values():
            self.assertEqual(item["raw_pass_count"], len(item["raw_passes"]))
            self.assertTrue(item["raw_passes_are_unfiltered"])
            self.assertLessEqual(len(item["top_10_readable_raw_passes"]), 10)

    def test_eight_method_units_have_releasable_surface_free_artifacts(self) -> None:
        summary = _load(OUTPUT / "M19_summary.json")
        self.assertEqual(8, summary["method_actual_prose_unit_count"])
        for section_id in ("6", "7", "9.1", "9.2", "9.4.1", "9.4.3", "9.4.6", "9.5.3"):
            meta = _load(OUTPUT / f"method_actual_prose/{section_id}.meta.json")
            self.assertEqual("pass", meta["status"])
            self.assertEqual(
                "method_whole_value_label_bound",
                meta["forward_coverage"]["guard_profile"],
            )

    def test_n5_f8_n7_and_guarded_writer_are_explicit(self) -> None:
        n5 = _load(OUTPUT / "N5_legacy_unspecified_population_migration.json")
        self.assertEqual(28, n5["before"]["unspecified_key_count"])
        self.assertEqual(0, n5["after"]["active_unspecified_key_count"])
        self.assertFalse(n5["release_eligible"])
        self.assertTrue(n5["source_unchanged"])

        f8 = _load(OUTPUT / "F8_dual_denominator.json")
        self.assertEqual((147, 250), (f8["result_side"]["matched"], f8["result_side"]["denominator"]))
        self.assertEqual((22, 364), (f8["frozen_shell_side"]["matched"], f8["frozen_shell_side"]["denominator"]))

        n7 = _load(OUTPUT / "adversary/N7_unfamiliar_card.json")
        self.assertTrue(n7["not_one_of_m18_nine_cards"])
        self.assertGreater(n7["candidate_count"], 0)

        guarded = _load(OUTPUT / "guarded_section_writer/attack_results.json")
        self.assertEqual(34, guarded["unit_count"])
        self.assertEqual(3, len(guarded["sentence_driven"]["arbitrary_prose_passed_section_ids"]))
        self.assertEqual(8, guarded["runtime_unwired_count"])

    def test_read_only_hash_audit_is_green(self) -> None:
        report = _load(OUTPUT / "source_hash_audit.json")
        self.assertEqual("pass", report["status"])
        self.assertFalse(report["real_clinical_files_accessed"])
        self.assertTrue(all(item["status"] == "pass" for item in report["records"]))


if __name__ == "__main__":
    unittest.main()
