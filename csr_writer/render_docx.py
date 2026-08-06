"""M1: render a title-only ICH E3 skeleton from a replaceable master DOCX."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm

from .errors import StructureError, TemplateError
from .io_utils import atomic_write_json, paths_alias, sha256_file
from .structure import ChapterTree, SectionNode, flatten_sections, load_chapter_tree
from .template import load_format_spec, validate_template


@dataclass(frozen=True)
class RenderResult:
    output: Path
    headings: tuple[SectionNode, ...]
    structure_sha256: str
    template_sha256: str
    output_sha256: str


def _clear_document_body(document: Document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _atomic_save_docx(document: Document, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".docx", dir=destination.parent
    )
    os.close(file_descriptor)
    try:
        document.save(temporary)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _clear_paragraph(paragraph) -> None:
    paragraph._p.clear_content()


def _add_complex_field(paragraph, instruction: str) -> None:
    """Append a field with no stale displayed result; Word fills it on F9/update."""
    begin_run = OxmlElement("w:r")
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin_run.append(begin)
    paragraph._p.append(begin_run)

    instruction_run = OxmlElement("w:r")
    instruction_text = OxmlElement("w:instrText")
    instruction_text.set(qn("xml:space"), "preserve")
    instruction_text.text = instruction
    instruction_run.append(instruction_text)
    paragraph._p.append(instruction_run)

    separate_run = OxmlElement("w:r")
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    separate_run.append(separate)
    paragraph._p.append(separate_run)

    end_run = OxmlElement("w:r")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run.append(end)
    paragraph._p.append(end_run)


def _add_bookmark_around_run(paragraph, name: str, bookmark_id: int, text: str) -> None:
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    paragraph._p.append(start)
    paragraph.add_run(text)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.append(end)


def _inject_multilevel_numbering(document: Document, levels: int = 5) -> int:
    numbering = document.part.numbering_part.element
    abstract_ids = [
        int(node.get(qn("w:abstractNumId")))
        for node in numbering.findall(qn("w:abstractNum"))
        if node.get(qn("w:abstractNumId"), "").isdigit()
    ]
    num_ids = [
        int(node.get(qn("w:numId")))
        for node in numbering.findall(qn("w:num"))
        if node.get(qn("w:numId"), "").isdigit()
    ]
    abstract_id = max(abstract_ids, default=0) + 1
    num_id = max(num_ids, default=0) + 1

    level_xml: list[str] = []
    for level in range(levels):
        numbering_text = ".".join(f"%{part + 1}" for part in range(level + 1))
        level_xml.append(
            f"""
            <w:lvl {nsdecls('w')} w:ilvl="{level}">
              <w:start w:val="1"/>
              <w:numFmt w:val="decimal"/>
              <w:pStyle w:val="Heading{level + 1}"/>
              <w:suff w:val="space"/>
              <w:lvlText w:val="{numbering_text}"/>
              <w:lvlJc w:val="left"/>
              <w:pPr><w:ind w:left="0" w:firstLine="0" w:hanging="0"/></w:pPr>
            </w:lvl>"""
        )
    abstract = parse_xml(
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{abstract_id}">'
        f'<w:multiLevelType w:val="multilevel"/>{"".join(level_xml)}</w:abstractNum>'
    )
    first_num_index = next(
        (index for index, child in enumerate(numbering) if child.tag == qn("w:num")),
        len(numbering),
    )
    numbering.insert(first_num_index, abstract)
    concrete = parse_xml(
        f'<w:num {nsdecls("w")} w:numId="{num_id}">'
        f'<w:abstractNumId w:val="{abstract_id}"/></w:num>'
    )
    numbering.append(concrete)
    return num_id


def _attach_numbering(paragraph, num_id: int, level: int) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    existing = ppr.find(qn("w:numPr"))
    if existing is not None:
        ppr.remove(existing)
    num_pr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), str(level - 1))
    number = OxmlElement("w:numId")
    number.set(qn("w:val"), str(num_id))
    num_pr.append(ilvl)
    num_pr.append(number)
    ppr.append(num_pr)


def _configure_section(section, page: dict) -> None:
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Cm(float(page["width_cm"]))
    section.page_height = Cm(float(page["height_cm"]))
    margin = page["margin_cm"]
    section.top_margin = Cm(float(margin["top"]))
    section.bottom_margin = Cm(float(margin["bottom"]))
    section.left_margin = Cm(float(margin["left"]))
    section.right_margin = Cm(float(margin["right"]))
    section.gutter = Cm(0)
    section.header_distance = Cm(float(page["header_distance_cm"]))
    section.footer_distance = Cm(float(page["footer_distance_cm"]))
    allowed_layout = {
        qn("w:headerReference"),
        qn("w:footerReference"),
        qn("w:pgSz"),
        qn("w:pgMar"),
        qn("w:pgNumType"),
        qn("w:cols"),
        qn("w:docGrid"),
    }
    for child in list(section._sectPr):
        if child.tag not in allowed_layout:
            section._sectPr.remove(child)

    columns = section._sectPr.find(qn("w:cols"))
    if columns is None:
        columns = OxmlElement("w:cols")
        grid = section._sectPr.find(qn("w:docGrid"))
        if grid is None:
            section._sectPr.append(columns)
        else:
            section._sectPr.insert(section._sectPr.index(grid), columns)
    columns.attrib.clear()
    columns.set(qn("w:space"), "720")
    for child in list(columns):
        columns.remove(child)

    grid = section._sectPr.find(qn("w:docGrid"))
    if grid is None:
        grid = OxmlElement("w:docGrid")
        section._sectPr.append(grid)
    grid.attrib.clear()
    grid.set(qn("w:linePitch"), "360")
    for child in list(grid):
        grid.remove(child)


def _clear_page_numbering(section) -> None:
    page_number = section._sectPr.find(qn("w:pgNumType"))
    if page_number is not None:
        section._sectPr.remove(page_number)


def _discard_template_stories(document: Document) -> None:
    """Remove every inherited story reference and its relationship before rebuilding."""
    for section in document.sections:
        for child in list(section._sectPr):
            if child.tag in {qn("w:headerReference"), qn("w:footerReference")}:
                section._sectPr.remove(child)
    for relationship_id, relationship in list(document.part.rels.items()):
        if relationship.reltype in {RT.HEADER, RT.FOOTER}:
            document.part.drop_rel(relationship_id)


def _disable_first_and_even_stories(document: Document) -> None:
    settings = document.settings._element
    even_and_odd = settings.find(qn("w:evenAndOddHeaders"))
    if even_and_odd is not None:
        settings.remove(even_and_odd)


def _set_page_number_start(section, start: int) -> None:
    sect_pr = section._sectPr
    existing = sect_pr.find(qn("w:pgNumType"))
    if existing is not None:
        sect_pr.remove(existing)
    page_number = OxmlElement("w:pgNumType")
    page_number.set(qn("w:start"), str(start))
    # python-docx's ordered insertion helper is not public; insert before cols/docGrid.
    insertion_index = len(sect_pr)
    for index, child in enumerate(sect_pr):
        if child.tag in {qn("w:cols"), qn("w:formProt"), qn("w:vAlign"), qn("w:docGrid")}:
            insertion_index = index
            break
    sect_pr.insert(insertion_index, page_number)


def _set_header(section, text: str) -> None:
    paragraph = section.header.paragraphs[0]
    _clear_paragraph(paragraph)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.style = "CSR Header"
    paragraph.add_run(text)


def _set_cover_footer(section) -> None:
    paragraph = section.footer.paragraphs[0]
    _clear_paragraph(paragraph)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.style = "CSR Footer"


def _set_body_footer(section, confidentiality: str) -> None:
    paragraph = section.footer.paragraphs[0]
    _clear_paragraph(paragraph)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.style = "CSR Footer"
    paragraph.add_run("第 ")
    _add_complex_field(paragraph, " PAGE ")
    paragraph.add_run(" 页，共 ")
    # SECTIONPAGES intentionally excludes the unnumbered cover section.
    _add_complex_field(paragraph, " SECTIONPAGES ")
    paragraph.add_run(f" 页    {confidentiality}")


def _enable_field_updates(document: Document) -> None:
    settings = document.settings._element
    zoom = settings.find(qn("w:zoom"))
    if zoom is not None:
        zoom.set(qn("w:percent"), "100")
    existing = settings.find(qn("w:updateFields"))
    if existing is None:
        existing = OxmlElement("w:updateFields")
        compatibility = settings.find(qn("w:compat"))
        if compatibility is None:
            settings.append(existing)
        else:
            settings.insert(settings.index(compatibility), existing)
    existing.set(qn("w:val"), "true")


def _render_heading(
    document: Document,
    node: SectionNode,
    num_id: int,
    title_language: str,
    bookmark_id: int,
    page_break_before: bool,
) -> None:
    paragraph = document.add_paragraph(style=f"Heading {node.level}")
    if page_break_before:
        paragraph.paragraph_format.page_break_before = True
    numeric_id = all(part.isdigit() for part in node.id.split("."))
    title = node.title(title_language)
    if numeric_id:
        _attach_numbering(paragraph, num_id, node.level)
        rendered_title = title
    else:
        # Letter-suffixed sponsor extensions cannot be represented by decimal OOXML numbering.
        rendered_title = f"{node.id} {title}"
    bookmark_name = "_Ref_Sec_" + node.id.replace(".", "_").replace("-", "_")
    _add_bookmark_around_run(paragraph, bookmark_name, bookmark_id, rendered_title)


def render_skeleton(
    structure_path: str | Path,
    template_path: str | Path,
    format_spec_path: str | Path,
    output_path: str | Path,
    layers: Iterable[str] = ("ich-official",),
    title_language: str = "zh",
    header_text: str = "临床试验总结报告骨架",
    confidentiality: str = "机密",
    manifest_path: str | Path | None = None,
) -> RenderResult:
    path_roles: list[tuple[str, Path]] = []
    for role, raw_path in (
        ("structure", structure_path),
        ("template", template_path),
        ("format-spec", format_spec_path),
        ("output", output_path),
        ("manifest", manifest_path),
    ):
        if raw_path is None:
            continue
        candidate = Path(raw_path)
        for prior_role, prior_path in path_roles:
            if paths_alias(candidate, prior_path):
                raise TemplateError(
                    f"Path collision: {role} aliases {prior_role}: {candidate}"
                )
        path_roles.append((role, candidate))

    selected_layers = tuple(layers)
    validate_template(template_path)
    tree: ChapterTree = load_chapter_tree(structure_path)
    selected_roots = tree.select_layers(selected_layers)
    headings = tuple(flatten_sections(selected_roots))
    if not headings or headings[0].id != "1":
        raise StructureError("Selected skeleton must begin with ICH E3 section 1")

    spec = load_format_spec(format_spec_path)
    page = spec["authoring_profile"]["page"]
    document = Document(template_path)
    _clear_document_body(document)
    numbering_id = _inject_multilevel_numbering(document)
    _disable_first_and_even_stories(document)

    cover_section = document.sections[0]
    _configure_section(cover_section, page)

    _render_heading(
        document,
        headings[0],
        numbering_id,
        title_language,
        bookmark_id=1,
        page_break_before=False,
    )

    body_section = document.add_section(WD_SECTION.NEW_PAGE)
    # add_section clones the preceding sectPr; normalize both sections only afterwards.
    cover_section = document.sections[0]
    body_section = document.sections[1]
    _discard_template_stories(document)
    for section in (cover_section, body_section):
        _configure_section(section, page)
        section.header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False
    _clear_page_numbering(cover_section)
    _set_header(cover_section, header_text)
    _set_cover_footer(cover_section)
    _configure_section(body_section, page)
    _set_header(body_section, header_text)
    _set_body_footer(body_section, confidentiality)
    _set_page_number_start(body_section, 1)

    for bookmark_id, node in enumerate(headings[1:], start=2):
        is_top_level = node.level == 1
        _render_heading(
            document,
            node,
            numbering_id,
            title_language,
            bookmark_id=bookmark_id,
            page_break_before=is_top_level and node.id != "2",
        )
        if node.id == "3":
            toc = document.add_paragraph(style="CSR TOC")
            # Keep one direct 1.5-line declaration so XML-only QC can verify the scaffold.
            toc.paragraph_format.line_spacing = float(
                spec["authoring_profile"]["styles"]["body"]["line_spacing"]
            )
            _add_complex_field(toc, ' TOC \\o "1-5" \\h \\z \\u ')

    _enable_field_updates(document)
    destination = Path(output_path)
    _atomic_save_docx(document, destination)

    result = RenderResult(
        output=destination,
        headings=headings,
        structure_sha256=sha256_file(structure_path),
        template_sha256=sha256_file(template_path),
        output_sha256=sha256_file(destination),
    )
    if manifest_path is not None:
        atomic_write_json(
            manifest_path,
            {
                "schema_version": "1.0",
                "milestone": "M1",
                "structure_sha256": result.structure_sha256,
                "template_sha256": result.template_sha256,
                "output_sha256": result.output_sha256,
                "layers": sorted(set(selected_layers)),
                "available_layer_counts": dict(
                    sorted(Counter(node.layer for node in tree.walk()).items())
                ),
                "selected_layer_counts": dict(
                    sorted(Counter(node.layer for node in headings).items())
                ),
                "title_language": title_language,
                "headings": [
                    {"id": node.id, "layer": node.layer, "title": node.title(title_language)}
                    for node in headings
                ],
            },
        )
    return result
