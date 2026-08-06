"""OOXML-only structural QC for the M9 pruned project skeleton."""

from __future__ import annotations

import re
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from lxml import etree

from .cross_references import bookmark_name
from .io_utils import sha256_file
from .publication_refs import validate_publication_reference_map


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
W = f"{{{W_NS}}}"


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def audit_pruned_project_docx(
    path: str | Path,
    skeleton_report: Mapping[str, Any],
    *,
    publication_reference_map: Mapping[str, Any] | None = None,
    clean: bool = False,
) -> dict[str, Any]:
    source = Path(path)
    with zipfile.ZipFile(source) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    paragraphs = root.xpath(".//w:p", namespaces=NS)
    visible = "\n".join(_paragraph_text(paragraph) for paragraph in paragraphs)
    bookmark_nodes = root.xpath(".//w:bookmarkStart", namespaces=NS)
    bookmarks = [node.get(W + "name", "") for node in bookmark_nodes]
    section_styles: dict[str, str] = {}
    section_text: dict[str, str] = {}
    for paragraph in paragraphs:
        starts = paragraph.xpath("./w:bookmarkStart", namespaces=NS)
        for start in starts:
            name = start.get(W + "name", "")
            if not name.startswith("_Ref_Sec_"):
                continue
            style = paragraph.find("./w:pPr/w:pStyle", namespaces=NS)
            section_styles[name] = style.get(W + "val", "") if style is not None else ""
            section_text[name] = _paragraph_text(paragraph)
    instructions = [
        "".join(node.itertext()).strip()
        for node in root.xpath(".//w:instrText", namespaces=NS)
    ]
    checks: list[dict[str, Any]] = []

    def add(code: str, passed: bool, detail: Any) -> None:
        checks.append(
            {"code": code, "status": "pass" if passed else "fail", "detail": detail}
        )

    forbidden = (
        "11.4.2.7",
        "11.4.5",
        "11.4.2.3",
        "11.4.2.5",
    )
    retained = set(skeleton_report.get("retained_section_ids", []))
    add(
        "fact_supported_conditional_sections_absent",
        all(section_id not in retained for section_id in forbidden),
        {section_id: section_id in retained for section_id in forbidden},
    )
    add(
        "unsupported_omissions_retained_for_review",
        {"11.4.3", "11.4.6"}.issubset(retained),
        sorted({"11.4.3", "11.4.6"} & retained),
    )
    add(
        "appendix_items_are_status_managed_not_deleted",
        all(
            not str(item.get("section_id", "")).startswith("16")
            for item in skeleton_report.get("omitted_sections", [])
        )
        and "16.4" in retained,
        {
            "appendix_omissions": [
                item.get("section_id")
                for item in skeleton_report.get("omitted_sections", [])
                if str(item.get("section_id", "")).startswith("16")
            ],
            "16.4_retained": "16.4" in retained,
        },
    )
    formal = {"11.5": "Heading2", "11.6": "Heading2", "12.7": "Heading2"}
    add(
        "formal_extension_heading_styles",
        all(section_styles.get("_Ref_Sec_" + key.replace(".", "_")) == style for key, style in formal.items()),
        {
            key: section_styles.get("_Ref_Sec_" + key.replace(".", "_"))
            for key in formal
        },
    )
    add(
        "formal_extension_bookmarks",
        all("_Ref_Sec_" + key.replace(".", "_") in bookmarks for key in formal),
        sorted(name for name in bookmarks if name in {"_Ref_Sec_11_5", "_Ref_Sec_11_6", "_Ref_Sec_12_7"}),
    )
    pk_children = [f"11.5.{value}" for value in range(1, 11)]
    add(
        "pk_branch_complete_and_heading_styled",
        all(
            section_styles.get("_Ref_Sec_" + value.replace(".", "_")) == "Heading3"
            for value in pk_children
        ),
        {
            value: section_styles.get("_Ref_Sec_" + value.replace(".", "_"))
            for value in pk_children
        },
    )
    conclusion_records = skeleton_report.get("conclusion_order", [])
    add(
        "semantic_conclusions_are_last_siblings",
        bool(conclusion_records) and all(item.get("last") for item in conclusion_records),
        conclusion_records,
    )
    numbering_order = skeleton_report.get("numbering_order", {})
    add(
        "visible_section_numbers_are_monotonic",
        numbering_order.get("status") == "pass"
        and not numbering_order.get("inversions"),
        numbering_order,
    )
    add(
        "letter_suffixes_removed",
        not re.search(r"\b11\.[ABCD](?:\b|\.)", visible),
        "no 11.A/11.B/11.C/11.D visible heading",
    )
    add(
        "chapter_3_title_and_directories",
        "3 本临床研究报告目录" in visible
        and all(f"{number} " in visible for number in ("3.1", "3.2", "3.3", "3.4", "3.5", "3.6"))
        and sum(instruction.startswith("TOC ") for instruction in instructions) >= 3,
        [instruction for instruction in instructions if instruction.startswith("TOC ")],
    )
    add(
        "local_tolerance_independent_section",
        section_styles.get("_Ref_Sec_12_5_1") == "Heading3"
        and "12.5.1 局部耐受性评分和用药部位反应" in visible,
        section_styles.get("_Ref_Sec_12_5_1"),
    )
    add(
        "adverse_event_heading_deduplicated",
        visible.count("12.2 不良事件（AE）") == 1,
        visible.count("12.2 不良事件（AE）"),
    )
    add(
        "appendix_status_table",
        "16.0 附录目录及提交状态" in visible
        and all(
            value in visible
            for value in (
                "附录编号",
                "本次是否提交",
                "TMF 留存、可按要求提供的附件",
                "不适用附件",
            )
        ),
        skeleton_report.get("appendix_status_row_count"),
    )
    add(
        "visible_numbering_gap_declarations",
        bool(skeleton_report.get("omission_declaration_blocks"))
        and "本章以下小节因本研究设计不适用而未纳入" in visible,
        skeleton_report.get("omission_declaration_blocks", []),
    )
    add(
        "no_unsupported_module_deletion_basis",
        all(
            basis.get("value") != "no supporting module"
            for item in skeleton_report.get("omitted_sections", [])
            for basis in item.get("basis", [])
            if isinstance(basis, Mapping)
        ),
        "unsupported-module automatic deletion basis count is zero",
    )

    if publication_reference_map is not None:
        entries = validate_publication_reference_map(publication_reference_map)["entries"]
        expected = {bookmark_name(item["publication_entity_id"]) for item in entries}
        refs = {
            match.group("name")
            for instruction in instructions
            if (match := re.fullmatch(r"REF\s+(?P<name>_Ref_[A-Za-z0-9_]+)\s+\\h", instruction))
        }
        add(
            "publication_bookmarks_complete",
            expected.issubset(set(bookmarks)),
            {"expected": len(expected), "observed": len(expected & set(bookmarks))},
        )
        add(
            "publication_ref_fields_resolve",
            refs.issubset(expected) and refs.issubset(set(bookmarks)),
            {"field_targets": len(refs), "unexpected": sorted(refs - expected)},
        )
        add(
            "independent_publication_numbering",
            all("-" in item["publication_entity_id"] for item in entries)
            and "表 14.3-1" in visible,
            [item["publication_entity_id"] for item in entries[:5]],
        )

    if clean:
        forbidden_clean = ("[待医学负责人确认]", "内部溯源", "内部 QC")
        add(
            "clean_has_no_tool_traces",
            all(token not in visible for token in forbidden_clean),
            {token: token in visible for token in forbidden_clean},
        )

    status = "pass" if all(item["status"] == "pass" for item in checks) else "fail"
    return {
        "schema_version": "1.0",
        "status": status,
        "scope": "OOXML structure and cross-reference integrity only",
        "conclusion_boundary": "未发现上述 OOXML 结构类问题" if status == "pass" else "发现 OOXML 结构类问题",
        "docx_sha256": sha256_file(source),
        "check_count": len(checks),
        "checks": checks,
    }
