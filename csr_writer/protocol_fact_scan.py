"""Conservative protocol-to-study_facts contradiction scanning.

This module proposes review questions only.  It never infers or mutates facts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .docx_extract import extract_docx
from .errors import ConfigurationError
from .io_utils import load_yaml, sha256_file
from .paths import REFERENCES_DIR
from .study_facts import validate_study_facts


DEFAULT_SCAN_RULES = REFERENCES_DIR / "protocol-facts-scan-rules.yaml"
_RULE_KEYS = {"id", "patterns", "targets", "question"}
_TARGET_KEYS = {"path", "inactive_when", "missing_value"}
_DIRECT_BODY_PARAGRAPH = re.compile(r"^word/document\.xml:/w:document/w:body/w:p\[")
_SECTION_HEADING = re.compile(
    r"^(?P<id>\d+(?:\.\d+)*)(?:\.)?\s*(?P<title>\S.{0,100})$"
)


def _lookup(payload: Mapping[str, Any], dotted: str) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _load_rules(path: str | Path = DEFAULT_SCAN_RULES) -> dict[str, Any]:
    payload = load_yaml(path)
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "contract",
        "rules",
    }:
        raise ConfigurationError("protocol fact scan rules have an invalid root contract")
    if payload["schema_version"] != "1.0" or not isinstance(payload["rules"], list):
        raise ConfigurationError("unsupported protocol fact scan rules schema")
    seen: set[str] = set()
    compiled: list[dict[str, Any]] = []
    for index, raw in enumerate(payload["rules"]):
        if not isinstance(raw, Mapping) or set(raw) != _RULE_KEYS:
            raise ConfigurationError(f"invalid protocol fact scan rule at index {index}")
        rule_id = str(raw["id"])
        if not re.fullmatch(r"[a-z][a-z0-9_]*", rule_id) or rule_id in seen:
            raise ConfigurationError(f"invalid or duplicate protocol scan rule id: {rule_id}")
        seen.add(rule_id)
        patterns = raw["patterns"]
        targets = raw["targets"]
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(value, str) and value for value in patterns
        ):
            raise ConfigurationError(f"{rule_id}.patterns must be a non-empty string list")
        if not isinstance(targets, list) or not targets:
            raise ConfigurationError(f"{rule_id}.targets must be non-empty")
        clean_targets: list[dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, Mapping) or not set(target).issubset(_TARGET_KEYS):
                raise ConfigurationError(f"invalid target in rule {rule_id}")
            if set(target) not in (
                {"path", "inactive_when"},
                {"path", "missing_value"},
            ):
                raise ConfigurationError(f"target in rule {rule_id} has an invalid predicate")
            clean_targets.append(dict(target))
        try:
            regex = re.compile("(?:" + ")|(?:".join(patterns) + ")", re.IGNORECASE)
        except re.error as exc:
            raise ConfigurationError(f"invalid regex in rule {rule_id}: {exc}") from exc
        compiled.append(
            {
                "id": rule_id,
                "regex": regex,
                "targets": clean_targets,
                "question": str(raw["question"]).strip(),
            }
        )
    return {
        "schema_version": "1.0",
        "contract": dict(payload["contract"]),
        "rules": compiled,
        "rules_sha256": sha256_file(path),
    }


def _inactive_target(facts: Mapping[str, Any], target: Mapping[str, Any]) -> tuple[bool, Any]:
    path = str(target["path"])
    if path == "appendix_status.16.1.8":
        current: Any = "unconfirmed"
    else:
        current = _lookup(facts, path)
    if "inactive_when" in target:
        return current == target["inactive_when"], current
    required = target["missing_value"]
    return not isinstance(current, list) or required not in current, current


def _sanitize_quote(text: str, match: re.Match[str], *, limit: int = 240) -> str:
    start = max(0, match.start() - 90)
    end = min(len(text), match.end() + 130)
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    quote = re.sub(
        r"\b[A-Z]{2,5}[-_]?\d{4,}(?:[-_][A-Z0-9ⅠⅡⅢⅣ]+)*(?![A-Za-z0-9])",
        "[研究标识]",
        quote,
    )
    quote = re.sub(r"\b[A-Z]{2}\d{6,}(?![A-Za-z0-9])", "[研究标识]", quote)
    quote = re.sub(
        r"(?:商品名|trade\s+name)[:：]?\s*[A-Za-z0-9®_-]+",
        "商品名[已脱敏]",
        quote,
        flags=re.IGNORECASE,
    )
    quote = re.sub(
        r"[\u4e00-\u9fff]{2,24}(?:大学附属医院|医院|研究中心)",
        "[机构]",
        quote,
    )
    if len(quote) > limit:
        quote = quote[: limit - 1].rstrip() + "…"
    return quote


def _section_locations(records: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    current = "前置页/目录前"
    locations: dict[int, str] = {}
    for record in records:
        sequence = int(record["sequence"])
        text = str(record.get("text", "")).strip()
        style = str(record.get("paragraph_style") or "")
        location = str(record.get("location", ""))
        if (
            text
            and not style.upper().startswith("TOC")
            and _DIRECT_BODY_PARAGRAPH.match(location)
        ):
            heading = _SECTION_HEADING.fullmatch(text)
            if heading is not None:
                current = f"§{heading.group('id')} {heading.group('title').strip()}"
        locations[sequence] = current
    return locations


def scan_protocol_facts(
    protocol_docx: str | Path,
    study_facts: Mapping[str, Any],
    *,
    rules_path: str | Path = DEFAULT_SCAN_RULES,
    expected_sha256: str | None = None,
    protocol_role: str = "protocol",
) -> dict[str, Any]:
    """Return traceable review findings without changing the validated facts."""

    validated = validate_study_facts(dict(study_facts))
    source = Path(protocol_docx)
    observed_before = sha256_file(source)
    if expected_sha256 is not None and observed_before != expected_sha256:
        raise ConfigurationError("protocol SHA-256 does not match the approved read-only source")
    extracted = extract_docx(source)
    rules = _load_rules(rules_path)
    locations = _section_locations(extracted["records"])
    findings: list[dict[str, Any]] = []
    matched_records = 0
    for record in extracted["records"]:
        text = str(record.get("text", ""))
        if not text:
            continue
        record_matched = False
        for rule in rules["rules"]:
            match = rule["regex"].search(text)
            if match is None:
                continue
            inactive = []
            for target in rule["targets"]:
                should_report, current = _inactive_target(validated, target)
                if should_report:
                    inactive.append(
                        {
                            "path": str(target["path"]),
                            "current_value": current,
                            "expected_marker": target.get("missing_value", True),
                        }
                    )
            if not inactive:
                continue
            record_matched = True
            findings.append(
                {
                    "finding_id": f"PF-{len(findings) + 1:04d}",
                    "rule_id": rule["id"],
                    "targets": inactive,
                    "source_location": locations[int(record["sequence"])],
                    "record_sequence": int(record["sequence"]),
                    "story": str(record["story"]),
                    "quote": _sanitize_quote(text, match),
                    "review_question": rule["question"],
                    "disposition": "human_confirmation_required",
                }
            )
        if record_matched:
            matched_records += 1
    observed_after = sha256_file(source)
    if observed_after != observed_before:
        raise ConfigurationError("protocol changed while contradiction scan was running")
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding["rule_id"]] = by_rule.get(finding["rule_id"], 0) + 1
    return {
        "schema_version": "1.0",
        "status": "review_required" if findings else "no_mismatch_signal_found",
        "contract": {
            "automatic_fact_updates": False,
            "model_inference_used": False,
            "false_positive_tolerance": "high; every hit requires human interpretation",
            "source_content_treated_as_data": True,
        },
        "protocol_source": {
            "role": protocol_role,
            "sha256_before": observed_before,
            "sha256_after": observed_after,
            "unchanged": True,
            "path_recorded": False,
            "filename_recorded": False,
        },
        "rules_sha256": rules["rules_sha256"],
        "metrics": {
            "records_scanned": len(extracted["records"]),
            "nonempty_records_scanned": extracted["statistics"]["nonempty_records"],
            "matched_records": matched_records,
            "finding_count": len(findings),
            "finding_count_by_rule": dict(sorted(by_rule.items())),
        },
        "findings": findings,
    }


def render_protocol_fact_scan(report: Mapping[str, Any]) -> str:
    lines = [
        "# 方案与 study_facts 反向核对",
        "",
        "> 本报告只提出核对项，不自动填写或修改 study_facts。命中不等于事实成立。",
        "",
        f"- 扫描记录：{report['metrics']['nonempty_records_scanned']}",
        f"- 待人工核对：{report['metrics']['finding_count']}",
        "",
    ]
    if not report["findings"]:
        lines.append("未发现“方案文本有提示而对应字段为 false/空”的规则命中。")
        lines.append("")
        return "\n".join(lines)
    for finding in report["findings"]:
        targets = "；".join(
            f"study_facts.{item['path']} = {item['current_value']!r}"
            for item in finding["targets"]
        )
        lines.extend(
            [
                f"## ⚠ {finding['finding_id']} · {finding['source_location']}",
                "",
                f"- 当前事实：{targets}",
                f"- 原文：\u300c{finding['quote']}\u300d",
                f"- 核对：{finding['review_question']}",
                "",
            ]
        )
    return "\n".join(lines)
