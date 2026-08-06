"""M3 P1-a: deterministic, read-only extraction of accepted-view DOCX text.

The extractor operates directly on OOXML.  It never asks Word or LibreOffice to
render the document, and it deliberately never reads comment bodies into the
fact-bearing text stream.
"""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from lxml import etree

from .errors import ConfigurationError
from .io_utils import atomic_write_json, paths_alias, sha256_file


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


W = lambda local: _tag(W_NS, local)
R = lambda local: _tag(R_NS, local)
MC = lambda local: _tag(MC_NS, local)
PR = lambda local: _tag(PKG_REL_NS, local)


_DELETED_CONTAINERS = {W("del"), W("moveFrom")}
_TEXT_BOX = W("txbxContent")
_COMMENT_MARKERS = {
    W("commentRangeStart"): "range_start",
    W("commentRangeEnd"): "range_end",
    W("commentReference"): "reference",
}
_FALSE_VALUES = {"0", "false", "off", "no"}
_SUPPORTED_MC_NAMESPACES = {W_NS, WPS_NS}


class DocxExtractionError(ConfigurationError):
    """The DOCX package cannot be extracted without ambiguity or data loss."""


def _parse_xml(payload: bytes, part_name: str) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_blank_text=False,
    )
    try:
        return etree.fromstring(payload, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise DocxExtractionError(f"Malformed OOXML part {part_name}: {exc}") from exc


def _effective_children(element: etree._Element) -> list[etree._Element]:
    """Return one effective Markup Compatibility branch, not Choice+Fallback."""
    if element.tag != MC("AlternateContent"):
        return [child for child in element if isinstance(child.tag, str)]
    choices = [child for child in element if child.tag == MC("Choice")]
    fallback = next((child for child in element if child.tag == MC("Fallback")), None)
    selected: etree._Element | None = None
    for choice in choices:
        required_prefixes = (choice.get("Requires") or "").split()
        if required_prefixes and all(
            choice.nsmap.get(prefix) in _SUPPORTED_MC_NAMESPACES
            for prefix in required_prefixes
        ):
            selected = choice
            break
    if selected is None:
        selected = fallback
    if selected is None:
        raise DocxExtractionError(
            "Unsupported mc:AlternateContent has no usable Fallback; extraction stopped"
        )
    return [child for child in selected if isinstance(child.tag, str)]


def _iter_effective(element: etree._Element) -> Iterator[etree._Element]:
    yield element
    for child in _effective_children(element):
        yield from _iter_effective(child)


def _on_off_value(element: etree._Element | None) -> bool | None:
    if element is None:
        return None
    value = element.get(W("val"))
    return True if value is None else value.strip().casefold() not in _FALSE_VALUES


@dataclass(frozen=True)
class _StyleDefinition:
    based_on: str | None
    vanish: bool | None


class _StyleVisibility:
    """Resolve direct and style-inherited w:vanish without invoking Word."""

    def __init__(self, styles_root: etree._Element | None):
        self._styles: dict[str, _StyleDefinition] = {}
        self._default_vanish: bool | None = None
        if styles_root is None:
            return
        default_run_properties = styles_root.find(
            f"./{W('docDefaults')}/{W('rPrDefault')}/{W('rPr')}"
        )
        if default_run_properties is not None:
            self._default_vanish = _on_off_value(
                default_run_properties.find(W("vanish"))
            )
        for style in styles_root.findall(W("style")):
            style_id = style.get(W("styleId"))
            if not style_id:
                continue
            based_on_node = style.find(W("basedOn"))
            based_on = based_on_node.get(W("val")) if based_on_node is not None else None
            run_properties = style.find(W("rPr"))
            vanish = (
                _on_off_value(run_properties.find(W("vanish")))
                if run_properties is not None
                else None
            )
            self._styles[style_id] = _StyleDefinition(based_on=based_on, vanish=vanish)

    def _apply_style_toggle(self, style_id: str | None, hidden: bool) -> bool:
        """Apply a toggle property from base style to derived style.

        In a style definition, ``w:vanish`` true reverses the inherited state and
        false leaves it unchanged.  Only direct run formatting treats false as an
        explicit request to make the text visible.
        """
        chain: list[tuple[str, _StyleDefinition]] = []
        seen: set[str] = set()
        current = style_id
        while current:
            if current in seen:
                raise DocxExtractionError(f"Cyclic DOCX style inheritance at {current!r}")
            seen.add(current)
            definition = self._styles.get(current)
            if definition is None:
                break
            chain.append((current, definition))
            current = definition.based_on
        for _, definition in reversed(chain):
            if definition.vanish is True:
                hidden = not hidden
            # A false toggle in a style preserves the inherited state.
        return hidden

    @staticmethod
    def paragraph_style(paragraph: etree._Element) -> str | None:
        style = paragraph.find(f"./{W('pPr')}/{W('pStyle')}")
        return style.get(W("val")) if style is not None else None

    def run_is_hidden(self, run: etree._Element, paragraph_style: str | None) -> bool:
        run_properties = run.find(W("rPr"))
        direct: bool | None = None
        run_style_id: str | None = None
        if run_properties is not None:
            direct = _on_off_value(run_properties.find(W("vanish")))
            run_style = run_properties.find(W("rStyle"))
            if run_style is not None:
                run_style_id = run_style.get(W("val"))
        hidden = bool(self._default_vanish)
        hidden = self._apply_style_toggle(paragraph_style, hidden)
        hidden = self._apply_style_toggle(run_style_id, hidden)
        return direct if direct is not None else hidden


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_text(element: etree._Element) -> str:
    chunks: list[str] = []
    for descendant in _iter_effective(element):
        if descendant.tag in {W("t"), W("delText")} and descendant.text:
            chunks.append(descendant.text)
    return "".join(chunks)


def _xml_location(part_name: str, element: etree._Element) -> str:
    return f"{part_name}:{element.getroottree().getpath(element)}"


def _visible_text(
    element: etree._Element,
    styles: _StyleVisibility,
    paragraph_style: str | None,
    *,
    skip_text_boxes: bool,
) -> str:
    chunks: list[str] = []

    def walk(current: etree._Element) -> None:
        if current.tag in _DELETED_CONTAINERS:
            return
        if skip_text_boxes and current.tag == _TEXT_BOX:
            return
        if current.tag == W("r") and styles.run_is_hidden(current, paragraph_style):
            return
        if current.tag == W("t"):
            if current.text:
                chunks.append(current.text)
            return
        if current.tag == W("tab"):
            chunks.append("\t")
            return
        if current.tag in {W("br"), W("cr")}:
            chunks.append("\n")
            return
        if current.tag == W("noBreakHyphen"):
            chunks.append("\u2011")
            return
        if current.tag == W("softHyphen"):
            chunks.append("\u00ad")
            return
        # delText and field instructions are never fact-bearing display text.
        if current.tag in {W("delText"), W("instrText"), W("delInstrText")}:
            return
        for child in _effective_children(current):
            walk(child)

    walk(element)
    return "".join(chunks)


def _top_level_text_boxes(
    element: etree._Element,
    styles: _StyleVisibility,
    paragraph_style: str | None,
) -> Iterator[tuple[etree._Element, int]]:
    """Yield visible text boxes and their accepted-text offset in the host paragraph."""

    character_offset = 0

    def walk(current: etree._Element) -> Iterator[tuple[etree._Element, int]]:
        nonlocal character_offset
        if current.tag in _DELETED_CONTAINERS:
            return
        if current.tag == W("r") and styles.run_is_hidden(current, paragraph_style):
            return
        if current.tag == _TEXT_BOX:
            yield current, character_offset
            return
        if current.tag == W("t"):
            character_offset += len(current.text or "")
            return
        if current.tag in {W("tab"), W("br"), W("cr"), W("noBreakHyphen"), W("softHyphen")}:
            character_offset += 1
            return
        if current.tag in {W("delText"), W("instrText"), W("delInstrText")}:
            return
        for child in _effective_children(current):
            yield from walk(child)

    yield from walk(element)


def _resolve_relationship_target(base_part: str, target: str) -> str:
    if not target or "\\" in target:
        raise DocxExtractionError(f"Unsafe relationship target in {base_part}: {target!r}")
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise DocxExtractionError(f"Relationship escapes DOCX package: {target!r}")
    return resolved


@dataclass
class _ExtractionState:
    styles: _StyleVisibility
    records: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    text_boxes: list[dict[str, Any]] = field(default_factory=list)
    insertions: list[dict[str, Any]] = field(default_factory=list)
    deletions: list[dict[str, Any]] = field(default_factory=list)
    hidden_runs: list[dict[str, Any]] = field(default_factory=list)
    comment_markers: list[dict[str, Any]] = field(default_factory=list)
    story_order: list[dict[str, Any]] = field(default_factory=list)
    active_comment_ids: dict[str, set[str]] = field(default_factory=dict)

    def _scan_story(
        self,
        root: etree._Element,
        *,
        story: str,
        story_id: str,
        part_name: str,
    ) -> None:
        for element in _iter_effective(root):
            location = _xml_location(part_name, element)
            if element.tag in {W("ins"), W("del")}:
                text = _raw_text(element)
                finding = {
                    "story": story,
                    "story_id": story_id,
                    "part": part_name,
                    "location": location,
                    "revision_id": element.get(W("id")),
                    "author": element.get(W("author")),
                    "date": element.get(W("date")),
                    "char_count": len(text),
                    "content_sha256": _hash_text(text),
                }
                (self.insertions if element.tag == W("ins") else self.deletions).append(
                    finding
                )
            if element.tag in _COMMENT_MARKERS:
                self.comment_markers.append(
                    {
                        "story": story,
                        "story_id": story_id,
                        "part": part_name,
                        "location": location,
                        "comment_id": element.get(W("id")),
                        "marker": _COMMENT_MARKERS[element.tag],
                    }
                )
            if element.tag != W("r"):
                continue
            paragraph = next((ancestor for ancestor in element.iterancestors(W("p"))), None)
            paragraph_style = (
                self.styles.paragraph_style(paragraph) if paragraph is not None else None
            )
            if not self.styles.run_is_hidden(element, paragraph_style):
                continue
            text = _raw_text(element)
            self.hidden_runs.append(
                {
                    "story": story,
                    "story_id": story_id,
                    "part": part_name,
                    "location": location,
                    "char_count": len(text),
                    "content_sha256": _hash_text(text),
                }
            )

    def add_story(
        self,
        root: etree._Element,
        *,
        story: str,
        story_id: str,
        part_name: str,
        references: list[dict[str, Any]],
    ) -> None:
        self.story_order.append(
            {
                "sequence": len(self.story_order) + 1,
                "story": story,
                "story_id": story_id,
                "part": part_name,
                "references": references,
            }
        )
        self._scan_story(
            root,
            story=story,
            story_id=story_id,
            part_name=part_name,
        )
        container = root.find(W("body")) if root.tag == W("document") else root
        if container is None:
            raise DocxExtractionError(f"Missing story container in {part_name}")
        self._walk_container(
            container,
            story=story,
            story_id=story_id,
            part_name=part_name,
            text_box_id=None,
            cell_context=None,
        )

    def _walk_container(
        self,
        container: etree._Element,
        *,
        story: str,
        story_id: str,
        part_name: str,
        text_box_id: str | None,
        cell_context: dict[str, Any] | None,
    ) -> None:
        def walk_block(child: etree._Element) -> None:
            if child.tag in _DELETED_CONTAINERS:
                return
            if child.tag == W("altChunk"):
                raise DocxExtractionError(
                    "w:altChunk embedded content is unsupported; extraction stopped before omitting facts"
                )
            if child.tag == W("p"):
                self._emit_paragraph(
                    child,
                    story=story,
                    story_id=story_id,
                    part_name=part_name,
                    text_box_id=text_box_id,
                    cell_context=cell_context,
                )
            elif child.tag == W("tbl"):
                self._walk_table(
                    child,
                    story=story,
                    story_id=story_id,
                    part_name=part_name,
                    text_box_id=text_box_id,
                )
            else:
                # Block-level wrappers include w:sdt/w:sdtContent, customXml,
                # and accepted block insertions.  Recurse without descending into
                # paragraphs/tables twice; deleted wrappers were stopped above.
                for nested in _effective_children(child):
                    walk_block(nested)

        for child in _effective_children(container):
            walk_block(child)

    def _emit_paragraph(
        self,
        paragraph: etree._Element,
        *,
        story: str,
        story_id: str,
        part_name: str,
        text_box_id: str | None,
        cell_context: dict[str, Any] | None,
    ) -> None:
        paragraph_style = self.styles.paragraph_style(paragraph)
        text = _visible_text(
            paragraph,
            self.styles,
            paragraph_style,
            skip_text_boxes=True,
        )
        paragraph_markers = [
            marker
            for marker in _iter_effective(paragraph)
            if marker.tag in _COMMENT_MARKERS
            and next(marker.iterancestors(W("p")), None) is paragraph
            and marker.get(W("id")) is not None
        ]
        comment_scope = f"{story_id}|{text_box_id or 'main'}"
        active_comments = self.active_comment_ids.setdefault(comment_scope, set())
        comment_id_values = set(active_comments)
        for marker in paragraph_markers:
            comment_id = marker.get(W("id"))
            assert comment_id is not None
            comment_id_values.add(comment_id)
            if marker.tag == W("commentRangeStart"):
                active_comments.add(comment_id)
            elif marker.tag == W("commentRangeEnd"):
                active_comments.discard(comment_id)
        comment_ids = sorted(
            comment_id_values,
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        )
        if cell_context is not None:
            kind = "table_cell_paragraph"
        elif text_box_id is not None:
            kind = "text_box_paragraph"
        else:
            kind = "paragraph"
        record: dict[str, Any] = {
            "sequence": len(self.records) + 1,
            "story": story,
            "story_id": story_id,
            "part": part_name,
            "kind": kind,
            "location": _xml_location(part_name, paragraph),
            "text": text,
            "paragraph_style": paragraph_style,
            "has_review_comment": bool(comment_ids),
            "review_comment_ids": comment_ids,
        }
        if text_box_id is not None:
            record["text_box_id"] = text_box_id
        if cell_context is not None:
            record.update(cell_context)
        self.records.append(record)

        for text_box, anchor_offset in _top_level_text_boxes(
            paragraph, self.styles, paragraph_style
        ):
            box_id = f"textbox-{len(self.text_boxes) + 1:04d}"
            self.text_boxes.append(
                {
                    "id": box_id,
                    "story": story,
                    "story_id": story_id,
                    "part": part_name,
                    "anchor_record_sequence": record["sequence"],
                    "anchor_character_offset": anchor_offset,
                    "location": _xml_location(part_name, text_box),
                }
            )
            self._walk_container(
                text_box,
                story=story,
                story_id=story_id,
                part_name=part_name,
                text_box_id=box_id,
                cell_context=None,
            )

    def _walk_table(
        self,
        table: etree._Element,
        *,
        story: str,
        story_id: str,
        part_name: str,
        text_box_id: str | None,
    ) -> None:
        table_id = f"table-{len(self.tables) + 1:04d}"
        metadata: dict[str, Any] = {
            "id": table_id,
            "story": story,
            "story_id": story_id,
            "part": part_name,
            "location": _xml_location(part_name, table),
            "text_box_id": text_box_id,
            "rows": 0,
            "physical_rows": 0,
            "excluded_deleted_rows": [],
            "cells": [],
        }
        self.tables.append(metadata)
        physical_rows = [child for child in _effective_children(table) if child.tag == W("tr")]
        metadata["physical_rows"] = len(physical_rows)
        accepted_row_index = 0
        for physical_row_index, row in enumerate(physical_rows, start=1):
            row_properties = row.find(W("trPr"))
            row_is_deleted = row_properties is not None and any(
                child.tag in _DELETED_CONTAINERS for child in _effective_children(row_properties)
            )
            if row_is_deleted:
                deleted_text = _raw_text(row)
                metadata["excluded_deleted_rows"].append(
                    {
                        "physical_row_index": physical_row_index,
                        "location": _xml_location(part_name, row),
                        "char_count": len(deleted_text),
                        "content_sha256": _hash_text(deleted_text),
                    }
                )
                continue
            accepted_row_index += 1
            row_index = accepted_row_index
            cells = [child for child in _effective_children(row) if child.tag == W("tc")]
            for cell_index, cell in enumerate(cells, start=1):
                start_sequence = len(self.records) + 1
                context = {
                    "table_id": table_id,
                    "row_index": row_index,
                    "physical_row_index": physical_row_index,
                    "cell_index": cell_index,
                }
                self._walk_container(
                    cell,
                    story=story,
                    story_id=story_id,
                    part_name=part_name,
                    text_box_id=text_box_id,
                    cell_context=context,
                )
                metadata["cells"].append(
                    {
                        "row_index": row_index,
                        "physical_row_index": physical_row_index,
                        "cell_index": cell_index,
                        "location": _xml_location(part_name, cell),
                        "record_sequences": list(range(start_sequence, len(self.records) + 1)),
                    }
                )
        metadata["rows"] = accepted_row_index


def _relationship_map(root: etree._Element) -> dict[str, dict[str, str | None]]:
    relationships: dict[str, dict[str, str | None]] = {}
    for relationship in root.findall(PR("Relationship")):
        relationship_id = relationship.get("Id")
        if not relationship_id:
            raise DocxExtractionError("document.xml.rels contains a relationship without Id")
        if relationship_id in relationships:
            raise DocxExtractionError(
                f"document.xml.rels contains duplicate relationship Id {relationship_id}"
            )
        relationships[relationship_id] = {
            "type": relationship.get("Type"),
            "target": relationship.get("Target"),
            "target_mode": relationship.get("TargetMode"),
        }
    return relationships


def _active_story_parts(
    document_root: etree._Element,
    relationships: dict[str, dict[str, str | None]],
    *,
    even_and_odd_headers: bool,
) -> list[dict[str, Any]]:
    """Resolve explicit and inherited section header/footer references."""
    active: dict[tuple[str, str], str] = {}
    active_order: list[tuple[str, str]] = []
    references_by_part: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ordered_parts: list[tuple[str, str]] = []
    section_properties = [
        node
        for node in _iter_effective(document_root)
        if node.tag == W("sectPr")
        and not any(ancestor.tag == W("sectPrChange") for ancestor in node.iterancestors())
    ]
    for section_index, section in enumerate(section_properties, start=1):
        title_page = bool(_on_off_value(section.find(W("titlePg"))))
        explicit_keys: set[tuple[str, str]] = set()
        for reference in _effective_children(section):
            if reference.tag not in {W("headerReference"), W("footerReference")}:
                continue
            story = "header" if reference.tag == W("headerReference") else "footer"
            variant = reference.get(W("type"), "default")
            key = (story, variant)
            relationship_id = reference.get(R("id"))
            if not relationship_id or relationship_id not in relationships:
                raise DocxExtractionError(
                    f"Section {section_index} has unresolved {story} relationship"
                )
            relationship = relationships[relationship_id]
            expected_suffix = f"/{story}"
            if not (relationship["type"] or "").endswith(expected_suffix):
                raise DocxExtractionError(
                    f"Relationship {relationship_id} is not a {story} relationship"
                )
            if relationship["target_mode"] == "External":
                raise DocxExtractionError(
                    f"External {story} relationship is unsupported: {relationship_id}"
                )
            target = _resolve_relationship_target(
                "word/document.xml", relationship["target"] or ""
            )
            if key not in active:
                active_order.append(key)
            active[key] = target
            explicit_keys.add(key)
        for key in active_order:
            story, variant = key
            if variant not in {"default", "first", "even"}:
                raise DocxExtractionError(
                    f"Section {section_index} has unsupported {story} type {variant!r}"
                )
            if variant == "first" and not title_page:
                continue
            if variant == "even" and not even_and_odd_headers:
                continue
            target = active[key]
            part_key = (story, target)
            if part_key not in references_by_part:
                references_by_part[part_key] = []
                ordered_parts.append(part_key)
            references_by_part[part_key].append(
                {
                    "section_index": section_index,
                    "variant": variant,
                    "inherited": key not in explicit_keys,
                }
            )
    return [
        {
            "story": story,
            "part": part,
            "references": references_by_part[(story, part)],
        }
        for story, part in ordered_parts
    ]


def _comment_ids(comments_root: etree._Element | None) -> list[str]:
    if comments_root is None:
        return []
    values = {
        node.get(W("id"))
        for node in comments_root.findall(W("comment"))
        if node.get(W("id")) is not None
    }
    return sorted(
        values,
        key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
    )


def _gap_report(
    state: _ExtractionState, package_comment_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    def add(code: str, message: str, locations: Iterable[str]) -> None:
        unique_locations = list(dict.fromkeys(locations))
        payload = {
            "code": code,
            "severity": "warning",
            "message": message,
            "count": len(unique_locations),
            "locations": unique_locations,
        }
        warnings.append(payload.copy())
        gaps.append(payload)

    if state.insertions or state.deletions:
        add(
            "SOURCE_HAS_UNACCEPTED_REVISIONS",
            "源 DOCX 存在未接受修订；抽取文本采用接受全部修订后的视图。",
            [item["location"] for item in state.insertions + state.deletions],
        )
    if state.comment_markers or package_comment_ids:
        marker_ids = {
            item["comment_id"]
            for item in state.comment_markers
            if item["comment_id"] is not None
        }
        orphan_payload_locations = [
            f"word/comments.xml:w:comment[@w:id='{comment_id}']"
            for comment_id in package_comment_ids
            if comment_id not in marker_ids
        ]
        add(
            "SOURCE_HAS_REVIEW_COMMENTS",
            "源 DOCX 存在审阅批注；批注正文未进入事实文本。",
            [item["location"] for item in state.comment_markers]
            + orphan_payload_locations,
        )
    if state.hidden_runs:
        add(
            "SOURCE_HAS_HIDDEN_TEXT",
            "源 DOCX 存在隐藏文本；隐藏文本已排除并仅保留位置和哈希证据。",
            [item["location"] for item in state.hidden_runs],
        )
    return warnings, gaps


def extract_docx(path: str | Path) -> dict[str, Any]:
    """Extract a DOCX accepted-view text stream without modifying the source."""
    source = Path(path).resolve()
    if not source.is_file():
        raise DocxExtractionError(f"DOCX input is not a file: {source}")
    before_sha256 = sha256_file(source)
    before_size = source.stat().st_size

    try:
        with zipfile.ZipFile(source) as archive:
            names = [item.filename for item in archive.infolist()]
            duplicate_names = sorted({name for name in names if names.count(name) > 1})
            if duplicate_names:
                raise DocxExtractionError(
                    f"DOCX package contains duplicate part names: {duplicate_names}"
                )
            available = set(names)
            if "word/document.xml" not in available:
                raise DocxExtractionError("DOCX package is missing word/document.xml")

            parts_read: list[str] = []

            def read_xml(part_name: str, *, required: bool = True) -> etree._Element | None:
                if part_name not in available:
                    if required:
                        raise DocxExtractionError(f"DOCX package is missing {part_name}")
                    return None
                parts_read.append(part_name)
                return _parse_xml(archive.read(part_name), part_name)

            document_root = read_xml("word/document.xml")
            assert document_root is not None
            if document_root.tag != W("document"):
                raise DocxExtractionError("word/document.xml has an unexpected root element")
            styles_root = read_xml("word/styles.xml", required=False)
            settings_root = read_xml("word/settings.xml", required=False)
            even_and_odd_headers = False
            if settings_root is not None:
                even_and_odd_headers = bool(
                    _on_off_value(settings_root.find(W("evenAndOddHeaders")))
                )
            relationships_root = read_xml(
                "word/_rels/document.xml.rels", required=False
            )
            relationships = (
                _relationship_map(relationships_root)
                if relationships_root is not None
                else {}
            )
            comments_root = read_xml("word/comments.xml", required=False)

            state = _ExtractionState(styles=_StyleVisibility(styles_root))
            state.add_story(
                document_root,
                story="document",
                story_id="document",
                part_name="word/document.xml",
                references=[],
            )
            for index, story_part in enumerate(
                _active_story_parts(
                    document_root,
                    relationships,
                    even_and_odd_headers=even_and_odd_headers,
                ),
                start=1,
            ):
                part_name = story_part["part"]
                story_root = read_xml(part_name)
                assert story_root is not None
                expected_root = W("hdr") if story_part["story"] == "header" else W("ftr")
                if story_root.tag != expected_root:
                    raise DocxExtractionError(f"Unexpected root element in {part_name}")
                state.add_story(
                    story_root,
                    story=story_part["story"],
                    story_id=f"{story_part['story']}-{index:02d}",
                    part_name=part_name,
                    references=story_part["references"],
                )
    except zipfile.BadZipFile as exc:
        raise DocxExtractionError(f"Input is not a valid DOCX ZIP package: {source}") from exc

    after_sha256 = sha256_file(source)
    after_size = source.stat().st_size
    if before_sha256 != after_sha256 or before_size != after_size:
        raise DocxExtractionError(
            "DOCX source changed while it was being extracted; no report can be trusted"
        )

    package_comment_ids = _comment_ids(comments_root)
    warnings, gaps = _gap_report(state, package_comment_ids)
    marker_comment_ids = sorted(
        {item["comment_id"] for item in state.comment_markers if item["comment_id"] is not None},
        key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
    )
    accepted_text = "\n".join(record["text"] for record in state.records if record["text"])
    return {
        "schema_version": "1.0",
        "view": "accepted_all_revisions",
        "source": {
            "path": str(source),
            "size_bytes": before_size,
            "sha256_before": before_sha256,
            "sha256_after": after_sha256,
            "unchanged": True,
        },
        "traversal_contract": {
            "story_order": "document body, then referenced header/footer parts in first-reference order",
            "body_order": "OOXML block order; paragraph text stays aggregate and each recursive text box records its exact host-character anchor offset",
            "revisions": "w:ins included; w:del excluded",
            "comments": "markers only; comment bodies excluded",
            "hidden_text": "direct or style-inherited w:vanish excluded",
        },
        "parts_read": list(dict.fromkeys(parts_read)),
        "story_order": state.story_order,
        "records": state.records,
        "accepted_text": accepted_text,
        "tables": state.tables,
        "text_boxes": state.text_boxes,
        "findings": {
            "revisions": {
                "present": bool(state.insertions or state.deletions),
                "insertions": state.insertions,
                "deletions": state.deletions,
            },
            "comments": {
                "present": bool(state.comment_markers or package_comment_ids),
                "package_comment_ids": package_comment_ids,
                "marker_comment_ids": marker_comment_ids,
                "markers": state.comment_markers,
            },
            "hidden_text": {
                "present": bool(state.hidden_runs),
                "runs": state.hidden_runs,
            },
        },
        "warnings": warnings,
        "gap_report": gaps,
        "statistics": {
            "stories": len(state.story_order),
            "records": len(state.records),
            "nonempty_records": sum(bool(record["text"]) for record in state.records),
            "tables": len(state.tables),
            "table_cells": sum(len(table["cells"]) for table in state.tables),
            "text_boxes": len(state.text_boxes),
            "insertions": len(state.insertions),
            "deletions": len(state.deletions),
            "comment_markers": len(state.comment_markers),
            "hidden_runs": len(state.hidden_runs),
        },
    }


def extract_docx_to_file(path: str | Path, output: str | Path) -> dict[str, Any]:
    source = Path(path)
    destination = Path(output)
    if paths_alias(source, destination):
        raise DocxExtractionError("Extraction output must not alias the source DOCX")
    report = extract_docx(source)
    atomic_write_json(destination, report)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract an accepted-revisions view from DOCX at the OOXML layer"
    )
    parser.add_argument("docx", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = extract_docx_to_file(args.docx, args.output)
    except (DocxExtractionError, OSError) as exc:
        print(f"DOCX extraction failed: {exc}", file=sys.stderr)
        return 4
    print(
        f"Extracted {report['statistics']['records']} records; "
        f"warnings={len(report['warnings'])}; output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
