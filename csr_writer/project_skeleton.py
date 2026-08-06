"""Fact-routed annotated/clean project skeletons on the verified M1 scaffold."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .appendix_status import build_appendix_status_plan
from .cross_chapter import REVIEW_MARKER
from .cross_references import REF_PLACEHOLDER_RE, bookmark_name, display_reference
from .deterministic_sections import validate_deterministic_bundle
from .errors import CSRWriterError
from .io_utils import sha256_file
from .m4_assembly import (
    _append_text_with_references,
    validate_clean_docx,
    validate_docx_references,
)
from .paths import (
    DEFAULT_FORMAT_SPEC,
    DEFAULT_STRUCTURE,
    DEFAULT_TEMPLATE,
    DEFAULT_TITLE_OVERRIDES,
)
from .publication_refs import (
    publication_aliases,
    rewrite_reference_text,
    rewrite_content_references,
    validate_publication_reference_map,
)
from .render_docx import _add_complex_field, _atomic_save_docx, render_skeleton
from .resolve_placeholders import PLACEHOLDER_RE
from .section_planner import FORMAL_EXTENSION_TITLES
from .title_overrides import load_title_overrides, localized_title


class ProjectSkeletonError(CSRWriterError):
    """The routed coverage/content manifest cannot form a safe annotated skeleton."""


CONTENT_KEYS = {
    "section_id",
    "variant",
    "title",
    "evidence_status",
    "paragraphs",
    "reference_entities",
}
PARAGRAPH_KEYS = {"text", "sources", "confidence", "needs_human"}

LEGACY_SECTION_ALIASES = {
    "11.A": "12.6",
    "11.B": "11.5",
    "11.C": "12.5.6",
    "11.D": "11.8",
}
EXPLORATORY_VARIANTS = {
    "exploratory",
    "exploratory_primary",
    "dermatology_scales",
    "dermatology_ancova",
    "infection_microbiology",
    "survival",
    "center_effect",
    "subgroup",
}
DIRECTORY_TITLES = {
    "3.1": "正文章节目录",
    "3.2": "正文表格目录",
    "3.3": "正文图目录",
    "3.4": "第 14 章表图目录",
    "3.5": "附录目录",
    "3.6": "单独递交的支持性报告目录",
}


def _section_sort_key(section_id: str) -> tuple[Any, ...]:
    pieces: list[Any] = []
    for part in re.split(r"[.]", section_id):
        match = re.fullmatch(r"(?P<number>\d+)(?P<suffix>[A-Z]+)?", part)
        if match:
            pieces.extend((int(match.group("number")), match.group("suffix") or ""))
        else:
            pieces.extend((10**6, part))
    return tuple(pieces)


def _formalize_content_section(item: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(item)
    section_id = str(clean.get("section_id", ""))
    for legacy, replacement in LEGACY_SECTION_ALIASES.items():
        if section_id == legacy or section_id.startswith(legacy + "."):
            section_id = replacement + section_id[len(legacy) :]
            break
    variant = str(clean.get("variant", ""))
    if variant in EXPLORATORY_VARIANTS and (
        section_id == "11.4" or section_id.startswith("11.4.")
    ):
        section_id = "11.6" + section_id[len("11.4") :]
    if variant in {"local_tolerance", "local_tolerance_ae_subset"} and section_id == "12.2":
        section_id = "12.5.1"
    if section_id == "12.7" and variant in {
        "dose_escalation",
        "dlt_definition",
        "dlt_results",
        "mtd_rp2d_decision",
    }:
        section_id = "12.6"
    clean["section_id"] = section_id
    return clean


def _bookmark_key(section_id: str) -> str:
    return "_Ref_Sec_" + section_id.replace(".", "_").replace("-", "_")


def _remove_paragraph(paragraph) -> None:
    parent = paragraph._p.getparent()
    if parent is not None:
        parent.remove(paragraph._p)


def _literalize_heading(paragraph, section_id: str, title: str) -> None:
    starts = paragraph._p.findall(qn("w:bookmarkStart"))
    bookmark_id = starts[0].get(qn("w:id"), "0") if starts else "0"
    bookmark = _bookmark_key(section_id)
    paragraph._p.clear_content()
    ppr = paragraph._p.get_or_add_pPr()
    number_properties = ppr.find(qn("w:numPr"))
    if number_properties is not None:
        ppr.remove(number_properties)
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), bookmark_id)
    start.set(qn("w:name"), bookmark)
    paragraph._p.append(start)
    paragraph.add_run(f"{section_id} {title}")
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), bookmark_id)
    paragraph._p.append(end)


def _next_bookmark_id(document: Document) -> int:
    values = [
        int(node.get(qn("w:id")))
        for node in document._element.iter(qn("w:bookmarkStart"))
        if (node.get(qn("w:id")) or "").isdigit()
    ]
    return max(values, default=0) + 1


def _new_heading(document: Document, section_id: str, title: str, bookmark_id: int):
    level = len(section_id.split("."))
    paragraph = document.add_paragraph(style=f"Heading {level}")
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), _bookmark_key(section_id))
    paragraph._p.append(start)
    paragraph.add_run(f"{section_id} {title}")
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.append(end)
    return paragraph


def _insert_before(anchor, paragraphs: Sequence[Any]) -> None:
    for paragraph in paragraphs:
        anchor._p.addprevious(paragraph._p)


def _move_after(anchor, elements: Sequence[Any]) -> None:
    cursor = anchor._p
    for element in elements:
        xml = element._element if hasattr(element, "_element") else element._p
        cursor.addnext(xml)
        cursor = xml


def _visible_numeric_headings(document: Document) -> list[tuple[str, Any]]:
    records: list[tuple[str, Any]] = []
    for paragraph in document.paragraphs:
        for start in paragraph._p.findall(qn("w:bookmarkStart")):
            name = start.get(qn("w:name"), "")
            if not name.startswith("_Ref_Sec_"):
                continue
            section_id = name.removeprefix("_Ref_Sec_").replace("_", ".")
            if re.fullmatch(r"\d+(?:\.\d+)*", section_id):
                records.append((section_id, paragraph))
            break
    return records


def _insert_formal_extensions_in_order(
    document: Document,
    extension_items: Sequence[Mapping[str, Any]],
    bookmark_id: int,
) -> int:
    """Insert every extension by its formal number, never by module arrival order."""

    for item in sorted(
        extension_items,
        key=lambda value: _section_sort_key(str(value["section_id"])),
    ):
        section_id = str(item["section_id"])
        headings = _find_heading_paragraphs(document)
        if _bookmark_key(section_id) in headings:
            raise ProjectSkeletonError(f"duplicate formal section number: {section_id}")
        parent_id = section_id.rsplit(".", 1)[0]
        if _bookmark_key(parent_id) not in headings:
            raise ProjectSkeletonError(
                f"formal extension {section_id} lacks visible parent {parent_id}"
            )
        candidates = [
            (candidate_id, paragraph)
            for candidate_id, paragraph in _visible_numeric_headings(document)
            if _section_sort_key(candidate_id) > _section_sort_key(section_id)
        ]
        if not candidates:
            raise ProjectSkeletonError(f"formal extension {section_id} lacks a later anchor")
        _candidate_id, anchor = min(
            candidates, key=lambda value: _section_sort_key(value[0])
        )
        paragraph = _new_heading(
            document, section_id, str(item["title"]), bookmark_id
        )
        bookmark_id += 1
        anchor._p.addprevious(paragraph._p)
    return bookmark_id


def _assert_numeric_heading_order(document: Document) -> dict[str, Any]:
    order = [section_id for section_id, _paragraph in _visible_numeric_headings(document)]
    inversions = [
        {"before": left, "after": right}
        for left, right in zip(order, order[1:])
        if _section_sort_key(left) > _section_sort_key(right)
    ]
    if inversions:
        raise ProjectSkeletonError(f"visible section numbering is not monotonic: {inversions}")
    return {"status": "pass", "visible_heading_count": len(order), "inversions": []}


def _reason_text(item: Mapping[str, Any]) -> str:
    evidence: list[str] = []
    for basis in item.get("basis", []):
        if not isinstance(basis, Mapping):
            continue
        if "path" in basis:
            evidence.append(f"{basis['path']}={basis.get('value')}")
        elif "capability" in basis:
            evidence.append(f"{basis['capability']}={basis.get('value')}")
        elif "module_id" in basis:
            evidence.append(f"{basis['module_id']}={basis.get('value')}")
    return "；".join(evidence) or str(item.get("reason", "适用条件未满足"))


def _human_reason_text(item: Mapping[str, Any]) -> str:
    value = str(item.get("human_reason", "")).strip()
    if not value:
        raise ProjectSkeletonError(
            f"omitted section {item.get('section_id')} lacks a human-readable protocol reason"
        )
    if re.search(r"\b(?:True|False|routing\.|has_[a-z_]+|is_[a-z_]+)\b", value):
        raise ProjectSkeletonError(
            f"omitted section {item.get('section_id')} exposes implementation syntax"
        )
    return value


def render_omitted_sections_markdown(
    section_plan: Mapping[str, Any], *, omission_policy: str = "remove"
) -> str:
    lines = [
        "# 条件性删节留痕",
        "",
        f"> 处置模式：`{omission_policy}`。决定来自 study_facts 派生能力或显式提交地区策略，不使用期别或治疗领域固定模板。",
        "",
        "| 节 | 标题 | 处置 | 依据字段与值 |",
        "|---|---|---|---|",
    ]
    action = "删除" if omission_policy == "remove" else "保留标题并标注不适用"
    for item in section_plan.get("omitted_sections", []):
        lines.append(
            f"| {item['section_id']} | {item['title']} | {action} | {_reason_text(item)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _add_omission_declaration_blocks(
    document: Document,
    headings: Mapping[str, Any],
    omitted_sections: Sequence[Mapping[str, Any]],
    official_sections: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Put paragraph-visible stable-numbering declarations at affected chapter starts."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in omitted_sections:
        section_id = str(item["section_id"])
        chapter = section_id.split(".", 1)[0]
        if "." not in section_id:
            continue
        grouped[chapter].append(item)
    records: list[dict[str, Any]] = []
    retained_ids = {
        str(item["section_id"])
        for item in official_sections
        if bool(item.get("retained"))
    }
    for chapter in sorted(grouped, key=_section_sort_key):
        anchor = headings.get(_bookmark_key(chapter))
        if anchor is None:
            raise ProjectSkeletonError(f"omission declaration anchor missing: chapter {chapter}")
        created: list[Any] = []
        declaration_items: list[dict[str, str]] = []
        chapter_items = sorted(
            grouped[chapter],
            key=lambda value: _section_sort_key(str(value["section_id"])),
        )
        for disposition, lead_text in (
            ("relocated", "本章以下小节已调整至更适合的跨结果域位置："),
            ("not_applicable", "本章以下小节因本研究设计不适用而未纳入："),
        ):
            items = [
                item
                for item in chapter_items
                if str(item.get("disposition", "not_applicable")) == disposition
            ]
            if not items:
                continue
            lead = document.add_paragraph()
            lead.add_run(lead_text).bold = True
            created.append(lead)

            if disposition == "not_applicable":
                branch_roots = sorted(
                    {
                        ".".join(str(item["section_id"]).split(".")[:2])
                        for item in items
                        if len(str(item["section_id"]).split(".")) > 2
                    },
                    key=_section_sort_key,
                )
                for root in branch_roots:
                    retained_descendants = sorted(
                        value
                        for value in retained_ids
                        if value.startswith(root + ".")
                    )
                    omitted_descendants = [
                        item
                        for item in items
                        if str(item["section_id"]).startswith(root + ".")
                    ]
                    if retained_descendants and omitted_descendants:
                        context = document.add_paragraph()
                        context.add_run(
                            f"{root} 主节为保持 ICH E3 定位而保留；"
                            f"其中 {'、'.join(retained_descendants)} 仍保留供人工确认，"
                            "其余不适用子节如下。"
                        )
                        created.append(context)

            for item in items:
                reason = _human_reason_text(item)
                paragraph = document.add_paragraph(style="List Bullet")
                paragraph.add_run(
                    f"{item['section_id']} {item['title']} —— {reason}"
                )
                created.append(paragraph)
                declaration_items.append(
                    {
                        "section_id": str(item["section_id"]),
                        "title": str(item["title"]),
                        "human_reason": reason,
                        "disposition": disposition,
                    }
                )
        _move_after(anchor, created)
        records.append(
            {
                "chapter": chapter,
                "omitted_count": len(declaration_items),
                "physical_form": "body_paragraphs",
                "items": declaration_items,
            }
        )
    return records


def _assert_conclusion_sections_last(
    document: Document,
    conclusion_semantics: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Hard-fail if any semantic conclusion precedes a visible sibling."""

    order: list[str] = []
    for paragraph in document.paragraphs:
        for start in paragraph._p.findall(qn("w:bookmarkStart")):
            name = start.get(qn("w:name"), "")
            if name.startswith("_Ref_Sec_"):
                order.append(name.removeprefix("_Ref_Sec_").replace("_", "."))
                break
    index = {section_id: position for position, section_id in enumerate(order)}
    records: list[dict[str, Any]] = []
    for item in conclusion_semantics:
        section_id = str(item["section_id"])
        if section_id not in index:
            continue
        parent = section_id.rsplit(".", 1)[0]
        siblings = [
            candidate
            for candidate in order
            if "." in candidate and candidate.rsplit(".", 1)[0] == parent
        ]
        if not siblings or siblings[-1] != section_id:
            raise ProjectSkeletonError(
                f"semantic conclusion {section_id} is not the last visible sibling: {siblings}"
            )
        records.append(
            {
                "section_id": section_id,
                "semantic_role": str(item["semantic_role"]),
                "visible_siblings": siblings,
                "last": True,
            }
        )
    return records


def _merge_items(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge same-target module blocks and deduplicate identical paragraphs."""

    references = list(
        dict.fromkeys(
            entity for item in items for entity in item.get("reference_entities", [])
        )
    )
    merged_paragraphs: list[dict[str, Any]] = []
    by_signature: dict[tuple[str, str, bool], dict[str, Any]] = {}
    for item in items:
        for paragraph in item["paragraphs"]:
            signature = (
                str(paragraph["text"]),
                str(paragraph["confidence"]),
                bool(paragraph["needs_human"]),
            )
            existing = by_signature.get(signature)
            if existing is None:
                existing = dict(paragraph)
                existing["sources"] = list(paragraph["sources"])
                by_signature[signature] = existing
                merged_paragraphs.append(existing)
            else:
                existing["sources"] = list(
                    dict.fromkeys([*existing["sources"], *paragraph["sources"]])
                )
    statuses = {str(item["evidence_status"]) for item in items}
    if "source_gap" in statuses:
        evidence_status = "source_gap"
    elif "derived_copy_only" in statuses:
        evidence_status = "derived_copy_only"
    else:
        evidence_status = "reviewed_project_evidence"
    return {
        "section_id": str(items[0]["section_id"]),
        "variant": "+".join(dict.fromkeys(str(item["variant"]) for item in items)),
        "title": str(items[0]["title"]),
        "evidence_status": evidence_status,
        "reference_entities": references,
        "paragraphs": merged_paragraphs,
    }


def _normalize_content(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ProjectSkeletonError("content sections must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or set(item) != CONTENT_KEYS:
            raise ProjectSkeletonError(f"content[{index}] contract drift")
        section_id = item.get("section_id")
        variant = item.get("variant")
        if not isinstance(section_id, str) or not section_id or not isinstance(variant, str) or not variant:
            raise ProjectSkeletonError(f"content[{index}] needs section_id and variant")
        if (section_id, variant) in seen:
            raise ProjectSkeletonError(f"duplicate content section/variant: {section_id}/{variant}")
        seen.add((section_id, variant))
        if item.get("evidence_status") not in {
            "reviewed_project_evidence",
            "source_gap",
            "derived_copy_only",
        }:
            raise ProjectSkeletonError(f"invalid evidence status for {section_id}/{variant}")
        references = item.get("reference_entities")
        if not isinstance(references, list) or not all(isinstance(value, str) for value in references):
            raise ProjectSkeletonError(f"invalid reference list for {section_id}/{variant}")
        if len(references) != len(set(references)):
            raise ProjectSkeletonError(f"duplicate references for {section_id}/{variant}")
        paragraphs = item.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            raise ProjectSkeletonError(f"content {section_id}/{variant} needs paragraphs")
        clean_paragraphs: list[dict[str, Any]] = []
        observed_refs: list[str] = []
        for para_index, paragraph in enumerate(paragraphs):
            if not isinstance(paragraph, Mapping) or set(paragraph) != PARAGRAPH_KEYS:
                raise ProjectSkeletonError(
                    f"content {section_id}/{variant} paragraph {para_index} contract drift"
                )
            text = paragraph.get("text")
            sources = paragraph.get("sources")
            if not isinstance(text, str) or not text.strip() or "\n" in text:
                raise ProjectSkeletonError(f"invalid physical paragraph in {section_id}/{variant}")
            scrubbed = REF_PLACEHOLDER_RE.sub("", text)
            if PLACEHOLDER_RE.search(scrubbed) or "{{" in scrubbed or "}}" in scrubbed:
                raise ProjectSkeletonError(
                    f"unresolved or malformed value placeholder in {section_id}/{variant}"
                )
            observed_refs.extend(match.group("entity") for match in REF_PLACEHOLDER_RE.finditer(text))
            if not isinstance(sources, list) or not sources or not all(
                isinstance(source, str) and source for source in sources
            ):
                raise ProjectSkeletonError(f"invalid sources in {section_id}/{variant}")
            if paragraph.get("confidence") not in {"verbatim", "derived", "inferred"}:
                raise ProjectSkeletonError(f"invalid confidence in {section_id}/{variant}")
            if not isinstance(paragraph.get("needs_human"), bool):
                raise ProjectSkeletonError(f"invalid needs_human in {section_id}/{variant}")
            clean_paragraphs.append(dict(paragraph))
        if any(entity not in references for entity in observed_refs):
            raise ProjectSkeletonError(f"unreviewed cross-reference in {section_id}/{variant}")
        normalized.append(
            {
                **dict(item),
                "paragraphs": clean_paragraphs,
                "reference_entities": list(references),
            }
        )
    return normalized


def _paragraph_for_gap(section_id: str, variant: str, title: str) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "variant": variant,
        "title": title,
        "evidence_status": "source_gap",
        "reference_entities": [],
        "paragraphs": [
            {
                "text": (
                    "本项所需的经审核项目来源尚未提供，内容保持空缺；"
                    "缺口已登记，未进行推断、替代或合理补全。"
                ),
                "sources": ["source-gap-register"],
                "confidence": "inferred",
                "needs_human": True,
            }
        ],
    }


def _find_heading_paragraphs(document: Document) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for paragraph in document.paragraphs:
        for start in paragraph._p.findall(qn("w:bookmarkStart")):
            name = start.get(qn("w:name"), "")
            if name.startswith("_Ref_Sec_"):
                result[name] = paragraph
    return result


def _insert_after(anchor, paragraphs: Sequence[Any]) -> None:
    cursor = anchor._p
    for paragraph in paragraphs:
        cursor.addnext(paragraph._p)
        cursor = paragraph._p


def _append_annotated_block(document: Document, item: Mapping[str, Any], *, show_label: bool) -> list[Any]:
    created: list[Any] = []
    if show_label:
        label = document.add_paragraph()
        run = label.add_run(f"【{item['title']}】")
        run.bold = True
        created.append(label)
    for index, paragraph in enumerate(item["paragraphs"], start=1):
        body = document.add_paragraph()
        if paragraph["needs_human"]:
            marker = body.add_run(REVIEW_MARKER + " ")
            marker.bold = True
            marker.font.highlight_color = WD_COLOR_INDEX.YELLOW
        text = paragraph["text"]
        if text.startswith(REVIEW_MARKER):
            text = text[len(REVIEW_MARKER) :].lstrip()
        _append_text_with_references(body, text)
        created.append(body)
        provenance = document.add_paragraph(style="CSR Footer")
        provenance_run = provenance.add_run(
            f"内部溯源：{item['section_id']}/{item['variant']}/p{index:03d}；"
            f"来源={', '.join(paragraph['sources'])}；置信度={paragraph['confidence']}"
        )
        provenance_run.italic = True
        created.append(provenance)
    return created


def _append_clean_block(document: Document, item: Mapping[str, Any]) -> list[Any]:
    """Render reviewed prose without markers, provenance, or text rewriting."""

    created: list[Any] = []
    for paragraph in item["paragraphs"]:
        text = str(paragraph["text"])
        if paragraph["needs_human"] or REVIEW_MARKER in text or "[待确认]" in text:
            raise ProjectSkeletonError(
                f"clean content for {item['section_id']} still needs human review"
            )
        body = document.add_paragraph()
        _append_text_with_references(body, text)
        created.append(body)
    return created


def _add_tc_field(paragraph, text: str, category: str) -> None:
    escaped = text.replace('"', "'")
    _add_complex_field(paragraph, f' TC "{escaped}" \\f "{category}" \\l "1" ')


def _append_raw_reference_field(paragraph, bookmark: str, display: str) -> None:
    """Emit a Word REF field for an already reviewed section bookmark."""

    begin = OxmlElement("w:r")
    begin_node = OxmlElement("w:fldChar")
    begin_node.set(qn("w:fldCharType"), "begin")
    begin.append(begin_node)
    paragraph._p.append(begin)

    instruction = OxmlElement("w:r")
    instruction_node = OxmlElement("w:instrText")
    instruction_node.set(qn("xml:space"), "preserve")
    instruction_node.text = f" REF {bookmark} \\h "
    instruction.append(instruction_node)
    paragraph._p.append(instruction)

    separate = OxmlElement("w:r")
    separate_node = OxmlElement("w:fldChar")
    separate_node.set(qn("w:fldCharType"), "separate")
    separate.append(separate_node)
    paragraph._p.append(separate)
    paragraph.add_run(display)

    end = OxmlElement("w:r")
    end_node = OxmlElement("w:fldChar")
    end_node.set(qn("w:fldCharType"), "end")
    end.append(end_node)
    paragraph._p.append(end)


def _append_bookmark(paragraph, name: str, bookmark_id: int, text: str) -> None:
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    paragraph._p.append(start)
    paragraph.add_run(text)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.append(end)


def _add_appendix_rows_table(
    document: Document, rows: Sequence[Mapping[str, Any]]
) -> Any:
    table = document.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, value in zip(
        table.rows[0].cells,
        (
            "附录编号",
            "文件名称",
            "本次是否提交",
            "提交位置",
            "未提交理由/是否可按要求提供",
        ),
        strict=True,
    ):
        _set_cell_text(cell, value, bold=True)
    last_category: str | None = None
    for row in rows:
        category = str(row["category_title"])
        if category != last_category:
            category_cells = table.add_row().cells
            merged = category_cells[0]
            for cell in category_cells[1:]:
                merged = merged.merge(cell)
            _set_cell_text(merged, category, bold=True)
            last_category = category
        cells = table.add_row().cells
        values = (
            row["appendix_id"],
            row["file_name"],
            row["submitted"],
            row["submission_location"],
            row["reason_or_on_request"],
        )
        for cell, value in zip(cells, values, strict=True):
            _set_cell_text(cell, str(value))
    return table


def _inject_deterministic_bundle(
    document: Document,
    headings: Mapping[str, Any],
    bundle: Mapping[str, Any],
    visible_section_ids: set[str],
    publication_reference_map: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Place a validated 48+4 bundle at routed section anchors, without prose edits."""

    validation = validate_deterministic_bundle(bundle)
    bookmark_id = _next_bookmark_id(document)
    inserted: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    directory_fields = 0
    publication_bookmarks = 0
    appendix_rows = 0
    title_controls = 0
    aliases = (
        publication_aliases(publication_reference_map)
        if publication_reference_map is not None
        else {}
    )
    for artifact in bundle["artifacts"]:
        section_id = str(artifact["section_id"])
        if section_id not in visible_section_ids:
            skipped.append(
                {
                    "section_id": section_id,
                    "reason": "route_inactive_or_fact_omitted",
                }
            )
            continue
        anchor = headings.get(_bookmark_key(section_id))
        if anchor is None:
            raise ProjectSkeletonError(
                f"deterministic artifact {section_id} lacks its visible heading anchor"
            )
        content = artifact["content"]
        kind = str(artifact["content_kind"])
        created: list[Any] = []
        if kind == "title_page_template":
            fields = content["fields"]
            created.extend(
                [
                    document.add_paragraph(str(fields["study_title"]), style="Title"),
                    document.add_paragraph(f"方案编号：{fields['protocol_id']}"),
                    document.add_paragraph(
                        f"报告版本/日期：{fields['report_version']} / {fields['report_date']}"
                    ),
                    document.add_paragraph(f"申办方：{fields['sponsor']}"),
                    document.add_paragraph(str(fields["confidentiality"])),
                ]
            )
            table = document.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            for cell, value in zip(
                table.rows[0].cells,
                ("形式审查项", "已确认内容", "证据状态"),
                strict=True,
            ):
                _set_cell_text(cell, value, bold=True)
            for control in content["title_page_controls"]:
                cells = table.add_row().cells
                for cell, value in zip(
                    cells,
                    (control["label"], control["value"], control["evidence_status"]),
                    strict=True,
                ):
                    _set_cell_text(cell, str(value))
                title_controls += 1
            created.append(table)
        elif kind == "confirmed_text_copy":
            for item in content["paragraphs"]:
                paragraph = document.add_paragraph()
                text = rewrite_reference_text(
                    str(item["text"]), aliases, require_mapped=bool(aliases)
                )
                _append_text_with_references(paragraph, text)
                created.append(paragraph)
        elif kind == "word_field":
            paragraph = document.add_paragraph(style="CSR TOC")
            _add_complex_field(paragraph, str(content["instruction"]))
            created.append(paragraph)
            directory_fields += 1
            if section_id == "3.6":
                for report in bundle.get("support_report_entries", []):
                    entry = document.add_paragraph()
                    _add_tc_field(entry, str(report["title"]), "SUP")
                    created.append(entry)
        elif kind == "fixed_template_with_slots":
            created.append(document.add_paragraph(str(content["text"])))
        elif kind == "word_ref":
            paragraph = document.add_paragraph()
            paragraph.add_run(str(content["prefix"]))
            _append_raw_reference_field(
                paragraph, str(content["bookmark"]), str(content["display"])
            )
            paragraph.add_run(str(content["suffix"]))
            created.append(paragraph)
        elif kind == "chapter14_entities":
            table = document.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            for cell, value in zip(
                table.rows[0].cells,
                ("发表编号", "标题", "正文归属"),
                strict=True,
            ):
                _set_cell_text(cell, value, bold=True)
            for entry in content["entries"]:
                cells = table.add_row().cells
                publication = str(entry["publication_entity_id"])
                first = cells[0].paragraphs[0]
                first.clear()
                _append_bookmark(first, bookmark_name(publication), bookmark_id, publication)
                bookmark_id += 1
                _add_tc_field(first, f"{publication} {entry['title']}", "TFL")
                _set_cell_text(cells[1], str(entry["title"]))
                _set_cell_text(cells[2], str(entry["expected_section"]))
                publication_bookmarks += 1
            created.append(table)
        elif kind == "reference_list":
            for index, entry in enumerate(content["entries"], start=1):
                created.append(document.add_paragraph(f"[{index}] {entry['citation']}"))
        elif kind == "appendix_status_table":
            rows = list(content["rows"])
            created.append(_add_appendix_rows_table(document, rows))
            appendix_rows = len(rows)
        elif kind == "appendix_status_entry":
            for row in content["rows"]:
                paragraph = document.add_paragraph(
                    f"提交状态：{row['submitted']}。提交位置："
                    f"{row['submission_location']}。说明：{row['reason_or_on_request']}"
                )
                _add_tc_field(
                    paragraph, f"{row['appendix_id']} {row['file_name']}", "APP"
                )
                created.append(paragraph)
            signature = content.get("annex_ii_signature")
            if isinstance(signature, Mapping):
                created.append(
                    document.add_paragraph(
                        f"{signature['label']}：{signature['value']}"
                        f"（{signature['evidence_status']}）"
                    )
                )
        else:
            raise ProjectSkeletonError(
                f"deterministic renderer missing for {section_id}/{kind}"
            )
        _move_after(anchor, created)
        inserted.append({"section_id": section_id, "content_kind": kind})

    return {
        **validation,
        "inserted_unit_count": len(inserted),
        "skipped_unit_count": len(skipped),
        "inserted": inserted,
        "skipped": skipped,
        "directory_word_field_count": directory_fields,
        "publication_bookmark_count": publication_bookmarks,
        "appendix_status_row_count": appendix_rows,
        "title_page_control_count": title_controls,
    }


def _append_reference_anchors(document: Document, entities: Sequence[str]) -> None:
    if not entities:
        return
    label = document.add_paragraph()
    label.add_run("支持性表、图和清单（内部定位锚点）").bold = True
    existing_ids = [
        int(node.get(qn("w:id")))
        for node in document._element.iter(qn("w:bookmarkStart"))
        if (node.get(qn("w:id")) or "").isdigit()
    ]
    next_id = max(existing_ids, default=0) + 1
    for entity in entities:
        paragraph = document.add_paragraph()
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(next_id))
        start.set(qn("w:name"), bookmark_name(entity))
        paragraph._p.append(start)
        paragraph.add_run(display_reference(entity))
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), str(next_id))
        paragraph._p.append(end)
        next_id += 1


def _set_cell_text(cell, text: str, *, bold: bool = False) -> None:
    paragraph = cell.paragraphs[0]
    paragraph.clear()
    run = paragraph.add_run(text)
    run.bold = bold


def _publication_entries_by_chapter(publication_map: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    validated = validate_publication_reference_map(publication_map)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in validated["entries"]:
        number = entry["publication_entity_id"][1:]
        chapter = ".".join(number.split("-")[0].split(".")[:2])
        grouped[chapter].append(entry)
    return grouped


def _add_publication_table(
    document: Document,
    entries: Sequence[Mapping[str, str]],
    *,
    add_bookmarks: bool,
) -> Any:
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, value in zip(table.rows[0].cells, ("表/图/清单号", "名称", "预期正文归属"), strict=True):
        _set_cell_text(cell, value, bold=True)
    bookmark_id = _next_bookmark_id(document)
    for entry in entries:
        cells = table.add_row().cells
        first = cells[0].paragraphs[0]
        first.clear()
        entity = entry["publication_entity_id"]
        if add_bookmarks:
            start = OxmlElement("w:bookmarkStart")
            start.set(qn("w:id"), str(bookmark_id))
            start.set(qn("w:name"), bookmark_name(entity))
            first._p.append(start)
        first.add_run(display_reference(entity))
        if add_bookmarks:
            end = OxmlElement("w:bookmarkEnd")
            end.set(qn("w:id"), str(bookmark_id))
            first._p.append(end)
            bookmark_id += 1
        _set_cell_text(cells[1], entry["title"])
        _set_cell_text(cells[2], entry["expected_section"])
    return table


def _add_appendix_status_table(document: Document, appendix_plan: Mapping[str, Any]) -> Any:
    table = document.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    headers = (
        "附录编号",
        "文件名称",
        "本次是否提交",
        "提交位置",
        "未提交理由/是否可按要求提供",
    )
    for cell, value in zip(table.rows[0].cells, headers, strict=True):
        _set_cell_text(cell, value, bold=True)
    for category in appendix_plan["categories"]:
        category_row = table.add_row()
        merged = category_row.cells[0]
        for cell in category_row.cells[1:]:
            merged = merged.merge(cell)
        _set_cell_text(merged, category["title"], bold=True)
        for item in category["rows"]:
            cells = table.add_row().cells
            values = (
                item["appendix_id"],
                item["file_name"],
                item["submitted"],
                item["submission_location"],
                item["reason_or_on_request"],
            )
            for cell, value in zip(cells, values, strict=True):
                _set_cell_text(cell, str(value))
    return table


def _add_report_directories(
    document: Document,
    headings: Mapping[str, Any],
    publication_map: Mapping[str, Any] | None,
    appendix_plan: Mapping[str, Any],
) -> None:
    main_toc = document.add_paragraph(style="CSR TOC")
    _add_complex_field(main_toc, ' TOC \\o "1-5" \\h \\z \\u ')
    _move_after(headings[_bookmark_key("3.1")], [main_toc])

    table_toc = document.add_paragraph(style="CSR TOC")
    _add_complex_field(table_toc, ' TOC \\c "Table" \\h \\z ')
    _move_after(headings[_bookmark_key("3.2")], [table_toc])

    figure_toc = document.add_paragraph(style="CSR TOC")
    _add_complex_field(figure_toc, ' TOC \\c "Figure" \\h \\z ')
    _move_after(headings[_bookmark_key("3.3")], [figure_toc])

    if publication_map is not None:
        entries = validate_publication_reference_map(publication_map)["entries"]
        directory = _add_publication_table(document, entries, add_bookmarks=False)
        _move_after(headings[_bookmark_key("3.4")], [directory])
    else:
        paragraph = document.add_paragraph("尚未登记第 14 章表图清单。")
        _move_after(headings[_bookmark_key("3.4")], [paragraph])

    # Chapter 3 must not contain a four-row self-reference that can be mistaken
    # for the actual status register.  Render the same concrete five-column
    # entries here and at 16.0; both are formal reader-facing directories.
    appendix_directory = _add_appendix_status_table(document, appendix_plan)
    _move_after(headings[_bookmark_key("3.5")], [appendix_directory])

    support = document.add_table(rows=1, cols=3)
    support.style = "Table Grid"
    for cell, value in zip(
        support.rows[0].cells,
        ("支持性报告", "提交位置", "登记状态"),
        strict=True,
    ):
        _set_cell_text(cell, value, bold=True)
    cells = support.add_row().cells
    _set_cell_text(cells[0], "现有已确认资料未登记单独递交的支持性报告")
    _set_cell_text(cells[1], "待正式提交清单确认")
    _set_cell_text(cells[2], "资料缺口；不推断为无")
    _move_after(headings[_bookmark_key("3.6")], [support])


def _add_chapter_14_publication_tables(
    document: Document,
    headings: Mapping[str, Any],
    publication_map: Mapping[str, Any],
) -> int:
    grouped = _publication_entries_by_chapter(publication_map)
    count = 0
    for chapter in ("14.1", "14.2", "14.3"):
        entries = grouped.get(chapter, [])
        anchor = headings.get(_bookmark_key(chapter))
        if not entries or anchor is None:
            continue
        table = _add_publication_table(document, entries, add_bookmarks=True)
        _move_after(anchor, [table])
        count += len(entries)
    return count


def render_project_skeleton(
    routing: Mapping[str, Any],
    content_sections: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    scaffold_path: str | Path,
    structure_path: str | Path = DEFAULT_STRUCTURE,
    template_path: str | Path = DEFAULT_TEMPLATE,
    format_spec_path: str | Path = DEFAULT_FORMAT_SPEC,
    mode: str = "annotated",
    omission_policy: str = "remove",
    publication_reference_map: Mapping[str, Any] | None = None,
    study_facts: Mapping[str, Any] | None = None,
    title_overrides_path: str | Path = DEFAULT_TITLE_OVERRIDES,
    deterministic_bundle: Mapping[str, Any] | None = None,
    composition_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Render active module contributions union the unconditional core."""

    if mode not in {"annotated", "clean"}:
        raise ProjectSkeletonError("project skeleton mode must be annotated or clean")
    if omission_policy not in {"remove", "retain_not_applicable"}:
        raise ProjectSkeletonError(
            "omission_policy must be remove or retain_not_applicable"
        )
    formalized = [_formalize_content_section(item) for item in content_sections]
    if publication_reference_map is not None:
        formalized = rewrite_content_references(formalized, publication_reference_map)
    normalized = _normalize_content(formalized)
    scaffold_result = render_skeleton(
        structure_path,
        template_path,
        format_spec_path,
        scaffold_path,
        layers=("ich-official", "industry-convention"),
        header_text=(
            "临床试验总结报告（内部批注骨架）"
            if mode == "annotated"
            else "临床试验总结报告骨架"
        ),
    )
    document = Document(scaffold_path)
    if composition_plan_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", composition_plan_sha256) is None:
            raise ProjectSkeletonError("composition plan hash must be lowercase SHA-256")
        document.core_properties.identifier = composition_plan_sha256
    headings = _find_heading_paragraphs(document)
    route_structure = routing.get("structure")
    if not isinstance(route_structure, Mapping):
        raise ProjectSkeletonError("routing lacks structure")
    contributions = route_structure.get("materialized_sections")
    if not isinstance(contributions, list) or any(not isinstance(item, Mapping) for item in contributions):
        raise ProjectSkeletonError("routing materialized_sections must be a list")
    section_plan = route_structure.get("section_plan")
    if not isinstance(section_plan, Mapping):
        raise ProjectSkeletonError("routing lacks the study-fact-driven section_plan")
    plan_items = section_plan.get("official_sections")
    if not isinstance(plan_items, list):
        raise ProjectSkeletonError("routing section_plan official_sections must be a list")
    plan_by_id = {str(item["section_id"]): item for item in plan_items}
    official_ids = {node.id for node in scaffold_result.headings}
    if set(plan_by_id) != official_ids:
        raise ProjectSkeletonError("section_plan does not classify every scaffold section")

    # The M1 scaffold is the formatting source only.  Its automatic numbering is
    # replaced with stable literal formal numbers before pruning, so omissions
    # never silently renumber later headings.
    title_overrides = load_title_overrides(
        title_overrides_path, structure_path=structure_path
    )
    applied_title_overrides: list[dict[str, str]] = []
    retained_official_ids: set[str] = set()
    not_applicable_items: list[dict[str, Any]] = []
    for node in scaffold_result.headings:
        item = plan_by_id[node.id]
        anchor = headings.get(_bookmark_key(node.id))
        if anchor is None:
            raise ProjectSkeletonError(f"official heading anchor missing: {node.id}")
        retained = bool(item["retained"])
        if not retained and omission_policy == "remove":
            _remove_paragraph(anchor)
            continue
        title, override = localized_title(node.id, node.title("zh"), title_overrides)
        dynamic_title = str(item.get("render_title", node.title("zh")))
        if dynamic_title != node.title("zh"):
            title = dynamic_title
        if override is not None:
            applied_title_overrides.append(override)
        _literalize_heading(anchor, node.id, title)
        retained_official_ids.add(node.id)
        if not retained:
            not_applicable_items.append(dict(item))

    # Remove the M1 placeholder TOC; chapter 3 now has six real directory parts.
    for paragraph in list(document.paragraphs):
        if paragraph.style and paragraph.style.name == "CSR TOC":
            _remove_paragraph(paragraph)

    headings = _find_heading_paragraphs(document)
    bookmark_id = _next_bookmark_id(document)
    extension_items = list(section_plan.get("formal_extensions", []))
    extension_by_id = {
        str(item["section_id"]): item for item in extension_items
    }

    directory_headings = []
    for section_id, title in DIRECTORY_TITLES.items():
        directory_headings.append(_new_heading(document, section_id, title, bookmark_id))
        bookmark_id += 1
    _insert_before(headings[_bookmark_key("4")], directory_headings)
    bookmark_id = _insert_formal_extensions_in_order(
        document, extension_items, bookmark_id
    )
    headings = _find_heading_paragraphs(document)

    # Optional sponsor convention: retain a false conditional heading and state
    # the exact fact-derived reason.  Default behaviour remains physical removal.
    if omission_policy == "retain_not_applicable":
        for item in not_applicable_items:
            anchor = headings[_bookmark_key(str(item["section_id"]))]
            prefix = (
                "本节内容已调整位置："
                if item.get("disposition") == "relocated"
                else "不适用，理由为："
            )
            paragraph = document.add_paragraph(prefix + _human_reason_text(item))
            _insert_after(anchor, [paragraph])

    content_by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in normalized:
        if item["section_id"] not in retained_official_ids and _bookmark_key(item["section_id"]) not in headings:
            raise ProjectSkeletonError(
                f"content targets an omitted or inactive section: {item['section_id']}"
            )
        content_by_section[item["section_id"]].append(item)

    coverage: list[dict[str, Any]] = []
    for contribution in contributions:
        section_id = str(contribution["section"])
        variant = str(contribution["variant"])
        if "-" in section_id and contribution.get("contributed_by") == "M-CORE":
            coverage.append(
                {
                    "module_id": contribution["contributed_by"],
                    "section_id": section_id,
                    "variant": variant,
                    "status": "official_scaffold_range",
                }
            )
            continue
        exact = next(
            (item for item in content_by_section.get(section_id, []) if item["variant"] == variant),
            None,
        )
        if exact is None and mode == "annotated":
            exact = _paragraph_for_gap(section_id, variant, str(contribution["title"]))
            content_by_section[section_id].append(exact)
        coverage.append(
            {
                "module_id": contribution["contributed_by"],
                "section_id": section_id,
                "variant": variant,
                "status": exact["evidence_status"] if exact is not None else "heading_only_clean",
            }
        )

    visible_ids = retained_official_ids | set(extension_by_id) | {"16.0"}
    children_by_parent: dict[str, set[str]] = defaultdict(set)
    for section_id in visible_ids:
        if "." in section_id:
            children_by_parent[section_id.rsplit(".", 1)[0]].add(section_id)

    if mode == "annotated":
        title_by_id = {
            str(item["section_id"]): str(item.get("render_title", item["title"]))
            for item in plan_items
        }
        title_by_id.update({item["section_id"]: item["title"] for item in extension_items})
        for section_id in sorted(visible_ids, key=_section_sort_key):
            if (
                section_id in {"3", "16.0"}
                or section_id.startswith("3.")
                or children_by_parent.get(section_id)
            ):
                continue
            if not content_by_section.get(section_id):
                content_by_section[section_id].append(
                    _paragraph_for_gap(
                        section_id, "retained_leaf", str(title_by_id.get(section_id, section_id))
                    )
                )

    for section_id in sorted(content_by_section, key=_section_sort_key):
        anchor = headings.get(_bookmark_key(section_id))
        if anchor is None:
            raise ProjectSkeletonError(f"retained heading anchor missing: {section_id}")
        merged = _merge_items(content_by_section[section_id])
        created = (
            _append_annotated_block(document, merged, show_label=False)
            if mode == "annotated"
            else _append_clean_block(document, merged)
        )
        _insert_after(anchor, created)

    deterministic_report: dict[str, Any] | None = None
    publication_anchor_count = 0
    if deterministic_bundle is not None:
        visible_for_deterministic = (
            retained_official_ids | set(extension_by_id) | set(DIRECTORY_TITLES) | {"16.0"}
        )
        deterministic_report = _inject_deterministic_bundle(
            document,
            headings,
            deterministic_bundle,
            visible_for_deterministic,
            publication_reference_map,
        )
        publication_anchor_count = deterministic_report["publication_bookmark_count"]
        appendix_plan = {
            "rows": next(
                artifact["content"]["rows"]
                for artifact in deterministic_bundle["artifacts"]
                if artifact["section_id"] == "16.0"
            )
        }
    else:
        appendix_plan = build_appendix_status_plan(
            study_facts or {}, structure_path=structure_path, submission_region="CDE"
        )
        _add_report_directories(document, headings, publication_reference_map, appendix_plan)
        appendix_table = _add_appendix_status_table(document, appendix_plan)
        _move_after(headings[_bookmark_key("16.0")], [appendix_table])
        if publication_reference_map is not None:
            publication_anchor_count = _add_chapter_14_publication_tables(
                document, headings, publication_reference_map
            )

    omission_declarations = _add_omission_declaration_blocks(
        document,
        headings,
        section_plan["omitted_sections"],
        plan_items,
    )
    numbering_order = _assert_numeric_heading_order(document)
    conclusion_order = _assert_conclusion_sections_last(
        document,
        section_plan.get("conclusion_semantics", []),
    )

    references = list(
        dict.fromkeys(
            entity
            for item in normalized
            for entity in item["reference_entities"]
        )
    )
    if publication_reference_map is None:
        _append_reference_anchors(document, references)
    _atomic_save_docx(document, Path(output_path))
    reference_report = validate_docx_references(output_path)
    if mode == "clean":
        validate_clean_docx(output_path)
    pending_count = sum(
        paragraph["needs_human"]
        for items in content_by_section.values()
        for item in items
        for paragraph in item["paragraphs"]
    )
    all_content = [item for items in content_by_section.values() for item in items]
    retained_extension_ids = set(extension_by_id) | {"16.0"}
    return {
        "schema_version": "2.0",
        "status": "pass",
        "composition_plan_sha256": composition_plan_sha256,
        "mode": f"{mode}_pruned_project_skeleton",
        "output_sha256": sha256_file(output_path),
        "full_scaffold_official_heading_count": len(official_ids),
        "retained_official_heading_count": len(retained_official_ids),
        "omitted_official_heading_count": len(section_plan["omitted_sections"]),
        "formal_extension_count": len(retained_extension_ids),
        "directory_subsection_count": len(DIRECTORY_TITLES),
        "content_block_count": len(all_content),
        "source_gap_block_count": sum(
            item["evidence_status"] == "source_gap" for item in all_content
        ),
        "pending_count": pending_count if mode == "annotated" else 0,
        "route_contribution_count": len(contributions),
        "route_coverage_count": len(coverage),
        "route_coverage": coverage,
        "omission_policy": omission_policy,
        "omitted_sections": section_plan["omitted_sections"],
        "manual_review_queue": section_plan.get("manual_review_queue", []),
        "omission_declaration_blocks": omission_declarations,
        "conclusion_order": conclusion_order,
        "numbering_order": numbering_order,
        "retained_section_ids": sorted(
            retained_official_ids | retained_extension_ids | set(DIRECTORY_TITLES),
            key=_section_sort_key,
        ),
        "publication_reference_count": (
            len(validate_publication_reference_map(publication_reference_map)["entries"])
            if publication_reference_map is not None
            else 0
        ),
        "publication_anchor_count": publication_anchor_count,
        "appendix_status_row_count": len(appendix_plan["rows"]),
        "appendix_status_plan": appendix_plan,
        "deterministic_bundle": deterministic_report,
        "title_overrides": {
            "config_sha256": title_overrides["config_sha256"],
            "structure_sha256": title_overrides["structure_sha256"],
            "applied": applied_title_overrides,
        },
        "cross_references": reference_report,
        "synopsis_contract": {
            "independent_drafting_allowed": False,
            "status": "blocked_until_body_confirmed",
        },
    }


def render_project_annotated_skeleton(
    routing: Mapping[str, Any],
    content_sections: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    scaffold_path: str | Path,
    structure_path: str | Path = DEFAULT_STRUCTURE,
    template_path: str | Path = DEFAULT_TEMPLATE,
    format_spec_path: str | Path = DEFAULT_FORMAT_SPEC,
    omission_policy: str = "remove",
    publication_reference_map: Mapping[str, Any] | None = None,
    study_facts: Mapping[str, Any] | None = None,
    title_overrides_path: str | Path = DEFAULT_TITLE_OVERRIDES,
    deterministic_bundle: Mapping[str, Any] | None = None,
    composition_plan_sha256: str | None = None,
) -> dict[str, Any]:
    return render_project_skeleton(
        routing,
        content_sections,
        output_path,
        scaffold_path=scaffold_path,
        structure_path=structure_path,
        template_path=template_path,
        format_spec_path=format_spec_path,
        mode="annotated",
        omission_policy=omission_policy,
        publication_reference_map=publication_reference_map,
        study_facts=study_facts,
        title_overrides_path=title_overrides_path,
        deterministic_bundle=deterministic_bundle,
        composition_plan_sha256=composition_plan_sha256,
    )


def render_project_clean_skeleton(
    routing: Mapping[str, Any],
    output_path: str | Path,
    *,
    content_sections: Sequence[Mapping[str, Any]] = (),
    scaffold_path: str | Path,
    structure_path: str | Path = DEFAULT_STRUCTURE,
    template_path: str | Path = DEFAULT_TEMPLATE,
    format_spec_path: str | Path = DEFAULT_FORMAT_SPEC,
    omission_policy: str = "remove",
    publication_reference_map: Mapping[str, Any] | None = None,
    study_facts: Mapping[str, Any] | None = None,
    title_overrides_path: str | Path = DEFAULT_TITLE_OVERRIDES,
    deterministic_bundle: Mapping[str, Any] | None = None,
    composition_plan_sha256: str | None = None,
) -> dict[str, Any]:
    return render_project_skeleton(
        routing,
        content_sections,
        output_path,
        scaffold_path=scaffold_path,
        structure_path=structure_path,
        template_path=template_path,
        format_spec_path=format_spec_path,
        mode="clean",
        omission_policy=omission_policy,
        publication_reference_map=publication_reference_map,
        study_facts=study_facts,
        title_overrides_path=title_overrides_path,
        deterministic_bundle=deterministic_bundle,
        composition_plan_sha256=composition_plan_sha256,
    )
