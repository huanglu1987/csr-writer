"""Source-bound attachments, clinical registers and aggregate tables for working drafts."""
import csv
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from pathlib import Path
import re
from zipfile import BadZipFile

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from .errors import ConfigurationError
from .project_table_match import COMPUTED_STATUS, CAPTION, PERSON, cell_location, data_rows, find_layout, study_matches
from .io_utils import load_json_unique
from .project_navigation import mark_destination
from .project_skeleton import _bookmark_key, _move_after, _section_sort_key


def _display_cell(cell):
    if cell.value is None:
        return ""
    if cell.data_type in {"f","e"}:
        return None
    if not isinstance(cell.value,(int,float)) or isinstance(cell.value,bool):
        return str(cell.value)
    fmt=cell.number_format
    if fmt in {"General","@"}:
        return str(cell.value)
    mask=re.fullmatch(r"(#,##)?0(?:\.(0+))?(%)?",fmt)
    if not mask:
        return None  # An unsupported display mask must not silently change a number's meaning.
    value=Decimal(str(cell.value))*(100 if mask[3] else 1)
    digits=len(mask[2] or "")
    rounded=value.quantize(Decimal(1).scaleb(-digits),rounding=ROUND_HALF_UP)
    return format(rounded,("," if mask[1] else "")+f".{digits}f")+("%" if mask[3] else "")


def read_workbook(path, role):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ConfigurationError("读取 Excel 需要 openpyxl") from exc
    try:
        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, OSError, ValueError) as exc:
        raise ConfigurationError(f"无法读取工作簿 {path.name}") from exc
    sheets = {}
    try:
        for ws in book.worksheets:
            if ws.sheet_state != "visible":
                continue
            sheet = {"max_row": ws.max_row, "max_column": ws.max_column}
            sheets[ws.title] = sheet
            # Keep participant-level files in their original location.
            if role in {"dataset", "listing", "randomization"}:
                continue
            # A nameless workbook is an aggregate source only when its contents establish that role.
            preview = [str(c.value or "").strip() for row in ws.iter_rows(max_row=min(25,ws.max_row or 1)) for c in row]
            if any(PERSON.fullmatch(value) for value in preview):
                continue
            if role == "other" and not any(CAPTION.match(value) for value in preview) and not ({"附录编号","附件编号","appendix id"} & set(v.casefold() for v in preview)):
                continue
            cells = []
            for row in ws.iter_rows():
                cells.append([_display_cell(c) for c in row])
            sheet["rows"] = cells
    finally:
        book.close()
    return {"sheets": sheets}, []


def three_line(table, header_count=1):
    pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for name in ("top", "bottom", "insideH", "insideV", "left", "right"):
        node = OxmlElement("w:" + name)
        node.set(qn("w:val"), "single" if name in {"top", "bottom"} else "nil")
        node.set(qn("w:sz"), "12")
        borders.append(node)
    pr.append(borders)
    for i, row in enumerate(table.rows):
        pr = row._tr.get_or_add_trPr()
        if not pr.xpath("./w:cantSplit"):
            pr.append(OxmlElement("w:cantSplit"))
        if i < header_count and not pr.xpath("./w:tblHeader"):
            pr.append(OxmlElement("w:tblHeader"))
        for cell in row.cells:
            if i == header_count - 1:
                border = OxmlElement("w:tcBorders")
                rule = OxmlElement("w:bottom")
                rule.set(qn("w:val"), "single")
                rule.set(qn("w:sz"), "6")
                border.append(rule)
                cell._tc.get_or_add_tcPr().append(border)
            for p in cell.paragraphs:
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.line_spacing = 1
                p.paragraph_format.space_after = Pt(2)
                for run in p.runs:
                    run.font.size = Pt(10.5)
                    run.bold = i < header_count


def _sheet_table(info, job, *, computed=False):
    sheet = info["sheets"][job["sheet"]]
    rows = sheet["rows"]
    if job.get("layout") == "source":
        layout = find_layout(rows,computed=computed)
        if (layout.title != job["title"] or layout.columns != job["columns"] or layout.population != job["population"]
                or not study_matches(layout,job["study"])):
            raise ConfigurationError("原表题、研究编号、人群或列顺序不符")
        data, notes = data_rows(rows,layout)
        header = layout.columns
        context = "｜".join((job["study"],COMPUTED_STATUS if computed else "定稿",layout.population))
    else:
        return _legacy_sheet_table(rows,job)
    return _summarize_rows(context,header,data,notes,job)


def _legacy_sheet_table(rows, job):
    if len(rows) < 4 or rows[0][0] != job["title"] or not rows[1][0]:
        raise ConfigurationError("表题或表头位置与原件不符")
    header = rows[2]
    while header and header[-1] == "":
        header = header[:-1]
    if header != job["columns"] or job["population"] not in rows[1][0].split("｜"):
        raise ConfigurationError("表列顺序、治疗组或分析人群与绑定不符")
    if len(header) < 2 or not all(header) or len(set(header)) != len(header):
        raise ConfigurationError("表头存在空项或重复列")
    data, notes = [], []
    for i, row in enumerate(rows[3:], 4):
        if not any(v not in ("", None) for v in row) and None not in row:
            continue
        if row[0] and row[0].startswith(("注：", "Note:")) and not any(row[1:]):
            notes.append(row[0])
        else:
            if not row[0] or None in row or any(row[len(header):]):
                raise ConfigurationError("结果表含未核公式、错误值或表头外数据")
            data.append((i, row[:len(header)]))
    if not data:
        raise ConfigurationError("原表没有可呈现的结果行")
    return _summarize_rows(rows[1][0],header,data,notes,job)


def _summarize_rows(context, header, data, notes, job):
    lookup = {line: row for line, row in data}
    texts = []
    selected = []
    for binding in job.get("highlights", []):
        line = binding["row"]
        row = lookup[line]
        if row[0] != binding["label"]:
            raise ConfigurationError("汇总指标与原行标签不一致")
        selected.append(row)
        if not all(row):
            raise ConfigurationError("所选摘要行有空值")
        phrases=[f"{col}{value}" if col=="P值" and str(value).startswith(("<",">")) else f"{col}为{value}"
                 for col,value in zip(header[1:],row[1:])]
        texts.append(f"{row[0]}，" + "，".join(phrases) + "。")
    return context, header, selected or [r for _, r in data], notes, texts


def _add_table(doc, matrix, *, notes_follow=False):
    table = doc.add_table(rows=0, cols=len(matrix[0]))
    if len(matrix[0])==4 and matrix[0][1]=="估计值" and matrix[0][3]=="P值":
        full=sum(c.width for c in table.columns)
        table.autofit=False
        for col,share in zip(table.columns,(.37,.19,.25,.19)):
            col.width=int(full*share)
    for row in matrix:
        for c, value in zip(table.add_row().cells, row, strict=True):
            c.text = value
    three_line(table)
    for i,row in enumerate(table.rows):
        # Keep small summaries together and keep the last row with its original footnote.
        if i == 0 or (len(matrix)<=4 and i<len(matrix)-1) or (notes_follow and i==len(matrix)-1):
            for cell in row.cells:
                for para in cell.paragraphs:
                    para.paragraph_format.keep_with_next = True
    return table


def assemble_evidence(doc, headings, titles, plan, inventory, records, out, issues, facts, root, selected=None):
    entries = {}
    paragraphs = defaultdict(list)
    appendix = {}
    cached = {}
    jobs = plan.get("evidence_sections", {})
    review = []
    for sid in sorted(jobs, key=_section_sort_key):
        blocks = []
        try:
            if sid not in titles or sid == "2" or not isinstance(jobs[sid], list) or not jobs[sid]:
                raise ConfigurationError("无有效章节或任务")
            sources, texts, kinds, nav = [], [], [], []
            section_review, section_appendix = [], {}
            for job in jobs[sid]:
                kind = job["kind"]
                if kind == "body_copy":
                    parent, index = job["section"], job["paragraph"]
                    copies_conclusion = (sid in {"11.4.7","11.6.7"} and parent in {"11.4.1","11.4.1.1","11.6.1.1"}
                                         or sid in {"12.6","12.7"} and "安全性结论" in titles[sid] and parent in {"12.2.1","12.3.1.2"})
                    if (sid != "13" and not copies_conclusion) or parent not in entries or entries[parent]["status"] != "sourced_body" or type(index) is not int or not 1 <= index <= len(paragraphs[parent]):
                        raise ConfigurationError("讨论仅可逐字复制本轮已写入的结果段落")
                    value = paragraphs[parent][index-1]
                    blocks.append(doc.add_paragraph(value))
                    texts.append(value)
                    sources.extend(entries[parent]["sources"])
                    kinds.append(kind)
                    continue
                src = job["source"]
                source = inventory[src]
                role = source["role"]
                def source_at(location):
                    return {"source": src, "location": location, "sha256": source["sha256"]}
                def read_line(location):
                    values = [r["text"].strip() for r in records.get(src, []) if r["location"] == location and not r.get("has_review_comment")]
                    if len(values) != 1 or not values[0]:
                        raise ConfigurationError("原文定位缺失或含待裁决批注")
                    return values[0]
                if kind == "table":
                    if role != "tfl" or not facts or not sid.startswith(("10.", "11.", "12.", "14.")):
                        raise ConfigurationError("须选用本研究的汇总结果表和对应结果章节")
                    if src not in cached:
                        cached[src] = load_json_unique(out/source["extraction"])
                    computed = source.get("origin")=="computed_from_subjects"
                    status = COMPUTED_STATUS if computed else "定稿"
                    context, header, values, notes, narrative = _sheet_table(cached[src], job, computed=computed)
                    if not facts or facts["protocol_id"] not in context.split("｜") or status not in context.split("｜"):
                        raise ConfigurationError("表内研究编号或定稿状态不匹配")
                    blocks.extend(doc.add_paragraph(t) for t in narrative)
                    caption = doc.add_paragraph(job["title"])
                    caption.paragraph_format.keep_with_next = True
                    target = "_Table_" + sid.replace(".", "_") + "_" + str(len(nav))
                    mark_destination(doc, caption, target)
                    nav.append((sid, job["title"], target, "table"))
                    blocks.append(caption)
                    population_label = doc.add_paragraph(context)
                    population_label.paragraph_format.keep_with_next = True
                    blocks.append(population_label)
                    blocks.append(_add_table(doc, [header, *values], notes_follow=bool(notes)))
                    blocks.extend(doc.add_paragraph(note) for note in notes)
                    texts.extend(narrative)
                    sheet = cached[src]["sheets"][job["sheet"]]
                    layout = find_layout(sheet["rows"],computed=computed) if job.get("layout") == "source" else None
                    offset = layout.offset if layout else 0
                    start = cell_location(job["sheet"], sheet, layout.title_row if layout else 1,
                                          next(i for i,v in enumerate(sheet["rows"][layout.title_row-1],1) if v == layout.title) if layout else 1)
                    origin = source_at(start if "locations" in sheet else start + ":" +
                                       cell_location(job["sheet"],sheet,len(sheet["rows"]),offset+len(header)).split("!")[-1])
                    if computed:
                        origin["analysis_manifest"]=source["analysis_manifest"]
                        origin["review_status"]=COMPUTED_STATUS
                    bindings = job.get("highlights", [])
                    if layout and not bindings:
                        bindings = [{"row":i,"label":v[0]} for i,v in data_rows(sheet["rows"],layout)[0]]
                    origin["numeric_cells"] = [
                        {"location":cell_location(job["sheet"],sheet,binding["row"],offset+c),
                         "group_or_statistic":header[c-1], "row_label":binding["label"],
                         "value":sheet["rows"][binding["row"]-1][offset+c-1]}
                        for binding in bindings for c in range(2,len(header)+1)]
                    sources.append(origin)
                elif kind == "figure":
                    if sid not in {"9.5.1","9.5.1.1","10.1","14.1.1"} or role != "figure" or source["format"] != ".png":
                        raise ConfigurationError("图文件或归属不符合条件")
                    from docx.shared import Cm
                    p = doc.add_paragraph()
                    blocks.append(p)
                    label = "研究访视与评价流程" if sid in {"9.5.1","9.5.1.1"} else "受试者处置流程"
                    cap = doc.add_paragraph("图　" + label)
                    target = "_Fig_" + sid.replace(".", "_")
                    mark_destination(doc, cap, target)
                    nav.append((sid, label, target, "figure"))
                    blocks.insert(-1,cap)
                    p.add_run().add_picture(str(root / src), width=Cm(14))
                    sources.append(source_at("整图"))
                elif kind in {"record","method"}:
                    allowed = {"1": {"operations"}, "5.1": {"ethics"}, "5.2": {"ethics"}, "5.3": {"ethics"},
                               "10.2":{"deviations"}, "11.3":{"operations"}, "6": {"investigators"}, "9.8": {"amendment","operations"}, "15": {"references"}}
                    if kind == "method":
                        from .project_autoplan import METHOD_SCOPE
                        if src not in (selected or {}).values() or sid not in set(METHOD_SCOPE) | {"1","9.7.1"}:
                            raise ConfigurationError("方法必须来自当前研究选定的方案或 SAP")
                    elif role not in allowed.get(sid, set()):
                        raise ConfigurationError("该节不能用这类记录补写")
                    for loc in job["locations"]:
                        value = read_line(loc)
                        texts.append(value)
                        blocks.append(doc.add_paragraph(value))
                        sources.append(source_at(loc))
                elif kind == "subject_case":
                    if sid not in {"12.3.2","14.3.3"} or not set(job["locations"])<=set(source.get("case_locations",[])):
                        raise ConfigurationError("逐例叙述必须来自本次按观察窗核对的严重事件原文")
                    for loc in job["locations"]:
                        value=read_line(loc)
                        texts.append(value)
                        blocks.append(doc.add_paragraph(value))
                        sources.append(source_at(loc))
                    blocks.append(doc.add_paragraph("〔逐例记录原文，待医学负责人复核〕"))
                    kind="medical"
                elif kind == "medical":
                    if role != "medical" or sid not in {"11.6.7","11.4.7","12.3.2","12.3.3","12.3.1.1","12.3.1.3","12.6","12.7","13","14.3.3"}:
                        raise ConfigurationError("仅可转录对应医学记录")
                    for loc in job["locations"]:
                        value = read_line(loc)
                        blocks.append(doc.add_paragraph(value))
                        texts.append(value)
                        sources.append(source_at(loc))
                    blocks.append(doc.add_paragraph("〔待医学负责人复核〕"))
                elif kind == "original_file":
                    if not sid.startswith("16.") and sid not in {"11.4.3","11.4.6","12.2.4","12.4.1","12.4.2.2"}:
                        raise ConfigurationError("实际附件的章节归属错误")
                    line = f"{titles[sid]}相关原件见《{src}》。"
                    texts.append(line)
                    blocks.append(doc.add_paragraph(line))
                    sources.append(source_at("完整原件"))
                elif kind in {"attachment","listing"}:
                    if role != "register" or not (sid.startswith("16.") or sid in {"11.4.3","11.4.6","12.2.4","12.4.1","12.4.2.2","14.3.2","14.3.4"}):
                        raise ConfigurationError("该节不接受附件索引")
                    fields = read_line(job["location"]).split("\t")
                    code, label, status, file, note = fields
                    if status=="已提供" and file not in inventory:
                        nearby=(Path(src).parent/file).as_posix()
                        matches=[p for p in inventory if p==nearby or (Path(file).name==file and Path(p).name==file)]
                        if len(matches)==1:
                            file=matches[0]
                    if kind == "attachment" and code != sid:
                        raise ConfigurationError("附录编号不符")
                    if status not in {"已提供","不适用","待签署","待决定"}:
                        raise ConfigurationError("未知附件状态")
                    if status == "已提供" and (file not in inventory or not file):
                        raise ConfigurationError("清单所列附件实物缺失")
                    if status != "已提供" and not note:
                        raise ConfigurationError("缺少状态依据")
                    line = f"{label}。{note}" + (f" 文件见《{file}》。" if file else "")
                    texts.append(line)
                    blocks.append(doc.add_paragraph(line))
                    sources.append(source_at(job["location"]))
                    if file in inventory:
                        sources.append({"source":file, "location":"完整原件", "sha256":inventory[file]["sha256"]})
                    if "display_fields" in job:
                        fields = job["display_fields"]
                        allowed = {"受试者","治疗组","首选术语","级别","结局","基线ALT","末次ALT","单位","临床重要"}
                        if (kind != "listing" or not sid.startswith("14.") or not file.endswith(".csv")
                                or not fields or not set(fields) <= allowed):
                            raise ConfigurationError("个例附表字段须来自限定的去标识资料")
                        with (root/file).open(encoding="utf-8-sig", newline="") as f:
                            reader = csv.DictReader(f)
                            if not set(fields) <= set(reader.fieldnames or []):
                                raise ConfigurationError("个例清单缺列")
                            matrix = [[row[k] for k in fields] for row in reader]
                        if matrix:
                            caption = doc.add_paragraph(label)
                            caption.paragraph_format.keep_with_next = True
                            target = "_Listing_" + sid.replace(".", "_") + "_" + str(len(nav))
                            mark_destination(doc, caption, target)
                            nav.append((sid, label, target, "listing"))
                            blocks.append(caption)
                            blocks.append(_add_table(doc,[fields,*matrix]))
                    if kind == "attachment":
                        section_appendix[sid] = (status, file or "—", note)
                    if status.startswith("待"):
                        section_review.append(f"{sid} {label}，{status}。{note}")
                    kind = {"不适用":"not_applicable","待签署":"decision_required","待决定":"decision_required"}.get(status,kind)
                else:
                    raise ConfigurationError("未知证据任务")
                kinds.append(kind)
            appendix.update(section_appendix)
            review.extend(section_review)
            paragraphs[sid].extend(texts)
            if "body_copy" in kinds and "medical" not in kinds and any("analysis_manifest" in s for s in sources):
                blocks.append(doc.add_paragraph("〔上述为统计复核稿结果，待医学负责人确认解释〕"))
                kinds.append("medical")
            if "medical" in kinds:
                review.append(f"{sid} 复核转录的医学意见与研究资料，并由医学负责人确认。")
            state = ("medical_review" if "medical" in kinds else "decision_required" if "decision_required" in kinds
                     else "not_applicable" if set(kinds)=={"not_applicable"} else "source_attachment" if kinds[0] in {"attachment","listing","original_file"}
                     else "statistical_table" if not texts and "table" in kinds else "source_figure" if kinds == ["figure"] else "sourced_body")
            blocks.append(doc.add_paragraph("来源　" + "；".join(f"{s['source']} · {s['location']}" for s in sources), style="CSR Footer"))
            _move_after(headings[_bookmark_key(sid)], blocks)
            entries[sid] = {"status":state, "sources":sources, "navigation":nav}
        except (KeyError, IndexError, TypeError, ValueError, ConfigurationError) as exc:
            for b in blocks:
                if b._element.getparent() is doc._element.body and sid not in entries:
                    b._element.getparent().remove(b._element)
            issues.append(f"{sid} 证据任务未完成。{exc}")
    issues.extend(review)
    if appendix:
        for table in doc.tables:
            if len(table.columns) != 5 or table.cell(0,0).text != "附录编号":
                continue
            for row in table.rows[1:]:
                c = row.cells
                if c[0].text == "待核对的附件":
                    c[0].text = "项目附录登记"
                elif c[0].text in appendix:
                    for cell, text in zip(c[2:], appendix[c[0].text]):
                        cell.text = text
            three_line(table)
    return entries, paragraphs
