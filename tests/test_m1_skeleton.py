from __future__ import annotations

import tempfile
import unittest
import zipfile
import shutil
from pathlib import Path

import yaml
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.shared import RGBColor
from lxml import etree

from csr_writer.errors import StructureError, TemplateError
from csr_writer.io_utils import sha256_file
from csr_writer.paths import DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE, DEFAULT_TEMPLATE
from csr_writer.qc_format import run_qc, run_qc_to_file
from csr_writer.render_docx import render_skeleton
from csr_writer.structure import (
    ICH_OFFICIAL,
    INDUSTRY_CONVENTION,
    SPONSOR_CUSTOM,
    flatten_sections,
    load_chapter_tree,
)
from csr_writer.template import create_default_template


class M1SkeletonTests(unittest.TestCase):
    @staticmethod
    def _rewrite_docx(source: Path, destination: Path, mutate) -> None:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(
            destination, "w", zipfile.ZIP_DEFLATED
        ) as rewritten:
            for item in original.infolist():
                payload = original.read(item.filename)
                replacement = mutate(item.filename, payload)
                rewritten.writestr(item, replacement)

    def test_verified_tree_layers_and_invariants(self):
        tree = load_chapter_tree(DEFAULT_STRUCTURE)
        official = flatten_sections(tree.select_layers([ICH_OFFICIAL]))
        all_nodes = flatten_sections(
            tree.select_layers([ICH_OFFICIAL, INDUSTRY_CONVENTION, SPONSOR_CUSTOM])
        )
        self.assertEqual(119, len(official))
        self.assertEqual(122, len(all_nodes))
        self.assertNotIn("16.1.13", {node.id for node in all_nodes})
        convention = {node.id: node.layer for node in all_nodes if node.id.startswith("14.1.")}
        self.assertEqual(
            {
                "14.1.1": INDUSTRY_CONVENTION,
                "14.1.2": INDUSTRY_CONVENTION,
                "14.1.3": INDUSTRY_CONVENTION,
            },
            convention,
        )
        self.assertFalse(any(node.id.startswith("14.2.") for node in all_nodes))

    def test_rendered_official_skeleton_passes_fixed_21_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "skeleton.docx"
            result = render_skeleton(
                DEFAULT_STRUCTURE,
                DEFAULT_TEMPLATE,
                DEFAULT_FORMAT_SPEC,
                output,
            )
            self.assertEqual(119, len(result.headings))
            report = run_qc(output, DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE)
            self.assertTrue(report.passed, report.format_text())
            self.assertEqual(21, len(report.items))
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertGreaterEqual(document_xml.count("<w:pStyle"), 119)
            self.assertEqual(0, document_xml.count("<w:rFonts"))

    def test_replacing_master_changes_styles_without_renderer_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            alternate_template = temporary_path / "alternate.docx"
            master = Document(DEFAULT_TEMPLATE)
            master.styles["Heading 1"].font.color.rgb = RGBColor(0, 0, 255)
            master.save(alternate_template)
            output = temporary_path / "alternate-output.docx"
            render_skeleton(
                DEFAULT_STRUCTURE,
                alternate_template,
                DEFAULT_FORMAT_SPEC,
                output,
            )
            with zipfile.ZipFile(output) as archive:
                styles_xml = archive.read("word/styles.xml").decode("utf-8")
                body_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn('w:val="0000FF"', styles_xml)
            self.assertEqual(0, body_xml.count("<w:rFonts"))

    def test_render_rejects_every_input_output_manifest_path_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_copy = root / "master.docx"
            shutil.copy2(DEFAULT_TEMPLATE, template_copy)
            before = sha256_file(template_copy)
            output = root / "skeleton.docx"

            with self.assertRaises(TemplateError):
                render_skeleton(
                    DEFAULT_STRUCTURE,
                    template_copy,
                    DEFAULT_FORMAT_SPEC,
                    output,
                    manifest_path=output,
                )
            self.assertFalse(output.exists())

            with self.assertRaises(TemplateError):
                render_skeleton(
                    DEFAULT_STRUCTURE,
                    template_copy,
                    DEFAULT_FORMAT_SPEC,
                    output,
                    manifest_path=template_copy,
                )
            self.assertEqual(before, sha256_file(template_copy))
            self.assertFalse(output.exists())

            case_output = root / "Result.docx"
            with self.assertRaises(TemplateError):
                render_skeleton(
                    DEFAULT_STRUCTURE,
                    template_copy,
                    DEFAULT_FORMAT_SPEC,
                    case_output,
                    manifest_path=root / "result.docx",
                )
            self.assertFalse(case_output.exists())

            with self.assertRaises(TemplateError):
                render_skeleton(
                    DEFAULT_STRUCTURE,
                    template_copy,
                    DEFAULT_FORMAT_SPEC,
                    root / "MASTER.docx",
                    manifest_path=None,
                )

            nfc_output = root / "Café.docx"
            nfd_manifest = root / "Café.docx"
            with self.assertRaises(TemplateError):
                render_skeleton(
                    DEFAULT_STRUCTURE,
                    template_copy,
                    DEFAULT_FORMAT_SPEC,
                    nfc_output,
                    manifest_path=nfd_manifest,
                )
            self.assertFalse(nfc_output.exists())

    def test_renderer_discards_landscape_first_even_and_stale_template_stories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_template = root / "legacy.docx"
            master = Document(DEFAULT_TEMPLATE)
            master.settings.odd_and_even_pages_header_footer = True
            section = master.sections[0]
            section.orientation = WD_ORIENT.LANDSCAPE
            section.page_width, section.page_height = section.page_height, section.page_width
            section.different_first_page_header_footer = True
            section.first_page_header.paragraphs[0].text = "{{stale_first_header}}"
            section.even_page_footer.paragraphs[0].text = "{{stale_even_footer}}"
            master.save(legacy_template)

            output = root / "normalized.docx"
            render_skeleton(
                DEFAULT_STRUCTURE,
                legacy_template,
                DEFAULT_FORMAT_SPEC,
                output,
            )
            report = run_qc(output, DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE)
            self.assertTrue(report.passed, report.format_text())

    def test_template_and_qc_reject_casefolded_input_output_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            format_copy = root / "Format.yaml"
            shutil.copy2(DEFAULT_FORMAT_SPEC, format_copy)
            format_before = sha256_file(format_copy)
            with self.assertRaises(TemplateError):
                create_default_template(root / "format.yaml", format_copy)
            self.assertEqual(format_before, sha256_file(format_copy))

            docx = root / "QcDoc.docx"
            render_skeleton(DEFAULT_STRUCTURE, DEFAULT_TEMPLATE, DEFAULT_FORMAT_SPEC, docx)
            docx_before = sha256_file(docx)
            with self.assertRaises(ValueError):
                run_qc_to_file(
                    docx,
                    DEFAULT_FORMAT_SPEC,
                    DEFAULT_STRUCTURE,
                    (ICH_OFFICIAL,),
                    "zh",
                    root / "qcdoc.docx",
                )
            self.assertEqual(docx_before, sha256_file(docx))

    def test_qc_rejects_mutated_profile_numbering_fields_and_story_references(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        ns = {"w": w_namespace}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            render_skeleton(DEFAULT_STRUCTURE, DEFAULT_TEMPLATE, DEFAULT_FORMAT_SPEC, source)

            def mutation(case: str):
                def mutate(name: str, payload: bytes) -> bytes:
                    if not name.endswith(".xml"):
                        return payload
                    xml = etree.fromstring(payload)
                    changed = False
                    if case == "footer" and name == "word/document.xml":
                        for node in xml.xpath(".//w:footerReference", namespaces=ns):
                            node.getparent().remove(node)
                            changed = True
                    elif case == "numbering" and name == "word/numbering.xml":
                        for node in xml.xpath(".//w:lvl[@w:ilvl='1']/w:lvlText", namespaces=ns):
                            node.set(qn("w:val"), "%1")
                            changed = True
                    elif case == "field" and name.startswith("word/footer"):
                        for node in xml.xpath(
                            ".//w:fldChar[@w:fldCharType='separate']", namespaces=ns
                        ):
                            node.getparent().remove(node)
                            changed = True
                    elif case == "profile" and name == "word/styles.xml":
                        heading4 = xml.xpath(
                            "./w:style[@w:styleId='Heading4']/w:rPr/w:sz", namespaces=ns
                        )[0]
                        heading4.set(qn("w:val"), "60")
                        normal_ind = xml.xpath(
                            "./w:style[@w:styleId='Normal']/w:pPr/w:ind", namespaces=ns
                        )[0]
                        normal_ind.set(qn("w:firstLineChars"), "0")
                        changed = True
                    elif case == "profile" and name == "word/document.xml":
                        for margin in xml.xpath(".//w:sectPr/w:pgMar", namespaces=ns):
                            margin.set(qn("w:top"), str(round(5.0 * 1440 / 2.54)))
                            changed = True
                    elif case == "schema_order" and name == "word/styles.xml":
                        paragraph_properties = xml.xpath(
                            "./w:style[@w:styleId='Heading1']/w:pPr", namespaces=ns
                        )[0]
                        indentation = paragraph_properties.find(qn("w:ind"))
                        paragraph_properties.remove(indentation)
                        paragraph_properties.append(indentation)
                        changed = True
                    elif case == "update_fields" and name == "word/settings.xml":
                        for node in xml.xpath("./w:updateFields", namespaces=ns):
                            node.getparent().remove(node)
                            changed = True
                    elif case == "continuous" and name == "word/document.xml":
                        section = xml.xpath(".//w:sectPr", namespaces=ns)[0]
                        section_type = etree.Element(qn("w:type"))
                        section_type.set(qn("w:val"), "continuous")
                        first_page_size = section.find(qn("w:pgSz"))
                        section.insert(section.index(first_page_size), section_type)
                        changed = True
                    elif case == "bookmark_name" and name == "word/document.xml":
                        bookmarks = xml.xpath(".//w:bookmarkStart", namespaces=ns)
                        bookmarks[1].set(qn("w:name"), bookmarks[0].get(qn("w:name")))
                        changed = True
                    elif case == "inline_format" and name == "word/document.xml":
                        run = xml.xpath(
                            ".//w:body/w:p[w:pPr/w:pStyle[@w:val='Heading1']]/w:r",
                            namespaces=ns,
                        )[0]
                        run_properties = etree.Element(qn("w:rPr"))
                        color = etree.SubElement(run_properties, qn("w:color"))
                        color.set(qn("w:val"), "FF0000")
                        size = etree.SubElement(run_properties, qn("w:sz"))
                        size.set(qn("w:val"), "80")
                        run.insert(0, run_properties)
                        changed = True
                    elif case == "toc_spacing" and name == "word/document.xml":
                        spacing = xml.xpath(
                            ".//w:p[w:pPr/w:pStyle[@w:val='CSRTOC']]/w:pPr/w:spacing",
                            namespaces=ns,
                        )[0]
                        spacing.set(qn("w:line"), "720")
                        changed = True
                    elif case == "hansi" and name == "word/styles.xml":
                        fonts = xml.xpath(
                            "./w:style[@w:styleId='Heading1']/w:rPr/w:rFonts",
                            namespaces=ns,
                        )[0]
                        fonts.set(qn("w:hAnsi"), "Arial")
                        changed = True
                    elif case == "number_override" and name == "word/numbering.xml":
                        concrete = xml.xpath("./w:num", namespaces=ns)[-1]
                        override = etree.SubElement(concrete, qn("w:lvlOverride"))
                        override.set(qn("w:ilvl"), "0")
                        start_override = etree.SubElement(override, qn("w:startOverride"))
                        start_override.set(qn("w:val"), "5")
                        changed = True
                    elif case == "level_restart" and name == "word/numbering.xml":
                        level = xml.xpath(
                            "./w:abstractNum[last()]/w:lvl[@w:ilvl='1']", namespaces=ns
                        )[0]
                        restart = etree.Element(qn("w:lvlRestart"))
                        restart.set(qn("w:val"), "0")
                        paragraph_style = level.find(qn("w:pStyle"))
                        level.insert(level.index(paragraph_style), restart)
                        changed = True
                    elif case == "page_format" and name == "word/document.xml":
                        page_number = xml.xpath(".//w:sectPr/w:pgNumType", namespaces=ns)[0]
                        page_number.set(qn("w:fmt"), "upperRoman")
                        changed = True
                    elif case == "footer_no_text" and name.startswith("word/footer"):
                        if xml.xpath(".//w:instrText[contains(., 'PAGE')]", namespaces=ns):
                            for node in xml.xpath(".//w:t", namespaces=ns):
                                node.text = ""
                            changed = True
                    elif case == "footer_extra" and name.startswith("word/footer"):
                        if xml.xpath(".//w:instrText[contains(., 'PAGE')]", namespaces=ns):
                            paragraph = etree.SubElement(xml, qn("w:p"))
                            run = etree.SubElement(paragraph, qn("w:r"))
                            text_node = etree.SubElement(run, qn("w:t"))
                            text_node.text = "错误附加行"
                            changed = True
                    elif case == "cover_footer_text" and name.startswith("word/footer"):
                        if not xml.xpath(".//w:instrText[contains(., 'PAGE')]", namespaces=ns):
                            paragraph = xml.find(qn("w:p"))
                            run = etree.SubElement(paragraph, qn("w:r"))
                            text_node = etree.SubElement(run, qn("w:t"))
                            text_node.text = "封面错误页脚"
                            changed = True
                    elif case == "toc_extra_switch" and name == "word/document.xml":
                        instruction = xml.xpath(
                            ".//w:instrText[starts-with(normalize-space(.), 'TOC')]",
                            namespaces=ns,
                        )[0]
                        instruction.text = (instruction.text or "") + ' \\n "1-5"'
                        changed = True
                    elif case == "toc_moved" and name == "word/document.xml":
                        body = xml.find(qn("w:body"))
                        toc = xml.xpath(
                            ".//w:body/w:p[w:r/w:instrText[starts-with(normalize-space(.), 'TOC')]]",
                            namespaces=ns,
                        )[0]
                        body.remove(toc)
                        section_properties = body.find(qn("w:sectPr"))
                        body.insert(body.index(section_properties), toc)
                        changed = True
                    elif case == "gutter" and name == "word/document.xml":
                        for margin in xml.xpath(".//w:sectPr/w:pgMar", namespaces=ns):
                            margin.set(qn("w:gutter"), str(round(5.0 * 1440 / 2.54)))
                            changed = True
                    elif case == "theme_color" and name == "word/styles.xml":
                        color = xml.xpath(
                            "./w:style[@w:styleId='Heading1']/w:rPr/w:color",
                            namespaces=ns,
                        )[0]
                        color.set(qn("w:themeColor"), "accent1")
                        changed = True
                    elif case == "style_type" and name == "word/styles.xml":
                        style = xml.xpath(
                            "./w:style[@w:styleId='Heading1']", namespaces=ns
                        )[0]
                        style.set(qn("w:type"), "character")
                        changed = True
                    elif case == "header_mismatch" and name.startswith("word/header"):
                        text_nodes = xml.xpath(".//w:t", namespaces=ns)
                        if text_nodes:
                            text_nodes[0].text = name
                            changed = True
                    elif case == "bookmark_partial" and name == "word/document.xml":
                        start = xml.xpath(".//w:bookmarkStart", namespaces=ns)[0]
                        parent = start.getparent()
                        children = list(parent)
                        start_index = children.index(start)
                        run = children[start_index + 1]
                        text_node = run.find(".//" + qn("w:t"))
                        original_text = text_node.text or ""
                        text_node.text = original_text[:1]
                        end = children[start_index + 2]
                        trailing_run = etree.Element(qn("w:r"))
                        trailing_text = etree.SubElement(trailing_run, qn("w:t"))
                        trailing_text.text = original_text[1:]
                        parent.insert(parent.index(end) + 1, trailing_run)
                        changed = True
                    elif case == "bookmark_duplicate_end" and name == "word/document.xml":
                        first_end = xml.xpath(".//w:bookmarkEnd", namespaces=ns)[0]
                        duplicate = etree.Element(qn("w:bookmarkEnd"))
                        duplicate.set(qn("w:id"), first_end.get(qn("w:id")))
                        first_end.getparent().insert(
                            first_end.getparent().index(first_end) + 1, duplicate
                        )
                        changed = True
                    elif case == "style_italic" and name == "word/styles.xml":
                        run_properties = xml.xpath(
                            "./w:style[@w:styleId='Heading1']/w:rPr", namespaces=ns
                        )[0]
                        italic = etree.Element(qn("w:i"))
                        color = run_properties.find(qn("w:color"))
                        run_properties.insert(run_properties.index(color), italic)
                        changed = True
                    elif case == "style_page_break" and name == "word/styles.xml":
                        paragraph_properties = xml.xpath(
                            "./w:style[@w:styleId='Heading2']/w:pPr", namespaces=ns
                        )[0]
                        page_break = etree.Element(qn("w:pageBreakBefore"))
                        spacing = paragraph_properties.find(qn("w:spacing"))
                        paragraph_properties.insert(
                            paragraph_properties.index(spacing), page_break
                        )
                        changed = True
                    elif case == "two_columns" and name == "word/document.xml":
                        for columns in xml.xpath(".//w:sectPr/w:cols", namespaces=ns):
                            columns.set(qn("w:num"), "2")
                            changed = True
                    elif case == "vertical_center" and name == "word/document.xml":
                        for section in xml.xpath(".//w:sectPr", namespaces=ns):
                            vertical = etree.Element(qn("w:vAlign"))
                            vertical.set(qn("w:val"), "center")
                            grid = section.find(qn("w:docGrid"))
                            section.insert(section.index(grid), vertical)
                            changed = True
                    elif case == "heading_page_break" and name == "word/document.xml":
                        run = xml.xpath(
                            ".//w:body/w:p[w:pPr/w:pStyle[@w:val='Heading1']]/w:r",
                            namespaces=ns,
                        )[0]
                        line_break = etree.Element(qn("w:br"))
                        line_break.set(qn("w:type"), "page")
                        run.insert(0, line_break)
                        changed = True
                    elif case == "header_line_break" and name.startswith("word/header"):
                        run = xml.find(".//" + qn("w:r"))
                        if run is not None:
                            run.insert(0, etree.Element(qn("w:br")))
                            changed = True
                    elif case == "footer_line_break" and name.startswith("word/footer"):
                        if xml.xpath(".//w:instrText[contains(., 'PAGE')]", namespaces=ns):
                            run = xml.find(".//" + qn("w:r"))
                            run.insert(0, etree.Element(qn("w:br")))
                            changed = True
                    elif case == "header_date_field" and name.startswith("word/header"):
                        paragraph = xml.find("./" + qn("w:p"))
                        for field_type in ("begin", "separate", "end"):
                            run = etree.SubElement(paragraph, qn("w:r"))
                            if field_type == "separate":
                                instruction_run = etree.Element(qn("w:r"))
                                instruction = etree.SubElement(
                                    instruction_run, qn("w:instrText")
                                )
                                instruction.set(qn("xml:space"), "preserve")
                                instruction.text = " DATE "
                                paragraph.insert(paragraph.index(run), instruction_run)
                            field = etree.SubElement(run, qn("w:fldChar"))
                            field.set(qn("w:fldCharType"), field_type)
                        changed = True
                    elif case == "toc_field_lock" and name == "word/document.xml":
                        field = xml.xpath(
                            ".//w:p[w:pPr/w:pStyle[@w:val='CSRTOC']]//w:fldChar[@w:fldCharType='begin']",
                            namespaces=ns,
                        )[0]
                        field.set(qn("w:fldLock"), "true")
                        changed = True
                    elif case == "page_field_lock" and name.startswith("word/footer"):
                        fields = xml.xpath(
                            ".//w:fldChar[@w:fldCharType='begin']", namespaces=ns
                        )
                        if fields:
                            fields[0].set(qn("w:fldLock"), "true")
                            changed = True
                    return etree.tostring(xml, xml_declaration=True, encoding="UTF-8", standalone=True) if changed else payload

                return mutate

            expected_failures = {
                "footer": {"04_SECTION_STORIES_EXACT", "05_BODY_PAGE_FIELDS_EXACT"},
                "numbering": {"12_NUMBERING_DEFINITION_EXACT"},
                "field": {"09_COMPLEX_FIELD_SEQUENCES"},
                "profile": {
                    "03_MARGIN_PROFILE_EXACT",
                    "17_STYLE_SIZE_PROFILE_EXACT",
                    "18_PARAGRAPH_PROFILE_EXACT",
                },
                "schema_order": {"01_PACKAGE_INTEGRITY"},
                "update_fields": {"01_PACKAGE_INTEGRITY"},
                "continuous": {"02_PAGE_PROFILE_EXACT"},
                "bookmark_name": {"10_BOOKMARK_PAIRS"},
                "inline_format": {"19_STYLE_DECOUPLE"},
                "toc_spacing": {"08_TOC_FIELD_EXACT"},
                "hansi": {"15_STYLE_LATIN_FONT_EXACT"},
                "number_override": {"12_NUMBERING_DEFINITION_EXACT"},
                "level_restart": {"12_NUMBERING_DEFINITION_EXACT"},
                "page_format": {"07_PAGE_NUMBER_RESTART"},
                "footer_no_text": {"05_BODY_PAGE_FIELDS_EXACT"},
                "footer_extra": {"05_BODY_PAGE_FIELDS_EXACT"},
                "cover_footer_text": {"05_BODY_PAGE_FIELDS_EXACT"},
                "toc_extra_switch": {"08_TOC_FIELD_EXACT"},
                "toc_moved": {"08_TOC_FIELD_EXACT"},
                "gutter": {"03_MARGIN_PROFILE_EXACT"},
                "theme_color": {"16_STYLE_FONT_COLOR_EXACT"},
                "style_type": {"16_STYLE_FONT_COLOR_EXACT"},
                "header_mismatch": {"04_SECTION_STORIES_EXACT"},
                "bookmark_partial": {"11_BOOKMARK_NONEMPTY"},
                "bookmark_duplicate_end": {"10_BOOKMARK_PAIRS"},
                "style_italic": {"18_PARAGRAPH_PROFILE_EXACT"},
                "style_page_break": {"18_PARAGRAPH_PROFILE_EXACT"},
                "two_columns": {"02_PAGE_PROFILE_EXACT"},
                "vertical_center": {"02_PAGE_PROFILE_EXACT"},
                "heading_page_break": {"20_HEADING_TREE_AND_LEVELS"},
                "header_line_break": {"04_SECTION_STORIES_EXACT"},
                "footer_line_break": {"05_BODY_PAGE_FIELDS_EXACT"},
                "header_date_field": {
                    "04_SECTION_STORIES_EXACT",
                    "05_BODY_PAGE_FIELDS_EXACT",
                    "09_COMPLEX_FIELD_SEQUENCES",
                },
                "toc_field_lock": {"08_TOC_FIELD_EXACT"},
                "page_field_lock": {"05_BODY_PAGE_FIELDS_EXACT"},
            }
            for case, expected_codes in expected_failures.items():
                with self.subTest(case=case):
                    damaged = root / f"damaged-{case}.docx"
                    self._rewrite_docx(source, damaged, mutation(case))
                    report = run_qc(damaged, DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE)
                    failed_codes = {
                        item.code for item in report.items if item.status == "FAIL"
                    }
                    self.assertTrue(expected_codes <= failed_codes, report.format_text())

    def test_qc_accepts_explicit_false_first_and_even_story_flags(self):
        w_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        ns = {"w": w_namespace}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            explicit_false = root / "explicit-false.docx"
            render_skeleton(DEFAULT_STRUCTURE, DEFAULT_TEMPLATE, DEFAULT_FORMAT_SPEC, source)

            def mutate(name: str, payload: bytes) -> bytes:
                if name not in {"word/document.xml", "word/settings.xml"}:
                    return payload
                xml = etree.fromstring(payload)
                if name == "word/document.xml":
                    for section in xml.xpath(".//w:sectPr", namespaces=ns):
                        title_page = etree.Element(qn("w:titlePg"))
                        title_page.set(qn("w:val"), "0")
                        doc_grid = section.find(qn("w:docGrid"))
                        section.insert(section.index(doc_grid), title_page)
                else:
                    even_odd = etree.Element(qn("w:evenAndOddHeaders"))
                    even_odd.set(qn("w:val"), "0")
                    compatibility = xml.find(qn("w:compat"))
                    xml.insert(xml.index(compatibility), even_odd)
                return etree.tostring(
                    xml, xml_declaration=True, encoding="UTF-8", standalone=True
                )

            self._rewrite_docx(source, explicit_false, mutate)
            report = run_qc(explicit_false, DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE)
            self.assertTrue(report.passed, report.format_text())

    def test_qc_rejects_placeholder_in_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "placeholder-header.docx"
            render_skeleton(
                DEFAULT_STRUCTURE,
                DEFAULT_TEMPLATE,
                DEFAULT_FORMAT_SPEC,
                output,
                header_text="{{drug_name}}",
            )
            report = run_qc(output, DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE)
            failed = {item.code for item in report.items if item.status == "FAIL"}
            self.assertIn("21_PLACEHOLDER_RESIDUE_ALL_STORIES", failed)

    def test_five_level_sponsor_extension_uses_heading5_and_toc_1_to_5(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = yaml.safe_load(DEFAULT_STRUCTURE.read_text(encoding="utf-8"))

            def find(section_id: str, nodes: list[dict]) -> dict:
                for node in nodes:
                    if node.get("id") == section_id:
                        return node
                    for field in (
                        "children",
                        "children_official",
                        "children_convention",
                        "children_sponsor",
                    ):
                        found = find(section_id, node.get(field) or [])
                        if found:
                            return found
                return {}

            parent = find("16.1", payload["sections"])
            parent.setdefault("children_sponsor", []).append(
                {
                    "id": "16.1.13",
                    "layer": "sponsor-custom",
                    "zh": "申办方扩展",
                    "children": [
                        {
                            "id": "16.1.13.1",
                            "zh": "扩展子节",
                            "children": [
                                {"id": "16.1.13.1.1", "zh": "五级扩展子节"}
                            ],
                        }
                    ],
                }
            )
            structure = root / "five-level.yaml"
            structure.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            output = root / "five-level.docx"
            layers = (ICH_OFFICIAL, SPONSOR_CUSTOM)
            render_skeleton(
                structure,
                DEFAULT_TEMPLATE,
                DEFAULT_FORMAT_SPEC,
                output,
                layers=layers,
            )
            report = run_qc(
                output,
                DEFAULT_FORMAT_SPEC,
                structure,
                layers=layers,
            )
            self.assertTrue(report.passed, report.format_text())
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn('w:pStyle w:val="Heading5"', document_xml)
            self.assertIn('TOC \\o "1-5"', document_xml)

    def test_structure_rejects_skipped_parent_and_noncontiguous_numeric_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = yaml.safe_load(DEFAULT_STRUCTURE.read_text(encoding="utf-8"))

            def find(section_id: str, nodes: list[dict]) -> dict:
                for node in nodes:
                    if node.get("id") == section_id:
                        return node
                    for field in (
                        "children",
                        "children_official",
                        "children_convention",
                        "children_sponsor",
                    ):
                        found = find(section_id, node.get(field) or [])
                        if found:
                            return found
                return {}

            parent = find("16.1", payload["sections"])
            parent.setdefault("children_sponsor", []).append(
                {"id": "16.1.20", "layer": "sponsor-custom", "zh": "跳号扩展"}
            )
            skipped = root / "skipped.yaml"
            skipped.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaises(StructureError):
                load_chapter_tree(skipped)

            payload = yaml.safe_load(DEFAULT_STRUCTURE.read_text(encoding="utf-8"))
            parent = find("16.1", payload["sections"])
            parent.setdefault("children_sponsor", []).append(
                {
                    "id": "16.1.20.1",
                    "layer": "sponsor-custom",
                    "zh": "跳级扩展",
                }
            )
            jumped = root / "jumped.yaml"
            jumped.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaises(StructureError):
                load_chapter_tree(jumped)

    def test_selected_layers_cannot_create_number_gap_and_ids_reject_leading_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def find(section_id: str, nodes: list[dict]) -> dict:
                for node in nodes:
                    if node.get("id") == section_id:
                        return node
                    for field in (
                        "children",
                        "children_official",
                        "children_convention",
                        "children_sponsor",
                    ):
                        found = find(section_id, node.get(field) or [])
                        if found:
                            return found
                return {}

            payload = yaml.safe_load(DEFAULT_STRUCTURE.read_text(encoding="utf-8"))
            parent = find("16.1", payload["sections"])
            parent.setdefault("children_convention", []).append(
                {"id": "16.1.13", "zh": "行业扩展"}
            )
            parent.setdefault("children_sponsor", []).append(
                {"id": "16.1.14", "zh": "申办方扩展"}
            )
            layered = root / "layer-gap.yaml"
            layered.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            tree = load_chapter_tree(layered)
            with self.assertRaisesRegex(StructureError, "must remain contiguous"):
                tree.select_layers((ICH_OFFICIAL, SPONSOR_CUSTOM))

            payload = yaml.safe_load(DEFAULT_STRUCTURE.read_text(encoding="utf-8"))
            parent = find("16.1", payload["sections"])
            parent.setdefault("children_sponsor", []).append(
                {"id": "16.1.013", "zh": "前导零扩展"}
            )
            leading_zero = root / "leading-zero.yaml"
            leading_zero.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(StructureError, "non-canonical numeric component"):
                load_chapter_tree(leading_zero)


if __name__ == "__main__":
    unittest.main()
