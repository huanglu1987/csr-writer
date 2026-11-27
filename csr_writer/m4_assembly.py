"""M4 annotated/clean DOCX assembly with a hard human-review gate."""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from .cross_chapter import (
    REVIEW_MARKER,
    CrossChapterError,
    ResolvedSection,
    apply_review_decisions,
    derive_synopsis_manifest,
    verify_synopsis_copy,
)
from .cross_references import (
    REF_PLACEHOLDER_RE,
    bookmark_name,
    display_reference,
)
from .errors import CSRWriterError
from .io_utils import atomic_write_json, paths_alias, sha256_file
from .paths import DEFAULT_TEMPLATE


class AssemblyError(CSRWriterError):
    """M4 output assembly cannot produce a safe document."""


class CleanOutputBlocked(AssemblyError):
    """The clean reader document is forbidden while review items remain."""


CLEAN_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pending-marker", re.compile(r"\[待[^\]\r\n]{0,30}(?:确认|核实|补充|处理)\]")),
    ("internal-draft-marker", re.compile(r"【待补】|〔待(?:确认|补充|签署|医学负责人复核)[^〕\r\n]*〕|内部审阅概要")),
    ("internal-provenance", re.compile(r"内部溯源|内部\s*QC", re.I)),
    ("tool-name", re.compile(r"csr[- ]?writer|数值守卫|工具生成", re.I)),
    ("qc-note", re.compile(r"(?:QC|质检)(?:说明|备注|发现|记录)", re.I)),
    ("unresolved-placeholder", re.compile(r"\{\{[^{}]+\}\}")),
    ("internal-error-code", re.compile(r"\[(?:E|W)-[A-Z0-9_-]+\]")),
    ("process-language", re.compile(r"去重过程|整合过程|处理过程")),
)


def _clear_document_body(document: Document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _new_document(template_path: str | Path) -> Document:
    template = Path(template_path)
    if not template.is_file():
        raise AssemblyError(f"DOCX template does not exist: {template}")
    try:
        document = Document(template)
    except Exception as exc:
        raise AssemblyError(f"Cannot open DOCX template {template}: {exc}") from exc
    _clear_document_body(document)
    return document


def _atomic_save_docx(
    document: Document,
    output_path: str | Path,
    *,
    prepublish_validator: Callable[[str | Path], Mapping[str, Any]] | None = None,
) -> Path:
    output = Path(output_path)
    if output.suffix.lower() != ".docx":
        raise AssemblyError(f"DOCX output must end in .docx: {output}")
    if output.is_dir():
        raise AssemblyError(f"DOCX output is a directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output.name}.staged.", suffix=".docx", dir=output.parent
    )
    os.close(fd)
    try:
        document.save(temporary)
        # Re-open the staged package before publishing it.
        Document(temporary)
        if prepublish_validator is not None:
            prepublish_validator(temporary)
        os.replace(temporary, output)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return output


def _add_demo_notice(document: Document, sections: Sequence[ResolvedSection]) -> None:
    if any(section.evidence_status == "demo_only" for section in sections):
        paragraph = document.add_paragraph()
        run = paragraph.add_run("【脱敏演示；非临床证据】")
        run.bold = True


def _append_reference_field(paragraph, entity_id: str) -> None:
    begin_run = OxmlElement("w:r")
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin_run.append(begin)
    paragraph._p.append(begin_run)

    instruction_run = OxmlElement("w:r")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = f" REF {bookmark_name(entity_id)} \\h "
    instruction_run.append(instruction)
    paragraph._p.append(instruction_run)

    separate_run = OxmlElement("w:r")
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    separate_run.append(separate)
    paragraph._p.append(separate_run)
    paragraph.add_run(display_reference(entity_id))

    end_run = OxmlElement("w:r")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run.append(end)
    paragraph._p.append(end_run)


def _append_text_with_references(paragraph, text: str) -> None:
    cursor = 0
    for match in REF_PLACEHOLDER_RE.finditer(text):
        if match.start() > cursor:
            paragraph.add_run(text[cursor : match.start()])
        _append_reference_field(paragraph, match.group("entity"))
        cursor = match.end()
    if cursor < len(text):
        paragraph.add_run(text[cursor:])


def _reference_entities(sections: Sequence[ResolvedSection]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            entity_id
            for section in sections
            for entity_id in section.reference_entities
        )
    )


def _add_reference_anchors(document: Document, sections: Sequence[ResolvedSection]) -> None:
    entities = _reference_entities(sections)
    if not entities:
        return
    document.add_heading("支持性表、图和清单", level=1)
    existing_ids = [
        int(node.get(qn("w:id")))
        for node in document._element.iter(qn("w:bookmarkStart"))
        if (node.get(qn("w:id")) or "").isdigit()
    ]
    next_id = max(existing_ids, default=0) + 1
    for entity_id in entities:
        paragraph = document.add_paragraph()
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(next_id))
        start.set(qn("w:name"), bookmark_name(entity_id))
        paragraph._p.append(start)
        paragraph.add_run(display_reference(entity_id))
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), str(next_id))
        paragraph._p.append(end)
        next_id += 1


def render_annotated_docx(
    sections: Sequence[ResolvedSection],
    output_path: str | Path,
    *,
    protocol_id: str,
    template_path: str | Path = DEFAULT_TEMPLATE,
) -> dict[str, Any]:
    """Render the internal document with review markers and paragraph provenance."""
    if not sections:
        raise AssemblyError("Annotated output needs at least one section")
    if paths_alias(output_path, template_path):
        raise AssemblyError("Annotated output must not overwrite the DOCX template")
    document = _new_document(template_path)
    document.add_heading(f"临床试验总结报告（内部批注版）—{protocol_id}", level=0)
    _add_demo_notice(document, sections)
    pending: list[str] = []
    for section in sections:
        document.add_heading(section.heading, level=1)
        for paragraph in section.paragraphs:
            body = document.add_paragraph()
            if paragraph.needs_human:
                marker = body.add_run(REVIEW_MARKER + " ")
                marker.bold = True
                marker.font.highlight_color = WD_COLOR_INDEX.YELLOW
                pending.append(f"{section.section_id}:{paragraph.para_id}")
            text = paragraph.text
            if text.startswith(REVIEW_MARKER):
                text = text[len(REVIEW_MARKER) :].lstrip()
            _append_text_with_references(body, text)
            provenance = document.add_paragraph(style="CSR Footer")
            provenance_run = provenance.add_run(
                f"内部溯源：{paragraph.para_id}；来源={', '.join(paragraph.sources)}；"
                f"置信度={paragraph.confidence}"
            )
            provenance_run.italic = True
    _add_reference_anchors(document, sections)
    output = _atomic_save_docx(document, output_path)
    reference_check = validate_docx_references(output)
    return {
        "schema_version": "1.0",
        "status": "pass",
        "mode": "annotated",
        "output": str(output.resolve()),
        "sha256": sha256_file(output),
        "pending_count": len(pending),
        "pending_paragraphs": pending,
        "cross_references": reference_check,
    }


def _docx_visible_text(path: str | Path) -> str:
    """Read visible textual XML stories for release-marker scanning."""
    source = Path(path)
    chunks: list[str] = []
    try:
        with zipfile.ZipFile(source) as package:
            members = sorted(
                name
                for name in package.namelist()
                if name.startswith("word/") and name.endswith(".xml")
            )
            for member in members:
                root = etree.fromstring(package.read(member))
                for element in root.iter():
                    if etree.QName(element).localname in {"t", "instrText"} and element.text:
                        chunks.append(element.text)
    except Exception as exc:
        raise AssemblyError(f"Cannot scan DOCX XML {source}: {exc}") from exc
    return "\n".join(chunks)


def validate_clean_docx(path: str | Path) -> dict[str, Any]:
    text = _docx_visible_text(path)
    findings = [
        {"code": code, "pattern": pattern.pattern}
        for code, pattern in CLEAN_FORBIDDEN_PATTERNS
        if pattern.search(text)
    ]
    if findings:
        raise CleanOutputBlocked(f"Clean DOCX contains internal/review markers: {findings}")
    return {
        "status": "pass",
        "checks": {
            "no_pending_markers": "pass",
            "no_internal_provenance": "pass",
            "no_tool_or_process_language": "pass",
        },
        "sha256": sha256_file(path),
    }


def validate_docx_references(path: str | Path) -> dict[str, Any]:
    """Verify every generated REF instruction resolves to exactly one bookmark."""

    source = Path(path)
    try:
        with zipfile.ZipFile(source) as package:
            root = etree.fromstring(package.read("word/document.xml"))
    except Exception as exc:
        raise AssemblyError(f"Cannot validate DOCX references in {source}: {exc}") from exc
    bookmarks = [
        node.get(qn("w:name"))
        for node in root.iter(qn("w:bookmarkStart"))
        if node.get(qn("w:name"), "").startswith("_Ref_")
    ]
    instructions = [
        (node.text or "").strip()
        for node in root.iter(qn("w:instrText"))
        if (node.text or "").strip().startswith("REF ")
    ]
    targets: list[str] = []
    for instruction in instructions:
        match = re.fullmatch(r"REF\s+(?P<name>_Ref_[A-Za-z0-9_]+)\s+\\h", instruction)
        if match is None:
            raise AssemblyError(f"Malformed generated REF field: {instruction!r}")
        targets.append(match.group("name"))
    duplicate_bookmarks = sorted(name for name in set(bookmarks) if bookmarks.count(name) != 1)
    missing_targets = sorted(set(targets) - set(bookmarks))
    if duplicate_bookmarks or missing_targets:
        raise AssemblyError(
            "DOCX cross-reference contract failed: "
            f"duplicate_bookmarks={duplicate_bookmarks}, missing_targets={missing_targets}"
        )
    return {
        "status": "pass",
        "field_count": len(targets),
        "bookmark_count": len(bookmarks),
        "unique_target_count": len(set(targets)),
    }


def _render_clean_document(
    sections: Sequence[ResolvedSection],
    clean_paragraphs: Mapping[str, Sequence[str]],
    *,
    protocol_id: str,
    template_path: str | Path,
) -> Document:
    document = _new_document(template_path)
    document.add_heading(f"临床试验总结报告—{protocol_id}", level=0)
    _add_demo_notice(document, sections)
    for section in sections:
        paragraphs = clean_paragraphs.get(section.section_id)
        if paragraphs is None or len(paragraphs) != len(section.paragraphs):
            raise AssemblyError(f"Clean paragraph contract missing for {section.section_id}")
        document.add_heading(section.heading, level=1)
        for text in paragraphs:
            paragraph = document.add_paragraph()
            _append_text_with_references(paragraph, str(text))
    _add_reference_anchors(document, sections)
    return document


def _render_synopsis_document(
    synopsis_paragraphs: Sequence[str],
    sections: Sequence[ResolvedSection],
    *,
    protocol_id: str,
    template_path: str | Path,
) -> Document:
    document = _new_document(template_path)
    document.add_heading(f"临床试验总结报告概要—{protocol_id}", level=0)
    _add_demo_notice(document, sections)
    for text in synopsis_paragraphs:
        paragraph = document.add_paragraph()
        _append_text_with_references(paragraph, str(text))
    _add_reference_anchors(document, sections)
    return document


def render_clean_and_synopsis(
    sections: Sequence[ResolvedSection],
    decisions: Mapping[str, Any],
    selection: Mapping[str, Sequence[str]],
    clean_output: str | Path,
    synopsis_output: str | Path,
    synopsis_manifest_output: str | Path,
    *,
    protocol_id: str,
    template_path: str | Path = DEFAULT_TEMPLATE,
) -> dict[str, Any]:
    """Render clean + Synopsis only after all review items have explicit replacements."""
    outputs = [Path(clean_output), Path(synopsis_output), Path(synopsis_manifest_output)]
    for index, output in enumerate(outputs):
        if paths_alias(output, template_path):
            raise AssemblyError("M4 output must not overwrite the DOCX template")
        for other in outputs[index + 1 :]:
            if paths_alias(output, other):
                raise AssemblyError(f"M4 output paths alias: {output} and {other}")
    try:
        clean_paragraphs = apply_review_decisions(sections, decisions)
    except CrossChapterError as exc:
        raise CleanOutputBlocked(str(exc)) from exc
    synopsis_paragraphs, manifest = derive_synopsis_manifest(
        sections, clean_paragraphs, selection
    )
    synopsis_check = verify_synopsis_copy(synopsis_paragraphs, manifest)

    clean_doc = _render_clean_document(
        sections, clean_paragraphs, protocol_id=protocol_id, template_path=template_path
    )
    synopsis_doc = _render_synopsis_document(
        synopsis_paragraphs, sections, protocol_id=protocol_id, template_path=template_path
    )
    clean_path = _atomic_save_docx(
        clean_doc, clean_output, prepublish_validator=validate_clean_docx
    )
    synopsis_path = _atomic_save_docx(
        synopsis_doc, synopsis_output, prepublish_validator=validate_clean_docx
    )
    clean_check = validate_clean_docx(clean_path)
    synopsis_clean_check = validate_clean_docx(synopsis_path)
    clean_reference_check = validate_docx_references(clean_path)
    synopsis_reference_check = validate_docx_references(synopsis_path)
    manifest = {
        **manifest,
        "clean_output_sha256": sha256_file(clean_path),
        "synopsis_output_sha256": sha256_file(synopsis_path),
    }
    atomic_write_json(synopsis_manifest_output, manifest)
    return {
        "schema_version": "1.0",
        "status": "pass",
        "clean": {
            "path": str(clean_path.resolve()),
            "validation": clean_check,
            "cross_references": clean_reference_check,
        },
        "synopsis": {
            "path": str(synopsis_path.resolve()),
            "validation": synopsis_clean_check,
            "copy_check": synopsis_check,
            "cross_references": synopsis_reference_check,
            "manifest": str(Path(synopsis_manifest_output).resolve()),
        },
    }


def load_review_decisions(path: str | Path) -> dict[str, Any]:
    from .io_utils import load_json_unique

    payload = load_json_unique(path)
    if not isinstance(payload, dict):
        raise AssemblyError("Review decisions JSON must be an object")
    return payload
