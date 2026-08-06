#!/usr/bin/env python3
"""Build the reproducible v1.1.0 runtime-and-visual-evidence bundle."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer import __version__
from csr_writer.io_utils import atomic_write_json, sha256_file


FIXED_ZIP_TIME = (2026, 8, 6, 0, 0, 0)
PACKAGE_NAME = f"csr-writer-{__version__}-runtime-visual-evidence.zip"
EXACT_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "README.md",
    "AGENTS.md",
    "DELIVERY.md",
    "tests/test_v1_1_visual_qc.py",
    "tests/test_v1_1_release_packaging.py",
    "tests/test_codex_platform_boundary.py",
)
INCLUDED_TREES = (
    "csr_writer",
    "skills/csr-writer/assets",
    "skills/csr-writer/references",
    "skills/csr-writer/section-playbooks",
    "skills/csr-writer/scripts",
    "scripts",
    "docs",
    "examples/fixtures",
    "examples/output/m21",
    "examples/output/v1.1.0",
)
EXCLUDED_NAMES = {
    ".DS_Store",
    PACKAGE_NAME,
    "component_hashes.json",
    "release_manifest.json",
    "discover.stdout.txt",
    "discover.stderr.txt",
    "discover.exit_code.txt",
    "delivery_self_check.stdout.txt",
    "delivery_self_check.stderr.txt",
    "delivery_self_check.exit_code.txt",
}
EXCLUDED_PARTS = {
    "__pycache__",
    "release",
    ".visual-qc-test-work",
    "libreoffice-profile",
    "_exploration",
}
READ_ONLY_SOURCE_HASHES = {
    "CSR撰写Skill开发规格书.md": "6053ea6fffa79f98d1c92840a812cd2cf12b4a329fb9420d1efcca5c6fb06bdb",
    "ICH-E3章节树.yaml": "87ea56d599120e265c1649881a9d3ad1c08db540a91be13de6a2bd31a6bc0942",
    "CDE申报要求核查表.md": "21374f376270f1c7d8352e7e3808e7d9385aa5e8d51cd46d12c9f178d682192b",
    "外部方案评估.md": "12581d0e5fefee3bf0b8c7d4e7e95147a5add099f394a73a89bf3031e4383e0c",
    "synthetic_result_tfl.deidentified.docx": "76338581eb2295e0ed6552a348da369a1f72aaa61a62421fba4fd22174a56c4a",
}


def _eligible(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return (
        path.is_file()
        and path.name not in EXCLUDED_NAMES
        and not EXCLUDED_PARTS.intersection(relative.parts)
        and path.suffix not in {".pyc", ".pyo"}
    )


def iter_release_files() -> Iterable[Path]:
    selected: set[Path] = set()
    for relative in EXACT_FILES:
        path = ROOT / relative
        if _eligible(path):
            selected.add(path)
    for relative in INCLUDED_TREES:
        tree = ROOT / relative
        if not tree.exists():
            continue
        for path in tree.rglob("*"):
            if _eligible(path):
                selected.add(path)
    return sorted(selected, key=lambda path: path.relative_to(ROOT).as_posix())


def _aggregate(records: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_zip(path: Path, files: list[Path], component_manifest: Path) -> None:
    entries = [(source.relative_to(ROOT).as_posix(), source) for source in files]
    entries.append(("examples/output/v1.1.0/release/component_hashes.json", component_manifest))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, source in sorted(entries):
            info = zipfile.ZipInfo(f"csr-writer/{relative}", date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, source.read_bytes(), compresslevel=9)


def build_release(output_dir: Path, *, regression_test_count: int = 0) -> dict[str, Any]:
    if __version__ != "1.1.0":
        raise RuntimeError(f"v1.1 release builder received version {__version__}")
    output_dir.mkdir(parents=True, exist_ok=True)
    files = list(iter_release_files())
    records = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(path),
            "bytes": str(path.stat().st_size),
        }
        for path in files
    ]
    components = {
        "schema_version": "1.0",
        "version": __version__,
        "file_count": len(records),
        "aggregate_sha256": _aggregate(records),
        "read_only_source_hashes": READ_ONLY_SOURCE_HASHES,
        "visual_qc": {
            "optional_external_renderer": True,
            "missing_renderer_is_nonfatal": True,
            "legacy_release_decision_changed": False,
            "config_sha256": "fcfb0246089dc2984eacca06fa52f1d93a9d4d3ef13a2775a6df28af76d8046d",
            "checker_sha256": "d09c182a7188cd956c0a28b9f89894e8cb1c71269433219da44465abe6de4ac1",
        },
        "regression_evidence": {
            "discover_status": "pass" if regression_test_count else "not_recorded",
            "discover_test_count": regression_test_count,
            "platform_boundary_status": "pass" if regression_test_count else "not_recorded",
        },
        "files": records,
    }
    component_path = output_dir / "component_hashes.json"
    atomic_write_json(component_path, components)
    package_path = output_dir / PACKAGE_NAME
    _write_zip(package_path, files, component_path)
    release = {
        "schema_version": "1.0",
        "status": "frozen",
        "version": __version__,
        "package": package_path.name,
        "package_sha256": sha256_file(package_path),
        "package_bytes": package_path.stat().st_size,
        "component_manifest": component_path.name,
        "component_aggregate_sha256": components["aggregate_sha256"],
        "real_clinical_files_included": False,
        "read_only_specification_files_included": False,
        "external_visual_dependencies_bundled": False,
        "note": "只读规格源以哈希引用；LibreOffice/PyMuPDF/Pillow 为可选外部能力。",
    }
    atomic_write_json(output_dir / "release_manifest.json", release)
    return release


def main() -> int:
    verification_path = (
        ROOT / "examples" / "output" / "v1.1.0" / "verification" / "full_discover.json"
    )
    test_count = 0
    if verification_path.is_file():
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        if verification.get("status") == "pass":
            test_count = int(verification.get("test_count", 0))
    release = build_release(
        ROOT / "examples" / "output" / "v1.1.0" / "release",
        regression_test_count=test_count,
    )
    print(json.dumps(release, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
