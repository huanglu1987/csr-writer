"""Deterministic section 16.0 appendix submission-status planning."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .io_utils import load_yaml
from .paths import DEFAULT_STRUCTURE


CATEGORY_TITLES = {
    "csr_required": "1. CSR 必需审评附件",
    "regional": "2. 本次注册申请需提交的地区性附件",
    "tmf_on_request": "3. TMF 留存、可按要求提供的附件",
    "not_applicable": "4. 不适用附件",
}


def _raw_section_index(structure_path: str | Path) -> dict[str, dict[str, Any]]:
    payload = load_yaml(structure_path)
    index: dict[str, dict[str, Any]] = {}

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            section_id = item.get("id")
            if isinstance(section_id, str):
                index[section_id] = item
            for key in ("children", "children_official", "children_convention"):
                visit(item.get(key))

    visit(payload.get("sections"))
    return index


def build_appendix_status_plan(
    facts: Mapping[str, Any],
    *,
    structure_path: str | Path = DEFAULT_STRUCTURE,
    submission_region: str = "CDE",
) -> dict[str, Any]:
    """Build a conservative appendix register from E3 metadata and Q&A Q3 notes."""

    index = _raw_section_index(structure_path)
    randomized = bool(facts.get("design", {}).get("randomized"))

    def title(section_id: str) -> str:
        raw = index[section_id]
        return str(raw.get("zh") or raw.get("en") or section_id)

    rows: list[dict[str, Any]] = []

    def add(
        category: str,
        section_id: str,
        submitted: str,
        location: str,
        reason: str,
        *,
        name_suffix: str = "",
    ) -> None:
        raw = index.get(section_id, {})
        rows.append(
            {
                "category": category,
                "category_title": CATEGORY_TITLES[category],
                "appendix_id": section_id,
                "file_name": title(section_id) + name_suffix,
                "submitted": submitted,
                "submission_location": location,
                "reason_or_on_request": reason,
                "e3_submit_metadata": raw.get("submit", "not-specified"),
                "qa_basis": raw.get("qa_note", ""),
            }
        )

    for section_id in ("16.1.1", "16.1.2", "16.1.5", "16.1.9"):
        add(
            "csr_required",
            section_id,
            "是",
            f"CSR {section_id}",
            "E3 核心审评附件；具体文件版本以正式附件清单为准。",
        )
    add(
        "csr_required" if randomized else "not_applicable",
        "16.1.7",
        "是" if randomized else "否",
        "CSR 16.1.7" if randomized else "不适用",
        (
            "本研究为随机化设计。"
            if randomized
            else "本研究为非随机化设计。"
        ),
    )
    add(
        "csr_required",
        "16.1.4",
        "部分提交",
        "CSR 16.1.4",
        "研究者和中心名单纳入附件；个人简历按 E3 Q&A Q3 留 TMF。",
        name_suffix="（名单部分）",
    )

    for section_id in (
        "16.2.1",
        "16.2.2",
        "16.2.3",
        "16.2.4",
        "16.2.5",
        "16.2.6",
        "16.2.7",
        "16.2.8",
        "16.3.1",
        "16.3.2",
    ):
        add(
            "regional",
            section_id,
            "条件性",
            "地区性申报附件位置",
            "由本次申报资料清单和实际事件集合决定；本结构轮不推断文件是否存在。",
        )

    add(
        "tmf_on_request",
        "16.1.3",
        "否",
        "TMF",
        "伦理批件、患者书面信息和知情同意文件通常留 TMF，可按监管要求提供（E3 Q&A Q3）。",
    )
    add(
        "tmf_on_request",
        "16.1.4",
        "否",
        "TMF",
        "研究者个人简历通常不纳入 CSR，可按要求提供（E3 Q&A Q3）。",
        name_suffix="（个人简历部分）",
    )
    add(
        "tmf_on_request",
        "16.1.6",
        "否",
        "TMF",
        "按受试者列出的批号信息通常留 TMF，可按要求提供（E3 Q&A Q3）。",
    )

    for section_id, reason in (
        ("16.1.8", "当前已确认研究事实未证实存在可提交的稽查证书。"),
        ("16.1.10", "当前已确认研究事实未证实使用实验室间标准化方法。"),
        ("16.1.11", "当前已确认研究事实未证实存在基于本研究的发表文章。"),
        ("16.1.12", "当前已确认研究事实未证实存在需作为附件的重要引用文献。"),
    ):
        add("not_applicable", section_id, "否", "不适用", reason)
    add(
        "not_applicable" if submission_region != "US" else "regional",
        "16.4",
        "否" if submission_region != "US" else "条件性",
        "不适用" if submission_region != "US" else "US 存档列表",
        f"本次提交地区为 {submission_region}；16.4 仅为 US 存档列表。",
    )

    grouped = [
        {
            "category": category,
            "title": title_text,
            "rows": [row for row in rows if row["category"] == category],
        }
        for category, title_text in CATEGORY_TITLES.items()
    ]
    return {
        "schema_version": "1.1",
        "status": "pass",
        "submission_region": submission_region,
        "source": "ICH E3 structure submit metadata and E3 Q&A Q3 notes",
        "categories": grouped,
        "rows": rows,
        # ``entries`` is the stable public name used by downstream delivery
        # reports.  ``rows`` remains as a backward-compatible alias.
        "entries": rows,
        "entry_count": len(rows),
        "unique_appendix_count": len({row["appendix_id"] for row in rows}),
        "complete_listing_policy": (
            "Every official 16.x item remains in the status register; "
            "not submitted is a status, never a deletion instruction."
        ),
    }
