"""Integrity lock for the four immutable project specification sources."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .io_utils import load_json_unique, sha256_file
from .paths import PLUGIN_ROOT, REFERENCES_DIR


DEFAULT_SOURCE_LOCK = REFERENCES_DIR / "source-file-lock.json"


def verify_source_file_lock(
    lock_path: str | Path = DEFAULT_SOURCE_LOCK,
    *,
    plugin_root: str | Path = PLUGIN_ROOT,
) -> dict[str, Any]:
    payload = load_json_unique(lock_path)
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "files",
        "bundled_structure_copy",
    }:
        raise ConfigurationError("source-file-lock.json has an invalid root contract")
    if payload["schema_version"] != "1.0" or not isinstance(payload["files"], list):
        raise ConfigurationError("unsupported source-file lock schema")
    root = Path(plugin_root).resolve()
    records: list[dict[str, Any]] = []
    roles: set[str] = set()
    for raw in [*payload["files"], payload["bundled_structure_copy"]]:
        if not isinstance(raw, Mapping):
            raise ConfigurationError("invalid source-file lock entry")
        role = str(raw.get("role") or "bundled_structure_copy")
        if role in roles:
            raise ConfigurationError(f"duplicate source lock role: {role}")
        roles.add(role)
        relative = str(raw.get("relative_path_from_plugin_root", ""))
        expected = str(raw.get("sha256", ""))
        if not relative or len(expected) != 64:
            raise ConfigurationError(f"invalid source lock entry for {role}")
        source = (root / relative).resolve()
        if not source.is_file():
            raise ConfigurationError(f"locked source missing for role {role}")
        observed = sha256_file(source)
        records.append(
            {
                "role": role,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "match": observed == expected,
                "path_recorded": False,
            }
        )
    mismatches = [record["role"] for record in records if not record["match"]]
    if mismatches:
        raise ConfigurationError(
            "immutable specification source hash mismatch: " + ", ".join(mismatches)
        )
    return {
        "schema_version": "1.0",
        "status": "pass",
        "source_count": len(payload["files"]),
        "bundled_copy_checked": True,
        "records": records,
    }
