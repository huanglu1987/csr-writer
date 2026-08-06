"""M1 fixed 21-check OOXML format QC; no PDF/LibreOffice dependency."""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lxml import etree

from .io_utils import atomic_write_json, paths_alias
from .structure import flatten_sections, load_chapter_tree
from .template import load_format_spec


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W = f"{{{W_NS}}}"
NS = {"w": W_NS, "r": R_NS}


@dataclass(frozen=True)
class QCItem:
    code: str
    status: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class QCReport:
    items: tuple[QCItem, ...]

    @property
    def passed(self) -> bool:
        return all(item.status == "PASS" for item in self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "check_count": len(self.items),
            "pass_count": sum(item.status == "PASS" for item in self.items),
            "fail_count": sum(item.status == "FAIL" for item in self.items),
            "status": "pass" if self.passed else "fail",
            "items": [item.as_dict() for item in self.items],
            "scope_note": "OOXML format scaffold only; this report never states submission readiness",
        }

    def format_text(self) -> str:
        lines = ["=" * 78, "CSR M1 OOXML 格式质检（固定 21 项）", "=" * 78]
        for item in self.items:
            mark = "✓" if item.status == "PASS" else "✗"
            lines.append(f" {mark} [{item.status}] {item.code:28s} {item.message}")
        lines.extend(
            [
                "=" * 78,
                f"PASS {sum(i.status == 'PASS' for i in self.items)}   "
                f"FAIL {sum(i.status == 'FAIL' for i in self.items)}",
                "结论边界：仅报告未发现/发现 OOXML 格式问题，不作申报就绪判断。",
            ]
        )
        return "\n".join(lines) + "\n"


def _item(code: str, ok: bool, passed: str, failed: str) -> QCItem:
    return QCItem(code, "PASS" if ok else "FAIL", passed if ok else failed)


def _parse_xml_parts(archive: zipfile.ZipFile) -> tuple[dict[str, etree._Element], list[str]]:
    parsed: dict[str, etree._Element] = {}
    errors: list[str] = []
    for name in archive.namelist():
        if not name.endswith((".xml", ".rels")):
            continue
        try:
            parsed[name] = etree.fromstring(archive.read(name))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return parsed, errors


def _field_names(root: etree._Element | None) -> list[str]:
    if root is None:
        return []
    names: list[str] = []
    for node in root.xpath(".//w:instrText", namespaces=NS):
        text = "".join(node.itertext()).strip()
        if text:
            names.append(text.split()[0].upper())
    return names


def _field_pairs_valid(root: etree._Element | None) -> bool:
    if root is None:
        return False
    stack: list[dict[str, bool]] = []
    field_count = 0
    for node in root.iter():
        if node.tag == W + "fldChar":
            kind = node.get(W + "fldCharType")
            if kind == "begin":
                stack.append({"instruction": False, "separate": False})
                field_count += 1
            elif kind == "separate":
                if not stack or stack[-1]["separate"] or not stack[-1]["instruction"]:
                    return False
                stack[-1]["separate"] = True
            elif kind == "end":
                if not stack or not stack[-1]["instruction"] or not stack[-1]["separate"]:
                    return False
                stack.pop()
            else:
                return False
        elif node.tag == W + "instrText":
            if not stack or stack[-1]["separate"] or not (node.text or "").strip():
                return False
            stack[-1]["instruction"] = True
    return field_count > 0 and not stack


def _relationship_targets(
    relationships: etree._Element | None,
) -> dict[str, str]:
    if relationships is None:
        return {}
    targets: dict[str, str] = {}
    for relationship in relationships.xpath(".//*[local-name()='Relationship']"):
        relationship_id = relationship.get("Id")
        target = relationship.get("Target")
        if relationship_id and target:
            targets[relationship_id] = posixpath.normpath(posixpath.join("word", target))
    return targets


def _section_story_parts(
    sections: list[etree._Element],
    relationships: etree._Element | None,
    package_names: set[str],
) -> tuple[list[dict[str, str]], list[str]]:
    targets = _relationship_targets(relationships)
    resolved_sections: list[dict[str, str]] = []
    errors: list[str] = []
    for index, section in enumerate(sections, start=1):
        stories: dict[str, str] = {}
        for kind in ("header", "footer"):
            references = section.findall(f"./w:{kind}Reference", namespaces=NS)
            default = [node for node in references if node.get(W + "type") == "default"]
            unexpected = [node.get(W + "type") for node in references if node.get(W + "type") != "default"]
            if len(default) != 1 or unexpected:
                errors.append(
                    f"section {index} {kind} references: default={len(default)} unexpected={unexpected}"
                )
                continue
            relationship_id = default[0].get(f"{{{R_NS}}}id")
            target = targets.get(relationship_id or "")
            if not relationship_id or not target or target not in package_names:
                errors.append(
                    f"section {index} {kind} reference does not resolve: {relationship_id!r}"
                )
                continue
            stories[kind] = target
        resolved_sections.append(stories)
    return resolved_sections, errors


def _style_properties(styles: etree._Element, style_id: str) -> dict[str, str | None]:
    by_id = {
        node.get(W + "styleId"): node
        for node in styles.xpath("./w:style", namespaces=NS)
        if node.get(W + "styleId")
    }
    seen: set[str] = set()
    current = by_id.get(style_id)
    properties: dict[str, str | None] = {
        "type": current.get(W + "type") if current is not None else None,
        "eastAsia": None,
        "ascii": None,
        "hAnsi": None,
        "cs": None,
        "eastAsiaTheme": None,
        "asciiTheme": None,
        "hAnsiTheme": None,
        "csTheme": None,
        "color": None,
        "themeColor": None,
        "themeTint": None,
        "themeShade": None,
        "size": None,
        "line": None,
        "lineRule": None,
        "before": None,
        "after": None,
        "firstLine": None,
        "firstLineChars": None,
        "bold": None,
        "keepNext": None,
        "outlineLvl": None,
    }
    while current is not None and current.get(W + "styleId") not in seen:
        seen.add(current.get(W + "styleId"))
        rfonts = current.find("./w:rPr/w:rFonts", namespaces=NS)
        if rfonts is not None:
            properties["eastAsia"] = properties["eastAsia"] or rfonts.get(W + "eastAsia")
            properties["ascii"] = properties["ascii"] or rfonts.get(W + "ascii")
            properties["hAnsi"] = properties["hAnsi"] or rfonts.get(W + "hAnsi")
            properties["cs"] = properties["cs"] or rfonts.get(W + "cs")
            for attribute in ("eastAsiaTheme", "asciiTheme", "hAnsiTheme", "csTheme"):
                properties[attribute] = properties[attribute] or rfonts.get(W + attribute)
        color = current.find("./w:rPr/w:color", namespaces=NS)
        size = current.find("./w:rPr/w:sz", namespaces=NS)
        spacing = current.find("./w:pPr/w:spacing", namespaces=NS)
        indentation = current.find("./w:pPr/w:ind", namespaces=NS)
        bold = current.find("./w:rPr/w:b", namespaces=NS)
        keep_next = current.find("./w:pPr/w:keepNext", namespaces=NS)
        outline = current.find("./w:pPr/w:outlineLvl", namespaces=NS)
        if color is not None:
            properties["color"] = properties["color"] or color.get(W + "val")
            properties["themeColor"] = properties["themeColor"] or color.get(W + "themeColor")
            properties["themeTint"] = properties["themeTint"] or color.get(W + "themeTint")
            properties["themeShade"] = properties["themeShade"] or color.get(W + "themeShade")
        if size is not None:
            properties["size"] = properties["size"] or size.get(W + "val")
        if spacing is not None:
            properties["line"] = properties["line"] or spacing.get(W + "line")
            properties["lineRule"] = properties["lineRule"] or spacing.get(W + "lineRule")
            properties["before"] = properties["before"] or spacing.get(W + "before")
            properties["after"] = properties["after"] or spacing.get(W + "after")
        if indentation is not None:
            properties["firstLine"] = properties["firstLine"] or indentation.get(W + "firstLine")
            properties["firstLineChars"] = properties["firstLineChars"] or indentation.get(W + "firstLineChars")
        if bold is not None and properties["bold"] is None:
            properties["bold"] = bold.get(W + "val") or "true"
        if keep_next is not None and properties["keepNext"] is None:
            properties["keepNext"] = keep_next.get(W + "val") or "true"
        if outline is not None:
            properties["outlineLvl"] = properties["outlineLvl"] or outline.get(W + "val")
        based_on = current.find("./w:basedOn", namespaces=NS)
        current = by_id.get(based_on.get(W + "val")) if based_on is not None else None
    return properties


def _twips_from_cm(value: float) -> int:
    return round(value * 1440 / 2.54)


def _twips_from_points(value: float) -> int:
    return round(value * 20)


def _close(actual: str | None, expected: int, tolerance: int = 1) -> bool:
    try:
        return abs(int(actual or "") - expected) <= tolerance
    except ValueError:
        return False


def _on_off_enabled(node: etree._Element | None) -> bool:
    if node is None:
        return False
    return (node.get(W + "val") or "true").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _local_children(node: etree._Element | None) -> list[str]:
    if node is None:
        return []
    return [etree.QName(child).localname for child in node]


def _attributes_exact(node: etree._Element | None, expected: dict[str, str]) -> bool:
    return node is not None and dict(node.attrib) == expected


def _run_matches(run: etree._Element, kind: str, value: str) -> bool:
    if run.tag != W + "r" or run.attrib or len(run) != 1:
        return False
    child = run[0]
    if kind == "text":
        expected_attributes = (
            {f"{{{XML_NS}}}space": "preserve"}
            if value != value.strip()
            else {}
        )
        return (
            child.tag == W + "t"
            and not len(child)
            and (child.text or "") == value
            and dict(child.attrib) == expected_attributes
        )
    if kind == "instruction":
        return (
            child.tag == W + "instrText"
            and not len(child)
            and (child.text or "") == value
            and dict(child.attrib) == {f"{{{XML_NS}}}space": "preserve"}
        )
    if kind == "field":
        return (
            child.tag == W + "fldChar"
            and not len(child)
            and not (child.text or "")
            and dict(child.attrib) == {W + "fldCharType": value}
        )
    return False


def _paragraph_runs_match(
    paragraph: etree._Element | None,
    expected: list[tuple[str, str]],
) -> bool:
    if paragraph is None or _local_children(paragraph) != [
        "pPr",
        *("r" for _ in expected),
    ]:
        return False
    runs = paragraph.findall("./w:r", namespaces=NS)
    return len(runs) == len(expected) and all(
        _run_matches(run, kind, value)
        for run, (kind, value) in zip(runs, expected)
    )


def _required_style_xml_contract(
    styles: etree._Element | None,
    authoring: dict[str, Any],
) -> bool:
    if styles is None:
        return False
    by_id = {
        node.get(W + "styleId"): node
        for node in styles.xpath("./w:style", namespaces=NS)
        if node.get(W + "styleId")
    }
    style_ids = (
        "Normal",
        "Heading1",
        "Heading2",
        "Heading3",
        "Heading4",
        "Heading5",
        "CSRTOC",
        "CSRHeader",
        "CSRFooter",
    )
    if any(style_id not in by_id for style_id in style_ids):
        return False

    fonts = authoring["fonts"]
    styles_cfg = authoring["styles"]
    expected_based_on = {
        "Normal": None,
        **{f"Heading{level}": "Normal" for level in range(1, 6)},
        "CSRTOC": "Normal",
        "CSRHeader": None,
        "CSRFooter": None,
    }
    expected_ppr_children = {
        "Normal": ["spacing", "ind"],
        **{
            f"Heading{level}": [
                "keepNext",
                "keepLines",
                "spacing",
                "ind",
                "outlineLvl",
            ]
            for level in range(1, 6)
        },
        "CSRTOC": ["spacing", "ind"],
        "CSRHeader": ["spacing", "ind"],
        "CSRFooter": ["spacing", "ind"],
    }
    expected_sizes = {
        "Normal": float(styles_cfg["body"]["size_pt"]),
        "CSRTOC": float(styles_cfg["toc"]["size_pt"]),
        "CSRHeader": float(styles_cfg["header_footer"]["size_pt"]),
        "CSRFooter": float(styles_cfg["header_footer"]["size_pt"]),
        **{
            f"Heading{level}": float(styles_cfg[f"heading{level}"]["size_pt"])
            for level in range(1, 6)
        },
    }

    for style_id in style_ids:
        style = by_id[style_id]
        if style.get(W + "type") != "paragraph":
            return False
        based_on = style.findall("./w:basedOn", namespaces=NS)
        expected_base = expected_based_on[style_id]
        if expected_base is None:
            if based_on:
                return False
        elif len(based_on) != 1 or not _attributes_exact(
            based_on[0], {W + "val": expected_base}
        ):
            return False

        ppr = style.find("./w:pPr", namespaces=NS)
        rpr = style.find("./w:rPr", namespaces=NS)
        if (
            ppr is None
            or rpr is None
            or ppr.attrib
            or rpr.attrib
            or _local_children(ppr) != expected_ppr_children[style_id]
            or _local_children(rpr) != ["rFonts", "b", "color", "sz"]
        ):
            return False

        heading = style_id.startswith("Heading")
        expected_east_asia = (
            fonts["chinese_heading"] if heading else fonts["chinese_body"]
        )
        rfonts = rpr.find("./w:rFonts", namespaces=NS)
        bold = rpr.find("./w:b", namespaces=NS)
        color = rpr.find("./w:color", namespaces=NS)
        size = rpr.find("./w:sz", namespaces=NS)
        if not _attributes_exact(
            rfonts,
            {
                W + "ascii": fonts["latin"],
                W + "hAnsi": fonts["latin"],
                W + "eastAsia": expected_east_asia,
                W + "cs": fonts["latin"],
            },
        ):
            return False
        if not _attributes_exact(bold, {} if heading else {W + "val": "0"}):
            return False
        if not _attributes_exact(color, {W + "val": str(fonts["color"]).upper()}):
            return False
        if not _attributes_exact(
            size, {W + "val": str(round(expected_sizes[style_id] * 2))}
        ):
            return False

        children = {etree.QName(child).localname: child for child in ppr}
        if style_id == "Normal":
            cfg = styles_cfg["body"]
            if not _attributes_exact(
                children["spacing"],
                {
                    W + "line": str(round(float(cfg["line_spacing"]) * 240)),
                    W + "lineRule": "auto",
                    W + "after": str(_twips_from_points(float(cfg["space_after_pt"]))),
                },
            ) or not _attributes_exact(
                children["ind"],
                {
                    W + "firstLineChars": str(int(cfg["first_line_indent_chars"]) * 100),
                    W + "firstLine": "480",
                },
            ):
                return False
        elif heading:
            level = int(style_id.removeprefix("Heading"))
            cfg = styles_cfg[f"heading{level}"]
            if (
                not _attributes_exact(children["keepNext"], {})
                or not _attributes_exact(children["keepLines"], {})
                or not _attributes_exact(
                    children["spacing"],
                    {
                        W + "before": str(_twips_from_points(float(cfg["space_before_pt"]))),
                        W + "after": str(_twips_from_points(float(cfg["space_after_pt"]))),
                    },
                )
                or not _attributes_exact(
                    children["ind"],
                    {W + "firstLineChars": "0", W + "firstLine": "0"},
                )
                or not _attributes_exact(
                    children["outlineLvl"], {W + "val": str(level - 1)}
                )
            ):
                return False
        elif style_id == "CSRTOC":
            cfg = styles_cfg["toc"]
            if not _attributes_exact(
                children["spacing"],
                {
                    W + "line": str(round(float(cfg["line_spacing"]) * 240)),
                    W + "lineRule": "auto",
                },
            ) or not _attributes_exact(
                children["ind"],
                {
                    W + "firstLine": "0",
                    W + "firstLineChars": str(int(cfg["first_line_indent_chars"]) * 100),
                },
            ):
                return False
        else:
            cfg = styles_cfg["header_footer"]
            if not _attributes_exact(
                children["spacing"],
                {
                    W + "line": str(round(float(cfg["line_spacing"]) * 240)),
                    W + "lineRule": "auto",
                    W + "before": str(_twips_from_points(float(cfg["space_before_pt"]))),
                    W + "after": str(_twips_from_points(float(cfg["space_after_pt"]))),
                },
            ) or not _attributes_exact(
                children["ind"],
                {
                    W + "firstLine": "0",
                    W + "firstLineChars": str(int(cfg["first_line_indent_chars"]) * 100),
                },
            ):
                return False

    defaults = styles.find("./w:docDefaults", namespaces=NS)
    default_rpr = (
        defaults.find("./w:rPrDefault/w:rPr", namespaces=NS)
        if defaults is not None
        else None
    )
    default_ppr = (
        defaults.find("./w:pPrDefault/w:pPr", namespaces=NS)
        if defaults is not None
        else None
    )
    return (
        _local_children(default_rpr) == ["rFonts", "sz", "szCs", "lang"]
        and _local_children(default_ppr) == ["spacing"]
    )


def run_qc(
    docx_path: str | Path,
    format_spec_path: str | Path,
    structure_path: str | Path,
    layers: Iterable[str] = ("ich-official",),
    title_language: str = "zh",
) -> QCReport:
    docx_file = Path(docx_path)
    spec = load_format_spec(format_spec_path)
    authoring = spec["authoring_profile"]
    official = spec["submission_profile"]["official"]
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/styles.xml",
        "word/numbering.xml",
        "word/_rels/document.xml.rels",
    }
    archive: zipfile.ZipFile | None = None
    parsed: dict[str, etree._Element] = {}
    package_errors: list[str] = []
    names: set[str] = set()
    try:
        archive = zipfile.ZipFile(docx_file)
        names = set(archive.namelist())
        parsed, package_errors = _parse_xml_parts(archive)
        settings = parsed.get("word/settings.xml")
        if settings is not None:
            zoom = settings.find("./w:zoom", namespaces=NS)
            if zoom is not None and zoom.get(W + "percent") is None:
                package_errors.append("word/settings.xml: w:zoom is missing w:percent")
            children = [etree.QName(child).localname for child in settings]
            if "updateFields" in children and "compat" in children:
                if children.index("updateFields") > children.index("compat"):
                    package_errors.append("word/settings.xml: updateFields must precede compat")
            update_fields = settings.find("./w:updateFields", namespaces=NS)
            if not _on_off_enabled(update_fields):
                package_errors.append(
                    "word/settings.xml: updateFields must exist and be enabled"
                )
        else:
            package_errors.append("word/settings.xml: settings part is missing")
        paragraph_property_order = {
            name: index
            for index, name in enumerate(
                (
                    "pStyle",
                    "keepNext",
                    "keepLines",
                    "pageBreakBefore",
                    "framePr",
                    "widowControl",
                    "numPr",
                    "suppressLineNumbers",
                    "pBdr",
                    "shd",
                    "tabs",
                    "suppressAutoHyphens",
                    "kinsoku",
                    "wordWrap",
                    "overflowPunct",
                    "topLinePunct",
                    "autoSpaceDE",
                    "autoSpaceDN",
                    "bidi",
                    "adjustRightInd",
                    "snapToGrid",
                    "spacing",
                    "ind",
                    "contextualSpacing",
                    "mirrorIndents",
                    "suppressOverlap",
                    "jc",
                    "textDirection",
                    "textAlignment",
                    "textboxTightWrap",
                    "outlineLvl",
                    "divId",
                    "cnfStyle",
                    "rPr",
                    "sectPr",
                    "pPrChange",
                )
            )
        }
        for part_name in ("word/styles.xml", "word/document.xml"):
            root = parsed.get(part_name)
            if root is None:
                continue
            for paragraph_properties in root.xpath(".//w:pPr", namespaces=NS):
                positions = [
                    paragraph_property_order.get(etree.QName(child).localname, 10**6)
                    for child in paragraph_properties
                ]
                if positions != sorted(positions):
                    package_errors.append(
                        f"{part_name}: paragraph property elements violate OOXML order"
                    )
                    break
    except Exception as exc:
        package_errors = [str(exc)]

    items: list[QCItem] = []
    package_ok = archive is not None and required <= names and not package_errors
    items.append(
        _item(
            "01_PACKAGE_INTEGRITY",
            package_ok,
            "required parts present and every XML part parses",
            f"missing={sorted(required - names)} xml_errors={package_errors[:2]}",
        )
    )
    document = parsed.get("word/document.xml")
    document_rels = parsed.get("word/_rels/document.xml.rels")
    styles = parsed.get("word/styles.xml")
    numbering = parsed.get("word/numbering.xml")

    sections = document.xpath(".//w:sectPr", namespaces=NS) if document is not None else []
    page = authoring["page"]
    page_ok = len(sections) == 2
    for section in sections:
        section_children = _local_children(section)
        allowed_section_children = {
            "headerReference",
            "footerReference",
            "type",
            "pgSz",
            "pgMar",
            "pgNumType",
            "cols",
            "titlePg",
            "docGrid",
        }
        page_ok &= (
            not (set(section_children) - allowed_section_children)
            and section_children.count("pgSz") == 1
            and section_children.count("pgMar") == 1
            and section_children.count("cols") == 1
            and section_children.count("docGrid") == 1
            and section_children.count("type") <= 1
            and section_children.count("titlePg") <= 1
        )
        page_size = section.find("./w:pgSz", namespaces=NS)
        if page_size is None:
            page_ok = False
            continue
        width, height = int(page_size.get(W + "w", "0")), int(page_size.get(W + "h", "0"))
        page_ok &= _close(str(width), _twips_from_cm(float(page["width_cm"])))
        page_ok &= _close(str(height), _twips_from_cm(float(page["height_cm"])))
        page_ok &= page_size.get(W + "orient") in {None, "portrait"} and width < height
        section_type = section.find("./w:type", namespaces=NS)
        page_ok &= section_type is None or section_type.get(W + "val") == "nextPage"
        columns = section.find("./w:cols", namespaces=NS)
        grid = section.find("./w:docGrid", namespaces=NS)
        page_ok &= (
            _attributes_exact(columns, {W + "space": "720"})
            and columns is not None
            and len(columns) == 0
            and _attributes_exact(grid, {W + "linePitch": "360"})
            and grid is not None
            and len(grid) == 0
        )
    items.append(
        _item(
            "02_PAGE_PROFILE_EXACT",
            page_ok,
            "two next-page portrait sections exactly match the authoring page profile",
            "section count, break type, size, or orientation differs from authoring_profile.page",
        )
    )

    margin_ok = len(sections) == 2
    for section in sections:
        margins = section.find("./w:pgMar", namespaces=NS)
        if margins is None:
            margin_ok = False
            continue
        for side in ("top", "bottom", "left", "right"):
            margin_ok &= _close(
                margins.get(W + side),
                _twips_from_cm(float(page["margin_cm"][side])),
            )
        margin_ok &= _close(
            margins.get(W + "header"),
            _twips_from_cm(float(page["header_distance_cm"])),
        )
        margin_ok &= _close(
            margins.get(W + "footer"),
            _twips_from_cm(float(page["footer_distance_cm"])),
        )
        margin_ok &= margins.get(W + "gutter") in {None, "0"}
        margin_ok &= set(margins.attrib) == {
            W + "top",
            W + "right",
            W + "bottom",
            W + "left",
            W + "header",
            W + "footer",
            W + "gutter",
        }
    items.append(
        _item(
            "03_MARGIN_PROFILE_EXACT",
            margin_ok,
            "all margins and header/footer distances exactly match authoring_profile",
            "a margin or story distance differs from authoring_profile.page",
        )
    )

    section_stories, story_errors = _section_story_parts(
        sections, document_rels, names
    )
    settings = parsed.get("word/settings.xml")
    no_first_or_even = bool(sections) and all(
        not _on_off_enabled(section.find("./w:titlePg", namespaces=NS))
        for section in sections
    ) and not _on_off_enabled(
        settings.find("./w:evenAndOddHeaders", namespaces=NS)
        if settings is not None
        else None
    )
    header_contract_ok = len(section_stories) == 2
    header_texts: list[str] = []
    for story in section_stories:
        header_root = parsed.get(story.get("header", ""))
        header_paragraphs = (
            header_root.xpath("./w:p", namespaces=NS) if header_root is not None else []
        )
        if len(header_paragraphs) != 1:
            header_contract_ok = False
            continue
        paragraph = header_paragraphs[0]
        header_text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        header_texts.append(header_text)
        ppr = paragraph.find("./w:pPr", namespaces=NS)
        pstyle = ppr.find("./w:pStyle", namespaces=NS) if ppr is not None else None
        justification = ppr.find("./w:jc", namespaces=NS) if ppr is not None else None
        header_contract_ok &= (
            bool(header_text)
            and pstyle is not None
            and pstyle.get(W + "val") == "CSRHeader"
            and justification is not None
            and justification.get(W + "val") == "center"
            and [etree.QName(child).localname for child in ppr] == ["pStyle", "jc"]
            and _paragraph_runs_match(paragraph, [("text", header_text)])
        )
    header_contract_ok &= len(header_texts) == 2 and len(set(header_texts)) == 1
    stories_ok = (
        len(section_stories) == 2
        and not story_errors
        and no_first_or_even
        and header_contract_ok
    )
    items.append(
        _item(
            "04_SECTION_STORIES_EXACT",
            stories_ok,
            "each section has normalized default stories, centered topic header, and no first/even variants",
            f"story errors={story_errors}; first/even disabled={no_first_or_even}; header contract={header_contract_ok}",
        )
    )

    referenced_story_names = sorted(
        {part for story in section_stories for part in story.values()}
    )
    story_names = ["word/document.xml", *referenced_story_names]
    story_roots = [parsed.get(name) for name in story_names]
    body_footer = (
        parsed.get(section_stories[1].get("footer", ""))
        if len(section_stories) == 2
        else None
    )
    cover_footer = (
        parsed.get(section_stories[0].get("footer", ""))
        if len(section_stories) == 2
        else None
    )
    body_footer_fields = _field_names(body_footer)
    body_footer_instructions = (
        [
            "".join(node.itertext()).strip().upper()
            for node in body_footer.xpath(".//w:instrText", namespaces=NS)
        ]
        if body_footer is not None
        else []
    )
    body_footer_paragraphs = (
        body_footer.xpath("./w:p", namespaces=NS) if body_footer is not None else []
    )
    cover_footer_paragraphs = (
        cover_footer.xpath("./w:p", namespaces=NS) if cover_footer is not None else []
    )
    footer_texts = (
        body_footer_paragraphs[0].xpath(".//w:t/text()", namespaces=NS)
        if len(body_footer_paragraphs) == 1
        else []
    )
    footer_literal_ok = (
        len(footer_texts) == 3
        and footer_texts[0].strip() == "第"
        and footer_texts[1].strip() == "页，共"
        and footer_texts[2].strip().startswith("页")
        and bool(footer_texts[2].strip()[1:].strip())
    )
    cover_footer_blank = (
        len(cover_footer_paragraphs) == 1
        and not "".join(
            cover_footer_paragraphs[0].xpath(".//w:t/text()", namespaces=NS)
        ).strip()
        and not _field_names(cover_footer)
    )
    body_footer_shape_ok = len(body_footer_paragraphs) == 1 and _paragraph_runs_match(
        body_footer_paragraphs[0],
        [
            ("text", "第 "),
            ("field", "begin"),
            ("instruction", " PAGE "),
            ("field", "separate"),
            ("field", "end"),
            ("text", " 页，共 "),
            ("field", "begin"),
            ("instruction", " SECTIONPAGES "),
            ("field", "separate"),
            ("field", "end"),
            ("text", footer_texts[2] if len(footer_texts) == 3 else ""),
        ],
    )
    cover_footer_shape_ok = len(cover_footer_paragraphs) == 1 and (
        _local_children(cover_footer_paragraphs[0]) == ["pPr"]
        and _local_children(cover_footer_paragraphs[0].find("./w:pPr", namespaces=NS))
        == ["pStyle", "jc"]
        and _attributes_exact(
            cover_footer_paragraphs[0].find("./w:pPr/w:pStyle", namespaces=NS),
            {W + "val": "CSRFooter"},
        )
        and _attributes_exact(
            cover_footer_paragraphs[0].find("./w:pPr/w:jc", namespaces=NS),
            {W + "val": "center"},
        )
    )
    document_fields = _field_names(document)
    header_fields = [
        field
        for story in section_stories
        for field in _field_names(parsed.get(story.get("header", "")))
    ]
    all_fields = [field for root in story_roots for field in _field_names(root)]
    page_field_ok = (
        body_footer_fields == ["PAGE", "SECTIONPAGES"]
        and body_footer_instructions == ["PAGE", "SECTIONPAGES"]
        and "PAGE" not in _field_names(cover_footer)
        and all_fields.count("PAGE") == 1
        and all_fields.count("SECTIONPAGES") == 1
        and len(body_footer_paragraphs) == 1
        and body_footer is not None
        and len(body_footer) == 1
        and footer_literal_ok
        and body_footer_shape_ok
        and cover_footer_blank
        and cover_footer_shape_ok
        and cover_footer is not None
        and len(cover_footer) == 1
        and document_fields == ["TOC"]
        and not header_fields
        and all_fields == ["TOC", "PAGE", "SECTIONPAGES"]
    )
    items.append(
        _item(
            "05_BODY_PAGE_FIELDS_EXACT",
            page_field_ok,
            "body footer contains exactly PAGE then SECTIONPAGES; cover has neither",
            f"body footer fields={body_footer_fields}; all fields={all_fields}",
        )
    )

    centered = False
    if body_footer is not None:
        for paragraph in body_footer.xpath(".//w:p", namespaces=NS):
            if "PAGE" not in _field_names(paragraph):
                continue
            justification = paragraph.find("./w:pPr/w:jc", namespaces=NS)
            indentation = paragraph.find("./w:pPr/w:ind", namespaces=NS)
            no_shift = indentation is None or all(
                indentation.get(W + attribute) in {None, "0"}
                for attribute in ("left", "right", "firstLine", "firstLineChars")
            )
            paragraph_properties = paragraph.find("./w:pPr", namespaces=NS)
            paragraph_style = (
                paragraph_properties.find("./w:pStyle", namespaces=NS)
                if paragraph_properties is not None
                else None
            )
            exact_properties = paragraph_properties is not None and [
                etree.QName(child).localname for child in paragraph_properties
            ] == ["pStyle", "jc"]
            if (
                justification is not None
                and justification.get(W + "val") == "center"
                and paragraph_style is not None
                and paragraph_style.get(W + "val") == "CSRFooter"
                and no_shift
                and exact_properties
            ):
                centered = True
    items.append(_item("06_PAGE_NUMBER_CENTER", centered, "PAGE field paragraph is centered", "PAGE field is not in a centered paragraph"))

    restart_ok = len(sections) == 2 and sections[0].find(
        "./w:pgNumType", namespaces=NS
    ) is None and (
        (page_number := sections[1].find("./w:pgNumType", namespaces=NS)) is not None
        and page_number.get(W + "start") == "1"
        and set(page_number.attrib) == {W + "start"}
        and len(page_number) == 0
    )
    items.append(_item("07_PAGE_NUMBER_RESTART", restart_ok, "numbered body restarts at page 1", "no section starts page numbering at 1"))
    toc_instructions = [
        "".join(node.itertext()).strip()
        for node in (document.xpath(".//w:instrText", namespaces=NS) if document is not None else [])
        if "".join(node.itertext()).strip().upper().startswith("TOC ")
    ]
    body_paragraphs = (
        document.xpath(".//w:body/w:p", namespaces=NS)
        if document is not None
        else []
    )
    toc_paragraphs = [
        paragraph
        for paragraph in body_paragraphs
        if _field_names(paragraph) == ["TOC"]
    ]
    section_three_paragraphs = [
        paragraph
        for paragraph in body_paragraphs
        if paragraph.xpath(
            ".//w:bookmarkStart[@w:name='_Ref_Sec_3']", namespaces=NS
        )
    ]
    toc_position_ok = (
        len(toc_paragraphs) == 1
        and len(section_three_paragraphs) == 1
        and body_paragraphs.index(toc_paragraphs[0])
        == body_paragraphs.index(section_three_paragraphs[0]) + 1
    )
    toc_profile = authoring["styles"]["toc"]
    toc_ok = len(toc_instructions) == 1 and toc_position_ok and re.fullmatch(
        r'\bTOC\s+\\o\s+"1-5"\s+\\h\s+\\z\s+\\u\b', toc_instructions[0], re.I
    ) is not None
    if len(toc_paragraphs) == 1:
        toc_ppr = toc_paragraphs[0].find("./w:pPr", namespaces=NS)
        toc_style = (
            toc_ppr.find("./w:pStyle", namespaces=NS) if toc_ppr is not None else None
        )
        toc_spacing = (
            toc_ppr.find("./w:spacing", namespaces=NS) if toc_ppr is not None else None
        )
        toc_children = (
            [etree.QName(child).localname for child in toc_ppr]
            if toc_ppr is not None
            else []
        )
        toc_ok &= (
            toc_style is not None
            and toc_style.get(W + "val") == "CSRTOC"
            and toc_spacing is not None
            and _close(
                toc_spacing.get(W + "line"),
                round(float(toc_profile["line_spacing"]) * 240),
                0,
            )
            and toc_spacing.get(W + "lineRule") == "auto"
            and toc_children == ["pStyle", "spacing"]
            and _paragraph_runs_match(
                toc_paragraphs[0],
                [
                    ("field", "begin"),
                    ("instruction", ' TOC \\o "1-5" \\h \\z \\u '),
                    ("field", "separate"),
                    ("field", "end"),
                ],
            )
        )
    items.append(_item("08_TOC_FIELD_EXACT", toc_ok, "one TOC field covers heading levels 1-5", f"TOC instructions={toc_instructions}"))

    field_roots = [document, body_footer]
    pair_ok = all(_field_pairs_valid(root) for root in field_roots) and all(
        not _field_names(root)
        for root in story_roots
        if root is not None and root not in field_roots
    )
    items.append(_item("09_COMPLEX_FIELD_SEQUENCES", pair_ok, "every generated field has begin/instruction/separate/end", "a generated field sequence is incomplete or malformed"))

    tree = load_chapter_tree(structure_path)
    expected_nodes = flatten_sections(tree.select_layers(layers))
    starts = document.xpath(".//w:bookmarkStart", namespaces=NS) if document is not None else []
    ends = document.xpath(".//w:bookmarkEnd", namespaces=NS) if document is not None else []
    start_ids = [node.get(W + "id") for node in starts]
    end_ids = [node.get(W + "id") for node in ends]
    bookmark_names = [node.get(W + "name") for node in starts]
    expected_bookmark_names = [
        "_Ref_Sec_" + node.id.replace(".", "_").replace("-", "_")
        for node in expected_nodes
    ]
    bookmark_pairs_ok = (
        bool(starts)
        and len(starts) == len(ends) == len(expected_nodes)
        and len(start_ids) == len(set(start_ids))
        and len(end_ids) == len(set(end_ids))
        and set(start_ids) == set(end_ids)
        and bookmark_names == expected_bookmark_names
        and len(bookmark_names) == len(set(bookmark_names))
    )
    items.append(_item("10_BOOKMARK_PAIRS", bookmark_pairs_ok, "bookmark IDs and expected section names are unique and paired", "bookmark IDs/names are missing, duplicated, unexpected, or unpaired"))

    expected_bookmark_texts = [
        node.title(title_language)
        if all(part.isdigit() for part in node.id.split("."))
        else f"{node.id} {node.title(title_language)}"
        for node in expected_nodes
    ]
    nonempty_ok = len(starts) == len(expected_bookmark_texts)
    for start, expected_text in zip(starts, expected_bookmark_texts):
        parent = start.getparent()
        bookmark_id = start.get(W + "id")
        children = list(parent)
        start_index = children.index(start)
        matching_end = next((node for node in children[start_index + 1 :] if node.tag == W + "bookmarkEnd" and node.get(W + "id") == bookmark_id), None)
        if matching_end is None:
            nonempty_ok = False
            continue
        end_index = children.index(matching_end)
        text = "".join("".join(node.itertext()) for node in children[start_index + 1 : end_index])
        nonempty_ok &= text == expected_text
    items.append(_item("11_BOOKMARK_NONEMPTY", nonempty_ok, "every bookmark encloses its complete expected heading text", "a bookmark range is empty, partial, extra, or malformed"))

    heading_paragraphs = (
        document.xpath(
            ".//w:body/w:p[w:pPr/w:pStyle[starts-with(@w:val, 'Heading')]]",
            namespaces=NS,
        )
        if document is not None
        else []
    )

    number_definition_ok = numbering is not None and document is not None
    number_refs_ok = numbering is not None and document is not None
    if numbering is not None:
        child_names = [etree.QName(child).localname for child in numbering]
        last_abstract = max((i for i, name in enumerate(child_names) if name == "abstractNum"), default=-1)
        first_num = min((i for i, name in enumerate(child_names) if name == "num"), default=10**9)
        number_definition_ok &= last_abstract < first_num
        abstract_ids = {node.get(W + "abstractNumId") for node in numbering.findall(W + "abstractNum")}
        number_refs_ok = all(
            (reference := node.find(W + "abstractNumId")) is not None
            and reference.get(W + "val") in abstract_ids
            for node in numbering.findall(W + "num")
        )
        referenced_num_ids = {
            node.get(W + "val")
            for paragraph in heading_paragraphs
            for node in paragraph.xpath("./w:pPr/w:numPr/w:numId", namespaces=NS)
        }
        defined_num_ids = {node.get(W + "numId") for node in numbering.findall(W + "num")}
        number_refs_ok &= len(referenced_num_ids) == 1 and referenced_num_ids <= defined_num_ids

        if len(referenced_num_ids) == 1:
            num_id = next(iter(referenced_num_ids))
            concrete = numbering.find(f"./w:num[@w:numId='{num_id}']", namespaces=NS)
            abstract_reference = (
                concrete.find("./w:abstractNumId", namespaces=NS)
                if concrete is not None
                else None
            )
            abstract_id = (
                abstract_reference.get(W + "val")
                if abstract_reference is not None
                else None
            )
            abstract = (
                numbering.find(
                    f"./w:abstractNum[@w:abstractNumId='{abstract_id}']",
                    namespaces=NS,
                )
                if abstract_id is not None
                else None
            )
            levels = abstract.findall("./w:lvl", namespaces=NS) if abstract is not None else []
            multilevel = (
                abstract.find("./w:multiLevelType", namespaces=NS)
                if abstract is not None
                else None
            )
            number_definition_ok &= (
                abstract is not None
                and multilevel is not None
                and multilevel.get(W + "val") == "multilevel"
                and len(levels) == 5
                and concrete is not None
                and [etree.QName(child).localname for child in concrete]
                == ["abstractNumId"]
                and [etree.QName(child).localname for child in abstract]
                == ["multiLevelType", "lvl", "lvl", "lvl", "lvl", "lvl"]
            )
            for level_index, level in enumerate(levels):
                level_children = [etree.QName(child).localname for child in level]
                expected_text = ".".join(f"%{part + 1}" for part in range(level_index + 1))
                start = level.find("./w:start", namespaces=NS)
                num_format = level.find("./w:numFmt", namespaces=NS)
                paragraph_style = level.find("./w:pStyle", namespaces=NS)
                suffix = level.find("./w:suff", namespaces=NS)
                level_text = level.find("./w:lvlText", namespaces=NS)
                level_justification = level.find("./w:lvlJc", namespaces=NS)
                level_properties = level.find("./w:pPr", namespaces=NS)
                level_indentation = (
                    level_properties.find("./w:ind", namespaces=NS)
                    if level_properties is not None
                    else None
                )
                number_definition_ok &= (
                    level.get(W + "ilvl") == str(level_index)
                    and set(level.attrib) == {W + "ilvl"}
                    and level_children
                    == ["start", "numFmt", "pStyle", "suff", "lvlText", "lvlJc", "pPr"]
                    and start is not None
                    and start.get(W + "val") == "1"
                    and num_format is not None
                    and num_format.get(W + "val") == "decimal"
                    and paragraph_style is not None
                    and paragraph_style.get(W + "val") == f"Heading{level_index + 1}"
                    and suffix is not None
                    and suffix.get(W + "val") == "space"
                    and level_text is not None
                    and level_text.get(W + "val") == expected_text
                    and level_justification is not None
                    and level_justification.get(W + "val") == "left"
                    and level_properties is not None
                    and [etree.QName(child).localname for child in level_properties]
                    == ["ind"]
                    and level_indentation is not None
                    and all(
                        level_indentation.get(W + attribute) == "0"
                        for attribute in ("left", "firstLine", "hanging")
                    )
                )
        else:
            number_definition_ok = False
    items.append(
        _item(
            "12_NUMBERING_DEFINITION_EXACT",
            number_definition_ok,
            "referenced multilevel numbering has exact five-level definitions",
            "numbering order, level definitions, or lvlText differ from the five-level contract",
        )
    )
    items.append(_item("13_NUMBERING_REFERENCES", number_refs_ok, "one heading numId resolves without dangling references", "dangling, absent, or multiple heading numbering references"))

    required_style_ids = [
        "Normal",
        "Heading1",
        "Heading2",
        "Heading3",
        "Heading4",
        "Heading5",
        "CSRTOC",
        "CSRHeader",
        "CSRFooter",
    ]
    style_props = {style_id: _style_properties(styles, style_id) for style_id in required_style_ids} if styles is not None else {}
    fonts = authoring["fonts"]
    body_style_ids = {"Normal", "CSRTOC", "CSRHeader", "CSRFooter"}
    heading_style_ids = {f"Heading{level}" for level in range(1, 6)}
    east_asia_ok = bool(style_props) and all(
        style_props[style_id]["eastAsia"] == fonts["chinese_body"]
        and style_props[style_id]["eastAsiaTheme"] is None
        for style_id in body_style_ids
    ) and all(
        style_props[style_id]["eastAsia"] == fonts["chinese_heading"]
        and style_props[style_id]["eastAsiaTheme"] is None
        for style_id in heading_style_ids
    )
    latin_ok = bool(style_props) and all(
        props["ascii"] == fonts["latin"]
        and props["hAnsi"] == fonts["latin"]
        and props["cs"] == fonts["latin"]
        and props["asciiTheme"] is None
        and props["hAnsiTheme"] is None
        and props["csTheme"] is None
        for props in style_props.values()
    )
    expected_color = str(fonts["color"]).upper()
    color_ok = bool(style_props) and all(
        (props["color"] or "").upper() == expected_color
        and props["themeColor"] is None
        and props["themeTint"] is None
        and props["themeShade"] is None
        and props["type"] == "paragraph"
        for props in style_props.values()
    )
    style_xml_contract_ok = _required_style_xml_contract(styles, authoring)
    items.append(_item("14_STYLE_CHINESE_FONT_EXACT", east_asia_ok, "all required styles match authoring_profile Chinese fonts", f"style properties={style_props}"))
    items.append(_item("15_STYLE_LATIN_FONT_EXACT", latin_ok, "all required styles match authoring_profile Latin font", f"style properties={style_props}"))
    items.append(_item("16_STYLE_FONT_COLOR_EXACT", color_ok, "all required styles match authoring_profile black", f"style properties={style_props}"))

    styles_cfg = authoring["styles"]
    expected_sizes = {
        "Normal": float(styles_cfg["body"]["size_pt"]),
        "CSRTOC": float(styles_cfg["toc"]["size_pt"]),
        "CSRHeader": float(styles_cfg["header_footer"]["size_pt"]),
        "CSRFooter": float(styles_cfg["header_footer"]["size_pt"]),
        **{
            f"Heading{level}": float(styles_cfg[f"heading{level}"]["size_pt"])
            for level in range(1, 6)
        },
    }
    size_ok = bool(style_props) and all(
        _close(style_props[style_id]["size"], round(points * 2), tolerance=0)
        for style_id, points in expected_sizes.items()
    )
    size_ok &= expected_sizes["Normal"] >= float(official["body_size_min_pt"])
    size_ok &= expected_sizes["Heading1"] >= float(official["cover_size_min_pt"])
    items.append(_item("17_STYLE_SIZE_PROFILE_EXACT", size_ok, "all required style sizes exactly match authoring_profile and official minima", f"style properties={style_props}"))

    paragraph_profile_ok = bool(style_props) and style_xml_contract_ok
    body_cfg = styles_cfg["body"]
    paragraph_profile_ok &= (
        _close(style_props["Normal"]["line"], round(float(body_cfg["line_spacing"]) * 240), 0)
        and style_props["Normal"]["lineRule"] == "auto"
        and _close(style_props["Normal"]["after"], _twips_from_points(float(body_cfg["space_after_pt"])), 0)
        and style_props["Normal"]["firstLineChars"] == str(int(body_cfg["first_line_indent_chars"]) * 100)
    )
    toc_cfg = styles_cfg["toc"]
    paragraph_profile_ok &= (
        _close(style_props["CSRTOC"]["line"], round(float(toc_cfg["line_spacing"]) * 240), 0)
        and style_props["CSRTOC"]["firstLineChars"] == str(int(toc_cfg["first_line_indent_chars"]) * 100)
    )
    story_cfg = styles_cfg["header_footer"]
    for style_id in ("CSRHeader", "CSRFooter"):
        paragraph_profile_ok &= (
            _close(style_props[style_id]["line"], round(float(story_cfg["line_spacing"]) * 240), 0)
            and _close(style_props[style_id]["before"], _twips_from_points(float(story_cfg["space_before_pt"])), 0)
            and _close(style_props[style_id]["after"], _twips_from_points(float(story_cfg["space_after_pt"])), 0)
            and style_props[style_id]["firstLineChars"] == str(int(story_cfg["first_line_indent_chars"]) * 100)
        )
    for level in range(1, 6):
        cfg = styles_cfg[f"heading{level}"]
        props = style_props[f"Heading{level}"]
        paragraph_profile_ok &= (
            _close(props["before"], _twips_from_points(float(cfg["space_before_pt"])), 0)
            and _close(props["after"], _twips_from_points(float(cfg["space_after_pt"])), 0)
            and props["firstLineChars"] == "0"
            and props["bold"] not in {None, "0", "false", "off"}
            and props["keepNext"] not in {None, "0", "false", "off"}
            and props["outlineLvl"] == str(level - 1)
        )
    items.append(_item("18_PARAGRAPH_PROFILE_EXACT", paragraph_profile_ok, "body, TOC, story, and Heading 1-5 paragraph/run properties match the exact authoring profile", f"style properties={style_props}; XML contract={style_xml_contract_ok}"))

    story_inline_formatting = sum(
        len(root.xpath(".//w:r/w:rPr", namespaces=NS))
        for root in story_roots
        if root is not None
    )
    style_decouple_ok = story_inline_formatting == 0
    items.append(
        _item(
            "19_STYLE_DECOUPLE",
            style_decouple_ok,
            "0 inline run-format overrides across generated stories",
            f"inline rPr={story_inline_formatting}",
        )
    )

    expected_titles = []
    for node in expected_nodes:
        title = node.title(title_language)
        expected_titles.append(title if all(part.isdigit() for part in node.id.split(".")) else f"{node.id} {title}")
    actual_titles: list[str] = []
    heading_contract_ok = len(heading_paragraphs) == len(expected_nodes)
    for paragraph, node in zip(heading_paragraphs, expected_nodes):
        expected_title = (
            node.title(title_language)
            if all(part.isdigit() for part in node.id.split("."))
            else f"{node.id} {node.title(title_language)}"
        )
        actual_titles.append("".join(paragraph.xpath(".//w:t/text()", namespaces=NS)))
        paragraph_properties = paragraph.find("./w:pPr", namespaces=NS)
        style_node = paragraph.find("./w:pPr/w:pStyle", namespaces=NS)
        heading_contract_ok &= (
            style_node is not None and style_node.get(W + "val") == f"Heading{node.level}"
        )
        num_pr = paragraph.find("./w:pPr/w:numPr", namespaces=NS)
        numeric_id = all(part.isdigit() for part in node.id.split("."))
        expected_ppr_children = ["pStyle"]
        if node.level == 1 and node.id not in {"1", "2"}:
            expected_ppr_children.append("pageBreakBefore")
        if numeric_id:
            expected_ppr_children.append("numPr")
        heading_contract_ok &= paragraph_properties is not None and [
            etree.QName(child).localname for child in paragraph_properties
        ] == expected_ppr_children
        bookmark_start = paragraph.find("./w:bookmarkStart", namespaces=NS)
        bookmark_end = paragraph.find("./w:bookmarkEnd", namespaces=NS)
        runs = paragraph.findall("./w:r", namespaces=NS)
        expected_bookmark_id = str(len(actual_titles))
        expected_bookmark_name = "_Ref_Sec_" + node.id.replace(".", "_").replace("-", "_")
        heading_contract_ok &= (
            _local_children(paragraph) == ["pPr", "bookmarkStart", "r", "bookmarkEnd"]
            and len(runs) == 1
            and _run_matches(runs[0], "text", expected_title)
            and _attributes_exact(
                bookmark_start,
                {W + "id": expected_bookmark_id, W + "name": expected_bookmark_name},
            )
            and _attributes_exact(bookmark_end, {W + "id": expected_bookmark_id})
        )
        if numeric_id:
            ilvl = num_pr.find("./w:ilvl", namespaces=NS) if num_pr is not None else None
            num_id_node = num_pr.find("./w:numId", namespaces=NS) if num_pr is not None else None
            heading_contract_ok &= (
                ilvl is not None
                and ilvl.get(W + "val") == str(node.level - 1)
                and num_id_node is not None
            )
        else:
            heading_contract_ok &= num_pr is None

    nonempty_body_texts = (
        [
            "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))
            for paragraph in document.xpath(".//w:body/w:p", namespaces=NS)
            if "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        ]
        if document is not None
        else []
    )
    heading_ok = (
        heading_contract_ok
        and actual_titles == expected_titles
        and nonempty_body_texts == expected_titles
    )
    items.append(
        _item(
            "20_HEADING_TREE_AND_LEVELS",
            heading_ok,
            f"title-only body exactly matches {len(expected_titles)} YAML nodes and levels",
            f"expected {len(expected_titles)} headings; got {len(actual_titles)} or mismatched style/numbering/body text",
        )
    )

    all_story_parts = [
        parsed.get(name)
        for name in parsed
        if name == "word/document.xml"
        or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
    ]
    generated_text = "".join(
        root.xpath("string(.)") for root in all_story_parts if root is not None
    )
    placeholder_ok = not re.search(r"[{}]|⟦缺失:", generated_text)
    items.append(_item("21_PLACEHOLDER_RESIDUE_ALL_STORIES", placeholder_ok, "no unresolved or malformed placeholder markers in any generated story", "unresolved placeholder, brace residue, or missing marker remains"))

    if archive is not None:
        archive.close()
    if len(items) != 21 or len({item.code for item in items}) != 21:
        raise AssertionError("QC contract must always emit 21 unique checks")
    return QCReport(tuple(items))


def run_qc_to_file(
    docx_path: str | Path,
    format_spec_path: str | Path,
    structure_path: str | Path,
    layers: Iterable[str],
    title_language: str,
    report_path: str | Path | None = None,
) -> QCReport:
    if report_path is not None:
        resolved_report = Path(report_path).resolve()
        for role, source in (
            ("DOCX", docx_path),
            ("format spec", format_spec_path),
            ("chapter tree", structure_path),
        ):
            if paths_alias(resolved_report, source):
                raise ValueError(f"QC report path must not overwrite {role}: {resolved_report}")
    report = run_qc(docx_path, format_spec_path, structure_path, layers, title_language)
    if report_path is not None:
        atomic_write_json(report_path, report.as_dict())
    return report
