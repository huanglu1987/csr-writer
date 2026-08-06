from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.shared import Cm

from csr_writer.io_utils import load_yaml, sha256_file
from csr_writer.visual_qc import (
    DEFAULT_VISUAL_QC_CONFIG,
    DEFAULT_VISUAL_QC_LOCK,
    PageBlock,
    SectionLayout,
    VisualQCError,
    _analyse_pdf,
    _contact_sheets,
    _directory_findings,
    _docx_layout,
    _is_story,
    _load_configuration,
    run_visual_qc,
)


ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_TEMP_ROOTS = (Path("/tmp"), Path("/private/tmp"), Path("/var/folders"))


def _inside_system_temp(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    for root in _SYSTEM_TEMP_ROOTS:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


TEST_WORK_PARENT = (
    Path.home() / "Library" / "Caches" / "csr-writer-visual-tests"
    if _inside_system_temp(ROOT)
    else ROOT / ".visual-qc-test-work"
)


class VisualQCV110Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TEST_WORK_PARENT.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            TEST_WORK_PARENT.rmdir()
        except OSError:
            pass

    def _minimal_docx(self, directory: Path) -> Path:
        path = directory / "minimal.docx"
        document = Document()
        section = document.sections[0]
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        document.add_paragraph("合法合成正文。")
        document.save(path)
        return path

    def test_configuration_hash_is_frozen(self) -> None:
        _, observed = _load_configuration(
            DEFAULT_VISUAL_QC_CONFIG, DEFAULT_VISUAL_QC_LOCK
        )
        self.assertEqual(
            "fcfb0246089dc2984eacca06fa52f1d93a9d4d3ef13a2775a6df28af76d8046d",
            observed,
        )

    def test_missing_renderer_degrades_without_changing_release_decision(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_WORK_PARENT) as directory:
            work = Path(directory)
            docx = self._minimal_docx(work)
            report = run_visual_qc(
                docx_path=docx,
                work_dir=work / "render",
                json_report=work / "report.json",
                markdown_report=work / "report.md",
                renderer_override="/definitely/not/installed/soffice",
            )
            self.assertEqual("not_run", report["status"])
            self.assertEqual(
                "I-VISUAL-RENDERER-NOT-FOUND", report["reason"]["code"]
            )
            self.assertFalse(report["legacy_release_decision_changed"])
            markdown = (work / "report.md").read_text(encoding="utf-8")
            self.assertIn("视觉检查未执行：未检出渲染器", markdown)
            self.assertIn("前置筛查，不替代", markdown)

    def test_system_temporary_render_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_WORK_PARENT) as directory:
            docx = self._minimal_docx(Path(directory))
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(
                    VisualQCError, "E-VISUAL-WORKDIR-UNSAFE"
                ):
                    run_visual_qc(
                        docx_path=docx,
                        work_dir=Path(temporary) / "render",
                        json_report=Path(directory) / "report.json",
                        markdown_report=Path(directory) / "report.md",
                        renderer_override="/definitely/not/installed/soffice",
                    )

    def test_header_footer_story_is_excluded_by_exact_story_evidence(self) -> None:
        layout = SectionLayout(
            section_index=0,
            page_width_pt=595.0,
            page_height_pt=842.0,
            body_left_pt=72.0,
            body_top_pt=72.0,
            body_right_pt=523.0,
            body_bottom_pt=770.0,
            header_story_keys=("临床试验总结报告骨架",),
            footer_story_keys=("第页共页机密",),
        )
        header = PageBlock(
            text="临床试验总结报告骨架",
            bbox=(72.0, 40.0, 523.0, 54.0),
            lines=("临床试验总结报告骨架",),
            max_font_size=9.0,
            bold=False,
        )
        body = PageBlock(
            text="临床试验结果",
            bbox=(72.0, 90.0, 523.0, 104.0),
            lines=("临床试验结果",),
            max_font_size=10.5,
            bold=False,
        )
        self.assertTrue(_is_story(header, layout, minimum=2))
        self.assertFalse(_is_story(body, layout, minimum=2))

    def test_linked_section_inherits_header_footer_story(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_WORK_PARENT) as directory:
            path = Path(directory) / "linked-story.docx"
            document = Document()
            document.sections[0].header.paragraphs[0].text = "合成页眉"
            document.sections[0].footer.paragraphs[0].text = "合成页脚"
            document.add_paragraph("第一节。")
            linked = document.add_section()
            linked.header.is_linked_to_previous = True
            linked.footer.is_linked_to_previous = True
            linked.page_width = Cm(29.7)
            linked.page_height = Cm(21.0)
            document.add_paragraph("第二节。")
            document.save(path)
            layouts = _docx_layout(path)["layouts"]
            self.assertGreaterEqual(len(layouts), 2)
            self.assertEqual(layouts[0].header_story_keys, layouts[1].header_story_keys)
            self.assertEqual(layouts[0].footer_story_keys, layouts[1].footer_story_keys)

    def test_unexpanded_nonempty_directory_is_class_a(self) -> None:
        findings = _directory_findings(
            global_lines=[(3, "3.1 正文章节目录"), (3, "3.2 正文表格目录")],
            directory_headings=["3.1 正文章节目录", "3.2 正文表格目录"],
            expected_counts=[12, 0],
            entry_pattern=r"\.{2,}\s*\d+\s*$",
            page_texts=["", "", "3.1 正文章节目录\n3.2 正文表格目录"],
            title_prefix_characters=12,
        )
        self.assertEqual(1, len(findings))
        self.assertEqual("A-FIELD-DIRECTORY-NOT-EXPANDED", findings[0]["code"])

    @unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF not installed")
    def test_a4_and_non_a4_pages_are_distinguished(self) -> None:
        fitz = importlib.import_module("fitz")
        config, _ = _load_configuration(
            DEFAULT_VISUAL_QC_CONFIG, DEFAULT_VISUAL_QC_LOCK
        )
        with tempfile.TemporaryDirectory(dir=TEST_WORK_PARENT) as directory:
            pdf_path = Path(directory) / "sizes.pdf"
            document = fitz.open()
            for width, height in ((595.276, 841.89), (612.0, 792.0)):
                page = document.new_page(width=width, height=height)
                page.insert_text((90, 100), "lawful synthetic body")
            document.save(pdf_path)
            document.close()
            metadata = {
                "layouts": [
                    SectionLayout(
                        0, 595.276, 841.89, 72.0, 72.0, 523.0, 770.0, (), ()
                    )
                ],
                "field_counts": {"toc": 0, "page": 0, "total_pages": 0, "ref": 0, "tc": 0},
                "toc_expected_counts": [],
            }
            findings, rendered, _ = _analyse_pdf(
                pdf_path=pdf_path,
                docx_metadata=metadata,
                config=config,
                fitz=fitz,
            )
            rendered.close()
            size_findings = [
                item for item in findings if item["code"] == "A-PAGE-SIZE-NOT-A4"
            ]
            self.assertEqual([2], [item["page"] for item in size_findings])

    @unittest.skipUnless(
        importlib.util.find_spec("fitz") and importlib.util.find_spec("PIL.Image"),
        "visual dependencies not installed",
    )
    def test_contact_sheet_splits_after_twenty_pages(self) -> None:
        fitz = importlib.import_module("fitz")
        image_module = importlib.import_module("PIL.Image")
        config = load_yaml(DEFAULT_VISUAL_QC_CONFIG)
        with tempfile.TemporaryDirectory(dir=TEST_WORK_PARENT) as directory:
            pdf_path = Path(directory) / "pages.pdf"
            document = fitz.open()
            for page_number in range(1, 22):
                page = document.new_page(width=595.276, height=841.89)
                page.insert_text((72, 90), f"synthetic page {page_number}")
            document.save(pdf_path)
            document.close()
            rendered = fitz.open(pdf_path)
            pages = [rendered[index] for index in range(rendered.page_count)]
            records = _contact_sheets(
                pages=pages,
                findings=[],
                output_dir=Path(directory),
                stem="split",
                config=config,
                image_module=image_module,
            )
            rendered.close()
            self.assertEqual(2, len(records))
            self.assertEqual(list(range(1, 21)), records[0]["pages"])
            self.assertEqual([21], records[1]["pages"])
            self.assertLess(records[0]["width_px"], 2000)
            self.assertLess(records[0]["height_px"], 3000)

    def test_holdout_generator_has_no_checker_or_config_dependency(self) -> None:
        for filename in (
            "build_v1_1_visual_holdout.py",
            "build_v1_1_visual_holdout_set.py",
        ):
            source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
            self.assertNotIn("csr_writer.visual_qc", source)
            self.assertNotIn("visual-qc-config.yaml", source)
            self.assertNotIn("visual-qc-config.lock.json", source)
        self.assertEqual(
            sha256_file(DEFAULT_VISUAL_QC_CONFIG),
            "fcfb0246089dc2984eacca06fa52f1d93a9d4d3ef13a2775a6df28af76d8046d",
        )


if __name__ == "__main__":
    unittest.main()
