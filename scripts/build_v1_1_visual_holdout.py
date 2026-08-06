#!/usr/bin/env python3
"""Build a lawful visual-layout holdout without reading visual-QC rules.

The holdout deliberately lives under examples/output rather than fixtures. It
is generated only after the visual-QC configuration has been frozen. This
module must remain independent of the checker module and its rule files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "examples" / "output" / "v1.1.0" / "holdout"
FIXED_CREATED = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_cell_text(cell, text: str, *, bold: bool = False, size: int = 9) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _repeat_table_header(row) -> None:
    properties = row._tr.get_or_add_trPr()
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "true")
    properties.append(marker)


def _set_a4(section, *, landscape: bool = False) -> None:
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = Cm(29.7)
        section.page_height = Cm(21.0)
    else:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    section.header_distance = Cm(1.27)
    section.footer_distance = Cm(1.27)


def _set_story(section) -> None:
    header = section.header.paragraphs[0]
    header.text = "合法版式留出集｜合成数据"
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer = section.footer.paragraphs[0]
    footer.text = "版式留出集｜仅用于视觉检查标定"
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _add_body_paragraph(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(text)
    paragraph.paragraph_format.first_line_indent = Cm(0.74)
    paragraph.paragraph_format.line_spacing = 1.5
    paragraph.paragraph_format.space_after = Pt(6)


def build_holdout(output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.core_properties.title = "合法版式留出集"
    document.core_properties.subject = "视觉 QC 留出标定"
    document.core_properties.author = "csr-writer synthetic holdout"
    document.core_properties.created = FIXED_CREATED
    document.core_properties.modified = FIXED_CREATED

    normal = document.styles["Normal"]
    normal.font.name = "SimSun"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(10.5)
    for section in document.sections:
        _set_a4(section)
        _set_story(section)

    document.add_heading("1 纵向正文页", level=1)
    for index in range(7):
        _add_body_paragraph(
            document,
            f"这是纵向正文页的第{index + 1}段合成文字。"
            "段落仅用于验证 A4 纸张、正文带和普通分页。",
        )

    document.add_page_break()
    document.add_heading("2 带页眉页脚的页", level=1)
    for _ in range(5):
        _add_body_paragraph(
            document,
            "本页的页眉和页脚位于正文带之外。"
            "合法的 story 文字不应被当作正文越界。",
        )

    document.add_page_break()
    heading = document.add_heading("3 章首页", level=1)
    heading.paragraph_format.keep_with_next = True
    _add_body_paragraph(
        document,
        "章标题与随后的正文保持在同一页。"
        "这一页用于检查章首页是否被误判为空白页或孤立标题。",
    )

    landscape = document.add_section(WD_SECTION.NEW_PAGE)
    _set_a4(landscape, landscape=True)
    _set_story(landscape)
    document.add_heading("4 横向宽表页", level=1)
    table = document.add_table(rows=1, cols=8)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for index, cell in enumerate(table.rows[0].cells, start=1):
        _set_cell_text(cell, f"统计列{index}", bold=True, size=8)
    _repeat_table_header(table.rows[0])
    for row_index in range(12):
        row = table.add_row()
        for column_index, cell in enumerate(row.cells, start=1):
            _set_cell_text(cell, f"R{row_index + 1}-C{column_index}", size=8)

    portrait = document.add_section(WD_SECTION.NEW_PAGE)
    _set_a4(portrait)
    _set_story(portrait)
    document.add_heading("5 长表跨页且表头重复", level=1)
    long_table = document.add_table(rows=1, cols=4)
    long_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    long_table.style = "Table Grid"
    for index, text in enumerate(("分类", "项目", "状态", "说明")):
        _set_cell_text(long_table.rows[0].cells[index], text, bold=True)
    _repeat_table_header(long_table.rows[0])
    for row_index in range(78):
        row = long_table.add_row()
        values = (
            f"分类{row_index // 6 + 1}",
            f"合成项目{row_index + 1}",
            "已核对",
            "跨页长表的合法合成行",
        )
        for cell, value in zip(row.cells, values):
            _set_cell_text(cell, value, size=8)

    appendix = document.add_section(WD_SECTION.NEW_PAGE)
    _set_a4(appendix)
    _set_story(appendix)
    document.add_heading("A 附录页", level=1)
    for _ in range(6):
        _add_body_paragraph(
            document,
            "本附录仅含脱敏合成文字，用于验证附录页的常规版式。",
        )

    docx_path = output_dir / "visual_layout_holdout.deidentified.docx"
    document.save(docx_path)
    manifest = {
        "schema_version": "1.0",
        "purpose": "lawful layout holdout created after configuration freeze",
        "source_kind": "synthetic_deidentified",
        "uses_visual_qc_configuration": False,
        "document": {"filename": docx_path.name, "sha256": _sha256(docx_path)},
        "scenarios": [
            "portrait_body",
            "landscape_wide_table",
            "long_table_repeated_header",
            "header_footer",
            "chapter_opener",
            "appendix_page",
        ],
        "expected_class_a_findings": 0,
        "generator": {
            "filename": Path(__file__).name,
            "sha256": _sha256(Path(__file__)),
            "forbidden_imports_absent": True,
        },
    }
    manifest_path = output_dir / "holdout_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_holdout(args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
