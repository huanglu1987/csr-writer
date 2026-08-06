#!/usr/bin/env python3
"""Freeze the M19 split-risk guard policy before holdout authoring starts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from csr_writer.constrained_narration import constrained_guard_policy_snapshot  # noqa: E402
from csr_writer.io_utils import atomic_write_text_bundle, sha256_file  # noqa: E402
from csr_writer.paths import (  # noqa: E402
    DEFAULT_CONSTRAINED_NARRATION_GUARDS,
    DEFAULT_METHOD_EXTRACTION_CONTRACTS,
    DEFAULT_PRODUCTION_METHODS,
)


def _bundle_sha256(items: list[dict[str, str]]) -> str:
    import hashlib

    canonical = json.dumps(
        items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "examples/output/m19/guard_policy_frozen.json",
    )
    args = parser.parse_args()
    snapshot = constrained_guard_policy_snapshot()
    implementation = ROOT / "csr_writer/constrained_narration.py"
    components = [
        {
            "role": "guard_policy",
            "path": str(DEFAULT_CONSTRAINED_NARRATION_GUARDS.relative_to(ROOT)),
            "sha256": sha256_file(DEFAULT_CONSTRAINED_NARRATION_GUARDS),
        },
        {
            "role": "guard_implementation",
            "path": str(implementation.relative_to(ROOT)),
            "sha256": sha256_file(implementation),
        },
        {
            "role": "production_assignment_registry",
            "path": str(DEFAULT_PRODUCTION_METHODS.relative_to(ROOT)),
            "sha256": sha256_file(DEFAULT_PRODUCTION_METHODS),
        },
        {
            "role": "method_extraction_contracts",
            "path": str(DEFAULT_METHOD_EXTRACTION_CONTRACTS.relative_to(ROOT)),
            "sha256": sha256_file(DEFAULT_METHOD_EXTRACTION_CONTRACTS),
        },
    ]
    guard_bundle_sha256 = _bundle_sha256(components)
    payload = {
        "schema_version": "1.0",
        "status": "frozen_before_holdout_authoring",
        "guard_bundle_sha256": guard_bundle_sha256,
        "frozen_components": components,
        **snapshot,
    }
    atomic_write_text_bundle(
        [
            (
                args.output,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
        ]
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "policy_sha256": snapshot["policy_sha256"],
                "guard_bundle_sha256": guard_bundle_sha256,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
