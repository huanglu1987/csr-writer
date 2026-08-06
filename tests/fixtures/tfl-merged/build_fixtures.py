"""Build deidentified, structurally equivalent merged TFL fixtures."""

from __future__ import annotations

import zipfile
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree


ROOT = Path(__file__).resolve().parent
DOCX_PATH = ROOT / "spanner-soc-pt.docx"
AMBIGUOUS_DOCX_PATH = ROOT / "ambiguous-vmerge.docx"
RTF_PATH = ROOT / "spanner-soc-pt.rtf"
AMBIGUOUS_RTF_PATH = ROOT / "ambiguous-hmerge.rtf"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _set_repeat_header(row) -> None:
    row_properties = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "1")
    row_properties.append(header)


def _set_cell(cell, text: str, *, bold: bool) -> None:
    cell.text = text
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            run.bold = bold


def build_docx() -> None:
    document = Document()
    table = document.add_table(rows=6, cols=3)
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 1).merge(table.cell(0, 2))

    _set_cell(table.cell(0, 0), "指标", bold=True)
    _set_cell(table.cell(0, 1), "例数", bold=True)
    _set_cell(table.cell(1, 1), "受试制剂T", bold=True)
    _set_cell(table.cell(1, 2), "参比制剂R", bold=True)
    _set_repeat_header(table.rows[0])
    _set_repeat_header(table.rows[1])

    body = (
        ("系统器官分类：胃肠系统", "xx", "xx"),
        ("  恶心", "xx (xx.x)", "xx (xx.x)"),
        ("系统器官分类：神经系统", "xx", "xx"),
        ("  恶心", "xx (xx.x)", "xx (xx.x)"),
    )
    for row_index, values in enumerate(body, start=2):
        for column_index, value in enumerate(values):
            _set_cell(table.cell(row_index, column_index), value, bold=False)
    document.save(DOCX_PATH)
    # python-docx emits a legacy w:zoom form that the strict OOXML validator
    # rejects.  It is display-only, so remove it from this parser fixture.
    with zipfile.ZipFile(DOCX_PATH) as source:
        parts = {item.filename: source.read(item.filename) for item in source.infolist()}
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    settings = etree.fromstring(parts["word/settings.xml"], parser=parser)
    for zoom in settings.findall(f"{{{W_NS}}}zoom"):
        settings.remove(zoom)
    parts["word/settings.xml"] = etree.tostring(
        settings, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    temporary = DOCX_PATH.with_name("spanner-soc-pt.tmp.docx")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
        for name, payload in parts.items():
            target.writestr(name, payload)
    temporary.replace(DOCX_PATH)


def _rtf_text(value: str) -> str:
    output: list[str] = []
    for character in value:
        if ord(character) < 128:
            output.append(character)
            continue
        for byte in character.encode("gbk"):
            output.append(f"\\'{byte:02x}")
    return "".join(output)


def _rtf_row(descriptor: str, values: tuple[tuple[str, bool], ...]) -> str:
    cells = []
    for value, bold in values:
        bold_control = r"\b " if bold else ""
        cells.append(rf"\pard\intbl\ql {bold_control}{_rtf_text(value)}\cell")
    return rf"\trowd{descriptor}" + "".join(cells) + r"\row" + "\n"


def build_rtf() -> None:
    rows = [
        _rtf_row(
            r"\clvmgf\cellx3000\clmgf\cellx6000\clmrg\cellx9000",
            (("指标", True), ("例数", True), ("", True)),
        ),
        _rtf_row(
            r"\clvmrg\cellx3000\cellx6000\cellx9000",
            (("", True), ("受试制剂T", True), ("参比制剂R", True)),
        ),
        _rtf_row(
            r"\cellx3000\cellx6000\cellx9000",
            (("系统器官分类：胃肠系统", False), ("xx", False), ("xx", False)),
        ),
        _rtf_row(
            r"\cellx3000\cellx6000\cellx9000",
            (("  恶心", False), ("xx (xx.x)", False), ("xx (xx.x)", False)),
        ),
        _rtf_row(
            r"\cellx3000\cellx6000\cellx9000",
            (("系统器官分类：神经系统", False), ("xx", False), ("xx", False)),
        ),
        _rtf_row(
            r"\cellx3000\cellx6000\cellx9000",
            (("  恶心", False), ("xx (xx.x)", False), ("xx (xx.x)", False)),
        ),
    ]
    content = (
        r"{\rtf1\ansi\ansicpg936\deff0"
        + "\n"
        + r"\pard\qc\b Table 14.3.1.3.1\par"
        + "\n"
        + rf"\pard\qc\b {_rtf_text('脱敏后 SOC/PT 合并表头演示')}\par"
        + "\n"
        + "".join(rows)
        + rf"\pard\ql\fs16 {_rtf_text('注：该文件仅保留结构特征。')}\par"
        + "\n"
        + r"\pard\ql\fs16 Program: synthetic_ae.sas   Output: SHELL\par"
        + "\n}"
    )
    RTF_PATH.write_bytes(content.encode("latin-1"))


def build_ambiguous_docx() -> None:
    with zipfile.ZipFile(DOCX_PATH) as source:
        parts = {item.filename: source.read(item.filename) for item in source.infolist()}
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(parts["word/document.xml"], parser=parser)
    vertical = root.find(f".//{{{W_NS}}}vMerge")
    if vertical is None:
        raise RuntimeError("Expected a vertical merge in the synthetic DOCX")
    vertical.set(f"{{{W_NS}}}val", "continue")
    parts["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    with zipfile.ZipFile(AMBIGUOUS_DOCX_PATH, "w", zipfile.ZIP_DEFLATED) as target:
        for name, payload in parts.items():
            target.writestr(name, payload)


def build_ambiguous_rtf() -> None:
    content = (
        r"{\rtf1\ansi\ansicpg936"
        + "\n"
        + _rtf_row(
            r"\clmrg\cellx3000\cellx6000",
            (("指标", True), ("例数", True)),
        )
        + _rtf_row(
            r"\cellx3000\cellx6000",
            (("脱敏行", False), ("xx", False)),
        )
        + "}"
    )
    AMBIGUOUS_RTF_PATH.write_bytes(content.encode("latin-1"))


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    build_docx()
    build_rtf()
    build_ambiguous_docx()
    build_ambiguous_rtf()
