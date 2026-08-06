"""Conservative semantic red-line review for generated CSR prose.

The guard does not decide whether a medical statement is true. It identifies
statements that the tool is not authorized to make and marks them for review.
It is independent from the writer so conclusion sections can be tested before
their writers exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .conditional_conclusions import CONCLUSION_SECTION_ROOTS


MEDICAL_REVIEW_MARKER = "[待医学负责人确认]"


@dataclass(frozen=True)
class SemanticPattern:
    pattern_id: str
    category: str
    expression: re.Pattern[str]
    message: str


@dataclass(frozen=True)
class SemanticReview:
    text: str
    findings: tuple[dict[str, Any], ...]

    @property
    def requires_human(self) -> bool:
        return bool(self.findings)


@dataclass(frozen=True)
class InternalLanguagePattern:
    """One deterministic pattern for prose that belongs to the authoring process."""

    pattern_id: str
    subclass: str
    expression: re.Pattern[str]
    message: str


@dataclass(frozen=True)
class InternalLanguageReview:
    """Hard-blocking review result for internal authoring language."""

    findings: tuple[dict[str, Any], ...]

    @property
    def blocked(self) -> bool:
        return bool(self.findings)


SEMANTIC_REDLINE_PATTERNS: tuple[SemanticPattern, ...] = (
    SemanticPattern(
        "benefit-risk-conclusion",
        "benefit_risk_conclusion",
        re.compile(
            r"疗效确切|安全性良好|"
            r"获益\s*[-–—]?\s*风险(?:比|评估|结论)?[^\n。；;]{0,24}"
            r"(?:优于|良好|有利|可接受|积极|支持)|"
            r"获益[^\n。；;]{0,12}(?:大于|超过)[^\n。；;]{0,8}风险|"
            r"建议(?:继续|加速|直接|尽快)?推进[^\n。；;]{0,24}(?:期|研究|开发)|"
            r"(?:证实|确证)(?:了)?[^\n。；;]{0,24}(?:疗效|临床获益|安全性)"
        ),
        "Final benefit-risk or clinical-value conclusions require medical-owner review",
    ),
    SemanticPattern(
        "causal-determination",
        "causal_determination",
        re.compile(
            r"(?:不良事件|严重不良事件|AE|SAE)[^\n。；;]{0,36}"
            r"(?:与|同)[^\n。；;]{0,16}(?:研究药物|试验药物|本品)"
            r"[^\n。；;]{0,10}(?:明确|肯定|很可能|可能)?(?:相关|有关|无关)|"
            r"(?:不良事件|严重不良事件|AE|SAE)[^\n。；;]{0,36}"
            r"(?:由|因)[^\n。；;]{0,16}(?:研究药物|试验药物|本品)"
            r"[^\n。；;]{0,10}(?:导致|引起|所致)|"
            r"(?:归因于|归属于)[^\n。；;]{0,16}(?:研究药物|试验药物|本品)",
            re.I,
        ),
        "AE causality must come from reviewed CRF or medical-monitor evidence",
    ),
    SemanticPattern(
        "sae-medical-narrative",
        "sae_medical_narrative",
        re.compile(
            r"(?:严重不良事件|SAE)[^\n。；;]{0,48}"
            r"(?:诊断为|入院|住院|接受[^\n。；;]{0,12}治疗|"
            r"给予[^\n。；;]{0,12}治疗|抢救|死亡|痊愈|恢复|转归|停药|减量)",
            re.I,
        ),
        "SAE narrative medical content requires reviewed source data and an authorized reviewer",
    ),
    SemanticPattern(
        "missing-information-inference",
        "missing_information_inference",
        re.compile(
            r"(?:可|可以|因此|据此)?\s*(?:合理)?(?:推断|推测|猜测|认定)"
            r"[^\n。；;]{0,36}(?:缺失|未提供|未获得|无记录|未记录)|"
            r"(?:缺失|未提供|未获得|无记录|未记录)"
            r"[^\n。；;]{0,36}(?:但|仍|故|因此|可)[^\n。；;]{0,12}"
            r"(?:推断|推测|推定|认为|视为|判断|补全)"
        ),
        "Missing information must be reported as a gap, not inferred or completed",
    ),
)


# These phrases describe the drafting machinery, not the clinical study.  They
# are intentionally separate from semantic red lines: a semantic red line is
# retained and marked for medical review, whereas internal process language is
# rejected before a section artifact can be published.
INTERNAL_PROCESS_LANGUAGE_PATTERNS: tuple[InternalLanguagePattern, ...] = (
    InternalLanguagePattern(
        "tool-self-reference",
        "tool_self_reference",
        re.compile(
            r"写手|本工具|由(?:本)?工具|由(?:本)?模型|"
            r"生成模型|写作模型|起草模型|"
            r"(?:Agent|AI\s*模型|语言模型)(?:生成|起草|输出|推断|改写)?|"
            r"(?:模型|Agent)(?:本身|自行|不得|不能|只可)",
            re.I,
        ),
        "CSR body text must not refer to the writer, Agent, model, or tool",
    ),
    InternalLanguagePattern(
        "internal-data-term",
        "internal_data_term",
        re.compile(
            r"证据包|data\s*pack|datapack|占位符|语义键|绑定|映射",
            re.I,
        ),
        "CSR body text must not expose internal data or placeholder terminology",
    ),
    InternalLanguagePattern(
        "process-negation",
        "process_negation",
        re.compile(
            r"(?:不由|不得|禁止|不能由)[^\n。；;]{0,32}(?:合并|改写|推断|外推)|"
            r"(?:仅限|只限)[^\n。；;]{0,24}范围[^\n。；;]{0,16}(?:不|未)(?:外推|扩展)"
        ),
        "Authoring-process prohibitions must be rewritten as study-facing CSR prose",
    ),
    InternalLanguagePattern(
        "internal-state",
        "internal_state",
        re.compile(r"置信度|\bderived\b|\binferred\b|\bsource[-_ ]?gap\b", re.I),
        "CSR body text must not expose internal confidence or source-gap states",
    ),
)


def scan_internal_process_language(text: str) -> InternalLanguageReview:
    """Return hard-blocking findings for authoring-process language.

    The patterns are deliberately contextual for ``模型`` and do not match
    legitimate clinical phrases such as ``幂函数模型`` or
    ``经审核的统计分析计划``.
    """

    if not isinstance(text, str):
        raise ValueError("text must be a string")
    findings: list[dict[str, Any]] = []
    for rule in INTERNAL_PROCESS_LANGUAGE_PATTERNS:
        for match in rule.expression.finditer(text):
            findings.append(
                {
                    "code": "S-INTERNAL-PROCESS-LANGUAGE",
                    "category": "internal_process_language",
                    "subclass": rule.subclass,
                    "pattern_id": rule.pattern_id,
                    "start": match.start(),
                    "end": match.end(),
                    "matched_text": match.group(0),
                    "message": rule.message,
                }
            )
    findings.sort(key=lambda item: (item["start"], item["end"], item["pattern_id"]))
    return InternalLanguageReview(findings=tuple(findings))


def is_conclusion_section(section_id: str) -> bool:
    """Return whether every paragraph in this section requires medical review."""

    normalized = section_id.strip().rstrip(".")
    return any(
        normalized == root or normalized.startswith(root + ".")
        for root in CONCLUSION_SECTION_ROOTS
    )


def review_semantic_redlines(section_id: str, text: str) -> SemanticReview:
    """Return deterministic red-line findings and marker-injected text."""

    if not isinstance(section_id, str) or not section_id.strip():
        raise ValueError("section_id must be a non-empty string")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")

    findings: list[dict[str, Any]] = []
    if is_conclusion_section(section_id):
        findings.append(
            {
                "code": "S-CONCLUSION-REVIEW",
                "category": "conclusion_section_default",
                "pattern_id": "conclusion-section-default",
                "start": 0,
                "end": len(text),
                "matched_text": "<entire-paragraph>",
                "message": "Conclusion-section paragraph requires medical-owner review",
            }
        )

    for rule in SEMANTIC_REDLINE_PATTERNS:
        for match in rule.expression.finditer(text):
            findings.append(
                {
                    "code": "S-SEMANTIC-REDLINE",
                    "category": rule.category,
                    "pattern_id": rule.pattern_id,
                    "start": match.start(),
                    "end": match.end(),
                    "matched_text": match.group(0),
                    "message": rule.message,
                }
            )

    guarded_text = text
    if findings and MEDICAL_REVIEW_MARKER not in text:
        guarded_text = f"{MEDICAL_REVIEW_MARKER} {text}"
    return SemanticReview(text=guarded_text, findings=tuple(findings))
