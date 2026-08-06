"""Create and validate the replaceable Chinese CSR master DOCX."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from .errors import ConfigurationError, TemplateError
from .io_utils import load_yaml, paths_alias


REQUIRED_STYLE_NAMES = (
    "Normal",
    "Heading 1",
    "Heading 2",
    "Heading 3",
    "Heading 4",
    "Heading 5",
    "CSR TOC",
    "CSR Header",
    "CSR Footer",
)


def load_format_spec(path: str | Path) -> dict[str, Any]:
    payload = load_yaml(path)
    if not isinstance(payload, dict):
        raise ConfigurationError("format-spec.yaml must be a mapping")
    for key in ("authoring_profile", "submission_profile"):
        if not isinstance(payload.get(key), dict):
            raise ConfigurationError(f"format-spec.yaml is missing {key}")
    return payload


def _set_style_font(style, en_font: str, zh_font: str, size_pt: float, bold: bool = False) -> None:
    """Write a canonical direct run profile, discarding stock-template residue."""
    rpr = style.element.get_or_add_rPr()
    for child in list(rpr):
        rpr.remove(child)

    rfonts = OxmlElement("w:rFonts")
    rfonts.set(qn("w:ascii"), en_font)
    rfonts.set(qn("w:hAnsi"), en_font)
    rfonts.set(qn("w:eastAsia"), zh_font)
    rfonts.set(qn("w:cs"), en_font)
    rpr.append(rfonts)

    bold_element = OxmlElement("w:b")
    if not bold:
        bold_element.set(qn("w:val"), "0")
    rpr.append(bold_element)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "000000")
    rpr.append(color)

    size = OxmlElement("w:sz")
    size.set(qn("w:val"), str(round(size_pt * 2)))
    rpr.append(size)


def _set_first_line_chars(style, chars: int, fallback_twips: int) -> None:
    ppr = style.element.get_or_add_pPr()
    ind = ppr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        outline = ppr.find(qn("w:outlineLvl"))
        if outline is None:
            ppr.append(ind)
        else:
            ppr.insert(ppr.index(outline), ind)
    else:
        outline = ppr.find(qn("w:outlineLvl"))
        if outline is not None and ppr.index(ind) > ppr.index(outline):
            ppr.remove(ind)
            ppr.insert(ppr.index(outline), ind)
    ind.set(qn("w:firstLineChars"), str(chars * 100))
    ind.set(qn("w:firstLine"), str(fallback_twips))


def create_default_template(output: str | Path, spec_path: str | Path) -> Path:
    output_path = Path(output)
    if paths_alias(output_path, spec_path):
        raise TemplateError("Template output must not overwrite format-spec.yaml")
    spec = load_format_spec(spec_path)
    authoring = spec["authoring_profile"]
    fonts = authoring["fonts"]
    page = authoring["page"]
    styles_cfg = authoring["styles"]

    document = Document()
    zoom = document.settings._element.find(qn("w:zoom"))
    if zoom is not None:
        # The stock python-docx template sets bestFit without the schema-required percent.
        zoom.set(qn("w:percent"), "100")
    section = document.sections[0]
    section.page_width = Cm(float(page["width_cm"]))
    section.page_height = Cm(float(page["height_cm"]))
    margins = page["margin_cm"]
    section.top_margin = Cm(float(margins["top"]))
    section.bottom_margin = Cm(float(margins["bottom"]))
    section.left_margin = Cm(float(margins["left"]))
    section.right_margin = Cm(float(margins["right"]))
    section.header_distance = Cm(float(page["header_distance_cm"]))
    section.footer_distance = Cm(float(page["footer_distance_cm"]))

    styles = document.styles
    normal = styles["Normal"]
    _set_style_font(
        normal,
        fonts["latin"],
        fonts["chinese_body"],
        float(styles_cfg["body"]["size_pt"]),
    )
    normal.paragraph_format.line_spacing = float(styles_cfg["body"]["line_spacing"])
    normal.paragraph_format.space_after = Pt(float(styles_cfg["body"]["space_after_pt"]))
    _set_first_line_chars(normal, int(styles_cfg["body"]["first_line_indent_chars"]), 480)

    for level in range(1, 6):
        style = styles[f"Heading {level}"]
        cfg = styles_cfg[f"heading{level}"]
        _set_style_font(
            style,
            fonts["latin"],
            fonts["chinese_heading"],
            float(cfg["size_pt"]),
            bool(cfg["bold"]),
        )
        style.paragraph_format.space_before = Pt(float(cfg["space_before_pt"]))
        style.paragraph_format.space_after = Pt(float(cfg["space_after_pt"]))
        style.paragraph_format.keep_with_next = True
        _set_first_line_chars(style, 0, 0)
        ppr = style.element.get_or_add_pPr()
        outline = ppr.find(qn("w:outlineLvl"))
        if outline is None:
            outline = OxmlElement("w:outlineLvl")
            ppr.append(outline)
        outline.set(qn("w:val"), str(level - 1))

    if "CSR TOC" in [style.name for style in styles]:
        toc_style = styles["CSR TOC"]
    else:
        toc_style = styles.add_style("CSR TOC", WD_STYLE_TYPE.PARAGRAPH)
    toc_style.base_style = normal
    _set_style_font(
        toc_style,
        fonts["latin"],
        fonts["chinese_body"],
        float(styles_cfg["toc"]["size_pt"]),
    )
    toc_style.paragraph_format.line_spacing = float(styles_cfg["toc"]["line_spacing"])
    toc_style.paragraph_format.first_line_indent = Pt(0)
    _set_first_line_chars(toc_style, int(styles_cfg["toc"]["first_line_indent_chars"]), 0)

    existing_names = {style.name for style in styles}
    story_cfg = styles_cfg["header_footer"]
    for style_name in ("CSR Header", "CSR Footer"):
        if style_name in existing_names:
            story_style = styles[style_name]
        else:
            story_style = styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
        _set_style_font(
            story_style,
            fonts["latin"],
            fonts["chinese_body"],
            float(story_cfg["size_pt"]),
        )
        story_style.paragraph_format.line_spacing = float(story_cfg["line_spacing"])
        story_style.paragraph_format.space_before = Pt(float(story_cfg["space_before_pt"]))
        story_style.paragraph_format.space_after = Pt(float(story_cfg["space_after_pt"]))
        story_style.paragraph_format.first_line_indent = Pt(0)
        _set_first_line_chars(
            story_style,
            int(story_cfg["first_line_indent_chars"]),
            0,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".docx", dir=output_path.parent
    )
    os.close(file_descriptor)
    try:
        document.save(temporary)
        validate_template(temporary)
        os.replace(temporary, output_path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return output_path


def validate_template(path: str | Path) -> None:
    template_path = Path(path)
    if not template_path.is_file():
        raise TemplateError(f"Master DOCX does not exist: {template_path}")
    try:
        document = Document(template_path)
    except Exception as exc:
        raise TemplateError(f"Cannot open master DOCX {template_path}: {exc}") from exc
    names = {style.name for style in document.styles}
    missing = [name for name in REQUIRED_STYLE_NAMES if name not in names]
    if missing:
        raise TemplateError(f"Master DOCX is missing required styles: {missing}")
