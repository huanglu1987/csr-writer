#!/usr/bin/env python3
"""Create the post-freeze lawful layout set used for false-positive calibration."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "examples" / "output" / "v1.1.0" / "holdout"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare(*, landscape: bool = False, stories: bool = False) -> Document:
    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width = Cm(29.7 if landscape else 21.0)
    section.page_height = Cm(21.0 if landscape else 29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    section.header_distance = Cm(1.25)
    section.footer_distance = Cm(1.25)
    if stories:
        header = section.header.paragraphs[0]
        header.text = "合成留出资料｜版式标定"
        header.alignment = WD_ALIGN_PARAGRAPH.CENTER
        footer = section.footer.paragraphs[0]
        footer.text = "脱敏合成文档｜非临床证据"
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    normal = document.styles["Normal"]
    normal.font.name = "SimSun"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(10.5)
    return document


def _paragraph(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(text)
    paragraph.paragraph_format.first_line_indent = Cm(0.74)
    paragraph.paragraph_format.line_spacing = 1.5
    paragraph.paragraph_format.space_after = Pt(6)


def _repeat_header(row) -> None:
    properties = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    properties.append(header)


def _table_text(cell, value: str, *, bold: bool = False, size: int = 8) -> None:
    cell.text = ""
    run = cell.paragraphs[0].add_run(value)
    run.bold = bold
    run.font.size = Pt(size)


def _save(document: Document, path: Path) -> dict[str, object]:
    document.save(path)
    return {"filename": path.name, "sha256": _sha256(path)}


def build_holdout_set(output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, object]] = []

    portrait = _prepare()
    portrait.add_heading("1 纵向正文", level=1)
    for index in range(12):
        _paragraph(
            portrait,
            f"第{index + 1}段用于检查纵向 A4 正文的常规分页。"
            "文字和数值均为脱敏合成内容。",
        )
    portrait.add_page_break()
    portrait.add_heading("2 纵向正文续页", level=1)
    for _ in range(6):
        _paragraph(portrait, "续页保持相同页边距，页面内不含异常对象。")
    documents.append(
        {"scenario": "portrait_body", **_save(portrait, output_dir / "01_portrait_body.docx")}
    )

    stories = _prepare(stories=True)
    stories.add_heading("1 带页眉页脚的正文", level=1)
    for _ in range(10):
        _paragraph(stories, "页眉和页脚位于正文带外，正文均在合法版心内。")
    stories.add_page_break()
    stories.add_heading("2 带 story 的续页", level=1)
    for _ in range(5):
        _paragraph(stories, "续页沿用同一页眉和页脚，不改变正文带。")
    documents.append(
        {"scenario": "header_footer", **_save(stories, output_dir / "02_header_footer.docx")}
    )

    chapter = _prepare()
    heading = chapter.add_heading("7 章首页示例", level=1)
    heading.paragraph_format.keep_with_next = True
    for _ in range(8):
        _paragraph(chapter, "章标题后立即跟随正文，页面不应被视为空白页。")
    documents.append(
        {"scenario": "chapter_opener", **_save(chapter, output_dir / "03_chapter_opener.docx")}
    )

    landscape = _prepare(landscape=True, stories=True)
    landscape.add_heading("14.2 横向宽表", level=1)
    wide = landscape.add_table(rows=1, cols=10)
    wide.alignment = WD_TABLE_ALIGNMENT.CENTER
    wide.style = "Table Grid"
    for column, cell in enumerate(wide.rows[0].cells, start=1):
        _table_text(cell, f"列{column}", bold=True, size=7)
    _repeat_header(wide.rows[0])
    for row_index in range(14):
        row = wide.add_row()
        for column, cell in enumerate(row.cells, start=1):
            _table_text(cell, f"{row_index + 1}-{column}", size=7)
    documents.append(
        {
            "scenario": "landscape_wide_table",
            **_save(landscape, output_dir / "04_landscape_wide_table.docx"),
        }
    )

    long_table = _prepare(stories=True)
    long_table.add_heading("14.3 长表跨页", level=1)
    table = long_table.add_table(rows=1, cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    for index, value in enumerate(("类别", "合成项目", "状态", "说明")):
        _table_text(table.rows[0].cells[index], value, bold=True)
    _repeat_header(table.rows[0])
    for row_index in range(105):
        row = table.add_row()
        values = (
            f"类别{row_index // 7 + 1}",
            f"项目{row_index + 1}",
            "完整",
            "合法跨页表行",
        )
        for cell, value in zip(row.cells, values):
            _table_text(cell, value)
    documents.append(
        {
            "scenario": "long_table_repeated_header",
            **_save(long_table, output_dir / "05_long_table_repeated_header.docx"),
        }
    )

    appendix = _prepare(stories=True)
    appendix.add_heading("16.1 附录页", level=1)
    for index in range(12):
        _paragraph(
            appendix,
            f"附录合成段落{index + 1}。本页不含临床事实或可识别信息。",
        )
    documents.append(
        {"scenario": "appendix_page", **_save(appendix, output_dir / "06_appendix_page.docx")}
    )

    generator = Path(__file__)
    manifest = {
        "schema_version": "1.0",
        "status": "post_freeze_holdout",
        "construction_rule": "created without importing or reading visual QC configuration",
        "source_kind": "synthetic_deidentified",
        "expected_class_a_findings": 0,
        "documents": documents,
        "generator": {"filename": generator.name, "sha256": _sha256(generator)},
    }
    (output_dir / "holdout_set_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_holdout_set(args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
