"""Shared deterministic TFL-number to expected CSR-section routing."""

from __future__ import annotations

from dataclasses import dataclass

from .cross_references import normalize_entity_id


@dataclass(frozen=True)
class ExpectedTFLRoute:
    section_id: str
    category_id: str
    label_zh: str


def _within(number: str, prefix: str) -> bool:
    """Match one numbering node without treating ``.3`` as a prefix of ``.35``."""

    return number == prefix or number.startswith(prefix + ".")


def expected_tfl_route(entity_id: str) -> ExpectedTFLRoute | None:
    canonical = normalize_entity_id(entity_id)
    number = canonical[1:]
    if _within(number, "14.1.1") or _within(number, "16.2.1"):
        return ExpectedTFLRoute("10.1", "10.1.disposition", "§10.1 受试者处置")
    if _within(number, "14.1.2") or _within(number, "16.2.4"):
        return ExpectedTFLRoute("11.2", "11.2.baseline", "§11.2 人口学与基线特征")
    if _within(number, "14.1.3") or _within(number, "14.1.4"):
        return ExpectedTFLRoute(
            "11.2", "11.2.baseline", "§11.2 人口学与基线特征"
        )
    if _within(number, "16.2.2"):
        return ExpectedTFLRoute("10.2", "10.2.deviations", "§10.2 方案偏离与相关资料")
    if _within(number, "14.2.4"):
        return ExpectedTFLRoute("11.5", "11.5.pk", "§11.5 药代动力学评价")
    if _within(number, "14.2"):
        return ExpectedTFLRoute("11.4", "11.4.efficacy", "§11.4 疗效")
    if _within(number, "16.2.3"):
        return ExpectedTFLRoute("10.3", "10.3.analysis_sets", "§10.3 分析数据集")
    if _within(number, "16.2.5"):
        return ExpectedTFLRoute("11.3", "11.3.compliance", "§11.3 治疗依从性")
    if _within(number, "16.2.6"):
        return ExpectedTFLRoute("11.4.3", "11.4.3.individual", "§11.4.3 个体疗效数据")
    if _within(number, "14.3.1.3.3") or _within(number, "14.3.1.3.4"):
        return ExpectedTFLRoute(
            "12.5.1", "12.5.1.local_tolerance", "§12.5.1 局部耐受性"
        )
    if _within(number, "14.3.1.1"):
        return ExpectedTFLRoute("12.1", "12.1.exposure", "§12.1 暴露程度")
    if (
        _within(number, "14.3.1.2")
        or _within(number, "14.3.1.3")
        or _within(number, "16.2.7")
    ):
        return ExpectedTFLRoute("12.2", "12.2.ae", "§12.2 不良事件")
    if _within(number, "16.2.8"):
        return ExpectedTFLRoute("12.4", "12.4.laboratory", "§12.4 临床实验室评估")
    if _within(number, "14.3"):
        return ExpectedTFLRoute("12", "12.safety_other", "§12 其他安全性内容")
    return None


def expected_tfl_sections(entity_id: str) -> list[str]:
    route = expected_tfl_route(entity_id)
    return [route.section_id] if route is not None else []
