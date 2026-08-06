from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from csr_writer.datapack import build_datapack, build_datapack_to_files, validate_datapack
from csr_writer.errors import DatapackError, RTFParseError, SemanticMappingError
from csr_writer.io_utils import load_yaml, sha256_file


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PLUGIN_ROOT / "examples" / "demo_tfl_manifest.yaml"
EFFICACY_FIXTURE = PLUGIN_ROOT.parent / "参考实现" / "fixture_t_14_2_1.rtf"
PK_FIXTURE = PLUGIN_ROOT.parent / "参考实现" / "fixture_t_pk_nca.rtf"


class M2DatapackTests(unittest.TestCase):
    def test_two_fixtures_build_reviewed_datapack(self):
        before = {path: sha256_file(path) for path in (EFFICACY_FIXTURE, PK_FIXTURE)}
        datapack, parsed = build_datapack(MANIFEST)
        after = {path: sha256_file(path) for path in (EFFICACY_FIXTURE, PK_FIXTURE)}
        self.assertEqual(before, after)
        self.assertEqual(sha256_file(MANIFEST), datapack["mapping_manifest"]["sha256"])
        self.assertEqual(datapack["mapping_manifest"], parsed["mapping_manifest"])
        self.assertEqual(2, len(datapack["tables"]))
        self.assertEqual(37, len(datapack["cells"]))
        self.assertEqual(2, len(parsed["tables"][0]["footnotes"]))
        self.assertEqual("t_orr.sas", parsed["tables"][0]["trailer"]["program"])
        self.assertEqual(2, len(parsed["tables"][1]["footnotes"]))
        self.assertEqual("t_pk_nca.sas", parsed["tables"][1]["trailer"]["program"])
        self.assertEqual(
            0,
            datapack["cells"][
                "T14.2.1|POP=FAS|ROW=OBJECTIVE_RESPONSE_CR_PR|COL=XYZ200MG"
            ]["row"]["indent"],
        )
        self.assertEqual(
            2,
            datapack["cells"]["T14.2.1|POP=FAS|ROW=95_CI|COL=XYZ200MG"]["row"]["indent"],
        )
        unit_cells = [cell for cell in datapack["cells"].values() if cell["type"] == "unit"]
        self.assertEqual(6, len(unit_cells))
        self.assertEqual(
            "unit",
            datapack["cells"]["T14.4.1.1|POP=PKAS|ROW=CMAX|COL=UNIT"]["type"],
        )
        self.assertIn("T14.2.1|POP=FAS|ROW=P_VALUE|COL=STAT", datapack["cells"])

    def test_json_output_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.json"
            second = Path(temporary) / "second.json"
            build_datapack_to_files(MANIFEST, first)
            build_datapack_to_files(MANIFEST, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_mapping_manifest_hash_anchor_is_required_by_schema(self):
        datapack, _ = build_datapack(MANIFEST)
        del datapack["mapping_manifest"]
        with self.assertRaisesRegex(DatapackError, "mapping_manifest"):
            validate_datapack(datapack)

    def test_semantic_row_collision_is_hard_failure(self):
        manifest = copy.deepcopy(load_yaml(MANIFEST))
        manifest["sources"][0]["path"] = str(EFFICACY_FIXTURE)
        manifest["sources"][1]["path"] = str(PK_FIXTURE)
        manifest["sources"][0]["mapping"]["rows"]["Partial Response (PR)"] = "COMPLETE_RESPONSE_CR"
        with tempfile.TemporaryDirectory() as temporary:
            bad_manifest = Path(temporary) / "collision.yaml"
            bad_manifest.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(SemanticMappingError):
                build_datapack(bad_manifest)

    def test_removed_reviewed_row_is_mapping_drift(self):
        manifest = copy.deepcopy(load_yaml(MANIFEST))
        manifest["sources"] = manifest["sources"][:1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = EFFICACY_FIXTURE.read_text(encoding="latin-1").splitlines(keepends=True)
            altered_lines: list[str] = []
            for line in lines:
                if "Partial Response (PR)" in line:
                    if altered_lines and altered_lines[-1].startswith("\\trowd"):
                        altered_lines.pop()
                    continue
                altered_lines.append(line)
            altered = root / "missing-row.rtf"
            altered.write_text("".join(altered_lines), encoding="latin-1")
            manifest["sources"][0]["path"] = str(altered)
            bad_manifest = root / "missing-row.yaml"
            bad_manifest.write_text(
                yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(SemanticMappingError, "Row mapping coverage drift"):
                build_datapack(bad_manifest)

    def test_ambiguous_merged_cell_with_continuation_text_is_rejected(self):
        manifest = copy.deepcopy(load_yaml(MANIFEST))
        manifest["sources"] = manifest["sources"][:1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = EFFICACY_FIXTURE.read_text(encoding="latin-1")
            raw = raw.replace(
                r"\trowd\trgaph108\cellx3000\cellx5500\cellx8000\cellx10500",
                r"\trowd\trgaph108\clmgf\cellx3000\clmrg\cellx5500\cellx8000\cellx10500",
                1,
            )
            altered = root / "merged.rtf"
            altered.write_text(raw, encoding="latin-1")
            manifest["sources"][0]["path"] = str(altered)
            bad_manifest = root / "merged.yaml"
            bad_manifest.write_text(
                yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(RTFParseError, "merged table cells"):
                build_datapack(bad_manifest)

    def test_concatenated_same_width_tables_are_rejected(self):
        manifest = copy.deepcopy(load_yaml(MANIFEST))
        manifest["sources"] = manifest["sources"][:1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            combined = root / "two-tables.rtf"
            combined.write_bytes(EFFICACY_FIXTURE.read_bytes() + EFFICACY_FIXTURE.read_bytes())
            manifest["sources"][0]["path"] = str(combined)
            bad_manifest = root / "two-tables.yaml"
            bad_manifest.write_text(
                yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(RTFParseError, "multiple RTF documents/tables"):
                build_datapack(bad_manifest)

    def test_unit_column_cannot_be_remapped_by_cell_override(self):
        manifest = copy.deepcopy(load_yaml(MANIFEST))
        manifest["sources"][0]["path"] = str(EFFICACY_FIXTURE)
        manifest["sources"][1]["path"] = str(PK_FIXTURE)
        manifest["sources"][1]["mapping"]["cell_column_overrides"]["CMAX|UNIT"] = "STAT"
        with tempfile.TemporaryDirectory() as temporary:
            bad_manifest = Path(temporary) / "unit-override.yaml"
            bad_manifest.write_text(
                yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(SemanticMappingError, "Physical UNIT column"):
                build_datapack(bad_manifest)


if __name__ == "__main__":
    unittest.main()
