from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHILD_FLAG = "CSR_WRITER_PLATFORM_BOUNDARY_CHILD"


class CodexPlatformBoundaryTests(unittest.TestCase):
    def test_platform_documents_can_be_removed_without_breaking_runtime(self):
        if os.environ.get(CHILD_FLAG) == "1":
            self.skipTest("nested platform-boundary invocation")

        with tempfile.TemporaryDirectory() as temporary:
            clone = Path(temporary) / "csr-writer-runtime-only"
            shutil.copytree(
                ROOT,
                clone,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
            reference_fixtures = clone.parent / "参考实现"
            reference_fixtures.mkdir()
            for filename in ("fixture_t_14_2_1.rtf", "fixture_t_pk_nca.rtf"):
                shutil.copy2(ROOT.parent / "参考实现" / filename, reference_fixtures / filename)
            # M10 deliberately locks the four immutable specification sources
            # outside the plugin root.  Mirror them byte-for-byte so the
            # runtime-only suite tests the same integrity boundary.
            for filename in (
                "CSR撰写Skill开发规格书.md",
                "ICH-E3章节树.yaml",
                "CDE申报要求核查表.md",
                "外部方案评估.md",
            ):
                shutil.copy2(ROOT.parent / filename, clone.parent / filename)
            # One legacy fixed report intentionally records its absolute fixture
            # path; rebasing that test datum is necessary when the runtime is
            # exercised from a temporary copy.
            fixed_report = clone / "examples" / "output" / "m3_docx_extraction.json"
            fixed_payload = json.loads(fixed_report.read_text(encoding="utf-8"))
            fixed_payload["source"]["path"] = str(
                (
                    clone
                    / "tests"
                    / "fixtures"
                    / "m3-docx"
                    / "deep-extraction-fixture.docx"
                ).resolve()
            )
            fixed_report.write_text(
                json.dumps(fixed_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            shutil.rmtree(clone / ".claude-plugin", ignore_errors=True)
            (clone / "AGENTS.md").unlink(missing_ok=True)
            (clone / "skills" / "csr-writer" / "SKILL.md").unlink(missing_ok=True)
            shutil.rmtree(clone / "skills" / "csr-writer" / "agents", ignore_errors=True)

            environment = os.environ.copy()
            environment[CHILD_FLAG] = "1"
            suite = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=clone,
                env=environment,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )
            self.assertEqual(
                0,
                suite.returncode,
                f"runtime-only suite failed\nstdout:\n{suite.stdout}\nstderr:\n{suite.stderr}",
            )

            discover = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import argparse; from csr_writer.cli import build_parser; "
                        "p=build_parser(); "
                        "a=next(x for x in p._actions if isinstance(x,argparse._SubParsersAction)); "
                        "print('\\n'.join(sorted(a.choices)))"
                    ),
                ],
                cwd=clone,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, discover.returncode, discover.stderr)
            commands = [item for item in discover.stdout.splitlines() if item]
            self.assertGreaterEqual(len(commands), 20)
            for command in commands:
                with self.subTest(command=command):
                    help_result = subprocess.run(
                        [sys.executable, "-m", "csr_writer", command, "--help"],
                        cwd=clone,
                        env=environment,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(0, help_result.returncode, help_result.stderr)
                    self.assertIn("usage:", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
