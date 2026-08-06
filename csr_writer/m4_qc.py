"""M4 QC reporting with a hard ban on regulatory-readiness claims."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .errors import CSRWriterError
from .io_utils import atomic_write_text


class QCAuthorityError(CSRWriterError):
    """A QC report exceeds the tool's decision authority."""


FORBIDDEN_QC_CLAIMS: tuple[re.Pattern[str], ...] = (
    re.compile(r"申报就绪"),
    re.compile(r"可以递交|可递交|建议递交"),
    re.compile(r"符合\s*CDE\s*要求", re.I),
    re.compile(r"已满足全部法规要求"),
)


def validate_qc_authority(text: str) -> None:
    if not isinstance(text, str):
        raise QCAuthorityError("QC report must be text")
    hits = [pattern.pattern for pattern in FORBIDDEN_QC_CLAIMS if pattern.search(text)]
    if hits:
        raise QCAuthorityError(
            "QC report contains a prohibited regulatory-readiness claim: " + ", ".join(hits)
        )


def build_qc_report(checks: Mapping[str, Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Build a bounded report: observed checks only, never a filing decision."""
    if not isinstance(checks, Mapping) or not checks:
        raise QCAuthorityError("QC report needs one or more executed checks")
    rows: list[tuple[str, str, str]] = []
    issue_categories: list[str] = []
    allowed_statuses = {"pass", "warning", "fail", "blocked"}
    for category, raw in sorted(checks.items()):
        if not isinstance(category, str) or not category or not isinstance(raw, Mapping):
            raise QCAuthorityError("QC checks must map category names to result objects")
        if set(raw) != {"status", "summary"}:
            raise QCAuthorityError(
                f"QC check {category} must contain exactly status and summary"
            )
        status, summary = raw.get("status"), raw.get("summary")
        if status not in allowed_statuses or not isinstance(summary, str) or not summary.strip():
            raise QCAuthorityError(f"Invalid QC check result for {category}")
        validate_qc_authority(summary)
        rows.append((category, status, summary.strip()))
        if status != "pass":
            issue_categories.append(category)

    lines = [
        "# M4 自动质检报告",
        "",
        "> 范围：本报告只陈述下列已执行检查的结果，不替代人工 QC、QA 或申办方决策。",
        "",
        "| 检查类别 | 状态 | 结果摘要 |",
        "|---|---|---|",
    ]
    for category, status, summary in rows:
        escaped = summary.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {category} | {status} | {escaped} |")
    lines.extend(["", "## 结论", ""])
    if issue_categories:
        conclusion = (
            "本次已执行自动检查发现需处理或人工确认的问题，"
            "详见上表；本结果不替代人工 QC、QA 或申办方决策。"
        )
        overall_status = "attention_required"
    else:
        categories = "、".join(category for category, _, _ in rows)
        conclusion = (
            f"未发现本次已执行自动检查所覆盖的{categories}类问题。"
            "本结果不替代人工 QC、QA 或申办方决策。"
        )
        overall_status = "pass"
    lines.append(conclusion)
    markdown = "\n".join(lines) + "\n"
    validate_qc_authority(markdown)
    machine = {
        "schema_version": "1.0",
        "status": overall_status,
        "scope": "executed_checks_only",
        "checks": {
            category: {"status": status, "summary": summary}
            for category, status, summary in rows
        },
        "issue_categories": issue_categories,
        "conclusion": conclusion,
        "regulatory_readiness_assessed": False,
    }
    return markdown, machine


def write_qc_report(path: str | Path, checks: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    markdown, machine = build_qc_report(checks)
    atomic_write_text(path, markdown)
    return machine
