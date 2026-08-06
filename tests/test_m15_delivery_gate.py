from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.delivery_self_check import discover_command, run_delivery_self_check


ROOT = Path(__file__).resolve().parents[1]


class M15DeliveryGateTests(unittest.TestCase):
    def test_tests_directory_is_an_explicit_package(self) -> None:
        self.assertTrue((ROOT / "tests" / "__init__.py").is_file())
        module = importlib.import_module("tests.test_m14_writing_style")
        self.assertTrue(hasattr(module, "M14WritingStyleTests"))

    def test_gate_runs_public_discover_command_from_project_root(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0)

        self.assertEqual(0, run_delivery_self_check(root=ROOT, runner=runner, python="py"))
        self.assertEqual(("py", "-m", "unittest", "discover", "-s", "tests"), calls[0][0])
        self.assertEqual(ROOT, calls[0][1]["cwd"])
        self.assertFalse(calls[0][1]["check"])

    def test_gate_propagates_failure_status(self) -> None:
        def runner(command, **kwargs):
            return SimpleNamespace(returncode=17)

        self.assertEqual(17, run_delivery_self_check(runner=runner))
        self.assertEqual(
            ("python", "-m", "unittest", "discover", "-s", "tests"),
            discover_command("python"),
        )


if __name__ == "__main__":
    unittest.main()
