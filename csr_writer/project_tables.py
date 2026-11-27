"""Insert frozen-map-matched aggregate RTFs with their complete header and footnotes."""
from collections import defaultdict
import re

from docx.oxml import OxmlElement

from .io_utils import load_json_unique
from .project_evidence import three_line
from .project_navigation import mark_destination, table_destination
from .project_skeleton import _move_after


def result_exhibits(pack, frozen, publication, inventory, titles, out, issues, root=None):
    exhibits = defaultdict(list)
    if not pack:
        return exhibits
    if not publication:
        issues.append("定稿统计表已匹配；补齐发表编号映射后可自动将汇总表纳入初稿。")
        return exhibits
    source_names = {row["sha256"]: name for name, row in inventory.items()}
    routes = {e["entity_id"]: e["ich_e3_sections"] for e in frozen["entities"]}
    entries = {e["source_entity_id"]: e for e in publication["entries"]}
    native = {}
    for t in pack["tables"]:
        entity = t["table_id"]
        if not entity.startswith("T") or t["ingest_strategy"] == "metadata_only":
            continue
        source = source_names.get(t["source_id"].removeprefix("sha256:"))
        if not source or inventory[source]["format"] not in {".rtf",".docx"}:
            continue
        if entity not in entries:
            issues.append(f"统计原表 {entity} 尚缺发表编号。")
            continue
        source_row = inventory[source]
        if source_row["role"] in {"shell", "listing"} or not source_row.get("extraction"):
            issues.append(f"请核对 {entity} 的定稿原表。")
            continue
        entry = entries[entity]
        permitted = [sid for sid in routes[entity] if sid in titles]
        stated = re.fullmatch(r"§?\s*(\d+(?:\.\d+)*)\s*", entry["expected_section"])
        if stated and stated[1] in permitted:
            owner = stated[1]
        elif len(permitted) == 1 and not stated:
            owner = permitted[0]
        else:
            issues.append(f"统计原表 {entity} 的正文归属需核对。")
            continue
        data = load_json_unique(out / source_row["extraction"])
        if source_row["format"] == ".docx":
            if source not in native:
                from .docx_result import _read_result_entities
                from .shell_mapping import TITLE_RE
                from .project_sources import DIRECT_P
                entities, _, _ = _read_result_entities(root / source)
                native[source] = {e.entity_id: {**e.parsed.as_dict(),"titles":[], "footnotes":[],
                                  "source_sha256":source_row["sha256"]} for e in entities}
                current = None
                for r in data["records"]:
                    if not DIRECT_P.fullmatch(r["location"]):
                        continue
                    value = r["text"].strip()
                    m = TITLE_RE.match(value)
                    if m:
                        current = {"表":"T","图":"F","清单":"L"}[m[1]] + m[2]
                        native[source][current]["titles"].append(value)
                    elif current and value:
                        native[source][current]["footnotes"].append(value)
            data = native[source][entity]
        if data["source_sha256"] != source_row["sha256"]:
            raise ValueError("原表提取物版本不一致")
        exhibits[owner].append((entry, data, source))
    return exhibits


def insert_exhibits(doc, anchor, exhibits):
    blocks = []
    for entry, data, name in exhibits:
        # Keep analysis set, treatment labels, denominators, unit rows and all notes.
        caption = doc.add_paragraph(f"{entry['publication_entity_id']}　{entry['title']}")
        caption.paragraph_format.keep_with_next = True
        mark_destination(doc, caption, table_destination(entry))
        blocks.append(caption)
        blocks.extend(doc.add_paragraph(s) for s in data["titles"])
        rows = data["rows"]
        table = doc.add_table(rows=len(rows), cols=len(rows[0]["cells"]))
        table.style = "Table Grid"
        cells = table._cells
        width = len(rows[0]["cells"])
        for i, row in enumerate(rows):
            if row["is_header"]:
                table.rows[i]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
            for j, cell in enumerate(row["cell_details"]):
                if cell["merged_from"] is not None:
                    continue
                dest = cells[i * width + j]
                if cell["row_span"] > 1 or cell["col_span"] > 1:
                    end = (i + cell["row_span"] - 1) * width + j + cell["col_span"] - 1
                    dest = dest.merge(cells[end])
                dest.text = cell["text"]
        three_line(table, data["header_row_count"])
        blocks.append(table)
        blocks.extend(doc.add_paragraph(note) for note in data["footnotes"])
        blocks.append(doc.add_paragraph(f"统计原表 · {name} · {data['source_sha256'][:12]}", style="CSR Footer"))
    _move_after(anchor, blocks)
