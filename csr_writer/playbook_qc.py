"""Deterministic depth checks for complete section playbooks."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .semantic_guard import scan_internal_process_language
from .writing_style import STYLE_SECTION_HEADING, analyze_writing_style


MIN_COMPLETE_PLAYBOOK_LINES = 150
MIN_REVIEW_ISSUES = 5

SEVEN_ELEMENTS: Mapping[str, str] = {
    "authority": "## 1. 本节的权威依据",
    "must_answer": "## 2. 必须回答的问题清单",
    "source_mapping": "## 3. 源文件 → 内容映射",
    "numeric_and_conclusion_rules": "## 4. 数值红线与结论性表述规则",
    "paragraph_structure": "## 5. 推荐段落结构",
    "common_review_issues": "## 6. 常见退审问题",
    "section_specific_pitfalls": "## 7. 本节特有的易错点",
}

PROCESS_SEPARATION_ELEMENTS: Mapping[str, str] = {
    "writer_constraints": "## 给写手的约束（不得写入正文）",
    "csr_standard_sentences": "## 证据不足时的标准表述（可写入正文，须按此措辞）",
}

STYLE_REQUIRED_PHRASES = (
    "禁用表达",
    "每节至多 1 次",
    "每段至多 1 个",
    "单句不超过 60 字",
    "放在段末",
)

M14_COMPLETE_PLAYBOOK_FILENAMES = (
    "s9-randomization-blinding-controls.md",
    "s10-subject-disposition.md",
    "s11-2-demographics-baseline.md",
    "s11-3-treatment-compliance.md",
    "s11-4-primary-efficacy.md",
    "s11-a-dose-escalation.md",
    "s11-clinpharm-special-pop-endpoints.md",
    "s11-topical-pk.md",
    "s11-5-10-pk-conclusion.md",
    "s11-6-7-exploratory-efficacy-conclusion.md",
    "s12-2-adverse-events.md",
    "s12-3-1-deaths-serious-significant-listings.md",
    "s12-3-2-sae-narratives-boundary.md",
    "s12-3-3-serious-event-analysis.md",
    "s12-4-2-laboratory-evaluation.md",
    "s12-exposure.md",
    "s12-route-specific-safety.md",
    "s13-discussion-overall-conclusions.md",
    "s13-overall-conclusion-conditioned.md",
)

M15_NEW_COMPLETE_PLAYBOOK_FILENAMES = (
    "s11-5-1-pk-analysis-set.md",
    "s11-5-2-concentration-data.md",
    "s11-5-3-blq-handling.md",
    "s11-5-5-peak-systemic-exposure.md",
    "s11-5-6-dose-proportionality.md",
    "s11-5-7-accumulation.md",
    "s11-5-8-application-site-effects.md",
    "s11-5-9-individual-pk.md",
    "s11-6-1-4-exploratory-endpoints.md",
    "s11-6-2-4-multicenter.md",
    "s12-2-1-ae-overview.md",
    "s12-2-3-ae-analysis.md",
    "s12-2-6-permanent-discontinuation-ae.md",
    "s12-2-7-interruption-dose-adjustment-ae.md",
    "s12-2-9-application-site-adverse-reactions.md",
    "s12-5-2-ecg-evaluation.md",
)

COMPLETE_PLAYBOOK_FILENAMES = (
    M14_COMPLETE_PLAYBOOK_FILENAMES + M15_NEW_COMPLETE_PLAYBOOK_FILENAMES
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _section_text(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    following = text.find("\n## ", start + len(heading))
    return text[start:] if following < 0 else text[start:following]


def _review_issue_count(text: str) -> int:
    section = _section_text(text, SEVEN_ELEMENTS["common_review_issues"])
    rows = 0
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        if cells[0] in {"问题", "常见问题"}:
            continue
        rows += 1
    return rows


def inspect_complete_playbook(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    text = content.decode("utf-8")
    line_count = len(text.splitlines())
    elements = {
        element: marker in text for element, marker in SEVEN_ELEMENTS.items()
    }
    process_separation = {
        element: marker in text
        for element, marker in PROCESS_SEPARATION_ELEMENTS.items()
    }
    standard_sentence_section = _section_text(
        text, PROCESS_SEPARATION_ELEMENTS["csr_standard_sentences"]
    )
    standard_sentence_findings = (
        list(scan_internal_process_language(standard_sentence_section).findings)
        if standard_sentence_section
        else []
    )
    standard_sentence_style = (
        analyze_writing_style(standard_sentence_section)
        if standard_sentence_section
        else None
    )
    style_section = _section_text(text, STYLE_SECTION_HEADING)
    style_requirements = {
        phrase: phrase in style_section for phrase in STYLE_REQUIRED_PHRASES
    }
    review_issue_count = _review_issue_count(text)
    failures: list[str] = []
    if line_count < MIN_COMPLETE_PLAYBOOK_LINES:
        failures.append(
            f"line_count={line_count} below minimum {MIN_COMPLETE_PLAYBOOK_LINES}"
        )
    failures.extend(
        f"missing_element={element}"
        for element, present in elements.items()
        if not present
    )
    failures.extend(
        f"missing_process_separation={element}"
        for element, present in process_separation.items()
        if not present
    )
    if standard_sentence_findings:
        failures.append("csr_standard_sentences_contain_internal_process_language")
    if standard_sentence_style is not None and standard_sentence_style.blocked:
        failures.append("csr_standard_sentences_contain_banned_style_expression")
    if not style_section:
        failures.append("missing_style_section=writing_style")
    failures.extend(
        f"missing_style_requirement={phrase}"
        for phrase, present in style_requirements.items()
        if not present
    )
    if review_issue_count < MIN_REVIEW_ISSUES:
        failures.append(
            f"review_issue_count={review_issue_count} below minimum {MIN_REVIEW_ISSUES}"
        )
    return {
        "filename": path.name,
        "sha256": _sha256(content),
        "line_count": line_count,
        "minimum_line_count": MIN_COMPLETE_PLAYBOOK_LINES,
        "seven_elements": elements,
        "process_separation": process_separation,
        "standard_sentence_internal_language_findings": standard_sentence_findings,
        "standard_sentence_style_findings": (
            list(standard_sentence_style.findings)
            if standard_sentence_style is not None
            else []
        ),
        "style_section_present": bool(style_section),
        "style_requirements": style_requirements,
        "review_issue_count": review_issue_count,
        "minimum_review_issue_count": MIN_REVIEW_ISSUES,
        "status": "pass" if not failures else "blocked",
        "failures": failures,
    }


def audit_complete_playbooks(
    directory: Path,
    filenames: Iterable[str] = COMPLETE_PLAYBOOK_FILENAMES,
) -> dict[str, Any]:
    records = [inspect_complete_playbook(directory / name) for name in filenames]
    return {
        "schema_version": "1.0",
        "status": "pass" if all(item["status"] == "pass" for item in records) else "blocked",
        "policy": {
            "minimum_line_count": MIN_COMPLETE_PLAYBOOK_LINES,
            "required_elements": list(SEVEN_ELEMENTS),
            "required_process_separation": list(PROCESS_SEPARATION_ELEMENTS),
            "required_style_section": STYLE_SECTION_HEADING,
            "required_style_phrases": list(STYLE_REQUIRED_PHRASES),
            "minimum_common_review_issues": MIN_REVIEW_ISSUES,
        },
        "playbook_count": len(records),
        "records": records,
    }
