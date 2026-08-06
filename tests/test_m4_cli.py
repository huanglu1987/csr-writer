from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from csr_writer.cli import main
from csr_writer.cross_chapter import load_sections_from_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
M4 = PROJECT_ROOT / "examples" / "m4"


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _review_decisions():
    sections = load_sections_from_manifest(M4 / "section_artifacts.json")
    replacements = {
        "p_11_4_001": "主要疗效终点、分析集和方法按已确认的研究文件执行。",
        "p_11_4_003": "本节仅呈现预先规定的分析结果。",
        "p_11_A_001": "剂量递增设计和停止规则按已确认的研究文件执行。",
        "p_11_A_003": "本节仅呈现已有来源支持的递增和剂量决策信息。",
    }
    return {
        "schema_version": "1.0",
        "sections": {
            section.section_id: {
                "draft_sha256": section.draft_sha256,
                "paragraphs": {
                    paragraph.para_id: {
                        "status": "resolved",
                        "replacement_text": replacements.get(
                            paragraph.para_id, "本段仅保留经确认来源支持的客观内容。"
                        ),
                    }
                    for paragraph in section.paragraphs
                    if paragraph.needs_human
                },
            }
            for section in sections
        },
    }


class M4CLITests(unittest.TestCase):
    def test_group_and_reverse_traceability_commands_publish_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group_output = root / "group.json"
            self.assertEqual(
                0,
                main(
                    [
                        "m4-group-semantics",
                        "--datapack",
                        str(M4 / "synthetic_datapack.json"),
                        "--section-metadata",
                        str(M4 / "section_metadata.json"),
                        "--contract",
                        str(M4 / "group_contract.json"),
                        "--output",
                        str(group_output),
                    ]
                ),
            )
            self.assertEqual("pass", json.loads(group_output.read_text())["status"])

            markdown = root / "unreferenced_tfl.md"
            report = root / "unreferenced_tfl.json"
            self.assertEqual(
                0,
                main(
                    [
                        "m4-unreferenced-tfl",
                        "--datapack",
                        str(M4 / "synthetic_datapack.json"),
                        "--section-metadata",
                        str(M4 / "section_metadata.json"),
                        "--output",
                        str(markdown),
                        "--report",
                        str(report),
                    ]
                ),
            )
            self.assertIn("T14.4.1.1", markdown.read_text(encoding="utf-8"))
            self.assertEqual(1, json.loads(report.read_text())["unreferenced_table_count"])

    def test_assemble_command_blocks_clean_then_publishes_after_resolutions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unresolved = root / "unresolved.json"
            _write_json(unresolved, {"schema_version": "1.0", "sections": {}})
            common = [
                "m4-assemble",
                "--sections-manifest",
                str(M4 / "section_artifacts.json"),
                "--synopsis-selection",
                str(M4 / "synopsis_selection.json"),
                "--protocol-id",
                "DEMO-001",
                "--annotated-output",
                str(root / "annotated.docx"),
                "--clean-output",
                str(root / "clean.docx"),
                "--synopsis-output",
                str(root / "synopsis.docx"),
                "--synopsis-manifest-output",
                str(root / "synopsis.json"),
                "--report",
                str(root / "assembly.json"),
            ]
            self.assertEqual(3, main([*common, "--review-decisions", str(unresolved)]))
            self.assertTrue((root / "annotated.docx").is_file())
            self.assertFalse((root / "clean.docx").exists())

            resolved = root / "resolved.json"
            _write_json(resolved, _review_decisions())
            self.assertEqual(0, main([*common, "--review-decisions", str(resolved)]))
            self.assertTrue((root / "clean.docx").is_file())
            self.assertTrue((root / "synopsis.docx").is_file())
            self.assertEqual("pass", json.loads((root / "assembly.json").read_text())["status"])

    def test_cross_check_and_bounded_qc_commands_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.json"
            _write_json(decisions, _review_decisions())
            cross = root / "cross.json"
            self.assertEqual(
                0,
                main(
                    [
                        "m4-cross-check",
                        "--sections-manifest",
                        str(M4 / "section_artifacts.json"),
                        "--review-decisions",
                        str(decisions),
                        "--synopsis-selection",
                        str(M4 / "synopsis_selection.json"),
                        "--analysis-set-contract",
                        str(M4 / "analysis_set_contract.json"),
                        "--claim-ledger",
                        str(M4 / "claim_ledger.json"),
                        "--output",
                        str(cross),
                    ]
                ),
            )
            self.assertEqual("pass", json.loads(cross.read_text())["status"])

            checks = root / "checks.json"
            _write_json(
                checks,
                {
                    "schema_version": "1.0",
                    "checks": {
                        "跨章一致性": {"status": "pass", "summary": "已执行的派生与哈希检查通过"},
                        "组别语义": {"status": "pass", "summary": "组别角色与语义列一致"},
                    },
                },
            )
            markdown, report = root / "qc.md", root / "qc.json"
            self.assertEqual(
                0,
                main(
                    [
                        "m4-qc-report",
                        "--checks",
                        str(checks),
                        "--output",
                        str(markdown),
                        "--report",
                        str(report),
                    ]
                ),
            )
            self.assertNotIn("申报就绪", markdown.read_text(encoding="utf-8"))
            self.assertFalse(json.loads(report.read_text())["regulatory_readiness_assessed"])

    def test_m4_cli_rejects_input_output_alias(self):
        self.assertEqual(
            4,
            main(
                [
                    "m4-group-semantics",
                    "--datapack",
                    str(M4 / "synthetic_datapack.json"),
                    "--section-metadata",
                    str(M4 / "section_metadata.json"),
                    "--contract",
                    str(M4 / "group_contract.json"),
                    "--output",
                    str(M4 / "group_contract.json"),
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
