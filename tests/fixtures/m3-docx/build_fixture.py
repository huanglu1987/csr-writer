"""Build the committed M3 deep-extraction DOCX fixture deterministically."""

from __future__ import annotations

import argparse
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def qn(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


DOCUMENT_BLOCKS = f"""
<fixture xmlns:w="{W_NS}"
         xmlns:r="{R_NS}"
         xmlns:v="urn:schemas-microsoft-com:vml"
         xmlns:o="urn:schemas-microsoft-com:office:office">
  <w:p>
    <w:r><w:t>正文开始，</w:t></w:r>
    <w:commentRangeStart w:id="0"/>
    <w:del w:id="10" w:author="审阅者" w:date="2026-08-04T00:00:00Z">
      <w:r><w:delText>删除旧文本100 mg，</w:delText></w:r>
    </w:del>
    <w:ins w:id="11" w:author="审阅者" w:date="2026-08-04T00:01:00Z">
      <w:r><w:t>插入文本，</w:t></w:r>
    </w:ins>
    <w:commentRangeEnd w:id="0"/>
    <w:r>
      <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
      <w:commentReference w:id="0"/>
    </w:r>
    <w:r><w:t>正文继续。</w:t></w:r>
    <w:r><w:rPr><w:vanish/></w:rPr><w:t>隐藏正文999 mg</w:t></w:r>
    <w:r><w:rPr><w:vanish w:val="0"/></w:rPr><w:t>显式可见。</w:t></w:r>
    <w:r>
      <w:pict>
        <v:shape id="TextBox1" o:spid="_x0000_s1026"
                 style="width:180pt;height:36pt" stroked="f">
          <v:textbox>
            <w:txbxContent>
              <w:p>
                <w:commentRangeStart w:id="1"/>
                <w:r><w:t>文本框可见。</w:t></w:r>
                <w:commentRangeEnd w:id="1"/>
                <w:r>
                  <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
                  <w:commentReference w:id="1"/>
                </w:r>
                <w:r><w:rPr><w:vanish/></w:rPr><w:t>文本框隐藏888 mg</w:t></w:r>
              </w:p>
            </w:txbxContent>
          </v:textbox>
        </v:shape>
      </w:pict>
    </w:r>
  </w:p>
  <w:tbl>
    <w:tblPr><w:tblW w:w="9000" w:type="dxa"/></w:tblPr>
    <w:tblGrid><w:gridCol w:w="4500"/><w:gridCol w:w="4500"/></w:tblGrid>
    <w:tr>
      <w:tc>
        <w:tcPr><w:tcW w:w="4500" w:type="dxa"/></w:tcPr>
        <w:p><w:r><w:t>单元格A</w:t></w:r></w:p>
      </w:tc>
      <w:tc>
        <w:tcPr><w:tcW w:w="4500" w:type="dxa"/></w:tcPr>
        <w:p><w:r><w:t>单元格B</w:t></w:r></w:p>
      </w:tc>
    </w:tr>
    <w:tr>
      <w:trPr>
        <w:del w:id="12" w:author="审阅者" w:date="2026-08-04T00:03:00Z"/>
      </w:trPr>
      <w:tc>
        <w:tcPr><w:tcW w:w="4500" w:type="dxa"/></w:tcPr>
        <w:p><w:r><w:t>删除整行555 mg</w:t></w:r></w:p>
      </w:tc>
      <w:tc>
        <w:tcPr><w:tcW w:w="4500" w:type="dxa"/></w:tcPr>
        <w:p><w:r><w:t>删除整行另一个单元格</w:t></w:r></w:p>
      </w:tc>
    </w:tr>
  </w:tbl>
  <w:p><w:r><w:t>正文结束。</w:t></w:r></w:p>
</fixture>
"""

COMMENTS_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="{W_NS}">
  <w:comment w:id="0" w:author="审阅者" w:date="2026-08-04T00:02:00Z">
    <w:p><w:r><w:t>批注秘密777 mg不要进事实库</w:t></w:r></w:p>
  </w:comment>
  <w:comment w:id="1" w:author="审阅者" w:date="2026-08-04T00:04:00Z">
    <w:p><w:r><w:t>文本框批注秘密444 mg不要进事实库</w:t></w:r></w:p>
  </w:comment>
</w:comments>
""".encode("utf-8")

HEADER_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="{W_NS}">
  <w:p>
    <w:r><w:t>页眉可见</w:t></w:r>
    <w:r><w:rPr><w:vanish/></w:rPr><w:t>页眉隐藏666 mg</w:t></w:r>
  </w:p>
</w:hdr>
""".encode("utf-8")

FOOTER_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="{W_NS}">
  <w:p><w:r><w:t>页脚可见</w:t></w:r></w:p>
</w:ftr>
""".encode("utf-8")


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, recover=False)


def _rewrite_document(payload: bytes) -> bytes:
    root = etree.fromstring(payload, parser=_xml_parser())
    body = root.find(qn(W_NS, "body"))
    if body is None:
        raise RuntimeError("Base document is missing w:body")
    section_properties = body.find(qn(W_NS, "sectPr"))
    if section_properties is None:
        raise RuntimeError("Base document is missing w:sectPr")
    for child in list(body):
        if child is not section_properties:
            body.remove(child)
    fixture = etree.fromstring(DOCUMENT_BLOCKS.encode("utf-8"), parser=_xml_parser())
    for child in list(fixture):
        body.insert(len(body) - 1, child)
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _add_comment_relationship(payload: bytes) -> bytes:
    root = etree.fromstring(payload, parser=_xml_parser())
    ids = {node.get("Id") for node in root.findall(qn(REL_NS, "Relationship"))}
    relationship_id = "rIdM3Comments"
    if relationship_id not in ids:
        relationship = etree.Element(qn(REL_NS, "Relationship"))
        relationship.set("Id", relationship_id)
        relationship.set(
            "Type",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
        )
        relationship.set("Target", "comments.xml")
        root.append(relationship)
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _add_comment_content_type(payload: bytes) -> bytes:
    root = etree.fromstring(payload, parser=_xml_parser())
    part_name = "/word/comments.xml"
    if not any(
        node.get("PartName") == part_name for node in root.findall(qn(CT_NS, "Override"))
    ):
        override = etree.Element(qn(CT_NS, "Override"))
        override.set("PartName", part_name)
        override.set(
            "ContentType",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        )
        root.append(override)
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _normalize_settings(payload: bytes) -> bytes:
    root = etree.fromstring(payload, parser=_xml_parser())
    zoom = root.find(qn(W_NS, "zoom"))
    if zoom is not None and zoom.get(qn(W_NS, "percent")) is None:
        zoom.set(qn(W_NS, "percent"), "100")
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def build_fixture(destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        base_path = Path(temporary) / "base.docx"
        document = Document()
        document.core_properties.title = "M3 DOCX deep extraction fixture"
        document.core_properties.author = "CSR Writer Test"
        document.core_properties.created = datetime(2026, 8, 4, tzinfo=timezone.utc)
        document.core_properties.modified = datetime(2026, 8, 4, tzinfo=timezone.utc)
        document.add_paragraph("placeholder")
        document.sections[0].header.paragraphs[0].text = "placeholder header"
        document.sections[0].footer.paragraphs[0].text = "placeholder footer"
        document.save(base_path)

        with zipfile.ZipFile(base_path) as archive:
            parts = {item.filename: archive.read(item.filename) for item in archive.infolist()}

    parts["word/document.xml"] = _rewrite_document(parts["word/document.xml"])
    parts["word/header1.xml"] = HEADER_XML
    parts["word/footer1.xml"] = FOOTER_XML
    parts["word/comments.xml"] = COMMENTS_XML
    parts["word/_rels/document.xml.rels"] = _add_comment_relationship(
        parts["word/_rels/document.xml.rels"]
    )
    parts["[Content_Types].xml"] = _add_comment_content_type(parts["[Content_Types].xml"])
    parts["word/settings.xml"] = _normalize_settings(parts["word/settings.xml"])

    temporary_output = destination.with_name(f".{destination.name}.staged")
    try:
        with zipfile.ZipFile(temporary_output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(parts):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, parts[name])
        temporary_output.replace(destination)
    finally:
        if temporary_output.exists():
            temporary_output.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=Path(__file__).with_name("deep-extraction-fixture.docx"),
    )
    args = parser.parse_args()
    build_fixture(args.output)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
