from __future__ import annotations

import unittest
import zipfile
from pathlib import Path

from csr_writer.docx_tfl_parser import DocxTFLParseError, parse_docx_tfl_tables
from csr_writer.errors import RTFParseError
from csr_writer.io_utils import sha256_file
from csr_writer.physical_table import PhysicalTableError
from csr_writer.rtf_parser import parse_rtf_table


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PLUGIN_ROOT / "tests" / "fixtures" / "tfl-merged"
RTF_FIXTURE = FIXTURE_ROOT / "spanner-soc-pt.rtf"
DOCX_FIXTURE = FIXTURE_ROOT / "spanner-soc-pt.docx"
AMBIGUOUS_RTF = FIXTURE_ROOT / "ambiguous-hmerge.rtf"
AMBIGUOUS_DOCX = FIXTURE_ROOT / "ambiguous-vmerge.docx"
HEADER_MAPPING = {
    "指标": "ROW_LABEL",
    "例数 / 受试制剂T": "N_T",
    "例数 / 参比制剂R": "N_R",
}


class DualTFLPhysicalParserTests(unittest.TestCase):
    def test_rtf_and_docx_normalize_to_the_same_physical_model(self):
        before = {path: sha256_file(path) for path in (RTF_FIXTURE, DOCX_FIXTURE)}
        rtf = parse_rtf_table(RTF_FIXTURE)
        docx_document = parse_docx_tfl_tables(DOCX_FIXTURE)
        after = {path: sha256_file(path) for path in (RTF_FIXTURE, DOCX_FIXTURE)}

        self.assertEqual(before, after)
        self.assertEqual(1, len(docx_document.tables))
        self.assertEqual(rtf.source_sha256, before[RTF_FIXTURE])
        self.assertEqual(docx_document.source_sha256, before[DOCX_FIXTURE])
        self.assertEqual(
            rtf.physical_table.as_dict(),
            docx_document.tables[0].physical_table.as_dict(),
        )

    def test_spanner_flattening_requires_exact_reviewed_mapping(self):
        rtf = parse_rtf_table(RTF_FIXTURE)
        self.assertEqual(
            ("指标", "例数 / 受试制剂T", "例数 / 参比制剂R"),
            rtf.physical_table.flatten_header_paths(),
        )
        self.assertEqual(
            ("ROW_LABEL", "N_T", "N_R"),
            rtf.physical_table.reviewed_header_ids(HEADER_MAPPING),
        )
        incomplete = dict(HEADER_MAPPING)
        del incomplete["例数 / 参比制剂R"]
        with self.assertRaisesRegex(PhysicalTableError, "mapping drift"):
            rtf.physical_table.reviewed_header_ids(incomplete)

    def test_merge_provenance_and_soc_pt_indent_are_retained(self):
        table = parse_rtf_table(RTF_FIXTURE).physical_table
        self.assertEqual(2, table.header_row_count)
        self.assertEqual(2, table.rows[0].grid[0].row_span)
        self.assertEqual(2, table.rows[0].grid[1].col_span)
        self.assertEqual((0, 1), table.rows[0].grid[2].merged_from)
        self.assertEqual((0, 0), table.rows[1].grid[0].merged_from)

        body = table.rows[table.header_row_count :]
        pt_rows = [row for row in body if row.cells[0].strip() == "恶心"]
        self.assertEqual(2, len(pt_rows), "same PT under two SOC parents must remain legal")
        self.assertTrue(all(row.grid[0].indent == 2 for row in pt_rows))
        self.assertEqual(0, body[0].grid[0].indent)
        self.assertEqual(0, body[2].grid[0].indent)

    def test_chinese_is_lossless_on_both_paths(self):
        rtf_payload = parse_rtf_table(RTF_FIXTURE).as_dict()
        docx_payload = parse_docx_tfl_tables(DOCX_FIXTURE).as_dict()
        for payload in (rtf_payload, docx_payload):
            rendered = str(payload)
            self.assertIn("胃肠系统", rendered)
            self.assertIn("神经系统", rendered)
            self.assertIn("恶心", rendered)
            self.assertNotIn("\ufffd", rendered)

    def test_fixtures_exercise_native_merge_controls(self):
        rtf = RTF_FIXTURE.read_text(encoding="latin-1")
        self.assertIn(r"\clmgf", rtf)
        self.assertIn(r"\clmrg", rtf)
        self.assertIn(r"\clvmgf", rtf)
        self.assertIn(r"\clvmrg", rtf)
        with zipfile.ZipFile(DOCX_FIXTURE) as archive:
            document_xml = archive.read("word/document.xml")
        self.assertIn(b"gridSpan", document_xml)
        self.assertIn(b"vMerge", document_xml)

    def test_irregular_or_ambiguous_merges_fail_closed(self):
        before = {path: sha256_file(path) for path in (AMBIGUOUS_RTF, AMBIGUOUS_DOCX)}
        with self.assertRaisesRegex(RTFParseError, "orphan"):
            parse_rtf_table(AMBIGUOUS_RTF)
        with self.assertRaisesRegex(DocxTFLParseError, "orphan"):
            parse_docx_tfl_tables(AMBIGUOUS_DOCX)
        after = {path: sha256_file(path) for path in (AMBIGUOUS_RTF, AMBIGUOUS_DOCX)}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
