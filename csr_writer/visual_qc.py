"""Optional LibreOffice/PyMuPDF visual preflight for composed CSR DOCX files."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import re
import shutil
import subprocess
import zipfile
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping, Sequence

from lxml import etree

from .errors import CSRWriterError, ConfigurationError
from .io_utils import (
    atomic_write_json,
    atomic_write_text,
    load_json_unique,
    load_yaml,
    paths_alias,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VISUAL_QC_CONFIG = (
    ROOT / "skills" / "csr-writer" / "references" / "visual-qc-config.yaml"
)
DEFAULT_VISUAL_QC_LOCK = (
    ROOT / "skills" / "csr-writer" / "references" / "visual-qc-cjk.lock.json"
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
W = f"{{{W_NS}}}"
R = f"{{{R_NS}}}"
NS = {"w": W_NS, "r": R_NS}


class VisualQCError(CSRWriterError):
    """A visual preflight input or rendering contract is invalid."""


@dataclass(frozen=True)
class SectionLayout:
    section_index: int
    page_width_pt: float
    page_height_pt: float
    body_left_pt: float
    body_top_pt: float
    body_right_pt: float
    body_bottom_pt: float
    header_story_keys: tuple[str, ...]
    footer_story_keys: tuple[str, ...]

    @property
    def orientation(self) -> str:
        return "landscape" if self.page_width_pt > self.page_height_pt else "portrait"


@dataclass(frozen=True)
class PageBlock:
    text: str
    bbox: tuple[float, float, float, float]
    lines: tuple[str, ...]
    max_font_size: float
    bold: bool


def _visible_word_text(node: etree._Element) -> str:
    return "".join(item.text or "" for item in node.iter(W + "t"))


def _normalise(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _story_key(value: str) -> str:
    return re.sub(r"[\W\d_]+", "", value or "", flags=re.UNICODE).casefold()


def _twips(node: etree._Element | None, attribute: str, default: int) -> int:
    if node is None:
        return default
    raw = node.get(W + attribute)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _relationship_targets(payload: bytes) -> dict[str, str]:
    root = etree.fromstring(payload)
    return {
        item.get("Id", ""): item.get("Target", "")
        for item in root.findall(f"{{{PR_NS}}}Relationship")
    }


def _story_payload(package: zipfile.ZipFile, target: str) -> bytes | None:
    candidate = (Path("word") / target).as_posix()
    try:
        return package.read(candidate)
    except KeyError:
        return None


def _field_instructions(package: zipfile.ZipFile) -> list[str]:
    instructions: list[str] = []
    for name in package.namelist():
        if not name.startswith("word/") or not name.endswith(".xml"):
            continue
        root = etree.fromstring(package.read(name))
        instructions.extend(
            (node.text or "").strip()
            for node in root.iter(W + "instrText")
            if (node.text or "").strip()
        )
    return instructions


def _docx_layout(docx_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(docx_path) as package:
        document = etree.fromstring(package.read("word/document.xml"))
        relationships = _relationship_targets(
            package.read("word/_rels/document.xml.rels")
        )
        instructions = _field_instructions(package)
        story_cache: dict[str, str] = {}

        previous = {
            "width": 11906,
            "height": 16838,
            "top": 1440,
            "right": 1440,
            "bottom": 1440,
            "left": 1440,
        }
        previous_header_keys: tuple[str, ...] = ()
        previous_footer_keys: tuple[str, ...] = ()
        layouts: list[SectionLayout] = []
        for index, section in enumerate(document.xpath(".//w:sectPr", namespaces=NS)):
            page_size = section.find(W + "pgSz")
            page_margin = section.find(W + "pgMar")
            values = {
                "width": _twips(page_size, "w", previous["width"]),
                "height": _twips(page_size, "h", previous["height"]),
                "top": _twips(page_margin, "top", previous["top"]),
                "right": _twips(page_margin, "right", previous["right"]),
                "bottom": _twips(page_margin, "bottom", previous["bottom"]),
                "left": _twips(page_margin, "left", previous["left"]),
            }
            previous = values
            header_keys: list[str] = []
            footer_keys: list[str] = []
            for kind, destination in (
                ("headerReference", header_keys),
                ("footerReference", footer_keys),
            ):
                for reference in section.findall(W + kind):
                    target = relationships.get(reference.get(R + "id", ""), "")
                    if not target:
                        continue
                    if target not in story_cache:
                        payload = _story_payload(package, target)
                        story_cache[target] = (
                            _visible_word_text(etree.fromstring(payload))
                            if payload is not None
                            else ""
                        )
                    key = _story_key(story_cache[target])
                    if key:
                        destination.append(key)
            if not header_keys:
                header_keys.extend(previous_header_keys)
            if not footer_keys:
                footer_keys.extend(previous_footer_keys)
            previous_header_keys = tuple(sorted(set(header_keys)))
            previous_footer_keys = tuple(sorted(set(footer_keys)))
            width_pt = values["width"] / 20.0
            height_pt = values["height"] / 20.0
            layouts.append(
                SectionLayout(
                    section_index=index,
                    page_width_pt=width_pt,
                    page_height_pt=height_pt,
                    body_left_pt=values["left"] / 20.0,
                    body_top_pt=values["top"] / 20.0,
                    body_right_pt=width_pt - values["right"] / 20.0,
                    body_bottom_pt=height_pt - values["bottom"] / 20.0,
                    header_story_keys=previous_header_keys,
                    footer_story_keys=previous_footer_keys,
                )
            )

        paragraph_styles = [
            item.get(W + "val", "")
            for item in document.xpath(".//w:pPr/w:pStyle", namespaces=NS)
        ]
        heading_count = sum(value.startswith("Heading") for value in paragraph_styles)
        seq_table_count = sum(
            instruction.upper().startswith("SEQ TABLE") for instruction in instructions
        )
        seq_figure_count = sum(
            instruction.upper().startswith("SEQ FIGURE") for instruction in instructions
        )
        tc_counts: dict[str, int] = {}
        for instruction in instructions:
            if not instruction.upper().startswith("TC "):
                continue
            match = re.search(r'\\f\s+"([^"]+)"', instruction, flags=re.IGNORECASE)
            if match:
                key = match.group(1)
                tc_counts[key] = tc_counts.get(key, 0) + 1
        toc_instructions = [
            instruction for instruction in instructions if instruction.upper().startswith("TOC ")
        ]
        toc_expected_counts: list[int] = []
        for instruction in toc_instructions:
            category = re.search(
                r'\\c\s+"([^"]+)"', instruction, flags=re.IGNORECASE
            )
            field = re.search(r'\\f\s+"([^"]+)"', instruction, flags=re.IGNORECASE)
            if category:
                name = category.group(1).casefold()
                toc_expected_counts.append(
                    seq_table_count if name == "table" else seq_figure_count
                )
            elif field:
                toc_expected_counts.append(tc_counts.get(field.group(1), 0))
            else:
                toc_expected_counts.append(heading_count)

    if not layouts:
        raise VisualQCError("E-VISUAL-DOCX-SECTIONS: no section properties found")
    return {
        "layouts": layouts,
        "field_counts": {
            "toc": len(toc_instructions),
            "page": sum(item.strip().upper() == "PAGE" for item in instructions),
            "total_pages": sum(
                item.strip().upper() in {"NUMPAGES", "SECTIONPAGES"}
                for item in instructions
            ),
            "ref": sum(item.upper().startswith("REF ") for item in instructions),
            "tc": sum(item.upper().startswith("TC ") for item in instructions),
        },
        "toc_expected_counts": toc_expected_counts,
    }


def _load_configuration(config_path: Path, lock_path: Path | None) -> tuple[dict[str, Any], str]:
    config = load_yaml(config_path)
    if not isinstance(config, Mapping) or config.get("schema_version") != "1.0":
        raise ConfigurationError("E-VISUAL-CONFIG: unsupported visual QC configuration")
    config_hash = sha256_file(config_path)
    if lock_path is not None:
        lock = load_json_unique(lock_path)
        if lock.get("config_sha256") != config_hash:
            raise ConfigurationError(
                "E-VISUAL-CONFIG-DRIFT: visual QC configuration differs from frozen hash"
            )
        if lock.get("profile_id") != config.get("profile_id"):
            raise ConfigurationError("E-VISUAL-CONFIG-PROFILE: lock/profile mismatch")
        checker_hash = lock.get("checker_sha256")
        if checker_hash and checker_hash != sha256_file(Path(__file__)):
            raise ConfigurationError(
                "E-VISUAL-CHECKER-DRIFT: visual QC implementation differs from frozen hash"
            )
    return dict(config), config_hash


def _unsafe_work_directory(path: Path, forbidden_roots: Sequence[str]) -> bool:
    resolved = path.resolve(strict=False)
    for root in forbidden_roots:
        candidate = Path(root).resolve(strict=False)
        try:
            resolved.relative_to(candidate)
            return True
        except ValueError:
            continue
    return False


def detect_renderer(
    candidates: Sequence[str], renderer_override: str | Path | None = None
) -> Path | None:
    requested = [str(renderer_override)] if renderer_override is not None else list(candidates)
    for candidate in requested:
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return path
        resolved = shutil.which(candidate)
        if resolved:
            return Path(resolved)
    return None


def _optional_visual_dependencies() -> tuple[Any | None, Any | None, str | None]:
    try:
        fitz = importlib.import_module("fitz")
    except ImportError:
        return None, None, "PyMuPDF (fitz)"
    try:
        image_module = importlib.import_module("PIL.Image")
    except ImportError:
        return fitz, None, "Pillow"
    return fitz, image_module, None


def _not_run_report(
    *,
    docx_path: Path,
    config_hash: str,
    limitation: str,
    reason_code: str,
    reason: str,
    renderer: Path | None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "not_run",
        "visual_gate_status": "not_run",
        "legacy_release_decision_changed": False,
        "limitations": limitation,
        "source": {
            "filename": docx_path.name,
            "sha256": sha256_file(docx_path),
        },
        "config_sha256": config_hash,
        "renderer": {
            "available": renderer is not None,
            "name": renderer.name if renderer is not None else None,
        },
        "reason": {"code": reason_code, "message": reason},
        "summary": {"class_a_count": 0, "class_b_count": 0},
        "findings": [],
        "contact_sheets": [],
    }


def _render_pdf(
    *,
    renderer: Path,
    docx_path: Path,
    work_dir: Path,
    timeout_seconds: int,
    signature: str,
) -> tuple[Path | None, dict[str, Any]]:
    render_dir = work_dir / "rendered"
    profile_dir = work_dir / "libreoffice-profile"
    render_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    # macOS headless LibreOffice can omit all system CJK glyphs in an isolated profile.
    # Register installed fonts in this render profile only.
    for font in (Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
                 Path("/System/Library/Fonts/STHeiti Medium.ttc")):
        if font.is_file():
            fonts_dir = profile_dir / "user" / "fonts"
            fonts_dir.mkdir(parents=True, exist_ok=True)
            target = fonts_dir / font.name
            if not target.exists() and not target.is_symlink():
                target.symlink_to(font)
    pdf_path = render_dir / f"{docx_path.stem}.pdf"
    pdf_path.unlink(missing_ok=True)
    command = [
        str(renderer),
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--headless",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(render_dir.resolve()),
        str(docx_path.resolve()),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=work_dir,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, {
            "status": "not_run",
            "code": "E-VISUAL-RENDER-TIMEOUT",
            "message": "LibreOffice rendering timed out",
        }
    if completed.returncode != 0 or not pdf_path.is_file():
        return None, {
            "status": "not_run",
            "code": "E-VISUAL-RENDER-FAILED",
            "message": f"LibreOffice rendering failed with exit code {completed.returncode}",
        }
    observed = pdf_path.read_bytes()[: len(signature.encode("ascii"))]
    if observed != signature.encode("ascii"):
        return None, {
            "status": "not_run",
            "code": "E-VISUAL-PDF-SIGNATURE",
            "message": "Rendered derivative does not have a valid PDF signature",
        }
    return pdf_path, {
        "status": "pass",
        "code": None,
        "message": "PDF rendered and signature verified",
    }


def _page_blocks(page: Any) -> list[PageBlock]:
    result: list[PageBlock] = []
    payload = page.get_text("dict")
    for raw in payload.get("blocks", []):
        if raw.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        bold = False
        for line in raw.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append(text.strip())
            for span in line.get("spans", []):
                sizes.append(float(span.get("size", 0.0)))
                font = str(span.get("font", "")).casefold()
                bold = bold or "bold" in font or "black" in font
        text = "\n".join(lines).strip()
        if not text:
            continue
        bbox = tuple(float(item) for item in raw.get("bbox", (0, 0, 0, 0)))
        result.append(
            PageBlock(
                text=text,
                bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
                lines=tuple(lines),
                max_font_size=max(sizes, default=0.0),
                bold=bold,
            )
        )
    return sorted(result, key=lambda item: (item.bbox[1], item.bbox[0]))


def _layout_for_page(layouts: Sequence[SectionLayout], width: float, height: float) -> SectionLayout:
    orientation = "landscape" if width > height else "portrait"
    candidates = [item for item in layouts if item.orientation == orientation]
    if not candidates:
        candidates = list(layouts)
    closest = min(
        candidates,
        key=lambda item: abs(item.page_width_pt - width) + abs(item.page_height_pt - height),
    )
    same_geometry = [
        item
        for item in candidates
        if abs(item.page_width_pt - closest.page_width_pt) <= 3
        and abs(item.page_height_pt - closest.page_height_pt) <= 3
    ]
    if len(same_geometry) == 1:
        return closest
    return SectionLayout(
        section_index=-1,
        page_width_pt=closest.page_width_pt,
        page_height_pt=closest.page_height_pt,
        body_left_pt=min(item.body_left_pt for item in same_geometry),
        body_top_pt=min(item.body_top_pt for item in same_geometry),
        body_right_pt=max(item.body_right_pt for item in same_geometry),
        body_bottom_pt=max(item.body_bottom_pt for item in same_geometry),
        header_story_keys=tuple(
            sorted({key for item in same_geometry for key in item.header_story_keys})
        ),
        footer_story_keys=tuple(
            sorted({key for item in same_geometry for key in item.footer_story_keys})
        ),
    )


def _is_story(block: PageBlock, layout: SectionLayout, minimum: int) -> bool:
    key = _story_key(block.text)
    if len(key) < minimum:
        return False
    stories = (*layout.header_story_keys, *layout.footer_story_keys)
    return any(key in story or story in key for story in stories if len(story) >= minimum)


def _finding(
    code: str,
    category: str,
    page: int | None,
    message: str,
    *,
    evidence: str | None = None,
    bbox: Sequence[float] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "class": category,
        "severity": "block" if category == "A" else "warning",
        "page": page,
        "message": message,
        "evidence": evidence,
        "bbox": [round(float(value), 2) for value in bbox] if bbox else None,
    }


def _page_number_map(page_texts: Sequence[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    for physical, text in enumerate(page_texts, start=1):
        match = re.search(r"第\s*(\d+)\s*页", text)
        if match:
            result[int(match.group(1))] = physical
    return result


def _directory_findings(
    *,
    global_lines: Sequence[tuple[int, str]],
    directory_headings: Sequence[str],
    expected_counts: Sequence[int],
    entry_pattern: str,
    page_texts: Sequence[str],
    title_prefix_characters: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    heading_positions: list[tuple[int, int, str]] = []
    for heading in directory_headings:
        normal = _normalise(heading)
        position = next(
            (
                (index, page, line)
                for index, (page, line) in enumerate(global_lines)
                if normal in _normalise(line)
            ),
            None,
        )
        if position is not None:
            heading_positions.append((position[0], position[1], heading))
    heading_positions.sort()
    page_labels = _page_number_map(page_texts)
    entry_regex = re.compile(entry_pattern)
    for heading_index, (position, page, heading) in enumerate(heading_positions):
        original_index = list(directory_headings).index(heading)
        expected = expected_counts[original_index] if original_index < len(expected_counts) else 0
        end = (
            heading_positions[heading_index + 1][0]
            if heading_index + 1 < len(heading_positions)
            else len(global_lines)
        )
        segment = [line for _, line in global_lines[position + 1 : end]]
        entries = [line for line in segment if entry_regex.search(line.strip())]
        if expected > 0 and not entries:
            findings.append(
                _finding(
                    "A-FIELD-DIRECTORY-NOT-EXPANDED",
                    "A",
                    page,
                    f"{heading} 预期至少 {expected} 项，但渲染目录区没有可见条目。",
                    evidence=heading,
                )
            )
            continue
        for entry in entries:
            match = entry_regex.search(entry.strip())
            if match is None:
                continue
            page_token = match.group(0)
            number_match = re.search(r"(\d+)\s*$", page_token)
            if number_match is None:
                continue
            label = int(number_match.group(1))
            target = page_labels.get(label)
            if target is None:
                findings.append(
                    _finding(
                        "B-TOC-PAGE-LABEL-NOT-FOUND",
                        "B",
                        page,
                        f"目录页码 {label} 在正文可见页码中不存在。",
                        evidence=entry,
                    )
                )
                continue
            title = entry[: match.start()].strip(" .…\t")
            prefix = _normalise(title)[:title_prefix_characters]
            if prefix and prefix not in _normalise(page_texts[target - 1]):
                findings.append(
                    _finding(
                        "B-TOC-PAGE-MISMATCH",
                        "B",
                        page,
                        f"目录条目指向可见页码 {label}，目标页未找到标题前缀。",
                        evidence=entry,
                    )
                )
    return findings


def _table_header(table: Any) -> str:
    try:
        names = getattr(table.header, "names", None)
        if names:
            return "|".join(_normalise(str(item or "")) for item in names)
        rows = table.extract()
        if rows:
            return "|".join(_normalise(str(item or "")) for item in rows[0])
    except Exception:
        return ""
    return ""


def _table_continuation_findings(
    pages: Sequence[Any], layouts: Sequence[SectionLayout], config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    tolerance = float(config["page_edge_tolerance_pt"])
    similarity_min = float(config["header_similarity_min"])
    tables_by_page: list[list[Any]] = []
    for page in pages:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                tables_by_page.append(list(page.find_tables().tables))
        except Exception:
            tables_by_page.append([])
    for index in range(len(pages) - 1):
        current_page, next_page = pages[index], pages[index + 1]
        current_layout = _layout_for_page(
            layouts, current_page.rect.width, current_page.rect.height
        )
        next_layout = _layout_for_page(layouts, next_page.rect.width, next_page.rect.height)
        endings = [
            table
            for table in tables_by_page[index]
            if float(table.bbox[3]) >= current_layout.body_bottom_pt - tolerance
        ]
        beginnings = [
            table
            for table in tables_by_page[index + 1]
            if float(table.bbox[1]) <= next_layout.body_top_pt + tolerance
        ]
        if not endings or not beginnings:
            continue
        before = _table_header(endings[-1])
        after = _table_header(beginnings[0])
        similarity = SequenceMatcher(None, before, after).ratio() if before and after else 0.0
        if similarity < similarity_min:
            findings.append(
                _finding(
                    "B-TABLE-HEADER-NOT-REPEATED",
                    "B",
                    index + 2,
                    "检测到疑似跨页表延续，但下一页未识别到相同表头。",
                    evidence=f"header_similarity={similarity:.3f}",
                )
            )
    return findings


def _missing_cjk_glyph_fonts(page):
    """Compare searchable Unicode with the glyphs used to paint it, per font."""
    expected, painted = defaultdict(set), defaultdict(set)
    for block in page.get_text("dict", flags=0)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                expected[span["font"]].update(re.findall(r"[\u4e00-\u9fff]", span["text"]))
    suspect = {name for name, symbols in expected.items() if len(symbols) >= 6}
    if not suspect:
        return []
    for span in page.get_texttrace():
        if span["font"] in suspect:
            painted[span["font"]].update(g for code, g, *_ in span["chars"] if 0x4e00 <= code <= 0x9fff)
    return sorted(name for name in suspect if len(painted[name]) <= 1)


def _analyse_pdf(
    *,
    pdf_path: Path,
    docx_metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    fitz: Any,
) -> tuple[list[dict[str, Any]], Any, list[str]]:
    document = fitz.open(pdf_path)
    pages = [document[index] for index in range(document.page_count)]
    layouts: Sequence[SectionLayout] = docx_metadata["layouts"]
    findings: list[dict[str, Any]] = []
    page_texts: list[str] = []
    global_lines: list[tuple[int, str]] = []
    page_blocks: list[list[PageBlock]] = []
    class_a = config["class_a"]
    class_b = config["class_b"]
    a4 = class_a["a4"]
    size_pairs = (
        (float(a4["portrait_width_pt"]), float(a4["portrait_height_pt"])),
        (float(a4["portrait_height_pt"]), float(a4["portrait_width_pt"])),
    )
    tolerance = float(a4["tolerance_pt"])
    overflow = class_b["body_overflow"]
    overflow_tolerance = float(overflow["tolerance_pt"])
    story_min = int(overflow["story_match_min_characters"])
    orphan = class_b["orphan_lines"]

    for page_number, page in enumerate(pages, start=1):
        text = page.get_text("text")
        page_texts.append(text)
        for font in _missing_cjk_glyph_fonts(page):
            findings.append(_finding("A-CJK-MISSING-GLYPHS", "A", page_number,
                                     "PDF 可检索的多种汉字只对应至多一个渲染字形。", evidence=font))
        blocks = _page_blocks(page)
        page_blocks.append(blocks)
        for block in blocks:
            global_lines.extend((page_number, line) for line in block.lines)
        if not any(
            abs(page.rect.width - width) <= tolerance
            and abs(page.rect.height - height) <= tolerance
            for width, height in size_pairs
        ):
            findings.append(
                _finding(
                    "A-PAGE-SIZE-NOT-A4",
                    "A",
                    page_number,
                    "渲染页面尺寸不是允许误差内的 A4。",
                    evidence=f"{page.rect.width:.2f} x {page.rect.height:.2f} pt",
                )
            )
        layout = _layout_for_page(layouts, page.rect.width, page.rect.height)
        body_blocks: list[PageBlock] = []
        for block in blocks:
            if _is_story(block, layout, story_min):
                continue
            left, top, right, bottom = block.bbox
            midpoint_y = (top + bottom) / 2
            if layout.body_top_pt <= midpoint_y <= layout.body_bottom_pt:
                body_blocks.append(block)
            if (
                left < layout.body_left_pt - overflow_tolerance
                or right > layout.body_right_pt + overflow_tolerance
                or top < layout.body_top_pt - overflow_tolerance
                or bottom > layout.body_bottom_pt + overflow_tolerance
            ):
                findings.append(
                    _finding(
                        "B-BODY-OVERFLOW",
                        "B",
                        page_number,
                        "非页眉页脚文本块越出当前节正文带。",
                        evidence=block.text[:160],
                        bbox=block.bbox,
                    )
                )
        body_characters = len(_normalise("\n".join(item.text for item in body_blocks)))
        if body_characters < int(class_a["blank_page"]["minimum_body_characters"]):
            findings.append(
                _finding(
                    "A-BLANK-PAGE",
                    "A",
                    page_number,
                    "正文带内没有可见文本。",
                )
            )
        if body_blocks:
            last = body_blocks[-1]
            bottom_gap = layout.body_bottom_pt - last.bbox[3]
            heading_text = _normalise(last.text)
            if (
                bottom_gap <= float(orphan["heading_bottom_gap_pt"])
                and re.match(r"^\d+(?:\.\d+)*[^\d]", heading_text)
                and (last.bold or last.max_font_size >= 12.0)
            ):
                findings.append(
                    _finding(
                        "B-HEADING-ORPHAN",
                        "B",
                        page_number,
                        "标题位于页尾，页内没有后续正文。",
                        evidence=last.text,
                        bbox=last.bbox,
                    )
                )
            for block in body_blocks:
                for line in block.lines:
                    cleaned = re.sub(r"[\s\W\d_]+", "", line, flags=re.UNICODE)
                    cjk = re.findall(r"[\u3400-\u9fff]", cleaned)
                    if (
                        0 < len(cjk) <= int(orphan["single_cjk_line_max_characters"])
                        and len(cleaned) <= int(orphan["single_cjk_line_max_characters"])
                    ):
                        findings.append(
                            _finding(
                                "B-SINGLE-CJK-LINE",
                                "B",
                                page_number,
                                "检测到段末单字成行。",
                                evidence=line,
                                bbox=block.bbox,
                            )
                        )

    all_text = "\n".join(page_texts)
    for pattern in class_a["fields"]["rendered_error_patterns"]:
        for match in re.finditer(re.escape(pattern), all_text, flags=re.IGNORECASE):
            page = next(
                (
                    index + 1
                    for index, text in enumerate(page_texts)
                    if pattern.casefold() in text.casefold()
                ),
                None,
            )
            findings.append(
                _finding(
                    "A-FIELD-RENDERED-ERROR",
                    "A",
                    page,
                    "渲染正文出现交叉引用或域错误文本。",
                    evidence=match.group(0),
                )
            )
            break
    for pattern in class_a["fields"]["empty_page_field_patterns"]:
        regex = re.compile(pattern)
        pages_with_empty = [
            index + 1 for index, text in enumerate(page_texts) if regex.search(text)
        ]
        if pages_with_empty:
            findings.append(
                _finding(
                    "A-FIELD-PAGE-NUMBER-NOT-EXPANDED",
                    "A",
                    pages_with_empty[0],
                    "页码或总页数字段在渲染结果中为空。",
                    evidence=f"pages={pages_with_empty}",
                )
            )
            break
    field_counts = docx_metadata["field_counts"]
    if field_counts["page"] and not any(re.search(r"第\s*\d+\s*页", text) for text in page_texts):
        findings.append(
            _finding(
                "A-FIELD-PAGE-NUMBER-NOT-VISIBLE",
                "A",
                None,
                "DOCX 含 PAGE 域，但渲染结果未发现可见当前页码。",
            )
        )
    findings.extend(
        _directory_findings(
            global_lines=global_lines,
            directory_headings=class_a["fields"]["directory_headings"],
            expected_counts=docx_metadata["toc_expected_counts"],
            entry_pattern=class_a["fields"]["directory_entry_page_number_pattern"],
            page_texts=page_texts,
            title_prefix_characters=int(
                class_b["toc_page_consistency"]["title_prefix_characters"]
            ),
        )
    )
    findings.extend(
        _table_continuation_findings(
            pages, layouts, class_b["table_continuation"]
        )
    )
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in findings:
        key = (item["code"], item["page"], item.get("evidence"), tuple(item.get("bbox") or ()))
        unique.setdefault(key, item)
    return list(unique.values()), document, page_texts


def _contact_sheets(
    *,
    pages: Sequence[Any],
    findings: Sequence[Mapping[str, Any]],
    output_dir: Path,
    stem: str,
    config: Mapping[str, Any],
    image_module: Any,
) -> list[dict[str, Any]]:
    draw_module = importlib.import_module("PIL.ImageDraw")
    settings = config["contact_sheet"]
    thumb_width = int(settings["thumbnail_width_px"])
    columns = int(settings["columns"])
    rows = int(settings["rows"])
    per_sheet = columns * rows
    margin = int(settings["outer_margin_px"])
    gutter = int(settings["gutter_px"])
    label_height = int(settings["label_height_px"])
    dpi = int(settings["render_dpi"])
    max_thumb_height = round(thumb_width * 1.45)
    cell_width = thumb_width
    cell_height = max_thumb_height + label_height
    sheet_width = margin * 2 + columns * cell_width + (columns - 1) * gutter
    sheet_height = margin * 2 + rows * cell_height + (rows - 1) * gutter
    by_page: dict[int, list[Mapping[str, Any]]] = {}
    for item in findings:
        if item.get("page") is not None:
            by_page.setdefault(int(item["page"]), []).append(item)
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for sheet_index, start in enumerate(range(0, len(pages), per_sheet), start=1):
        image = image_module.new(
            "RGB", (sheet_width, sheet_height), settings["background_color"]
        )
        drawer = draw_module.Draw(image)
        page_numbers: list[int] = []
        for local, page in enumerate(pages[start : start + per_sheet]):
            page_number = start + local + 1
            page_numbers.append(page_number)
            row, column = divmod(local, columns)
            x = margin + column * (cell_width + gutter)
            y = margin + row * (cell_height + gutter)
            matrix = page.get_pixmap(dpi=dpi, alpha=False)
            thumbnail = image_module.frombytes(
                "RGB", (matrix.width, matrix.height), matrix.samples
            )
            thumbnail.thumbnail((thumb_width, max_thumb_height))
            tx = x + (thumb_width - thumbnail.width) // 2
            ty = y + (max_thumb_height - thumbnail.height) // 2
            image.paste(thumbnail, (tx, ty))
            issues = by_page.get(page_number, [])
            category = (
                "A"
                if any(item["class"] == "A" for item in issues)
                else "B" if issues else "clear"
            )
            color = settings[
                "class_a_color"
                if category == "A"
                else "class_b_color" if category == "B" else "clear_color"
            ]
            drawer.rectangle(
                (tx - 3, ty - 3, tx + thumbnail.width + 3, ty + thumbnail.height + 3),
                outline=color,
                width=5,
            )
            codes = ",".join(sorted({str(item["code"]) for item in issues}))
            label = f"p.{page_number}" + (f"  {codes}" if codes else "")
            drawer.text((x, y + max_thumb_height + 8), label[:45], fill="#111111")
        path = output_dir / f"{stem}.contact_sheet_{sheet_index:03d}.png"
        image.save(path, format="PNG", optimize=False)
        records.append(
            {
                "filename": path.name,
                "sha256": sha256_file(path),
                "pages": page_numbers,
                "width_px": sheet_width,
                "height_px": sheet_height,
            }
        )
    return records


def render_visual_report_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# CSR 视觉检查报告",
        "",
        "> " + str(report["limitations"]).replace("\n", " "),
        "",
        f"- 状态：**{report['status']}**",
        f"- 视觉闸门：**{report['visual_gate_status']}**",
        "- 既有发布判定受影响：**否**",
        f"- 输入文件：`{report['source']['filename']}`",
        f"- 输入 SHA-256：`{report['source']['sha256']}`",
        f"- 配置 SHA-256：`{report['config_sha256']}`",
        "",
    ]
    if report["status"] == "not_run":
        lines.extend(
            [
                "## 未执行原因",
                "",
                f"- `{report['reason']['code']}`：{report['reason']['message']}",
                "",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            "## 汇总",
            "",
            f"- 页数：{report['pdf']['page_count']}",
            f"- A 类：{report['summary']['class_a_count']}（可阻断视觉预检）",
            f"- B 类：{report['summary']['class_b_count']}（仅警告）",
            "",
            "## 逐项问题",
            "",
            "| 分级 | 页码 | 代码 | 说明 | 证据 |",
            "|---|---:|---|---|---|",
        ]
    )
    for item in report["findings"]:
        evidence = str(item.get("evidence") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item['class']} | {item.get('page') or '-'} | `{item['code']}` | "
            f"{item['message']} | {evidence[:180]} |"
        )
    if not report["findings"]:
        lines.append("| - | - | - | 未发现已配置类别的问题 | - |")
    lines.extend(["", "## 缩略图墙", ""])
    for item in report["contact_sheets"]:
        lines.append(f"- `{item['filename']}`：页 {item['pages'][0]}–{item['pages'][-1]}")
    lines.append("")
    return "\n".join(lines)


def run_visual_qc(
    *,
    docx_path: str | Path,
    work_dir: str | Path,
    json_report: str | Path,
    markdown_report: str | Path,
    config_path: str | Path = DEFAULT_VISUAL_QC_CONFIG,
    lock_path: str | Path | None = DEFAULT_VISUAL_QC_LOCK,
    renderer_override: str | Path | None = None,
) -> dict[str, Any]:
    docx = Path(docx_path)
    work = Path(work_dir)
    json_output = Path(json_report)
    markdown_output = Path(markdown_report)
    config_source = Path(config_path)
    lock_source = Path(lock_path) if lock_path is not None else None
    if not docx.is_file() or docx.suffix.casefold() != ".docx":
        raise VisualQCError("E-VISUAL-INPUT: a readable DOCX is required")
    for output in (json_output, markdown_output):
        if paths_alias(docx, output):
            raise VisualQCError("E-VISUAL-PATH-COLLISION: report aliases input DOCX")
    config, config_hash = _load_configuration(config_source, lock_source)
    limitation = str(config["limitations"]["zh"])
    forbidden_roots = config["renderer"]["forbidden_work_roots"]
    if _unsafe_work_directory(work, forbidden_roots):
        raise VisualQCError(
            "E-VISUAL-WORKDIR-UNSAFE: rendered files must not be written to a system temporary directory"
        )
    work.mkdir(parents=True, exist_ok=True)
    renderer = detect_renderer(
        config["renderer"]["candidates"], renderer_override=renderer_override
    )
    if renderer is None:
        report = _not_run_report(
            docx_path=docx,
            config_hash=config_hash,
            limitation=limitation,
            reason_code="I-VISUAL-RENDERER-NOT-FOUND",
            reason="视觉检查未执行：未检出渲染器。",
            renderer=None,
        )
        atomic_write_json(json_output, report)
        atomic_write_text(markdown_output, render_visual_report_markdown(report))
        return report
    fitz, image_module, missing_dependency = _optional_visual_dependencies()
    if missing_dependency is not None:
        report = _not_run_report(
            docx_path=docx,
            config_hash=config_hash,
            limitation=limitation,
            reason_code="I-VISUAL-DEPENDENCY-NOT-FOUND",
            reason=f"视觉检查未执行：未检出 {missing_dependency}。",
            renderer=renderer,
        )
        atomic_write_json(json_output, report)
        atomic_write_text(markdown_output, render_visual_report_markdown(report))
        return report
    pdf_path, render = _render_pdf(
        renderer=renderer,
        docx_path=docx,
        work_dir=work,
        timeout_seconds=int(config["renderer"]["timeout_seconds"]),
        signature=str(config["renderer"]["accepted_pdf_signature"]),
    )
    if pdf_path is None:
        report = _not_run_report(
            docx_path=docx,
            config_hash=config_hash,
            limitation=limitation,
            reason_code=render["code"],
            reason=render["message"],
            renderer=renderer,
        )
        atomic_write_json(json_output, report)
        atomic_write_text(markdown_output, render_visual_report_markdown(report))
        return report
    metadata = _docx_layout(docx)
    findings, pdf_document, _ = _analyse_pdf(
        pdf_path=pdf_path,
        docx_metadata=metadata,
        config=config,
        fitz=fitz,
    )
    pages = [pdf_document[index] for index in range(pdf_document.page_count)]
    contacts = _contact_sheets(
        pages=pages,
        findings=findings,
        output_dir=work,
        stem=docx.stem,
        config=config,
        image_module=image_module,
    )
    pdf_document.close()
    class_a_count = sum(item["class"] == "A" for item in findings)
    class_b_count = sum(item["class"] == "B" for item in findings)
    report = {
        "schema_version": "1.0",
        "status": "blocked" if class_a_count else "pass_with_warnings" if class_b_count else "pass",
        "visual_gate_status": "blocked" if class_a_count else "pass",
        "legacy_release_decision_changed": False,
        "limitations": limitation,
        "source": {"filename": docx.name, "sha256": sha256_file(docx)},
        "config_sha256": config_hash,
        "renderer": {
            "available": True,
            "name": renderer.name,
            "render_status": render["status"],
        },
        "pdf": {
            "filename": pdf_path.name,
            "sha256": sha256_file(pdf_path),
            "page_count": len(pages),
            "signature_verified": True,
        },
        "docx_layout": {
            "section_count": len(metadata["layouts"]),
            "field_counts": metadata["field_counts"],
            "toc_expected_counts": metadata["toc_expected_counts"],
            "header_footer_excluded_from_body_checks": True,
        },
        "summary": {
            "class_a_count": class_a_count,
            "class_b_count": class_b_count,
            "finding_count": len(findings),
        },
        "findings": sorted(
            findings,
            key=lambda item: (
                item["class"],
                item.get("page") if item.get("page") is not None else 10**9,
                item["code"],
            ),
        ),
        "contact_sheets": contacts,
    }
    atomic_write_json(json_output, report)
    atomic_write_text(markdown_output, render_visual_report_markdown(report))
    return report
