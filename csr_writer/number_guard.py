"""M2 fact-derived literal whitelist and bare-number guard (Guard C)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .errors import ConfigurationError


STATIC_ALLOWED: tuple[tuple[str, str], ...] = (
    (r"ICH\s*E\d+\s*\(?R?\d*\)?", "standard identifier"),
    (r"第\s*\d+(?:\.\d+)*\s*[章节]", "section reference"),
    (r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", "calendar date"),
    (r"\d{4}-\d{2}-\d{2}", "ISO date"),
    (r"CTCAE\s*v?\d+(?:\.\d+)*", "coding standard version"),
    (r"MedDRA\s*(?:版本)?\s*\d+(?:\.\d+)*", "coding dictionary version"),
    (r"[1-5]\s*级", "AE grade vocabulary"),
    (r"[IVX]+\s*期", "study phase label; never used for structural routing"),
    (r"95\s*%\s*(?:置信区间|CI)", "fixed confidence-interval term"),
    (r"双侧\s*0?\.\d+", "fixed analysis-method term"),
)


@dataclass(frozen=True)
class WhitelistEntry:
    pattern: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"pattern": self.pattern, "source": self.source}


@dataclass(frozen=True)
class CompiledWhitelist:
    entries: tuple[WhitelistEntry, ...]
    facts_sha256: str

    def strip(self, text: str) -> str:
        for entry in self.entries:
            text = re.sub(entry.pattern, " ", text, flags=re.I)
        return text


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    return value


def _fact(facts: dict[str, Any], nested: dict[str, Any], key: str, default: Any = None) -> Any:
    return nested[key] if key in nested else facts.get(key, default)


def _number_text(value: Any, fact_path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigurationError(f"{fact_path} must contain numeric values")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigurationError(f"{fact_path} contains non-numeric value {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ConfigurationError(f"{fact_path} must contain finite non-negative values")
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _literal_number_pattern(value: Any, fact_path: str) -> str:
    number = _number_text(value, fact_path)
    if "." not in number:
        body = re.escape(number) + r"(?:\.0+)?"
    else:
        body = re.escape(number.rstrip("0").rstrip(".")) + r"0*"
    return rf"(?<![\d.]){body}(?![\d.])"


def compile_whitelist(protocol_facts: dict[str, Any] | None) -> CompiledWhitelist:
    if not isinstance(protocol_facts, dict) or not protocol_facts:
        raise ConfigurationError(
            "protocol_facts/study_facts are required; project-specific numbers never fall back to broad regexes"
        )
    design_raw = protocol_facts.get("design", {})
    if isinstance(design_raw, str):
        design = {"escalation_scheme": design_raw}
    else:
        design = _mapping(design_raw, "design")
    # M3 freezes ``data_domains`` as the canonical five-dimensional facts shape.
    # ``data`` remains a read-only compatibility path for already accepted M2 inputs;
    # values are never merged when the canonical field is present.
    data_domains = _mapping(protocol_facts.get("data_domains", {}), "data_domains")
    legacy_data = _mapping(protocol_facts.get("data", {}), "data")

    entries = [WhitelistEntry(pattern, f"static:{source}") for pattern, source in STATIC_ALLOWED]
    protocol_id = protocol_facts.get("protocol_id") or protocol_facts.get("protocol")
    if protocol_id:
        if not isinstance(protocol_id, str):
            raise ConfigurationError("protocol_id must be a string")
        entries.append(WhitelistEntry(re.escape(protocol_id), "fact:protocol_id"))

    escalation = _fact(protocol_facts, design, "escalation_scheme")
    if escalation in {"3+3", "3 + 3"}:
        entries.append(WhitelistEntry(r"3\s*\+\s*3", "fact:design.escalation_scheme"))

    canonical_doses = design.get("dose_levels")
    dose_values: list[Any] = []
    if canonical_doses is not None:
        if not isinstance(canonical_doses, list):
            raise ConfigurationError("design.dose_levels must be a list")
        for index, dose in enumerate(canonical_doses):
            if not isinstance(dose, dict) or set(dose) != {"value", "unit"}:
                raise ConfigurationError(
                    f"design.dose_levels[{index}] must contain exactly value and unit"
                )
            unit = dose["unit"]
            if not isinstance(unit, str) or not unit.strip():
                raise ConfigurationError(f"design.dose_levels[{index}].unit must be non-empty")
            number = _literal_number_pattern(
                dose["value"], f"design.dose_levels[{index}].value"
            )
            dose_values.append(dose["value"])
            entries.append(
                WhitelistEntry(
                    rf"{number}\s*{re.escape(unit.strip())}(?![A-Za-z/])",
                    f"fact:design.dose_levels[{index}]",
                )
            )
    else:
        legacy_doses = _fact(protocol_facts, design, "dose_levels_mg", []) or []
        if not isinstance(legacy_doses, list):
            raise ConfigurationError("design.dose_levels_mg must be a list")
        for index, dose in enumerate(legacy_doses):
            number = _literal_number_pattern(dose, f"design.dose_levels_mg[{index}]")
            dose_values.append(dose)
            entries.append(
                WhitelistEntry(
                    rf"{number}\s*mg(?!\s*/)",
                    f"fact:design.dose_levels_mg[{index}]",
                )
            )
    declared_n = _fact(protocol_facts, design, "n_dose_levels")
    if declared_n is not None and int(declared_n) != len(dose_values):
        raise ConfigurationError(
            f"design.n_dose_levels={declared_n} conflicts with {len(dose_values)} dose-level entries"
        )
    if dose_values:
        entries.append(
            WhitelistEntry(
                rf"{_literal_number_pattern(len(dose_values), 'derived:n_dose_levels')}\s*个剂量(?:水平|组)",
                "derived:length(design.dose_levels)",
            )
        )

    cycle_days = _fact(protocol_facts, design, "cycle_days")
    if cycle_days is not None:
        cycle = _literal_number_pattern(cycle_days, "design.cycle_days")
        entries.extend(
            [
                WhitelistEntry(
                    rf"每\s*{cycle}\s*天\s*为\s*{_literal_number_pattern(1, 'derived:cycle_definition')}\s*个周期",
                    "derived:one cycle per design.cycle_days",
                ),
                WhitelistEntry(rf"每\s*{cycle}\s*天", "fact:design.cycle_days"),
                WhitelistEntry(rf"{cycle}\s*天", "fact:design.cycle_days"),
            ]
        )

    interval_weeks = _fact(protocol_facts, design, "dosing_interval_weeks")
    if interval_weeks is not None:
        interval = _literal_number_pattern(interval_weeks, "design.dosing_interval_weeks")
        entries.extend(
            [
                WhitelistEntry(
                    rf"每\s*{interval}\s*周(?:\s*给药)?\s*(?:1|一)\s*次",
                    "derived:once per design.dosing_interval_weeks",
                ),
                WhitelistEntry(rf"Q\s*{interval}\s*W", "fact:design.dosing_interval_weeks"),
                WhitelistEntry(rf"每\s*{interval}\s*周", "fact:design.dosing_interval_weeks"),
            ]
        )

    dlt_window = _mapping(_fact(protocol_facts, design, "dlt_window", {}), "design.dlt_window")
    if dlt_window:
        for key, suffix in (("cycle", "周期"), ("day_from", "天"), ("day_to", "天")):
            if key not in dlt_window:
                raise ConfigurationError(f"design.dlt_window is missing {key}")
            value = _literal_number_pattern(dlt_window[key], f"design.dlt_window.{key}")
            entries.append(WhitelistEntry(rf"第\s*{value}\s*{suffix}", f"fact:design.dlt_window.{key}"))

    if "pk_sampling_timepoints_h" in data_domains:
        timepoints = data_domains["pk_sampling_timepoints_h"] or []
        timepoint_path = "data_domains.pk_sampling_timepoints_h"
    else:
        timepoints = legacy_data.get(
            "pk_timepoints_h", protocol_facts.get("pk_timepoints_h", [])
        ) or []
        timepoint_path = "data.pk_timepoints_h"
    if not isinstance(timepoints, list):
        raise ConfigurationError(f"{timepoint_path} must be a list")
    for index, timepoint in enumerate(timepoints):
        value = _literal_number_pattern(timepoint, f"{timepoint_path}[{index}]")
        entries.append(
            WhitelistEntry(
                rf"{value}(?=\s*(?:[、,，]|小时))",
                f"fact:{timepoint_path}[{index}]",
            )
        )

    return CompiledWhitelist(tuple(entries), _canonical_hash(protocol_facts))


ASCII_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?(?:(?:\d{1,3}(?:,\d{3})+)|(?:\d+(?:\.\d*)?)|(?:\.\d+))"
    r"(?:[eE][-+]?\d+)?\s*%?"
)
FULLWIDTH_NUMBER_RE = re.compile(r"[＋－-]?[０-９]+(?:[．.][０-９]+)?\s*[％%]?")
COMMON_CHINESE_NUMERAL = "零〇一二两三四五六七八九十百千万亿億点"
FINANCIAL_CHINESE_NUMERAL = "零壹贰叁肆伍陆柒捌玖拾佰仟萬万億亿"
ALL_CHINESE_NUMERAL = COMMON_CHINESE_NUMERAL + FINANCIAL_CHINESE_NUMERAL
APPROXIMATE_PREFIX = r"(?:(?:约|近|接近|将近|大约|约有|超过|逾|不足|不到|多于|少于)\s*)?"

CHINESE_PERCENT_RE = re.compile(rf"百分之[{ALL_CHINESE_NUMERAL}]+")
CHINESE_TENTHS_RE = re.compile(
    rf"{APPROXIMATE_PREFIX}[{ALL_CHINESE_NUMERAL}]+成(?:半|[{ALL_CHINESE_NUMERAL}]+)?(?!不变)"
)
CHINESE_FRACTION_RE = re.compile(
    rf"{APPROXIMATE_PREFIX}[{ALL_CHINESE_NUMERAL}]+分之[{ALL_CHINESE_NUMERAL}]+"
)
CHINESE_MULTIPLE_RE = re.compile(
    rf"{APPROXIMATE_PREFIX}(?:[{ALL_CHINESE_NUMERAL}]+|半|数)倍"
)
FINANCIAL_NUMBER_RE = re.compile(
    rf"(?<![{ALL_CHINESE_NUMERAL}])"
    rf"(?=[{FINANCIAL_CHINESE_NUMERAL}]*[壹贰叁肆伍陆柒捌玖拾佰仟萬億])"
    rf"[{FINANCIAL_CHINESE_NUMERAL}]+"
    rf"(?:\s*(?:百分点|个百分点|例|人|名|个|次|天|周|小时|周期|队列|倍))?"
    rf"(?![{ALL_CHINESE_NUMERAL}])"
)
FUZZY_QUANTITY_RE = re.compile(
    r"(?:(?:约|近|接近|将近|大约|约有|超过|逾|不足|不到)\s*)?"
    r"(?:半数|过半|近半|一半|多数|大多数|大部分|绝大部分)"
)
CHINESE_COUNT_RE = re.compile(
    rf"[{COMMON_CHINESE_NUMERAL}]+\s*(?:个百分点|百分点|例|人|名|个|次|天|周|小时|周期|队列)"
)
Q_SCHEDULE_RE = re.compile(r"Q\s*\d+\s*W", re.I)

# Lexical PK parameter names are masked before tokenization. They deliberately
# do not enter either the static or fact-derived numeric whitelist.
PK_PARAMETER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"t\s*1\s*/\s*2|"
    r"AUC\s*0\s*[-–—]\s*(?:∞|t|last|tau|τ|\d+(?:\.\d+)?)"
    r")(?![A-Za-z0-9])",
    re.I,
)


def _mask_tokenization_exceptions(text: str) -> str:
    chars = list(text)
    for match in PK_PARAMETER_TOKEN_RE.finditer(text):
        chars[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def find_bare_numbers(text: str, whitelist: CompiledWhitelist) -> list[dict[str, Any]]:
    probe = _mask_tokenization_exceptions(whitelist.strip(text))
    findings: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    def collect(regex: re.Pattern[str], kind: str) -> None:
        for match in regex.finditer(probe):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append((match.start(), match.end()))
            context = probe[max(0, match.start() - 24) : match.end() + 24].replace("\n", " ")
            findings.append(
                {
                    "code": "E-BARENUM",
                    "kind": kind,
                    "token": match.group(0).strip(),
                    "start": match.start(),
                    "context": f"…{context}…",
                    "message": "Draft contains a literal number not authorized by static rules or protocol facts",
                }
            )

    collect(FULLWIDTH_NUMBER_RE, "fullwidth-number")
    collect(FINANCIAL_NUMBER_RE, "financial-chinese-number")
    # A percentage is also syntactically an "X 分之 Y" fraction.  Collect the
    # more specific form first so the evidence report retains the right meaning.
    collect(CHINESE_PERCENT_RE, "chinese-percentage")
    collect(CHINESE_FRACTION_RE, "chinese-fraction")
    collect(CHINESE_TENTHS_RE, "chinese-tenths")
    collect(CHINESE_MULTIPLE_RE, "chinese-multiple")
    collect(FUZZY_QUANTITY_RE, "fuzzy-quantity")
    collect(CHINESE_COUNT_RE, "chinese-number-with-unit")
    collect(Q_SCHEDULE_RE, "schedule")
    collect(ASCII_NUMBER_RE, "ascii-number")
    return sorted(findings, key=lambda item: item["start"])
