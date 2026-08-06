from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from csr_writer.docx_extract import (
    DocxExtractionError,
    extract_docx,
    extract_docx_to_file,
)
from csr_writer.io_utils import sha256_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "m3-docx" / "deep-extraction-fixture.docx"
FIXED_REPORT = PROJECT_ROOT / "examples" / "output" / "m3_docx_extraction.json"


class M3DocxExtractionTests(unittest.TestCase):
    @staticmethod
    def _rewrite_docx(source: Path, destination: Path, mutate) -> None:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(
            destination, "w", zipfile.ZIP_DEFLATED
        ) as rewritten:
            for item in original.infolist():
                payload = original.read(item.filename)
                rewritten.writestr(item, mutate(item.filename, payload))

    def test_fixture_contains_every_required_ooxml_risk_and_story(self):
        self.assertTrue(FIXTURE.is_file())
        with zipfile.ZipFile(FIXTURE) as archive:
            names = set(archive.namelist())
            self.assertIn("word/comments.xml", names)
            self.assertIn("word/header1.xml", names)
            self.assertIn("word/footer1.xml", names)
            document_xml = archive.read("word/document.xml").decode("utf-8")
            comments_xml = archive.read("word/comments.xml").decode("utf-8")
        for marker in (
            "<w:ins ",
            "<w:del ",
            "<w:commentRangeStart ",
            "<w:commentRangeEnd ",
            "<w:commentReference ",
            "<w:vanish",
            "<w:txbxContent>",
            "<w:tbl>",
            "<w:tc>",
        ):
            self.assertIn(marker, document_xml)
        self.assertIn("批注秘密777 mg不要进事实库", comments_xml)
        self.assertIn("文本框批注秘密444 mg不要进事实库", comments_xml)

    def test_accepted_view_preserves_physical_order_and_excludes_risk_text(self):
        before = sha256_file(FIXTURE)
        report = extract_docx(FIXTURE)
        after = sha256_file(FIXTURE)
        self.assertEqual(before, after)
        self.assertEqual(before, report["source"]["sha256_before"])
        self.assertEqual(after, report["source"]["sha256_after"])
        self.assertTrue(report["source"]["unchanged"])

        self.assertEqual(
            [
                "正文开始，插入文本，正文继续。显式可见。",
                "文本框可见。",
                "单元格A",
                "单元格B",
                "正文结束。",
                "页眉可见",
                "页脚可见",
            ],
            [record["text"] for record in report["records"]],
        )
        self.assertEqual(
            ["document", "header", "footer"],
            [story["story"] for story in report["story_order"]],
        )
        self.assertEqual(
            [
                "paragraph",
                "text_box_paragraph",
                "table_cell_paragraph",
                "table_cell_paragraph",
                "paragraph",
                "paragraph",
                "paragraph",
            ],
            [record["kind"] for record in report["records"]],
        )
        self.assertEqual(["0"], report["records"][0]["review_comment_ids"])
        self.assertTrue(report["records"][0]["has_review_comment"])
        self.assertEqual(["1"], report["records"][1]["review_comment_ids"])
        self.assertTrue(report["records"][1]["has_review_comment"])

        serialized = json.dumps(report, ensure_ascii=False)
        for excluded in (
            "删除旧文本100 mg",
            "隐藏正文999 mg",
            "文本框隐藏888 mg",
            "页眉隐藏666 mg",
            "批注秘密777 mg不要进事实库",
            "文本框批注秘密444 mg不要进事实库",
            "删除整行555 mg",
            "删除整行另一个单元格",
        ):
            self.assertNotIn(excluded, serialized)

    def test_findings_and_gap_report_are_explicit_and_non_textual(self):
        report = extract_docx(FIXTURE)
        statistics = report["statistics"]
        self.assertEqual(1, statistics["insertions"])
        self.assertEqual(2, statistics["deletions"])
        self.assertEqual(6, statistics["comment_markers"])
        self.assertEqual(3, statistics["hidden_runs"])
        self.assertEqual(1, statistics["tables"])
        self.assertEqual(2, statistics["table_cells"])
        self.assertEqual(1, statistics["text_boxes"])
        self.assertEqual(["0", "1"], report["findings"]["comments"]["package_comment_ids"])
        self.assertEqual(["0", "1"], report["findings"]["comments"]["marker_comment_ids"])
        self.assertEqual(2, report["tables"][0]["physical_rows"])
        self.assertEqual(1, report["tables"][0]["rows"])
        self.assertEqual(1, len(report["tables"][0]["excluded_deleted_rows"]))
        self.assertEqual(
            [
                "SOURCE_HAS_UNACCEPTED_REVISIONS",
                "SOURCE_HAS_REVIEW_COMMENTS",
                "SOURCE_HAS_HIDDEN_TEXT",
            ],
            [item["code"] for item in report["gap_report"]],
        )
        for collection in (
            report["findings"]["revisions"]["insertions"],
            report["findings"]["revisions"]["deletions"],
            report["findings"]["hidden_text"]["runs"],
        ):
            self.assertTrue(collection)
            self.assertTrue(all("content_sha256" in item for item in collection))
            self.assertTrue(all("text" not in item for item in collection))

    def test_fixed_extraction_report_is_current(self):
        expected = json.loads(FIXED_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(expected, extract_docx(FIXTURE))

    def test_report_output_cannot_alias_read_only_source(self):
        before = sha256_file(FIXTURE)
        with self.assertRaisesRegex(DocxExtractionError, "must not alias"):
            extract_docx_to_file(FIXTURE, FIXTURE)
        self.assertEqual(before, sha256_file(FIXTURE))

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            report = extract_docx_to_file(FIXTURE, output)
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
            self.assertEqual(before, sha256_file(FIXTURE))

    def test_orphan_comment_payload_still_triggers_gap_without_reading_body(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        marker_tags = {
            f"{{{w_namespace}}}commentRangeStart",
            f"{{{w_namespace}}}commentRangeEnd",
            f"{{{w_namespace}}}commentReference",
        }
        with tempfile.TemporaryDirectory() as temporary:
            orphaned = Path(temporary) / "orphan-comments.docx"
            with zipfile.ZipFile(FIXTURE) as source, zipfile.ZipFile(
                orphaned, "w", zipfile.ZIP_DEFLATED
            ) as destination:
                for item in source.infolist():
                    payload = source.read(item.filename)
                    if item.filename == "word/document.xml":
                        root = etree.fromstring(payload)
                        for element in list(root.iter()):
                            if element.tag in marker_tags and element.getparent() is not None:
                                element.getparent().remove(element)
                        payload = etree.tostring(
                            root,
                            xml_declaration=True,
                            encoding="UTF-8",
                            standalone=True,
                        )
                    destination.writestr(item, payload)

            report = extract_docx(orphaned)
            self.assertEqual(0, report["statistics"]["comment_markers"])
            self.assertEqual(
                ["0", "1"], report["findings"]["comments"]["package_comment_ids"]
            )
            comment_gap = next(
                item
                for item in report["gap_report"]
                if item["code"] == "SOURCE_HAS_REVIEW_COMMENTS"
            )
            self.assertEqual(2, comment_gap["count"])
            self.assertTrue(
                all(location.startswith("word/comments.xml:") for location in comment_gap["locations"])
            )
            self.assertNotIn(
                "批注秘密",
                json.dumps(report, ensure_ascii=False),
            )

    def test_schema_valid_block_sdt_is_traversed_in_accepted_view(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        def mutate(name: str, payload: bytes) -> bytes:
            if name != "word/document.xml":
                return payload
            root = etree.fromstring(payload)
            body = root.find(f"{{{w_namespace}}}body")
            assert body is not None
            paragraphs = [child for child in body if child.tag == f"{{{w_namespace}}}p"]
            first = paragraphs[0]

            first_index = body.index(first)
            body.remove(first)
            sdt = etree.Element(f"{{{w_namespace}}}sdt")
            sdt.append(etree.Element(f"{{{w_namespace}}}sdtPr"))
            content = etree.SubElement(sdt, f"{{{w_namespace}}}sdtContent")
            content.append(first)
            body.insert(first_index, sdt)

            return etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        with tempfile.TemporaryDirectory() as temporary:
            wrapped = Path(temporary) / "wrapped-blocks.docx"
            self._rewrite_docx(FIXTURE, wrapped, mutate)
            report = extract_docx(wrapped)
            self.assertIn("正文开始，插入文本，正文继续。显式可见。", report["accepted_text"])
            self.assertIn("正文结束。", report["accepted_text"])
            self.assertEqual(1, report["statistics"]["insertions"])

    def test_altchunk_fails_closed_instead_of_silently_omitting_embedded_content(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        r_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

        def mutate(name: str, payload: bytes) -> bytes:
            if name != "word/document.xml":
                return payload
            root = etree.fromstring(payload)
            body = root.find(f"{{{w_namespace}}}body")
            assert body is not None
            alt_chunk = etree.Element(f"{{{w_namespace}}}altChunk")
            alt_chunk.set(f"{{{r_namespace}}}id", "rIdUnsupportedAltChunk")
            body.insert(0, alt_chunk)
            return etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        with tempfile.TemporaryDirectory() as temporary:
            embedded = Path(temporary) / "embedded-altchunk.docx"
            self._rewrite_docx(FIXTURE, embedded, mutate)
            with self.assertRaisesRegex(DocxExtractionError, "altChunk embedded content"):
                extract_docx(embedded)

    def test_style_vanish_uses_toggle_semantics_base_to_derived(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        w = lambda local: f"{{{w_namespace}}}{local}"

        def style(style_id: str, *, based_on: str | None, vanish: str) -> etree._Element:
            element = etree.Element(w("style"))
            element.set(w("type"), "character")
            element.set(w("styleId"), style_id)
            name = etree.SubElement(element, w("name"))
            name.set(w("val"), style_id)
            if based_on is not None:
                base = etree.SubElement(element, w("basedOn"))
                base.set(w("val"), based_on)
            run_properties = etree.SubElement(element, w("rPr"))
            hidden = etree.SubElement(run_properties, w("vanish"))
            hidden.set(w("val"), vanish)
            return element

        def styled_run(style_id: str, text: str) -> etree._Element:
            run = etree.Element(w("r"))
            run_properties = etree.SubElement(run, w("rPr"))
            run_style = etree.SubElement(run_properties, w("rStyle"))
            run_style.set(w("val"), style_id)
            text_node = etree.SubElement(run, w("t"))
            text_node.text = text
            return run

        def mutate(name: str, payload: bytes) -> bytes:
            if name == "word/styles.xml":
                root = etree.fromstring(payload)
                root.append(style("M3BaseHidden", based_on=None, vanish="1"))
                root.append(
                    style("M3DerivedFalse", based_on="M3BaseHidden", vanish="0")
                )
                root.append(
                    style("M3DerivedToggle", based_on="M3BaseHidden", vanish="1")
                )
                return etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
            if name == "word/document.xml":
                root = etree.fromstring(payload)
                body = root.find(w("body"))
                assert body is not None
                paragraph = [child for child in body if child.tag == w("p")][-1]
                paragraph.append(
                    styled_run("M3DerivedFalse", "样式隐藏泄漏555 mg")
                )
                paragraph.append(
                    styled_run("M3DerivedToggle", "样式二次切换可见。")
                )
                return etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
            return payload

        with tempfile.TemporaryDirectory() as temporary:
            styled = Path(temporary) / "style-toggle.docx"
            self._rewrite_docx(FIXTURE, styled, mutate)
            report = extract_docx(styled)
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("样式隐藏泄漏555 mg", serialized)
            self.assertIn("样式二次切换可见。", report["accepted_text"])
            self.assertEqual(4, report["statistics"]["hidden_runs"])

    def test_unknown_mc_choice_uses_display_fallback(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        mc_namespace = "http://schemas.openxmlformats.org/markup-compatibility/2006"
        w = lambda local: f"{{{w_namespace}}}{local}"
        mc = lambda local: f"{{{mc_namespace}}}{local}"

        def paragraph(text: str) -> etree._Element:
            node = etree.Element(w("p"))
            run = etree.SubElement(node, w("r"))
            text_node = etree.SubElement(run, w("t"))
            text_node.text = text
            return node

        def mutate(name: str, payload: bytes) -> bytes:
            if name != "word/document.xml":
                return payload
            root = etree.fromstring(payload)
            body = root.find(w("body"))
            assert body is not None
            final_paragraph = [child for child in body if child.tag == w("p")][-1]
            insertion_index = body.index(final_paragraph)
            body.remove(final_paragraph)
            alternate = etree.Element(
                mc("AlternateContent"),
                nsmap={"mc": mc_namespace, "unsupported": "urn:m3:unsupported"},
            )
            choice = etree.SubElement(alternate, mc("Choice"))
            choice.set("Requires", "unsupported")
            choice.append(paragraph("错误 Choice 秘密444 mg"))
            fallback = etree.SubElement(alternate, mc("Fallback"))
            fallback.append(final_paragraph)
            body.insert(insertion_index, alternate)
            return etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        with tempfile.TemporaryDirectory() as temporary:
            alternate = Path(temporary) / "alternate-content.docx"
            self._rewrite_docx(FIXTURE, alternate, mutate)
            report = extract_docx(alternate)
            self.assertIn("正文结束。", report["accepted_text"])
            self.assertNotIn("错误 Choice 秘密444 mg", json.dumps(report, ensure_ascii=False))

    def test_inactive_first_and_even_headers_are_not_fact_text(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        w = lambda local: f"{{{w_namespace}}}{local}"

        def mutation_for(variant: str):
            def mutate(name: str, payload: bytes) -> bytes:
                if name != "word/document.xml":
                    return payload
                root = etree.fromstring(payload)
                reference = root.find(f".//{w('headerReference')}")
                assert reference is not None
                reference.set(w("type"), variant)
                return etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )

            return mutate

        for variant in ("first", "even"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                inactive = Path(temporary) / f"inactive-{variant}.docx"
                self._rewrite_docx(FIXTURE, inactive, mutation_for(variant))
                report = extract_docx(inactive)
                self.assertNotIn("页眉可见", report["accepted_text"])
                self.assertNotIn("word/header1.xml", report["parts_read"])
                self.assertEqual(
                    ["document", "footer"],
                    [story["story"] for story in report["story_order"]],
                )

    def test_cross_paragraph_comment_range_marks_every_covered_record(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        w = lambda local: f"{{{w_namespace}}}{local}"

        def paragraph(text: str, marker: str | None) -> etree._Element:
            node = etree.Element(w("p"))
            if marker == "start":
                start = etree.SubElement(node, w("commentRangeStart"))
                start.set(w("id"), "2")
            run = etree.SubElement(node, w("r"))
            text_node = etree.SubElement(run, w("t"))
            text_node.text = text
            if marker == "end":
                end = etree.SubElement(node, w("commentRangeEnd"))
                end.set(w("id"), "2")
            return node

        def mutate(name: str, payload: bytes) -> bytes:
            if name != "word/document.xml":
                return payload
            root = etree.fromstring(payload)
            body = root.find(w("body"))
            assert body is not None
            final_paragraph = [child for child in body if child.tag == w("p")][-1]
            insertion_index = body.index(final_paragraph)
            for offset, (text, marker) in enumerate(
                (("跨段批注起点", "start"), ("跨段批注中间", None), ("跨段批注终点", "end"))
            ):
                body.insert(insertion_index + offset, paragraph(text, marker))
            return etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        with tempfile.TemporaryDirectory() as temporary:
            ranged = Path(temporary) / "cross-paragraph-comment.docx"
            self._rewrite_docx(FIXTURE, ranged, mutate)
            report = extract_docx(ranged)
            covered = [
                record
                for record in report["records"]
                if record["text"].startswith("跨段批注")
            ]
            self.assertEqual(3, len(covered))
            self.assertTrue(all(record["review_comment_ids"] == ["2"] for record in covered))
            self.assertTrue(all(record["has_review_comment"] for record in covered))

    def test_text_box_records_exact_mid_paragraph_anchor_offset(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        w = lambda local: f"{{{w_namespace}}}{local}"

        def mutate(name: str, payload: bytes) -> bytes:
            if name != "word/document.xml":
                return payload
            root = etree.fromstring(payload)
            body = root.find(w("body"))
            assert body is not None
            paragraph = [child for child in body if child.tag == w("p")][0]
            run = etree.Element(w("r"))
            text_node = etree.SubElement(run, w("t"))
            text_node.text = "锚点后正文"
            paragraph.append(run)
            return etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

        with tempfile.TemporaryDirectory() as temporary:
            anchored = Path(temporary) / "mid-paragraph-textbox.docx"
            self._rewrite_docx(FIXTURE, anchored, mutate)
            report = extract_docx(anchored)
            host = report["records"][0]["text"]
            self.assertTrue(host.endswith("锚点后正文"))
            self.assertEqual(
                len(host) - len("锚点后正文"),
                report["text_boxes"][0]["anchor_character_offset"],
            )


if __name__ == "__main__":
    unittest.main()
