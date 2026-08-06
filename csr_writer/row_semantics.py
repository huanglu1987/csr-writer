"""Conservative row semantics shared by shell mapping and result ingestion.

Shell labels that cannot be represented by a reviewed, readable identifier are
not frozen as result keys.  They are deferred until the value-bearing TFL
provides the real data-driven row labels.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass


SAFE_ASCII_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-/]*$")
OPAQUE_ROW_SEGMENT_RE = re.compile(r"^ROW_[0-9A-F]{10}$")
POSITION_ROW_SEGMENT_RE = re.compile(r"^(?:ROW_POSITION|POSITION_BLOCK)_\d+$")

_PLACEHOLDER_LABEL_PATTERNS = (
    re.compile(r"^(?:系统器官分类|首选术语|SOC|PT)\s*\d*$", re.I),
    re.compile(r"^(?:System\s+Organ\s+Class|Preferred\s+Term)\s*\d*$", re.I),
    re.compile(r"^(?:ATC(?:\s*分类)?|药物名|偏离类别)\s*\d+$", re.I),
)

_RAW_ALIASES = {
    "n": "N",
    "均值": "MEAN",
    "mean": "MEAN",
    "标准差": "SD",
    "sd": "SD",
    "均值（标准差）": "MEAN_SD",
    "mean (sd)": "MEAN_SD",
    "mean(sd)": "MEAN_SD",
    "n (%)": "N_PCT",
    "n(%)": "N_PCT",
    "n（%）": "N_PCT",
    "中位数": "MEDIAN",
    "median": "MEDIAN",
    "最小值": "MIN",
    "min": "MIN",
    "最大值": "MAX",
    "max": "MAX",
    "最小值，最大值": "MIN_MAX",
    "min, max": "MIN_MAX",
    "p值": "P_VALUE",
    "p-value": "P_VALUE",
    "p value": "P_VALUE",
    "95%ci": "CI95",
    "95% ci": "CI95",
    "ls mean": "LS_MEAN",
    "ls mean diff": "LS_MEAN_DIFFERENCE",
    "ls mean difference": "LS_MEAN_DIFFERENCE",
    "基线": "BASELINE",
    "实测值": "OBSERVED_VALUE",
    "较基线变化值": "CHANGE_FROM_BASELINE",
    "<80%": "LT_80_PCT",
    "80%~120%": "PCT_80_TO_120",
    "80%～120%": "PCT_80_TO_120",
    ">120%": "GT_120_PCT",
    "cmax": "CMAX",
    "tmax": "TMAX",
    "t1/2": "T_HALF",
    "auc0-t": "AUC0_T",
    "auc0-∞": "AUC0_INF",
}

# The lookup value is NFKC-normalized below, so configuration keys must pass
# through the identical normalization pipeline.  This keeps full-width Chinese
# punctuation aliases reachable instead of leaving dead dictionary entries.
_ALIASES = {
    unicodedata.normalize("NFKC", key).strip().casefold(): value
    for key, value in _RAW_ALIASES.items()
}


@dataclass(frozen=True)
class RowSegment:
    segment: str
    semantic: bool
    origin: str


def _opaque_id(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10].upper()
    return f"ROW_{digest}"


def classify_shell_row_segment(label: str) -> RowSegment:
    """Classify one shell label without treating an opaque hash as semantics."""

    normalized = unicodedata.normalize("NFKC", label).strip()
    if not normalized:
        return RowSegment("ROW_POSITION_0", False, "blank_position")
    if any(pattern.fullmatch(normalized) for pattern in _PLACEHOLDER_LABEL_PATTERNS):
        return RowSegment(_opaque_id(normalized), False, "placeholder_label")
    alias = _ALIASES.get(normalized.casefold())
    if alias is not None:
        return RowSegment(alias, True, "reviewed_alias")
    compact = re.sub(r"\s+", "", normalized)
    lowered = compact.casefold()
    if "95" in compact and ("ci" in lowered or "置信区间" in compact):
        prefix = "BETWEEN_GROUP_" if any(
            token in compact for token in ("组间", "率差", "差值")
        ) else ""
        return RowSegment(prefix + "CI95", True, "reviewed_statistic_pattern")
    if ("ls" in lowered or "最小二乘" in compact) and "均值" in compact:
        suffix = "_DIFFERENCE" if any(
            token in compact for token in ("差", "difference")
        ) else ""
        if "se" in lowered or "标准误" in compact:
            suffix += "_SE"
        return RowSegment("LS_MEAN" + suffix, True, "reviewed_statistic_pattern")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").upper()
    if safe and SAFE_ASCII_RE.fullmatch(safe):
        return RowSegment(safe[:64], True, "readable_ascii")
    return RowSegment(_opaque_id(normalized), False, "opaque_label_hash")


def row_id_is_semantic(row_id: str) -> bool:
    """Return false when any hierarchy segment is positional or opaque."""

    return bool(row_id) and all(
        OPAQUE_ROW_SEGMENT_RE.fullmatch(segment) is None
        and POSITION_ROW_SEGMENT_RE.fullmatch(segment) is None
        for segment in row_id.split("/")
    )


def expected_result_row_source(table_id: str) -> str:
    """Describe the controlled vocabulary expected when result rows materialize."""

    if table_id == "T14.1.1.3":
        return "protocol_deviation_category_from_result_tfl"
    if table_id.startswith("T14.1.3.") or table_id.startswith("T14.3.1.3."):
        return "meddra_soc_pt_from_result_tfl"
    if table_id.startswith("T14.1.4."):
        return "whodrug_atc_and_drug_name_from_result_tfl"
    if table_id == "T14.2.4.2":
        return "pk_steady_state_row_labels_from_result_tfl"
    return "result_tfl_row_labels"


def readable_result_segment(label: str, semantic_source: str, *, has_parent: bool) -> str:
    """Build a readable result-stage segment while escaping key delimiters."""

    normalized = unicodedata.normalize("NFKC", label).strip()
    if not normalized:
        raise ValueError("result row label is blank")
    replacements = {"|": "｜", "=": "＝", "{": "｛", "}": "｝", "/": "／"}
    escaped = "".join(replacements.get(character, character) for character in normalized)
    escaped = re.sub(r"\s+", "_", escaped)
    if semantic_source == "meddra_soc_pt_from_result_tfl":
        prefix = "PT" if has_parent else "SOC"
    elif semantic_source == "whodrug_atc_and_drug_name_from_result_tfl":
        prefix = "DRUG" if has_parent else "ATC"
    elif semantic_source == "protocol_deviation_category_from_result_tfl":
        prefix = "DEVIATION"
    else:
        prefix = "RESULT"
    if escaped.upper().startswith(prefix + "_"):
        value = escaped
    else:
        value = f"{prefix}_{escaped}"
    if len(value) <= 96:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10].upper()
    return value[:80] + "_" + digest
