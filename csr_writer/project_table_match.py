"""Find aggregate table boundaries, identifiers and source coordinates in office files."""
import re
from dataclasses import dataclass

from .errors import ConfigurationError

CAPTION = re.compile(r"^(?:表(?:格)?\s*|table\s+|T(?=\d))\d+(?:[.\-–－]\d+)*(?:[\s　：:、.－-]+|$)", re.I)
HEADER = re.compile(r"^(?:指标|项目|参数|评估指标|特征|类别|比较|分析指标|统计量|首选术语|访视|人群|终点|项目名称|metric|measure|parameter|characteristic|category|outcome|endpoint|visit|statistic|comparison|event|population)(?:\s*[(（].*)?$", re.I)
PERSON = re.compile(r"^(?:USUBJID|SUBJID|Subject ID|Participant ID|受试者(?:编号|号|编码)?|患者编号|姓名|随机号)$", re.I)
COMPUTED_STATUS = "按SAP计算，待统计复核"
FINAL = {"定稿","最终版","锁定","已定稿","已锁定","final","locked","validated final"}
STATUS = re.compile(r"^(?:状态|版本状态|发布状态|表状态|status|release status)\s*[:：=]\s*", re.I)
POP = re.compile(r"^(?:分析人群|分析集|人群|population|analysis set)\s*[:：=]\s*(.+)$", re.I)
ID = re.compile(r"^(?:研究编号|方案编号|试验编号|study(?: id| number)|protocol(?: id| no\.?| number)?)\s*[:：=]\s*(.+)$", re.I)
NOTE = re.compile(r"^(?:注[：:]|说明[：:]|来源[：:]|note[s]?[：:]|source[：:])", re.I)


@dataclass(frozen=True)
class Layout:
    title: str
    title_row: int
    header_row: int
    offset: int
    columns: list
    tokens: list
    population: str


def find_layout(rows, *, computed=False):
    titles = []
    for i,row in enumerate(rows):
        for j,value in enumerate(row):
            if isinstance(value,str) and CAPTION.match(value.strip()):
                titles.append((i,j,value.strip()))
    if len(titles) != 1:
        raise ConfigurationError("单表区域须有唯一可识别的原表标题")
    start, _, title = titles[0]
    for i,row in enumerate(rows[:min(len(rows), start + 21)]):
        if i <= start:
            continue
        filled = [j for j,v in enumerate(row) if v not in ("",None)]
        if not filled or len(filled) < 2 or not HEADER.fullmatch(str(row[filled[0]]).strip()):
            continue
        if any(PERSON.fullmatch(str(v).strip()) for v in row):
            raise ConfigurationError("含个体标识的清单不能作为汇总统计表")
        if len(filled)==2 and str(row[filled[0]]).strip().casefold() in {"population","分析人群","分析集"}:
            if re.fullmatch(r"FAS|SAF|SS|m?ITT|PPS|PP|PKS|.*(?:人群|集)|safety population", str(row[filled[1]]).strip(),re.I):
                continue
        left,right=filled[0],filled[-1]+1
        header=row[left:right]
        if not all(header) or len(header)!=len(set(header)):
            raise ConfigurationError("统计表表头缺列或重名")
        tokens=[]
        for r in rows[:i]:
            for k,v in enumerate(r):
                if not v or v.strip()==title:
                    continue
                parts=[x.strip() for x in re.split(r"[｜|；;]",v) if x.strip()]
                tokens.extend(parts)
                if k+1<len(r) and r[k+1] and re.fullmatch(r"(研究编号|方案编号|study id|状态|status|分析集|分析人群|population)",v.strip(),re.I):
                    tokens.append(f"{v.strip()}：{r[k+1].strip()}")
        explicit={m[1].strip() for t in tokens if (m:=POP.match(t))}
        if not explicit:
            explicit={t for t in tokens if re.fullmatch(r"FAS|SAF|SS|ITT|mITT|PPS|PP|PKS|全分析集|安全性分析集|安全集|筛选及随机人群|随机人群|筛选人群|Safety population|All randomized",t,re.I)}
        if len(explicit)!=1:
            raise ConfigurationError("缺少唯一明确的分析人群")
        states={STATUS.sub("",t).strip().casefold() for t in tokens}
        acceptable={COMPUTED_STATUS.casefold()} if computed else FINAL
        if not acceptable.intersection(states) or any(re.search(r"草稿|待定稿|未定稿|未锁定|draft|preliminary|not final|withdrawn|superseded|已作废|已废止|未确认",t,re.I) for t in tokens):
            raise ConfigurationError("表内没有明确的定稿状态，或同时标有草稿")
        return Layout(title,start+1,i+1,left,header,tokens,explicit.pop())
    raise ConfigurationError("未识别统计表列标题或存在多层表头")


def study_matches(layout, study):
    ids={m[1].strip().upper() for t in layout.tokens if (m:=ID.match(t))}
    if ids:
        return ids=={study.upper()}
    return study.upper() in {t.upper() for t in layout.tokens}


def cell_location(sheet_name, sheet, row, col):
    if "locations" in sheet:
        return sheet["locations"][row-1][col-1]
    from openpyxl.utils import get_column_letter
    return f"{sheet_name}!{get_column_letter(col)}{row}"


def data_rows(rows,layout):
    result,notes=[],[]
    end=layout.offset+len(layout.columns)
    for i,row in enumerate(rows[layout.header_row:],layout.header_row+1):
        if None in row:
            raise ConfigurationError("统计表含未核公式或错误值")
        if not any(row):
            continue
        values=row[layout.offset:end]
        if len([v for v in row if v])==1 and any(NOTE.match(v) for v in row if v):
            notes.append(next(v for v in row if v))
            continue
        if not values or not values[0] or any(row[:layout.offset]+row[end:]) or len(values)!=len(layout.columns):
            raise ConfigurationError("表体行与表头边界不一致")
        result.append((i,values))
    if not result:
        raise ConfigurationError("定稿表无结果行")
    return result,notes


def word_tables(records):
    """A Word caption and its following native table share an evidence region."""
    tables={}
    context=[]
    active=None
    last_table=None
    for record in records:
        if record.get("story")!="document":
            continue
        text=record["text"].strip()
        location=record["location"]
        if record.get("kind")=="table_cell_paragraph":
            key=record["table_id"]
            if key!=last_table:
                active=None
                if context:
                    active={"rows":[[value] for value,_ in context],"locations":[[loc] for _,loc in context]}
                    tables[key]=active
                last_table=key
                prefix_length=len(context)
                context=[]
            if active is None:
                continue
            row=prefix_length+record["physical_row_index"]-1
            col=record["cell_index"]-1
            while len(active["rows"])<=row:
                active["rows"].append([])
                active["locations"].append([])
            values=active["rows"][row]
            while len(values)<=col:
                values.append("")
                active["locations"][row].append("")
            if values[col] != "" or record.get("has_review_comment"):
                values[col]=None  # ambiguous or annotated physical cell
            else:
                values[col]=text
            active["locations"][row][col]=location
        elif re.fullmatch(r"word/document\.xml:/w:document/w:body/w:p(?:\[\d+\])?",location):
            if CAPTION.match(text):
                context=[(text,location)]
            elif context and text:
                context.append((text,location))
            elif last_table and active and NOTE.match(text):
                active["rows"].append([text])
                active["locations"].append([location])
            elif text:
                active=None
    return tables
