"""Visible Word directory entries and body page totals for internal review drafts."""
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from .cross_references import bookmark_name
from .project_skeleton import _bookmark_key, _next_bookmark_id, _section_sort_key
from .render_docx import _add_complex_field


def table_destination(entry):
    return "_Body" + bookmark_name(entry["publication_entity_id"])


def mark_destination(doc, paragraph, marker):
    ident = str(_next_bookmark_id(doc))
    for tag in ("bookmarkStart", "bookmarkEnd"):
        node = OxmlElement("w:" + tag)
        node.set(qn("w:id"), ident)
        if tag == "bookmarkStart":
            node.set(qn("w:name"), marker)
        paragraph._p.append(node)


def prepare_draft_navigation(doc, titles, exhibits, supplemental=()):
    last = doc.paragraphs[-1]
    if last._p.getnext() is not doc._element.body.sectPr:
        last = doc.add_paragraph()
    marker = "_CSRReportEnd"
    mark_destination(doc, last, marker)
    for section in doc.sections:
        for instruction in section.footer._element.xpath(".//w:instrText"):
            if (instruction.text or "").strip() == "SECTIONPAGES":
                instruction.text = f" PAGEREF {marker} "

    main = next(p for p in doc.paragraphs if any(
        (i.text or "").strip().startswith("TOC ") and "\\o" in (i.text or "")
        for i in p._p.xpath(".//w:instrText")))
    entries = [(sid, f"{sid} {titles[sid]}", _bookmark_key(sid)) for sid in sorted(titles, key=_section_sort_key)
               if not sid.startswith("3.")]
    _linked_directory(doc, main, entries)
    for p in doc.paragraphs:
        codes = [i.text or "" for i in p._p.xpath(".//w:instrText")]
        if any('\\c "Table"' in code for code in codes):
            table_entries = [(sid, f"{entry['publication_entity_id']}　{entry['title']}", table_destination(entry))
                             for sid, tables in exhibits.items() for entry, _, _ in tables]
            table_entries.extend((sid, label, target) for sid,label,target,kind in supplemental if kind=="table" and not sid.startswith("14."))
            if table_entries:
                _linked_directory(doc, p, table_entries)
        if any('\\c "Figure"' in code for code in codes):
            figures = [(sid,label,target) for sid,label,target,kind in supplemental if kind=="figure"]
            if figures:
                _linked_directory(doc,p,figures)
        if p.text == "尚未登记第 14 章表图清单。":
            appendix = [(sid,label,target) for sid,label,target,kind in supplemental if sid.startswith("14.")]
            if appendix:
                _linked_directory(doc,p,appendix)


def _linked_directory(doc, field, entries):
    # Native bookmark page references update independently of the TOC refresh dialog.
    field._p.clear_content()
    p = field
    for i, (sid, label, target) in enumerate(entries):
        if i:
            nxt = doc.add_paragraph(style="CSR TOC")
            p._p.addnext(nxt._p)
            p = nxt
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.05
        p.paragraph_format.left_indent = Cm(0.35 * sid.count("."))
        p.add_run(label + "  ……  ")
        if sid == "1":
            p.add_run("封面")
        else:
            _add_complex_field(p, f" PAGEREF {target} \\h ")
