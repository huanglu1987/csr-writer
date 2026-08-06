#!/usr/bin/env python3
"""Build reproducible, deidentified M20 unfamiliar DOCX inputs.

The documents are synthetic and deliberately differ from the topical parallel-group
fixtures used in earlier milestones.  No clinical source file is read.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_BREAK
from docx.shared import Inches


ROOT = Path(__file__).resolve().parent
PROTOCOL = ROOT / "protocol.single_arm_migraine.deidentified.docx"
SHELL = ROOT / "tfl_shell.single_arm_migraine.deidentified.docx"
RESULT = ROOT / "result_tfl.single_arm_migraine.deidentified.docx"

NOTICE = "合成数据｜非临床证据｜不得用于受试者或申报判断"


def _set_cell(cell, text: str, *, indent: bool = False) -> None:
    paragraph = cell.paragraphs[0]
    paragraph.text = text
    if indent:
        paragraph.paragraph_format.left_indent = Inches(0.28)


def _add_stable_table(
    document: Document,
    title: str,
    values: tuple[str, str, str, str, str] | None,
) -> None:
    document.add_paragraph(title)
    table = document.add_table(rows=6, cols=2)
    table.style = "Table Grid"
    _set_cell(table.cell(0, 0), "统计量")
    _set_cell(table.cell(0, 1), "Active Total")
    labels = ("N", "Mean (SD)", "Median", "Min, Max", "P值")
    for index, label in enumerate(labels, start=1):
        _set_cell(table.cell(index, 0), label)
        _set_cell(table.cell(index, 1), values[index - 1] if values else "xx")
    document.add_paragraph(NOTICE)


def _add_event_table(document: Document, title: str, *, result: bool) -> None:
    document.add_paragraph(title)
    table = document.add_table(rows=6, cols=2)
    table.style = "Table Grid"
    _set_cell(table.cell(0, 0), "事件分类")
    _set_cell(table.cell(0, 1), "Active Total / n (%)")
    if result:
        rows = (
            ("重要医学事件", "", False),
            ("死亡", "0 (0.0)", True),
            ("严重不良事件", "2 (4.2)", True),
            ("其他重要不良事件", "3 (6.3)", True),
            ("导致永久停药", "1 (2.1)", True),
        )
    else:
        rows = (
            ("事件类别1", "", False),
            ("首选术语1", "xx", True),
            ("首选术语2", "xx", True),
            ("首选术语3", "xx", True),
            ("首选术语4", "xx", True),
        )
    for index, (label, value, indent) in enumerate(rows, start=1):
        _set_cell(table.cell(index, 0), label, indent=indent)
        _set_cell(table.cell(index, 1), value)
    document.add_paragraph(NOTICE)


def build_protocol() -> None:
    document = Document()
    document.add_heading("脱敏合成临床试验方案", level=0)
    document.add_paragraph(NOTICE)
    document.add_heading("1 研究概述", level=1)
    document.add_paragraph(
        "本研究为Ⅱ期、单臂、开放性、多中心研究，评价口服研究药物 A 用于成人偏头痛预防治疗。"
    )
    document.add_heading("2 研究目标", level=1)
    document.add_paragraph(
        "主要目标为描述治疗第4周每月偏头痛日数较基线的变化。次要目标包括安全性和应答情况。"
    )
    document.add_heading("3 研究设计", level=1)
    document.add_paragraph(
        "全部受试者接受同一固定给药方案。本研究不进行随机分配，不设置对照组，也不实施盲法。"
    )
    document.add_heading("4 研究人群", level=1)
    document.add_paragraph(
        "研究对象为符合方案诊断标准的成人偏头痛患者。计划纳入人数记录于经确认的 study_facts。"
    )
    document.add_heading("5 治疗方案", level=1)
    document.add_paragraph(
        "研究药物采用固定口服给药方案。具体剂量、频次和疗程仅以经确认方案字段为准。"
    )
    document.add_heading("6 疗效评价", level=1)
    document.add_paragraph(
        "主要终点为治疗第4周每月偏头痛日数较基线的变化。结果按 ITT 描述，并以 PPS 进行支持性分析。"
    )
    document.add_heading("7 安全性评价", level=1)
    document.add_paragraph(
        "安全性评价包括不良事件、临床实验室检查、生命体征和心电图。安全性结果按 SAF 汇总。"
    )
    document.add_heading("8 统计分析", level=1)
    document.add_paragraph(
        "本研究采用探索性描述方法，不进行确证性假设检验。分析集名称为 SAF、ITT 和 PPS。"
    )
    document.add_page_break()
    document.add_paragraph("文档结束。" + NOTICE)
    document.save(PROTOCOL)


def _build_tfl(path: Path, *, result: bool) -> None:
    document = Document()
    document.add_heading("脱敏合成 TFL " + ("结果" if result else "壳"), level=0)
    document.add_paragraph(NOTICE)

    document.add_paragraph("表14.1.1.1 受试者处置宽表 (ITT)")
    wide = document.add_table(rows=3, cols=5)
    wide.style = "Table Grid"
    for column, value in enumerate(("序号", "脱敏记录号", "分析集", "状态", "访视完成情况")):
        _set_cell(wide.cell(0, column), value)
    for row in range(1, 3):
        values = (
            str(row) if result else "xx",
            f"SYN-{row:03d}" if result else "xx",
            "ITT" if result else "xx",
            "完成" if result else "xx",
            "完成主要终点评价" if result else "xx",
        )
        for column, value in enumerate(values):
            _set_cell(wide.cell(row, column), value)
    document.add_paragraph(NOTICE)

    stable = [
        ("表14.1.2.1 人口学资料 (ITT)", ("48", "41.2 (10.6)", "40.0", "20, 63", "0.317")),
        ("表14.1.3.1 基线疾病特征 (ITT)", ("48", "8.4 (3.1)", "8.0", "4, 18", "0.228")),
        ("表14.2.1 主要终点描述性分析 (ITT)", ("46", "-3.1 (2.4)", "-3.0", "-8, 1", "0.041")),
        ("表14.2.2 支持性终点描述性分析 (PPS)", ("42", "-3.3 (2.2)", "-3.0", "-8, 0", "0.038")),
        ("表14.3.1.1 治疗暴露程度 (SAF)", ("48", "27.4 (2.1)", "28.0", "21, 28", "0.752")),
        ("表14.3.3.1 重要事件叙述索引 (SAF)", ("48", "2.0 (1.0)", "2.0", "1, 3", "0.481")),
        ("表14.3.4.1 临床实验室检查变化 (SAF)", ("48", "0.4 (1.2)", "0.2", "-2, 4", "0.612")),
    ]
    for title, actual in stable:
        _add_stable_table(document, title, actual if result else None)

    _add_event_table(document, "表14.3.2.1 死亡和其他重要医学事件 (SAF)", result=result)

    if not result:
        _add_stable_table(
            document,
            "表14.2.9.1 壳中预设但结果未提供的敏感性分析 (ITT)",
            None,
        )
    else:
        _add_stable_table(
            document,
            "表14.3.9.1 结果新增但壳中未预设的生命体征分析 (SAF)",
            ("48", "0.8 (4.5)", "1.0", "-12, 14", "0.566"),
        )

    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    document.add_paragraph("文档结束。" + NOTICE)
    document.save(path)


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    build_protocol()
    _build_tfl(SHELL, result=False)
    _build_tfl(RESULT, result=True)
    print(PROTOCOL)
    print(SHELL)
    print(RESULT)


if __name__ == "__main__":
    main()
