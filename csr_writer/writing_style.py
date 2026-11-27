"""Deterministic Chinese CSR prose-style checks.

The checks in this module regulate *how* a reviewed fact is phrased.  They do
not relax numeric, semantic, provenance, or medical-review guards.  Numeric and
reference placeholders are masked before sentence-length measurements so a
long semantic key cannot create a false style warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal


STYLE_SECTION_HEADING = "## 文风规范"

_VALUE_PLACEHOLDER_RE = re.compile(r"\{\{(?!@)[^{}]+\}\}")
_CONTROL_PLACEHOLDER_RE = re.compile(r"\{\{@(?:ref|conclusion):[^{}]+\}\}")

_BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("blq_as_zero", re.compile(r"(?:低于定量下限|BLQ)[^。！？；\n]{0,12}(?:等同于零|归零|已被完全清除)", re.I)),
    ("summary_as_copula", re.compile(r"汇总(?:结果)?为")),
    (
        "abstract_noun_as_field",
        re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{1,12}性为"),
    ),
)

_ZERO_EVENT_RESULT_RE = re.compile(
    r"未发生(?:任何)?(?:死亡|严重不良事件|SAE|TEAE|TRAE|"
    r"导致(?:永久)?停(?:止|用)研究治疗(?:的)?(?:不良)?事件|重度(?:不良)?事件)"
)

_N_PERCENT_PAIR_RE = re.compile(
    r"(?<![\d.])(?P<n>\d+)\s*(?P<unit>例|名|人)?\s*"
    r"[（(]\s*(?P<percent>\d+(?:\.\d+)?)\s*(?P<percent_sign>%|％)?\s*[）)]"
)
_N_PERCENT_CONTEXT_RE = re.compile(
    r"(?:见于|发生|出现|报告|其中|共有|共|受试者|患者|不良事件|TEAE|TRAE|SAE)$",
    re.I,
)

_ABSTRACT_NOUN_TERMS = (
    "可计算性",
    "完整性",
    "支持性",
    "探索性",
    "严重性",
    "一致性",
    "局限性",
)

_SHORT_FIELD_COPULA_RE = re.compile(r"^[^，,；;：:。！？]{1,16}为[^。！？]{1,36}[。！？]$")


@dataclass(frozen=True)
class StyleFinding:
    code: str
    severity: Literal["block", "warning"]
    category: str
    message: str
    matched_text: str | None = None
    paragraph_index: int | None = None
    sentence_length: int | None = None


@dataclass(frozen=True)
class WritingStyleReview:
    status: str
    blocked: bool
    findings: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]


def _mask_placeholders(text: str) -> str:
    masked = _VALUE_PLACEHOLDER_RE.sub("〔数值〕", text)
    return _CONTROL_PLACEHOLDER_RE.sub("〔受控字段〕", masked)


def _prose_paragraphs(text: str) -> list[str]:
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
        and not paragraph.lstrip().startswith(("#", ">", "- ", "|"))
    ]


def _sentences(paragraphs: list[str]) -> list[str]:
    sentences: list[str] = []
    for paragraph in paragraphs:
        sentences.extend(
            item.strip()
            for item in re.split(r"(?<=[。！？；])", paragraph)
            if item.strip()
        )
    return sentences


def n_percent_format_findings(text: str) -> list[dict[str, Any]]:
    """Find prose event-count pairs that omit the count unit or percent sign.

    A bare ``2 (4.2)`` is ambiguous in isolation because it may also be a
    statistic such as mean (SD).  The rule therefore requires event/count
    context unless either the count unit or percent sign already identifies the
    expression as n (%).  Markdown table rows are excluded; they are governed
    by the TFL renderer rather than CSR prose style.
    """

    findings: list[dict[str, Any]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("|"):
            offset += len(line)
            continue
        for match in _N_PERCENT_PAIR_RE.finditer(line):
            prefix = line[max(0, match.start() - 18) : match.start()].rstrip()
            identified = bool(
                match.group("unit")
                or match.group("percent_sign")
                or _N_PERCENT_CONTEXT_RE.search(prefix)
            )
            if not identified:
                continue
            missing: list[str] = []
            if not match.group("unit"):
                missing.append("count_unit")
            if not match.group("percent_sign"):
                missing.append("percent_sign")
            if not missing:
                continue
            findings.append(
                {
                    "code": "E-STYLE-N-PERCENT-FORMAT",
                    "severity": "block",
                    "category": "n_percent_format",
                    "message": "正文 n (%) 表达必须同时含例数单位和百分号",
                    "matched_text": match.group(0),
                    "missing": missing,
                    "start": offset + match.start(),
                    "end": offset + match.end(),
                }
            )
        offset += len(line)
    return findings


def analyze_writing_style(
    text: str,
    *,
    zero_event_policy: Literal["allow", "block_literal"] = "allow",
    protected_verbatim: tuple[str, ...] = (),
) -> WritingStyleReview:
    """Return blocking style defects, non-blocking warnings, and stable metrics."""

    if not isinstance(text, str):
        raise TypeError("style review text must be a string")
    masked = _mask_placeholders(text)
    paragraphs = _prose_paragraphs(masked)
    sentences = _sentences(paragraphs)
    findings: list[StyleFinding] = []

    for item in n_percent_format_findings(masked):
        findings.append(
            StyleFinding(
                code=item["code"],
                severity="block",
                category=item["category"],
                message=item["message"],
                matched_text=item["matched_text"],
            )
        )

    for category, pattern in _BANNED_PATTERNS:
        for match in pattern.finditer(masked):
            findings.append(
                StyleFinding(
                    code="E-STYLE-BANNED-EXPRESSION",
                    severity="block",
                    category=category,
                    message="正文命中禁用的字段式或机械系动表达",
                    matched_text=match.group(0),
                )
            )

    if zero_event_policy == "block_literal":
        for match in _ZERO_EVENT_RESULT_RE.finditer(masked):
            findings.append(
                StyleFinding(
                    code="E-STYLE-LITERAL-ZERO-EVENT",
                    severity="block",
                    category="literal_zero_event",
                    message="零事件判断必须由取值后的条件化规则生成",
                    matched_text=match.group(0),
                )
            )

    for index, paragraph in enumerate(paragraphs, start=1):
        semicolon_count = paragraph.count("；")
        if semicolon_count > 1:
            findings.append(
                StyleFinding(
                    code="W-STYLE-SEMICOLON-BUDGET",
                    severity="warning",
                    category="semicolon_budget",
                    message=f"单段含 {semicolon_count} 个分号；建议拆句",
                    paragraph_index=index,
                )
            )

    for phrase in ("经审核", "按预设"):
        count = masked.count(phrase)
        if count > 1:
            findings.append(
                StyleFinding(
                    code="W-STYLE-QUALIFIER-BUDGET",
                    severity="warning",
                    category="qualifier_budget",
                    message=f"单节“{phrase}”出现 {count} 次；配额为 1",
                    matched_text=phrase,
                )
            )

    sentence_lengths: list[int] = []
    for sentence in sentences:
        length = len(sentence)
        sentence_lengths.append(length)
        if length > 60:
            protected = any(len(v) > 60 and v in sentence and len(sentence.replace(v,"",1)) <= 60
                            for v in protected_verbatim if v)
            findings.append(
                StyleFinding(
                    code="W-STYLE-VERBATIM-LENGTH" if protected else "E-STYLE-SENTENCE-LENGTH",
                    severity="warning" if protected else "block",
                    category="sentence_length",
                    message=f"单句 {length} 字，含须逐字保留的原文" if protected else f"单句 {length} 字，超过 60 字阈值",
                    matched_text=sentence,
                    sentence_length=length,
                )
            )

    field_enumeration_count = 0
    field_dump_sequence_count = 0
    for paragraph in paragraphs:
        sentence_parts = [
            item.strip() for item in re.findall(r"[^。！？]+[。！？]?", paragraph) if item.strip()
        ]
        longest_field_run = 0
        current_field_run = 0
        for sentence in sentence_parts:
            if _SHORT_FIELD_COPULA_RE.fullmatch(sentence):
                current_field_run += 1
                longest_field_run = max(longest_field_run, current_field_run)
            else:
                current_field_run = 0
        has_field_dump_sequence = longest_field_run >= 3
        if has_field_dump_sequence:
            field_dump_sequence_count += 1
            findings.append(
                StyleFinding(
                    code="W-STYLE-FIELD-DUMP-SEQUENCE",
                    severity="warning",
                    category="field_enumeration",
                    message="连续三个以上短句采用“字段为取值”句式；建议合并为自然叙述",
                    matched_text=paragraph,
                )
            )
        if paragraph.count("；") > 1 or any(
            sentence.count("为") >= 2 for sentence in sentence_parts
        ) or has_field_dump_sequence:
            field_enumeration_count += 1
    metrics = {
        "summary_copula_count": len(re.findall(r"汇总(?:结果)?为", text)),
        "reviewed_qualifier_count": text.count("经审核"),
        "prespecified_qualifier_count": text.count("按预设"),
        "semicolon_count": text.count("；"),
        "abstract_noun_count": sum(text.count(term) for term in _ABSTRACT_NOUN_TERMS),
        "field_enumeration_paragraph_count": field_enumeration_count,
        "field_dump_sequence_count": field_dump_sequence_count,
        "sentence_count": len(sentences),
        "average_sentence_length": (
            round(sum(sentence_lengths) / len(sentence_lengths), 1)
            if sentence_lengths
            else 0.0
        ),
        "overlong_sentence_count": sum(length > 60 for length in sentence_lengths),
        "maximum_sentence_length": max(sentence_lengths, default=0),
        "n_percent_format_error_count": sum(
            finding.category == "n_percent_format" for finding in findings
        ),
    }
    serialized = tuple(finding.__dict__.copy() for finding in findings)
    blocked = any(item["severity"] == "block" for item in serialized)
    return WritingStyleReview(
        status="blocked" if blocked else "pass_with_warning" if serialized else "pass",
        blocked=blocked,
        findings=serialized,
        metrics=metrics,
    )


def style_report(text: str, *, block_literal_zero_events: bool = False, protected_verbatim: tuple[str,...] = ()) -> dict[str, Any]:
    """Serialize a style review for section metadata and QC artifacts."""

    review = analyze_writing_style(
        text,
        zero_event_policy="block_literal" if block_literal_zero_events else "allow",
        protected_verbatim=protected_verbatim,
    )
    return {
        "schema_version": "1.0",
        "status": review.status,
        "blocked": review.blocked,
        "findings": list(review.findings),
        "metrics": review.metrics,
        "policy": {
            "banned_expression_is_blocking": True,
            "semicolon_per_paragraph": 1,
            "qualifier_per_section": 1,
            "sentence_length_hard_threshold": 60,
            "literal_zero_event_claim_requires_conditioned_data": True,
            "n_percent_prose_requires_count_unit_and_percent_sign": True,
        },
    }
