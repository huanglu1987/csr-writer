from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.tfl_map import (
    TFLMapDriftError,
    TFLMapError,
    build_tfl_map_candidate,
    check_result_against_frozen_map,
    freeze_tfl_map,
)
from csr_writer.cli import main


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "fixtures"


class TFLMapTests(unittest.TestCase):
    def setUp(self):
        self.shell = json.loads(
            (FIXTURES / "datapack_semantic_v2.deidentified.json").read_text(encoding="utf-8")
        )
        self.routes = json.loads(
            (FIXTURES / "tfl_route_config.deidentified.json").read_text(encoding="utf-8")
        )
        self.candidate = build_tfl_map_candidate(self.shell, self.routes)
        confirmation = json.loads(
            (FIXTURES / "tfl_map_confirmation.deidentified.json").read_text(encoding="utf-8")
        )
        confirmation["candidate_sha256"] = self.candidate["candidate_sha256"]
        self.frozen = freeze_tfl_map(self.candidate, confirmation)

    def test_candidate_contains_no_cell_values_and_requires_exact_routes(self):
        self.assertEqual("awaiting_human_confirmation", self.candidate["status"])
        self.assertNotIn("xx", json.dumps(self.candidate, ensure_ascii=False))
        self.assertEqual(6, len(self.candidate["entities"]))
        rogue = copy.deepcopy(self.routes)
        rogue["routes"].pop()
        with self.assertRaisesRegex(TFLMapError, "exactly cover"):
            build_tfl_map_candidate(self.shell, rogue)

    def test_freeze_is_bound_to_exact_candidate_and_role_not_person(self):
        self.assertEqual("frozen", self.frozen["status"])
        confirmation = copy.deepcopy(self.frozen["confirmation"])
        confirmation["candidate_sha256"] = "0" * 64
        with self.assertRaisesRegex(TFLMapError, "different candidate"):
            freeze_tfl_map(self.candidate, confirmation)

    def test_result_values_can_change_without_changing_frozen_keys(self):
        result = copy.deepcopy(self.shell)
        for cell in result["cells"].values():
            cell["v"] = "SYNTHETIC_RESULT"
            cell["display_value"] = "SYNTHETIC_RESULT"
        report = check_result_against_frozen_map(result, self.frozen)
        self.assertEqual("pass", report["status"])
        self.assertEqual("pass", report["checks"]["values_may_change"])

    def test_missing_key_type_change_and_added_dimension_are_drift(self):
        mutations = []
        missing = copy.deepcopy(self.shell)
        missing["cells"].pop(next(iter(missing["cells"])))
        mutations.append(missing)
        changed_type = copy.deepcopy(self.shell)
        changed_type["cells"][next(iter(changed_type["cells"]))]["type"] = "text"
        mutations.append(changed_type)
        for rogue in mutations:
            with self.subTest(kind=len(rogue["cells"])):
                with self.assertRaises(TFLMapDriftError) as caught:
                    check_result_against_frozen_map(rogue, self.frozen)
                self.assertEqual("blocked", caught.exception.report["status"])
                self.assertEqual("E-TFL-MAP-DRIFT", caught.exception.report["issues"][0]["code"])

    def test_numbering_system_is_configuration_not_ich_14x_code(self):
        alternate_shell = copy.deepcopy(self.shell)
        old = "T14.3.1.3.1"
        new = "T1.1.1"
        table = next(item for item in alternate_shell["tables"] if item["table_id"] == old)
        table["table_id"] = new
        alternate_shell["cells"] = {
            (new + key[len(old):] if key.startswith(old + "|") else key): value
            for key, value in alternate_shell["cells"].items()
        }
        alternate_routes = copy.deepcopy(self.routes)
        alternate_routes["numbering_system"] = {
            "id": "VENDOR_1X",
            "description": "Deidentified alternate vendor numbering",
        }
        route = next(item for item in alternate_routes["routes"] if item["entity_id"] == old)
        route["entity_id"] = new
        route["ich_e3_sections"] = ["12.2"]
        candidate = build_tfl_map_candidate(alternate_shell, alternate_routes)
        self.assertEqual("VENDOR_1X", candidate["numbering_system"]["id"])
        self.assertIn(new, {item["entity_id"] for item in candidate["entities"]})

    def test_cli_candidate_freeze_apply_and_drift_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_path = root / "candidate.json"
            self.assertEqual(
                0,
                main(
                    [
                        "tfl-map-candidate",
                        "--shell-datapack",
                        str(FIXTURES / "datapack_semantic_v2.deidentified.json"),
                        "--route-config",
                        str(FIXTURES / "tfl_route_config.deidentified.json"),
                        "--output",
                        str(candidate_path),
                    ]
                ),
            )
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            confirmation = json.loads(
                (FIXTURES / "tfl_map_confirmation.deidentified.json").read_text(encoding="utf-8")
            )
            confirmation["candidate_sha256"] = candidate["candidate_sha256"]
            confirmation_path = root / "confirmation.json"
            confirmation_path.write_text(
                json.dumps(confirmation, ensure_ascii=False), encoding="utf-8"
            )
            frozen_path = root / "frozen.json"
            self.assertEqual(
                0,
                main(
                    [
                        "tfl-map-freeze",
                        "--candidate",
                        str(candidate_path),
                        "--confirmation",
                        str(confirmation_path),
                        "--output",
                        str(frozen_path),
                    ]
                ),
            )
            result_path = root / "result.json"
            result_path.write_text(json.dumps(self.shell, ensure_ascii=False), encoding="utf-8")
            report_path = root / "apply.json"
            self.assertEqual(
                0,
                main(
                    [
                        "tfl-map-apply",
                        "--result-datapack",
                        str(result_path),
                        "--frozen-map",
                        str(frozen_path),
                        "--output",
                        str(report_path),
                    ]
                ),
            )
            rogue = copy.deepcopy(self.shell)
            rogue["cells"].pop(next(iter(rogue["cells"])))
            result_path.write_text(json.dumps(rogue, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                3,
                main(
                    [
                        "tfl-map-apply",
                        "--result-datapack",
                        str(result_path),
                        "--frozen-map",
                        str(frozen_path),
                        "--output",
                        str(report_path),
                    ]
                ),
            )
            self.assertEqual(
                "E-TFL-MAP-DRIFT",
                json.loads(report_path.read_text(encoding="utf-8"))["issues"][0]["code"],
            )


if __name__ == "__main__":
    unittest.main()
