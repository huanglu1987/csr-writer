"""Select each table separately and retain the full coordinate-level comparison."""
from collections import defaultdict
from decimal import Decimal
import re
from .project_answers import digest
import unicodedata

from .errors import ConfigurationError
from .io_utils import load_json_unique
from .project_table_match import CAPTION, cell_location, data_rows, find_layout, study_matches


def norm(x):
    return re.sub(r"\s+","",unicodedata.normalize("NFKC",str(x))).casefold()


def same_value(a,b):
    if a is None or b is None:
        return False
    x,y=norm(a),norm(b)
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?",x) and re.fullmatch(r"[-+]?\d+(?:\.\d+)?",y):
        return Decimal(x)==Decimal(y)
    return x==y


def reconcile(out, inventory, book, tables, study, cutoff, qc, review):
    computed={}
    by_title={}
    for t in tables:
        key=t["number"]
        computed[key]=t
        by_title[norm(CAPTION.sub("",t["title"]))]=key
    external=defaultdict(list)
    other=[]
    for src,meta in inventory.items():
        if src==book or meta.get("role")!="tfl" or not meta.get("extraction"):
            continue
        for sheet,region in load_json_unique(out/meta["extraction"]).get("sheets",{}).items():
            try:
                rows=region["rows"]
                layout=find_layout(rows)
                if not study_matches(layout,study):
                    continue
                values,notes=data_rows(rows,layout)
            except (ConfigurationError,KeyError,ValueError):
                continue
            text=CAPTION.sub("",layout.title)
            key=by_title.get(norm(text))
            code=re.search(r"\d+(?:\.\d+)*",layout.title)[0]
            if not key and code in computed:
                key=code
            if not key:
                # A renumbered or retitled delivery can still identify one table by its complete data contract.
                same=[k for k,t in computed.items() if norm(t["population"])==norm(layout.population)
                      and {norm(c) for c in t["columns"]}=={norm(c) for c in layout.columns[1:]}
                      and {norm(r[0]) for r in t["rows"]}=={norm(v[0]) for _,v in values}]
                if len(same)==1:
                    key=same[0]
            record=dict(source=src,sheet=sheet,title=layout.title,population=layout.population,
                        version=[re.split("[:：]",v,1)[1].strip() for v in layout.tokens if re.match(r"版本\s*[:：]",v)],notes=notes,cutoff=[v.split("：")[-1].split(":")[-1].strip() for v in layout.tokens
                                if re.match(r"数据截止日\s*[:：]",v)],sha256=meta["sha256"])
            if key is None:
                other.append({**record,"status":"仅有外部表，交由原件匹配","independent":"未覆盖"})
                continue
            t=computed[key]
            record["table_digest"]=digest([layout.title,layout.population,layout.columns,layout.tokens,values,notes])
            diff=[]
            if norm(text)!=norm(CAPTION.sub("",t["title"])) or norm(layout.population)!=norm(t["population"]):
                diff.append({"reason":"表义或分析集不同"})
            if len(record["version"])!=1:
                diff.append({"reason":"外部统计输出缺少唯一版本"})
            if any(re.search("排除|剔除|不含|不纳入|插补|观察窗|仅纳入",n) and norm(n.lstrip("注：: ")) not in norm(t["note"]) for n in notes):
                diff.append({"reason":"外部表脚注包含需核对的分析条件","external":notes,"calculated":t["note"]})
            if not cutoff or record["cutoff"]!=[cutoff]:
                diff.append({"reason":"截止日缺失或与研究数据不同","expected":cutoff,"external":record["cutoff"]})
            cols=layout.columns[1:]
            cells={}
            for row,v in values:
                for j,(c,x) in enumerate(zip(cols,v[1:]),1):
                    addr=(norm(v[0]),norm(c))
                    if addr in cells:
                        diff.append({"reason":"行列标签重复","row":v[0],"column":c})
                    cells[addr]=(x,cell_location(sheet,region,row,layout.offset+j+1))
            for i,row in enumerate(t["rows"],4):
                for j,(col,x) in enumerate(zip(t["columns"],row[1:]),2):
                    from openpyxl.utils import get_column_letter
                    pair=cells.pop((norm(row[0]),norm(col)),(None,None))
                    if not same_value(pair[0],x):
                        diff.append(dict(row=row[0],column=col,external=pair[0],external_cell=pair[1],
                                         calculated=x,calculated_cell=f"{key}!{get_column_letter(j)}{i}"))
            diff.extend({"reason":"外部表额外单元格","external":v,"external_cell":loc} for v,loc in cells.values())
            external[key].append({**record,"differences":diff})

    result=[]
    for key,t in computed.items():
        rows=external[key]
        checked=qc.get(key,{"status":"未覆盖","checks":[]})
        entry=dict(number=key,title=t["title"],computed_source=book,external=rows,independent=checked["status"])
        def skip(src,sheet):
            inventory[src].setdefault("withheld_sheets",[]).append(sheet)
        different=any(r["differences"] for r in rows) or len(rows)>1
        failed=checked["status"]=="不一致"
        if failed:
            status="独立复算不一致，暂缓"
        elif different:
            options=["采用逐例计算"] if len(rows)!=1 or any(d.get("reason") for r in rows for d in r["differences"]) else ["采用逐例计算","采用定稿表"]
            evidence=dict(releases=rows,calculation=t,independent=checked)
            reply=review.ask("TFL-"+key,"统计负责人",t["title"]+"存在版本或单元格差异，请确定本稿采用的结果及理由。",
                             ["12.1","14.3"] if key=="2.1" else ["11","12","14"],evidence,options,dependencies=[t,checked,[(r["source"],r["sheet"],r["table_digest"]) for r in rows]])
            status=reply["choice"] if reply else "差异待裁定"
        else:
            status="采用定稿表" if rows else "逐例补算"
        if status not in {"采用逐例计算","逐例补算"}:
            skip(book,key)
        if status!="采用定稿表":
            for r in rows:
                skip(r["source"],r["sheet"])
        entry.update(status=status,selected=({"source":rows[0]["source"],"sheet":rows[0]["sheet"]} if status=="采用定稿表"
                                   else {"source":book,"sheet":key} if status in {"采用逐例计算","逐例补算"} else None))
        result.append(entry)
    return result+other
