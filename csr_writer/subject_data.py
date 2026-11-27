"""Load supported participant domains by columns; retain file, sheet and row references."""
import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from .errors import ConfigurationError

ALIASES = {
    "study": ("STUDYID","研究编号"), "id": ("USUBJID","SUBJID","受试者编号"),
    "rand": ("RANDYN","是否随机"), "arm": ("ARM","随机组"), "rand_date": ("RANDDT","随机日期"),
    "rescue": ("RESCUEYN","使用救援"), "rescue_date": ("RESCUEDT","首次救援日期"),
    "dosed": ("EXYN","是否实际用药"), "narrative": ("AENARR","病例原文"),
    "age": ("AGE","年龄"), "sex": ("SEX","性别"),
    "ae_complete": ("AECOMP","不良事件收集完成"), "end_status": ("DSSTATUS","研究完成状态"),
    "stop_yn":("TRTSTOP","永久停药标志"),"stop_date":("TRTSTOPDT","永久停药日期"),
    "actual": ("EXTRT","实际用药组"), "dose": ("EXDOSE","给药剂量"), "dose_date": ("EXSTDAT","给药日期"),
    "test": ("TESTCD","QSTESTCD","指标代码"), "visit": ("VISIT","访视"),
    "value": ("QSSTRESN","数值","评分"), "meas_date": ("QSDAT","测量日期"),
    "stratum":("RANDSTR","随机登记层"), "event_code":("EVTESTCD",),"event_date":("EVFIRSTDT",),"last_known":("EVLASTDT",),
    "cat_code":("CATCD","分类终点代码"),"cat_date":("CATDT","分类评估日期"),"category":("CATVAL","实测类别"),
    "task_code":("TASKCD",),"task_date":("TASKDT",),"task_done":("TASKYN",),
    "lab_code":("LBTESTCD",),"lab_value":("LBSTRESN",),"lab_unit":("LBSTRESU",),
    "lab_date":("LBDTC",),"clin_sig":("LBCLSIG",),
    "vs_code":("VSTESTCD",),"vs_value":("VSSTRESN",),"vs_unit":("VSSTRESU",),"vs_date":("VSDTC",),
    "seq": ("AESEQ","事件序号"), "pt": ("AEDECOD","首选术语"), "onset": ("AESTDAT","发生日期"),
    "worse": ("AEWORSDT","加重日期"), "serious": ("AESER","是否严重"), "fatal": ("AESDTH","是否致死"),
    "grade": ("AETOXGR","严重程度分级"), "related": ("AEREL","研究者相关性"),
}
DOMAINS = {
    "categorical":{"cat_code","cat_date","category"},
    "stops":{"stop_yn","stop_date"},
    "time_event":{"event_code","event_date","last_known"},"task":{"task_code","task_date","task_done"},
    "lab": {"lab_code","lab_value","lab_unit","lab_date","clin_sig","visit"},
    "vital": {"vs_code","vs_value","vs_unit","vs_date","visit"},
    "subjects": {"rand","arm","rand_date"},
    "exposure": {"actual","dose","dose_date"},
    "scores": {"test","visit","value","meas_date"},
    "events": {"seq","pt","onset","worse","serious","fatal","grade","related"},
}
NAMES={"categorical":"逐次分类结局","stops":"永久停药处置记录","time_event":"首次事件与末次联系","task":"逐例任务评估","lab":"逐例实验室数据","vital":"逐例生命体征","subjects":"随机与受试者","exposure":"实际给药","scores":"疗效测量","events":"不良事件"}


def day(value, origin):
    try:
        return date.fromisoformat(value)
    except (ValueError,TypeError):
        raise ConfigurationError(f"{origin} 日期为空、不完整或不是 ISO 日期")


def number(value, origin):
    try:
        n=Decimal(value)
        if not n.is_finite():
            raise ValueError
        return n
    except (ValueError,TypeError,InvalidOperation):
        raise ConfigurationError(f"{origin} 缺少有效数值")


def yn(value, origin):
    if value.upper() in {"是","Y","YES","1"}:
        return True
    if value.upper() in {"否","N","NO","0"}:
        return False
    raise ConfigurationError(f"{origin} 是/否标志缺失或未识别")


def columns(header):
    normalized=[str(v or "").strip().upper() for v in header]
    result={}
    for field,aliases in ALIASES.items():
        indexes=[i for i,v in enumerate(normalized) if v in aliases]
        if len(indexes)>1:
            raise ConfigurationError(f"字段 {field} 匹配到多个源列")
        if indexes:
            result[field]=indexes[0]
    return result


def collect(root, inventory, study, *, covariates=()):
    """A faulty domain does not prevent independent analyses using valid domains."""
    found=defaultdict(list)
    errors={}
    def ingest(header, iterator, filename, sheet=""):
        keys={str(k or "").strip().upper() for k in header}
        candidates=[k for k, fields in DOMAINS.items()
                    if all(keys.intersection(ALIASES[field]) for field in fields)]
        if "subjects" in candidates and "stops" in candidates:
            candidates.remove("stops")  # Disposition may be on the participant record itself.
        for domain in candidates:
            try:
                index=columns(header)
                if not {"study","id"}<=index.keys():
                    raise ConfigurationError("缺少研究编号或受试者编号列")
                if len(candidates)>1:
                    raise ConfigurationError("同一区域同时匹配多个原始数据域")
                cov_idx={}
                if domain=="subjects":
                    normalized=[str(v or "").strip().upper() for v in header]
                    for field in covariates:
                        pos=[i for i,v in enumerate(normalized) if v==field]
                        if len(pos)==1:
                            cov_idx[field]=pos[0]
                rows=[]
                for line, cells in iterator:
                    if not any(v is not None and str(v).strip() for v in cells):
                        continue
                    location=f"{filename}{' / '+sheet if sheet else ''} 第{line}行"
                    if len(cells)!=len(header):
                        raise ConfigurationError(f"{location} 列数不符")
                    def display(v):
                        if isinstance(v,datetime) and v.time()==datetime.min.time():
                            return v.date().isoformat()
                        return "" if v is None else str(v).strip()
                    item={field:display(cells[i]) for field,i in index.items()}
                    if item["study"].upper()!=study.upper() or not item["id"]:
                        raise ConfigurationError(f"{location} 研究编号不符或个例编号为空")
                    if cov_idx:
                        item["covariates"]={k:display(cells[i]) for k,i in cov_idx.items()}
                    item["loc"]=location
                    rows.append(item)
                found[domain].append({"source":filename,"sheet":sheet,"rows":rows,
                                      "columns":{k:str(header[i]) for k,i in index.items()}})
            except ConfigurationError as exc:
                errors[domain]=f"{filename} {exc}"
    for filename, entry in inventory.items():
        fmt=entry["format"]
        if fmt not in {".csv",".tsv",".xlsx"}:
            continue
        if fmt==".xlsx":
            from openpyxl import load_workbook
            book=load_workbook(root/filename,read_only=True,data_only=False,keep_links=False)
            try:
                for ws in book.worksheets:
                    if ws.sheet_state!="visible":
                        continue
                    data=ws.values
                    ingest(next(data,()),enumerate(data,2),filename,ws.title)
            finally:
                book.close()
        else:
            with (root/filename).open(encoding="utf-8-sig",newline="") as f:
                reader=csv.reader(f,delimiter="\t" if fmt==".tsv" else ",")
                header=next(reader,[])
                ingest(header,((reader.line_num,row) for row in reader),filename)
    domains={}
    for name in DOMAINS:
        if len(found[name])==1 and name not in errors:
            domains[name]=found[name][0]
        elif len(found[name])>1:
            errors[name]=NAMES[name]+"数据域有多个原件，需确定所用版本"
        else:
            errors.setdefault(name,"缺少唯一可用的"+NAMES[name]+"数据域")
    return domains,errors
