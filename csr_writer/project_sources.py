"""Read-only discovery and source-located candidates for the internal first draft."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import defaultdict
import re
from pathlib import Path

from .docx_extract import extract_docx
from .errors import CSRWriterError, ConfigurationError
from .io_utils import atomic_write_json, sha256_file
from .rtf_parser import parse_rtf_table
from .project_table_match import CAPTION, word_tables

ROLE_PATTERNS = (
    ("register", r"附件登记|附录登记|附录目录|归档清册|appendix register"),
    ("medical", r"医学评估记录|医学复核记录|病例医学记录|医学监查意见|医学审阅意见|医学总结|病例叙述|medical review|case narratives"),
    ("operations", r"研究大事记|研究基本信息|项目执行记录|试验执行简报|研究概况|研究实施记录|study administration record"),
    ("figure", r"受试者流程图|研究访视图|访视流程图|受试者处置图|patient flow|visit[ _-]?(?:schedule|flow|diagram)|schedule of assessments"),
    ("amendment", r"修订案|修正案|变更说明|amendment"),
    ("sap", r"统计分析计划|统计分析方案|statistical analysis plan|(?<![a-z])sap(?![a-z])"),
    ("shell", r"shell|空表|表壳|模拟表"),
    ("listing", r"listing|个例清单|受试者清单|患者清单|^l[_.-]?\d"),
    ("tfl", r"tfl|tlf|统计分析表|结果表|^t[_.-]?\d|^表\s*\d"),
    ("deviations", r"方案偏离|protocol deviation"),
    ("randomization", r"随机化方案|随机号|随机表|randomization"),
    ("ethics", r"伦理|知情同意|参与须知与同意样本|ethics|consent|^icf$"),
    ("crf", r"病例报告表|crf"),
    ("safety", r"药物警戒|严重不良事件|个例叙述|(?<![a-z])sae(?![a-z])"),
    ("references", r"参考文献|bibliography"),
    ("investigators", r"研究者名录|研究者名单|研究中心名单|职责分工|研究组织与职责|investigator list"),
    ("protocol", r"研究方案|试验方案|^方案(?:$|[\s_.\dvV-])|protocol"),
)
# Only planned methodology can be suggested from a protocol or SAP heading.
# Source headings are matched as complete labels, never as result assertions.
HEADING_TARGETS = {
    "4": ("缩略语", "缩略语和术语定义", "abbreviations"),
    "6": ("研究管理结构", "研究组织与职责", "study administration"),
    "7": ("研究背景", "研究依据", "研究背景与依据", "background", "introduction"),
    "8": ("研究目的", "试验目的", "研究目标", "study objectives", "objectives"),
    "9.1": ("研究设计", "试验设计", "总体设计", "整体研究设计", "study design"),
    "9.2": ("研究设计依据", "对照组的选择"),
    "9.3.1": ("入选标准", "纳入标准", "inclusion criteria"),
    "9.3.2": ("排除标准", "exclusion criteria"),
    "9.3.3": ("退出标准", "提前终止标准"),
    "9.4.1": ("治疗方案", "给药方案", "研究治疗", "study treatment"),
    "9.4.2": ("研究药物标识", "试验药物标识"),
    "9.4.4": ("剂量选择依据", "研究剂量选择"),
    "9.4.3": ("随机化", "随机化方法", "randomization"),
    "9.4.5": ("给药计划", "给药时间"),
    "9.4.6": ("盲法", "设盲与揭盲", "blinding"),
    "9.4.7": ("合并用药", "伴随治疗"),
    "9.4.8": ("治疗依从性", "用药依从性"),
    "9.5.1": ("疗效评价", "安全性评价", "疗效和安全性变量", "研究终点", "study endpoints"),
    "9.5.1.1": ("访视与评价时间表", "研究评估流程"),
    "9.5.2": ("衡量指标的适当性", "评估指标选择依据"),
    "9.5.8": ("安全性终点", "安全性评价指标"),
    "9.6": ("质量控制", "质量保证", "数据质量保证"),
    "9.7.1": ("统计分析", "统计分析方法", "统计方法", "缺失数据处理", "statistical methods"),
    "9.7.2": ("样本量", "样本量估算", "样本量确定", "sample size"),
}
METHOD_TARGETS = frozenset(HEADING_TARGETS) | {"4", "6"}
HEADINGS = {text.casefold(): sid for sid, names in HEADING_TARGETS.items() for text in names}
NUMBER_PREFIX = re.compile(r"^(?:第[一二三四五六七八九十百\d]+[章节]|\d+(?:\.\d+)*(?:[.、．])?(?=\s|[\u3400-\u9fff]))\s*")
DIRECT_P = re.compile(r"word/document\.xml:/w:document/w:body/w:p(?:\[\d+\])?$")


def discover(root: Path):
    if not root.is_dir():
        raise ConfigurationError("输入资料目录不存在")
    files, skipped = {}, []

    def fail(exc):
        raise exc

    for parent, dirs, names in os.walk(root, onerror=fail):
        for name in sorted(dirs + names):
            p = Path(parent, name)
            rel = p.relative_to(root).as_posix()
            if name.startswith((".", "~$")) or p.is_symlink():
                if name in dirs:
                    dirs.remove(name)
                skipped.append(rel)
            elif name in names:
                if p.is_file():
                    files[rel] = sha256_file(p)
                else:
                    skipped.append(rel)
    if not files:
        raise ConfigurationError("输入目录内没有可登记的普通文件")
    return dict(sorted(files.items())), sorted(skipped)


def snapshot_id(files):
    value = json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def classify(label):
    for role, pattern in ROLE_PATTERNS:
        if re.search(pattern, label, re.I):
            return role
    return "other"


def _records(path, role):
    if path.suffix.lower() == ".xlsx":
        from .project_evidence import read_workbook
        workbook, records = read_workbook(path, role)
        register = workbook_register(workbook) if role in {"register","other"} else []
        if register:
            workbook["records"] = register
            return "register", workbook, register
        if role == "other" and any(sheet.get("rows") for sheet in workbook["sheets"].values()):
            role = "tfl"
        return role, workbook, records
    if role in {"listing", "dataset"}:
        return role, None, []
    if path.suffix.lower() == ".docx":
        extraction = extract_docx(path)
        records = extraction["records"]
        if role == "other":
            title = next((r["text"] for r in records if r["story"] == "document" and r["text"].strip()), "")
            role = classify(title)
        if role in {"tfl","other"}:
            sheets = word_tables(records)
            if sheets:
                role = "tfl"
                extraction["sheets"] = sheets
        return role, extraction, records
    if path.suffix.lower() == ".pdf" and role not in {"listing","dataset","randomization"}:
        try:
            import pymupdf
        except ImportError:
            return role, None, []
        records = []
        with pymupdf.open(path) as pdf:
            for index, page in enumerate(pdf, 1):
                for block in page.get_text("blocks"):
                    if block[6] == 0 and block[4].strip():
                        records.append({"location": f"page:{index}/block:{block[5]}",
                                        "text": block[4].strip(), "kind": "pdf_block"})
            pages = len(pdf)
        if role == "other":
            role = classify(" ".join(r["text"] for r in records[:2]))
        return role, {"pages": pages, "records": records,
                      "warnings": ["PDF 文本块需对照原页核对。"] if records else ["扫描件需识别并核对原页。"]}, records
    if path.suffix.lower() in {".txt", ".md"}:
        records = []
        for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            value = line.strip()
            if value:
                records.append({"text": value, "location": f"line:{n}", "kind": "paragraph"})
        if role == "other" and records:
            role = classify(records[0]["text"].lstrip("# "))
        return role, {"records": records}, records
    if path.suffix.lower() == ".rtf" and role not in {"shell", "listing"}:
        result = parse_rtf_table(path)
        data = result.as_dict()
        titles = list(result.titles)
        start = next((i for i,t in enumerate(titles) if CAPTION.match(t)), None)
        if start is not None:
            rows = [[t] for t in titles] + [list(r.cells) for r in result.rows] + [[n] for n in result.footnotes]
            coords = [[f"title:{i}"] for i in range(1,len(titles)+1)]
            coords += [[f"row:{i}/cell:{j}" for j in range(1,len(r.cells)+1)] for i,r in enumerate(result.rows,1)]
            coords += [[f"note:{i}"] for i in range(1,len(result.footnotes)+1)]
            data["sheets"] = {titles[start]: {"rows":rows, "locations":coords}}
        return role if role != "other" else "tfl", data, []
    return role, None, []


def read_sources(root, files, output):
    inventory, by_path = {}, {}
    for n, rel in enumerate(files, 1):
        path = root / rel
        role = classify(path.stem)
        if role == "other":
            role = classify(" ".join(reversed(Path(rel).parts[:-1])))
        if path.suffix.lower() in {".xpt", ".sas7bdat", ".csv", ".tsv"} and role == "other":
            role = "dataset"
        row = {"sha256": files[rel], "role": role, "format": path.suffix.lower(),
               "status": "indexed"}
        try:
            if row["format"] in {".csv",".tsv"}:
                with path.open(encoding="utf-8-sig",newline="") as f:
                    row["columns"] = next(csv.reader(f,delimiter="\t" if row["format"]==".tsv" else ","),[])
            role, data, records = _records(path, role)
        except (CSRWriterError, UnicodeError, OSError, ValueError, RuntimeError) as exc:
            row.update(status="unreadable", issue=str(exc))
        else:
            row["role"] = role
            if role in {"protocol","sap"}:
                statuses = [m[1].strip().casefold() for r in records[:12] if eligible_record(r)
                            for m in re.finditer(r"^(?:文件状态|版本状态|document status)\s*[:：]\s*(.+)$", r["text"].strip(), re.M | re.I)]
                if statuses:
                    row["document_status"] = sorted(set(statuses))
            if data is not None:
                dest = f"sources/{n:04d}.json"
                atomic_write_json(output / dest, data)
                row.update(status="extracted", extraction=dest)
                if data.get("warnings"):
                    row["warnings"] = data["warnings"]
                by_path[rel] = records
        inventory[rel] = row
    return inventory, by_path


def choose_sources(inventory, choices):
    if not isinstance(choices, dict) or set(choices) - {"protocol", "sap"}:
        raise ConfigurationError("selected_sources 只能指定 protocol 或 sap")
    selected, issues = {}, []
    for role, label in (("protocol", "方案"), ("sap", "SAP")):
        matches = [p for p, row in inventory.items() if row["role"] == role]
        if role in choices:
            path = choices[role]
            if not isinstance(path, str) or path not in inventory or inventory[path]["role"] not in (role, "other"):
                raise ConfigurationError(f"{label}所选来源不存在或类型冲突")
        elif len({inventory[p]["sha256"] for p in matches}) == 1:
            path = matches[0]
        elif matches:
            issues.append(f"{label}存在多个不同版本，请确定本次采用版本：" + "、".join(matches))
            continue
        else:
            issues.append(f"请提供或标明本研究的{label}。")
            continue
        stated = inventory[path].get("document_status", [])
        if len(stated) > 1 or any(v in {"已作废","已废止","已被替代","superseded","withdrawn","草稿","draft"} for v in stated):
            issues.append(f"{label}原件仅有历史、草稿或矛盾状态，需提供有效版本：{path}")
            continue
        selected[role] = path
        if inventory[path]["status"] != "extracted":
            issues.append(f"{label}尚需核对原件或提取文本：{path}")
    return selected, issues


DIRECT_CELL = re.compile(
    r"word/document\.xml:/w:document/w:body/w:tbl(?:\[\d+\])?/w:tr(?:\[\d+\])?"
    r"/w:tc(?:\[\d+\])?/w:p(?:\[\d+\])?$"
)


def _summary_table_candidates(rows, role, source, candidates):
    grouped = defaultdict(lambda: defaultdict(list))
    for record in rows:
        if DIRECT_CELL.fullmatch(record["location"]):
            grouped[(record["table_id"], record["physical_row_index"])][record["cell_index"]].append(record)
    for cells in grouped.values():
        if set(cells) != {1, 2} or len(cells[1]) != 1:
            continue
        if any(not eligible_record(r) for group in cells.values() for r in group):
            continue
        label = cells[1][0]["text"].strip().rstrip("：:").casefold()
        sid = HEADINGS.get(label)
        if sid:
            for r in cells[2]:
                if r["text"].strip():
                    candidates.setdefault(sid, []).append({
                        "source": source, "location": r["location"], "text": r["text"].strip(), "role": role,
                    })


def method_candidates(selected, records):
    candidates = {}
    for role, source in selected.items():
        section = None
        rows = records.get(source, [])
        _summary_table_candidates(rows, role, source, candidates)
        for r in rows:
            if r.get("kind") in {"pdf_block", "table_cell_paragraph"} or not eligible_record(r):
                # Tables, comments and text boxes must not be continued as prose.
                if r.get("story", "document") == "document":
                    section = None
                continue
            text = r["text"].strip()
            title = text.lstrip("#").strip().rstrip("：:")
            label = NUMBER_PREFIX.sub("", title).strip().casefold()
            target = HEADINGS.get(label)
            style = str(r.get("paragraph_style") or "").lower()
            if target or style.startswith(("heading", "title", "toc")) or text.startswith("#") or NUMBER_PREFIX.match(title):
                section = target
                continue
            if not text:
                section = None
            if section and text:
                candidates.setdefault(section, []).append({
                    "source": source, "location": r["location"], "text": text,
                    "role": role,
                })
    return candidates


def eligible_record(record):
    loc = record["location"]
    return (record.get("kind")=="register_row" or loc.startswith(("line:", "page:")) or bool(DIRECT_P.fullmatch(loc) or DIRECT_CELL.fullmatch(loc))) and (
        not record.get("has_review_comment") and not str(record.get("paragraph_style") or "").lower().startswith("toc")
    )


REGISTER_HEADERS=(
    ("附录编号","附件编号","编号","appendix id"),
    ("文件名称","资料名称","附件名称","名称","description"),
    ("状态","归档状态","提供状态","status"),
    ("文件路径","相对路径","文件名","文件","file"),
    ("说明","依据","备注","reason"),
)


def workbook_register(workbook):
    """Read registers by column names, retaining the original worksheet and row."""
    records=[]
    for name,sheet in workbook["sheets"].items():
        rows=sheet.get("rows",[])
        for ix,row in enumerate(rows[:16]):
            labels=[str(v).strip().casefold() for v in row]
            indexes=[[i for i,k in enumerate(labels) if k in names] for names in REGISTER_HEADERS]
            if not all(len(x)==1 for x in indexes):
                continue
            ixes=[x[0] for x in indexes]
            for line,values in enumerate(rows[ix+1:],ix+2):
                if all(i<len(values) and values[i] is not None for i in ixes):
                    fields=[str(values[i]).strip() for i in ixes]
                    if fields[0] and fields[2]:
                        records.append({"text":"\t".join(fields),"location":f"{name}!row:{line}","kind":"register_row"})
            break
    return records
