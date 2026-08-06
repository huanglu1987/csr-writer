#!/usr/bin/env python3
"""Run the mandatory pre-delivery regression gate.

The command deliberately executes the public discovery form used by an
independent recipient.  Its exit status is propagated unchanged so a failed
suite cannot be reported as a successful delivery check.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


ROOT = Path(__file__).resolve().parents[1]
DISCOVER_ARGUMENTS = ("-m", "unittest", "discover", "-s", "tests")


class _CompletedProcess(Protocol):
    returncode: int


Runner = Callable[..., _CompletedProcess]


def discover_command(python: str = sys.executable) -> tuple[str, ...]:
    """Return the exact interpreter-relative command used by the gate."""

    return (python, *DISCOVER_ARGUMENTS)


def run_delivery_self_check(
    *,
    root: Path = ROOT,
    runner: Runner = subprocess.run,
    python: str = sys.executable,
) -> int:
    """Run discovery from the project root and return its unmodified status."""

    completed = runner(discover_command(python), cwd=root, check=False)
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise SystemExit("delivery_self_check.py does not accept arguments")
    status = run_delivery_self_check()
    if status:
        print("delivery blocked: unittest discovery failed", file=sys.stderr)
        return status
    print("delivery gate passed: unittest discovery is green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
