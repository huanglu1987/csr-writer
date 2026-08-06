from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from csr_writer.datapack import build_datapack_to_files
from csr_writer.io_utils import load_yaml, sha256_file


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PLUGIN_ROOT / "examples" / "demo_tfl_manifest.yaml"
DRAFT = PLUGIN_ROOT / "examples" / "demo_draft.md"
FACTS = PLUGIN_ROOT / "examples" / "demo_protocol_facts.json"
EFFICACY_FIXTURE = PLUGIN_ROOT.parent / "参考实现" / "fixture_t_14_2_1.rtf"
PK_FIXTURE = PLUGIN_ROOT.parent / "参考实现" / "fixture_t_pk_nca.rtf"


def run_cli(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "csr_writer", *(str(value) for value in arguments)],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class M2CLITests(unittest.TestCase):
    def test_resolve_reports_exact_input_file_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            datapack = root / "datapack.json"
            output = root / "resolved.txt"
            report = root / "guard.json"
            build_datapack_to_files(MANIFEST, datapack)

            completed = run_cli(
                "resolve",
                "--draft",
                DRAFT,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                output,
                "--report",
                report,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "draft": sha256_file(DRAFT),
                    "datapack": sha256_file(datapack),
                    "protocol_facts": sha256_file(FACTS),
                },
                payload["input_file_sha256"],
            )

    def test_guard_failure_archives_previous_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            datapack = root / "datapack.json"
            draft = root / "draft.md"
            output = root / "resolved.txt"
            report = root / "guard.json"
            build_datapack_to_files(MANIFEST, datapack)
            draft.write_text("共有7例发生 SAE。", encoding="utf-8")
            output.write_text("OLD RELEASE", encoding="utf-8")
            report.write_text("OLD REPORT", encoding="utf-8")

            completed = run_cli(
                "resolve",
                "--draft",
                draft,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                output,
                "--report",
                report,
            )

            self.assertEqual(2, completed.returncode, completed.stderr)
            self.assertFalse(output.exists())
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual("review_required", payload["status"])
            archived = payload["archived_previous_outputs"]
            old_output = Path(archived["resolved output"])
            old_report = Path(archived["guard report"])
            self.assertEqual("OLD RELEASE", old_output.read_text(encoding="utf-8"))
            self.assertEqual("OLD REPORT", old_report.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            datapack = root / "datapack.json"
            blocker = root / "not-a-directory"
            output = blocker / "resolved.txt"
            report = root / "guard.json"
            build_datapack_to_files(MANIFEST, datapack)
            blocker.write_text("BLOCKER", encoding="utf-8")

            completed = run_cli(
                "resolve",
                "--draft",
                DRAFT,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                output,
                "--report",
                report,
            )
            self.assertEqual(4, completed.returncode, completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            datapack = root / "datapack.json"
            output = root / "resolved.txt"
            report = root / ".resolved.txt.previous"
            build_datapack_to_files(MANIFEST, datapack)
            output.write_text("OLD RELEASE", encoding="utf-8")

            completed = run_cli(
                "resolve",
                "--draft",
                DRAFT,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                output,
                "--report",
                report,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            archived = Path(payload["archived_previous_outputs"]["resolved output"])
            self.assertEqual((root / ".resolved.txt.previous.1").resolve(), archived)
            self.assertEqual("OLD RELEASE", archived.read_text(encoding="utf-8"))

    def test_build_failure_archives_previous_outputs(self):
        manifest = copy.deepcopy(load_yaml(MANIFEST))
        manifest["sources"][0]["path"] = str(EFFICACY_FIXTURE)
        manifest["sources"][1]["path"] = str(PK_FIXTURE)
        manifest["sources"][0]["mapping"]["rows"]["Partial Response (PR)"] = (
            "COMPLETE_RESPONSE_CR"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "bad.yaml"
            output = root / "datapack.json"
            parsed = root / "parsed.json"
            manifest_path.write_text(
                yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
            )
            output.write_text("OLD DATAPACK", encoding="utf-8")
            parsed.write_text("OLD PARSED", encoding="utf-8")

            completed = run_cli(
                "build-datapack",
                "--manifest",
                manifest_path,
                "--output",
                output,
                "--parsed-output",
                parsed,
            )

            self.assertEqual(4, completed.returncode, completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(parsed.exists())
            self.assertEqual(
                "OLD DATAPACK",
                (root / ".datapack.json.previous").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "OLD PARSED",
                (root / ".parsed.json.previous").read_text(encoding="utf-8"),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "malformed.yaml"
            output = root / "datapack.json"
            manifest_path.write_text("sources: [\n", encoding="utf-8")
            output.write_text("OLD DATAPACK", encoding="utf-8")

            completed = run_cli(
                "build-datapack",
                "--manifest",
                manifest_path,
                "--output",
                output,
            )
            self.assertEqual(4, completed.returncode, completed.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(
                "OLD DATAPACK",
                (root / ".datapack.json.previous").read_text(encoding="utf-8"),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocker = root / "not-a-directory"
            output = root / "datapack.json"
            parsed = blocker / "parsed.json"
            blocker.write_text("BLOCKER", encoding="utf-8")

            completed = run_cli(
                "build-datapack",
                "--manifest",
                MANIFEST,
                "--output",
                output,
                "--parsed-output",
                parsed,
            )
            self.assertEqual(4, completed.returncode, completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(parsed.exists())

    def test_non_object_datapack_is_schema_exit_four_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            datapack = root / "bad.json"
            output = root / "resolved.txt"
            report = root / "guard.json"
            datapack.write_text("[]\n", encoding="utf-8")

            completed = run_cli(
                "resolve",
                "--draft",
                DRAFT,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                output,
                "--report",
                report,
            )

            self.assertEqual(4, completed.returncode)
            self.assertIn("Datapack JSON must be an object", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(report.exists())

    def test_resolve_rejects_output_input_and_output_output_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            datapack = root / "datapack.json"
            draft = root / "draft.md"
            report = root / "guard.json"
            build_datapack_to_files(MANIFEST, datapack)
            draft.write_text(DRAFT.read_text(encoding="utf-8"), encoding="utf-8")
            before = draft.read_bytes()

            input_collision = run_cli(
                "resolve",
                "--draft",
                draft,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                draft,
                "--report",
                report,
            )
            self.assertEqual(4, input_collision.returncode)
            self.assertEqual(before, draft.read_bytes())
            self.assertFalse(report.exists())

            shared_output = root / "shared.txt"
            shared_output.write_text("KEEP", encoding="utf-8")
            output_collision = run_cli(
                "resolve",
                "--draft",
                draft,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                shared_output,
                "--report",
                shared_output,
            )
            self.assertEqual(4, output_collision.returncode)
            self.assertEqual("KEEP", shared_output.read_text(encoding="utf-8"))

            case_output = root / "Result.txt"
            case_report = root / "result.txt"
            case_collision = run_cli(
                "resolve",
                "--draft",
                draft,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                case_output,
                "--report",
                case_report,
            )
            self.assertEqual(4, case_collision.returncode)
            self.assertFalse(case_output.exists())

            nfc_output = root / "Café.txt"
            nfd_report = root / "Café.txt"
            unicode_collision = run_cli(
                "resolve",
                "--draft",
                draft,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                nfc_output,
                "--report",
                nfd_report,
            )
            self.assertEqual(4, unicode_collision.returncode)
            self.assertFalse(nfc_output.exists())

            hardlink_output = root / "draft-hardlink.md"
            os.link(draft, hardlink_output)
            hardlink_collision = run_cli(
                "resolve",
                "--draft",
                draft,
                "--datapack",
                datapack,
                "--protocol-facts",
                FACTS,
                "--output",
                hardlink_output,
                "--report",
                report,
            )
            self.assertEqual(4, hardlink_collision.returncode)
            self.assertEqual(before, draft.read_bytes())

    def test_build_rejects_manifest_source_and_output_aliases(self):
        manifest_before = MANIFEST.read_bytes()
        fixture_before = EFFICACY_FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parsed = root / "parsed.json"

            manifest_collision = run_cli(
                "build-datapack",
                "--manifest",
                MANIFEST,
                "--output",
                MANIFEST,
            )
            self.assertEqual(4, manifest_collision.returncode)
            self.assertEqual(manifest_before, MANIFEST.read_bytes())

            source_collision = run_cli(
                "build-datapack",
                "--manifest",
                MANIFEST,
                "--output",
                EFFICACY_FIXTURE,
            )
            self.assertEqual(4, source_collision.returncode)
            self.assertEqual(fixture_before, EFFICACY_FIXTURE.read_bytes())

            output_collision = run_cli(
                "build-datapack",
                "--manifest",
                MANIFEST,
                "--output",
                parsed,
                "--parsed-output",
                parsed,
            )
            self.assertEqual(4, output_collision.returncode)
            self.assertFalse(parsed.exists())

            partial_manifest = root / "partial-invalid.yaml"
            declared_source = root / "declared-source.rtf"
            declared_source.write_text("SOURCE MUST SURVIVE", encoding="utf-8")
            partial_manifest.write_text(
                yaml.safe_dump(
                    {"sources": [{"path": str(declared_source)}, {}]},
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            partial_collision = run_cli(
                "build-datapack",
                "--manifest",
                partial_manifest,
                "--output",
                declared_source,
            )
            self.assertEqual(4, partial_collision.returncode)
            self.assertIn("path collision", partial_collision.stderr.lower())
            self.assertEqual(
                "SOURCE MUST SURVIVE", declared_source.read_text(encoding="utf-8")
            )
            self.assertFalse((root / ".declared-source.rtf.previous").exists())


if __name__ == "__main__":
    unittest.main()
