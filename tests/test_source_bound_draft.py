import copy
import tempfile
import unittest
from pathlib import Path

from docx import Document
from csr_writer.errors import ConfigurationError
from csr_writer.m4_assembly import CleanOutputBlocked, validate_clean_docx
from csr_writer.project_evidence import _sheet_table, read_workbook
from csr_writer.rtf_controls import body_controls
from csr_writer.shell_mapping import _column_id
from csr_writer.writing_style import style_report


class SourceBoundDraftTests(unittest.TestCase):
    def test_table_binding_rejects_column_population_and_row_drift(self):
        columns = ["终点", "治疗A", "治疗B", "对照"]
        job = {"sheet":"结果", "title":"冻表", "population":"FAS", "columns":columns,
               "highlights":[{"row":4, "label":"应答"}]}
        rows = [["冻表"], ["研究｜定稿｜FAS"], columns, ["应答","8","13","7"]]
        info = {"sheets":{"结果":{"rows":rows}}}
        self.assertEqual(["应答，治疗A为8，治疗B为13，对照为7。"], _sheet_table(info,job)[-1])
        for overrides in ({"columns":list(reversed(columns))}, {"population":"SAF"},
                          {"highlights":[{"row":4,"label":"死亡"}]}):
            with self.subTest(overrides=overrides), self.assertRaises(ConfigurationError):
                _sheet_table(info, {**job, **overrides})
        changed = copy.deepcopy(info)
        changed["sheets"]["结果"]["rows"][3][2] = None
        with self.assertRaises(ConfigurationError):
            _sheet_table(changed,job)

    def test_identifiable_workbooks_only_export_dimensions(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl optional")
        with tempfile.TemporaryDirectory() as root:
            f = Path(root)/"分配.xlsx"
            book = Workbook()
            book.active.append(["受试者", "治疗组"])
            book.active.append(["S017","组B"])
            book.save(f)
            for role in ("listing","dataset","randomization"):
                sheet = read_workbook(f,role)[0]["sheets"]["Sheet"]
                self.assertEqual({"max_row":2,"max_column":2},sheet)

    def test_rtf_fallback_with_nested_destinations_and_column_identity(self):
        value = body_controls(r"{\rtf1\uc2\u20013\'a4\'a4{\header 隐藏{\b 标题}}{\uc0\u25991}\u-10179??\u-8704??}")
        self.assertIn("中",value)
        self.assertIn("文",value)
        self.assertIn("😀",value)
        self.assertNotIn("隐藏",value)
        for group in ("0.5% BID","10 mg QD","200 mg组","试验组","Cohort 1"):
            self.assertEqual(_column_id(group+" (N=XX)"), _column_id(group+" (N=30)"))
        self.assertNotEqual(_column_id("试验组"),_column_id("对照组"))

    def test_protected_long_sentence_and_clean_review_markers(self):
        value = "必须从已确认资料逐字保留的原文"*6
        self.assertTrue(style_report(value+"。")["blocked"])
        self.assertFalse(style_report(value+"。",protected_verbatim=(value,))["blocked"])
        self.assertTrue(style_report("低于定量下限等同于零。")["blocked"])
        for marker in ("【待补】","〔待确认：来源〕","〔待医学负责人复核〕"):
            with tempfile.TemporaryDirectory() as root:
                p=Path(root)/"稿.docx"
                d=Document()
                d.add_paragraph(marker)
                d.save(p)
                with self.assertRaises(CleanOutputBlocked):
                    validate_clean_docx(p)

    def test_searchable_cjk_must_have_distinct_painted_glyphs(self):
        from csr_writer.visual_qc import _missing_cjk_glyph_fonts
        from unittest.mock import Mock
        page = Mock()
        chars = "临床试验初稿"
        page.get_text.return_value = {"blocks":[{"lines":[{"spans":[{"font":"Test","text":chars}]}]}]}
        page.get_texttrace.return_value = [{"font":"Test","chars":[(ord(c),1) for c in chars]}]
        self.assertEqual(["Test"], _missing_cjk_glyph_fonts(page))
        page.get_texttrace.return_value = [{"font":"Test","chars":[(ord(c),i+1) for i,c in enumerate(chars)]}]
        self.assertEqual([], _missing_cjk_glyph_fonts(page))

    def test_repeated_rtf_header_preserves_data_and_rejects_drift(self):
        from csr_writer.errors import RTFParseError
        from csr_writer.rtf_parser import parse_rtf_table
        descriptor = r"\trowd\cellx3000\cellx5500"
        header = descriptor.replace(r"\trowd",r"\trowd\trhdr") + r"\pard\intbl\b A\cell\pard\intbl\b N\cell\row"
        row = descriptor + r"\pard\intbl R\cell\pard\intbl 5\cell\row"
        repeated = header.replace(r"\pard", r" \pard",1)
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)/"pages.rtf"
            text = r"{\rtf1\ansi\ansicpg1252 " + header + row + r"\page\par " + repeated + row + "}"
            p.write_bytes(text.encode("ascii"))
            self.assertEqual(["A","R","R"],[r.cells[0].strip() for r in parse_rtf_table(p).rows])
            p.write_bytes(text.replace(repeated,repeated.replace("b A","b Z")).encode("ascii"))
            with self.assertRaises(RTFParseError):
                parse_rtf_table(p)
