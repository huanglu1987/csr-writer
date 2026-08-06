"""M2 strict Guard A -> deterministic fill -> Guard B -> Guard C pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .cross_references import (
    ANY_PLACEHOLDER_RE,
    REF_PLACEHOLDER_RE,
    find_reference_ids,
    normalize_entity_id,
    validate_reference_syntax,
)
from .datapack import validate_datapack
from .number_guard import CompiledWhitelist, compile_whitelist, find_bare_numbers
from .writing_style import n_percent_format_findings


VALUE_PLACEHOLDER_RE = re.compile(r"\{\{(?!@(?:ref|conclusion):)([^{}]+)\}\}")
# Compatibility name used by M2-M4 callers: this now means value placeholders only.
PLACEHOLDER_RE = VALUE_PLACEHOLDER_RE
RESERVED_MISSING_RE = re.compile(r"⟦缺失:[^⟧]+⟧")
N_PCT_VALUE_RE = re.compile(
    r"^\s*(?P<n>\d+)\s*(?:例|名|人)?\s*[（(]\s*"
    r"(?P<pct>\d+(?:\.\d+)?)\s*(?:%|％)?\s*[）)]\s*$"
)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(encoded)


@dataclass(frozen=True)
class GuardResult:
    status: str
    resolved_text: str | None
    report: dict[str, Any]


def _placeholder_syntax_issues(
    draft: str,
) -> tuple[list[re.Match[str]], list[re.Match[str]], list[dict[str, Any]]]:
    value_matches = list(VALUE_PLACEHOLDER_RE.finditer(draft))
    reference_matches = list(REF_PLACEHOLDER_RE.finditer(draft))
    scrubbed = ANY_PLACEHOLDER_RE.sub("", draft)
    issues: list[dict[str, Any]] = []
    if "{" in scrubbed or "}" in scrubbed:
        issues.append(
            {
                "code": "E-PLACEHOLDER-SYNTAX",
                "message": "Malformed, nested, or unbalanced placeholder braces",
            }
        )
    for match in value_matches:
        key = match.group(1)
        if key != key.strip() or not key.strip():
            issues.append(
                {
                    "code": "E-PLACEHOLDER-SYNTAX",
                    "message": f"Placeholder keys cannot have surrounding whitespace: {match.group(0)}",
                }
            )
    issues.extend(validate_reference_syntax(draft))
    return value_matches, reference_matches, issues


def _reference_scope(
    binding_manifest: Mapping[str, Any] | list[Mapping[str, Any]] | None,
    frozen_tfl_map: Mapping[str, Any] | None,
) -> set[str]:
    allowed: set[str] = set()
    raw_bindings: Any = binding_manifest
    if isinstance(binding_manifest, Mapping):
        raw_bindings = binding_manifest.get("binding_manifest", [])
    if raw_bindings is not None:
        if not isinstance(raw_bindings, list):
            raise ValueError("binding_manifest must be a list or section metadata object")
        for index, item in enumerate(raw_bindings):
            if not isinstance(item, Mapping):
                raise ValueError(f"binding_manifest[{index}] must be an object")
            source_id = item.get("source_id")
            if isinstance(source_id, str):
                allowed.add(normalize_entity_id(source_id))
    if frozen_tfl_map is not None:
        if not isinstance(frozen_tfl_map, Mapping) or frozen_tfl_map.get("status") != "frozen":
            raise ValueError("cross-reference scope requires a frozen TFL map")
        entities = frozen_tfl_map.get("entities")
        if not isinstance(entities, list):
            raise ValueError("frozen TFL map entities must be a list")
        for index, item in enumerate(entities):
            if not isinstance(item, Mapping) or not isinstance(item.get("entity_id"), str):
                raise ValueError(f"frozen TFL map entity {index} is malformed")
            allowed.add(normalize_entity_id(item["entity_id"]))
    return allowed


def resolve(
    draft: str,
    datapack: dict[str, Any],
    protocol_facts: dict[str, Any],
    *,
    binding_manifest: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
    frozen_tfl_map: Mapping[str, Any] | None = None,
) -> GuardResult:
    validate_datapack(datapack)
    whitelist: CompiledWhitelist = compile_whitelist(protocol_facts)
    cells = datapack["cells"]
    matches, reference_matches, syntax_issues = _placeholder_syntax_issues(draft)
    key_issues: list[dict[str, Any]] = []
    for match in matches:
        key = match.group(1)
        if key not in cells:
            key_issues.append(
                {
                    "code": "E-KEY",
                    "key": key,
                    "message": f"Placeholder key does not exist in datapack: {key}",
                }
            )
    allowed_references = _reference_scope(binding_manifest, frozen_tfl_map)
    for entity_id in find_reference_ids(draft):
        if entity_id not in allowed_references:
            key_issues.append(
                {
                    "code": "E-REF-UNBOUND",
                    "entity_id": entity_id,
                    "message": (
                        "Cross-reference entity is absent from this section binding_manifest "
                        "and the confirmed TFL map"
                    ),
                }
            )
    stage_a_issues = syntax_issues + key_issues
    base_report: dict[str, Any] = {
        "schema_version": "1.0",
        "draft_sha256": _hash_text(draft),
        "datapack_sha256": _hash_json(datapack),
        "protocol_facts_sha256": whitelist.facts_sha256,
        "placeholder_count": len(matches),
        "placeholder_keys": [match.group(1) for match in matches],
        "reference_count": len(reference_matches),
        "reference_entities": [match.group("entity") for match in reference_matches],
        "whitelist": [entry.as_dict() for entry in whitelist.entries],
        "stages": {},
        "issues": [],
    }
    if stage_a_issues:
        base_report["status"] = "blocked"
        base_report["stages"] = {
            "A_key_existence": {"status": "fail", "issues": stage_a_issues},
            "B_residual": {"status": "not_run"},
            "C_bare_numbers": {"status": "not_run"},
            "D_result_format": {"status": "not_run"},
        }
        base_report["issues"] = stage_a_issues
        return GuardResult("blocked", None, base_report)

    cell_format_issues: list[dict[str, Any]] = []

    def resolve_value(match: re.Match[str]) -> str:
        key = match.group(1)
        cell = cells[key]
        value = str(cell["v"])
        if cell.get("type") != "n_pct":
            return value
        parsed = N_PCT_VALUE_RE.fullmatch(value)
        if parsed is None:
            cell_format_issues.append(
                {
                    "code": "E-STYLE-N-PERCENT-FORMAT",
                    "severity": "block",
                    "category": "n_percent_format",
                    "key": key,
                    "message": "n_pct 单元格无法确定性格式化为“n例（p%）”",
                    "matched_text": value,
                }
            )
            return value
        return f"{parsed.group('n')}例（{parsed.group('pct')}%）"

    resolved = VALUE_PLACEHOLDER_RE.sub(resolve_value, draft)
    residual_issues: list[dict[str, Any]] = []
    without_references = REF_PLACEHOLDER_RE.sub("", resolved)
    if (
        "{{" in without_references
        or "}}" in without_references
        or RESERVED_MISSING_RE.search(resolved)
    ):
        residual_issues.append(
            {
                "code": "E-UNRESOLVED",
                "message": "Resolved text still contains placeholder or reserved missing markers",
            }
        )
    if residual_issues:
        base_report["status"] = "blocked"
        base_report["stages"] = {
            "A_key_existence": {"status": "pass"},
            "B_residual": {"status": "fail", "issues": residual_issues},
            "C_bare_numbers": {"status": "not_run"},
            "D_result_format": {"status": "not_run"},
        }
        base_report["issues"] = residual_issues
        return GuardResult("blocked", None, base_report)

    placeholder_free_draft = REF_PLACEHOLDER_RE.sub(" ", VALUE_PLACEHOLDER_RE.sub(" ", draft))
    bare_number_issues = find_bare_numbers(placeholder_free_draft, whitelist)
    if bare_number_issues:
        base_report["status"] = "review_required"
        base_report["stages"] = {
            "A_key_existence": {"status": "pass"},
            "B_residual": {"status": "pass"},
            "C_bare_numbers": {"status": "fail", "issues": bare_number_issues},
            "D_result_format": {"status": "not_run"},
        }
        base_report["issues"] = bare_number_issues
        # No resolved preview is returned through the release API until findings are adjudicated.
        return GuardResult("review_required", None, base_report)

    result_format_issues = cell_format_issues + n_percent_format_findings(resolved)
    if result_format_issues:
        base_report["status"] = "blocked"
        base_report["stages"] = {
            "A_key_existence": {"status": "pass"},
            "B_residual": {"status": "pass"},
            "C_bare_numbers": {"status": "pass"},
            "D_result_format": {"status": "fail", "issues": result_format_issues},
        }
        base_report["issues"] = result_format_issues
        return GuardResult("blocked", None, base_report)

    base_report["status"] = "pass"
    base_report["stages"] = {
        "A_key_existence": {"status": "pass"},
        "B_residual": {"status": "pass"},
        "C_bare_numbers": {"status": "pass"},
        "D_result_format": {"status": "pass"},
    }
    base_report["resolved_sha256"] = _hash_text(resolved)
    return GuardResult("pass", resolved, base_report)
