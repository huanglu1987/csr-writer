"""Second implementation. Read physical input rows again; never consume first-pass subject selections or fits."""
from collections import defaultdict
from contextlib import closing
import csv
from datetime import date, timedelta, datetime
import math
from decimal import Decimal
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile

from .project_answers import digest


def read_rows(root,domain):
    src=root/domain["source"]
    if src.suffix.lower()==".xlsx":
        from openpyxl import load_workbook
        with closing(load_workbook(src,read_only=True,data_only=False)) as wb:
            rows=list(wb[domain["sheet"]].values)
    else:
        with src.open(encoding="utf-8-sig",newline="") as f:
            rows=list(csv.reader(f,delimiter="\t" if src.suffix.lower()==".tsv" else ","))
    headers=list(rows[0])
    indices={k:headers.index(v) for k,v in domain["columns"].items()}
    def value(x):
        if isinstance(x,datetime):
            return x.date().isoformat()
        return "" if x is None else str(x).strip()
    return [{**{k:value(r[i]) for k,i in indices.items()},"row":n} for n,r in enumerate(rows[1:],2) if any(v not in (None,"") for v in r)]


def verify(root, domains, study, arms, specs, tables, overrides):
    data={k:read_rows(root,v) for k,v in domains.items() if k in {"subjects","exposure","scores","events","lab","vital"}}
    yes=lambda x: x.upper() in {"Y","YES","1","是"}
    dm={r["id"]:r for r in data["subjects"] if r["study"].upper()==study.upper() and yes(r["rand"])}
    exposure=defaultdict(set)
    actual={}
    dose_rows=defaultdict(list)
    for r in data.get("exposure",[]):
        if Decimal(r["dose"])>0:
            sid=r["id"]
            exposure[sid].add(date.fromisoformat(r["dose_date"]))
            actual[sid]=r["actual"]
            dose_rows[sid,r["dose_date"]].append(r["row"])
    groups={a:{s for s,r in dm.items() if r["arm"]==a} for a in arms}
    safe={a:{s for s in actual if actual[s]==a} for a in arms}
    expected=defaultdict(dict)
    def put(code,label,vals):
        expected[code][label]=[str(x) for x in vals]
    def pct(n,d):
        return f"{n}/{d}（{100*n/d:.1f}%）" if d else "不可估计（分母为0）"
    put("1.1","已随机人数",[len(groups[a]) for a in arms])
    for label in ["完成","提前结束"]:
        put("1.1",label,[sum(dm[s].get("end_status")==label for s in groups[a]) for a in arms])
    put("1.2","ITT（随机组）",[len(groups[a]) for a in arms])
    put("1.2","SAF（实际用药组）",[len(safe[a]) for a in arms])
    put("2.1","实际用药人数",[len(safe[a]) for a in arms])
    put("2.1","总用药人日",[sum(len(exposure[s]) for s in safe[a]) for a in arms])
    codes={t["number"] for t in tables}
    if "1.3" in codes:
        ages=[[Decimal(dm[s]["age"]) for s in groups[a]] for a in arms]
        put("1.3","平均年龄（岁）",[f"{statistics.mean(v):.1f}" for v in ages])
        put("1.3","年龄样本标准差（岁）",[f"{statistics.stdev(v):.1f}" if len(v)>1 else "不可估计" for v in ages])
        put("1.3","女性 n/N（%）",[pct(sum(dm[s]["sex"] in {"F","女"} for s in groups[a]),len(groups[a])) for a in arms])
    sp=specs.get("safety",{})
    events=[]
    if "follow" in sp and "grade" in sp and "related_terms" in sp:
        for r in data.get("events",[]):
            sid=r["id"]
            if sid not in actual:
                continue
            start=min(exposure[sid]); end=max(exposure[sid])+timedelta(days=int(sp["follow"]))
            onset=date.fromisoformat(r["onset"])
            when=onset if onset>=start else date.fromisoformat(r["worse"]) if r["worse"] else None
            if when and start<=when<=end:
                events.append({**r,"related":overrides.get((sid,r["seq"]),r["related"])})
        def cases(code,label,predicate):
            put(code,label,[pct(len({r["id"] for r in events if r["id"] in safe[a] and predicate(r)}),len(safe[a])) for a in arms])
        cases("4.1","至少一次TEAE n/N（%）",lambda r:True)
        put("4.1","TEAE事件数",[sum(r["id"] in safe[a] for r in events) for a in arms])
        cases("4.1",sp["grade"]+"级及以上TEAE n/N（%）",lambda r:int(r["grade"])>=int(sp["grade"]))
        cases("4.1","已经判定药物相关的TEAE n/N（%）",lambda r:r["related"] in re.split("[、，,及和]",sp["related_terms"]))
        cases("4.1","相关性待定的TEAE n/N（%）",lambda r:r["related"] in {"","未知","待评估","待定"})
        cases("4.3","致死TEAE n/N（%）",lambda r:yes(r["fatal"]))
        cases("4.3","严重TEAE n/N（%）",lambda r:yes(r["serious"]))
        for pt in {r["pt"] for r in events}:
            cases("4.2",pt,lambda r:r["pt"]==pt)
    for code,domain,prefix in [("5.1","lab","lab"),("5.2","vital","vs")]:
        if code not in codes:
            continue
        test=specs["descriptors"][prefix+"_code"]; last=specs["descriptors"][prefix+"_visit"]
        selected={(r["visit"],r["id"]):r for r in data[domain] if r[prefix+"_code"]==test and r["id"] in actual and r["visit"] in ("BL",last)}
        unit=next(iter(selected.values()))[prefix+"_unit"]
        for visit in ["BL",last,"配对变化量"]:
            vals=[]
            for a in arms:
                current=[]
                for sid in safe[a]:
                    if visit=="配对变化量":
                        if (last,sid) in selected and ("BL",sid) in selected:
                            current.append(Decimal(selected[last,sid][prefix+"_value"])-Decimal(selected["BL",sid][prefix+"_value"]))
                    elif (visit,sid) in selected:
                        current.append(Decimal(selected[visit,sid][prefix+"_value"]))
                vals.append(current)
            put(code,f"{test} {visit} 观察人数",[len(v) for v in vals])
            put(code,f"{test} {visit} 均值（{unit}）",[f"{statistics.mean(v):.2f}" if v else "不可估计" for v in vals])
        if domain=="lab":
            put(code,"随访临床重要异常人数",[sum(yes(r["clin_sig"]) for (v,s),r in selected.items() if v==last and s in safe[a]) for a in arms])
    sp=specs.get("efficacy",{})
    model={}
    if "ancova_role" in sp and not sp.get("adjust_models") and shutil.which("Rscript") and "3.2" in codes:
        groupscores=defaultdict(list)
        for r in data["scores"]:
            if r["id"] in dm and r["test"]==sp["code"]:
                groupscores[r["id"]].append(r)
        sample=[]
        for sid,observations in sorted(groupscores.items()):
            origin=date.fromisoformat(dm[sid]["rand_date"])
            visits=[];bl=[]
            for r in observations:
                day=(date.fromisoformat(r["meas_date"])-origin).days+1
                if r["visit"].upper()=="BL" and 1-int(sp["baseline_days"])<=day<=1:
                    bl.append(r)
                if r["visit"].upper()!="BL" and int(sp["start"])<=day<=int(sp["end"]):
                    visits.append((abs(day-int(sp["target"])),day if sp["tie"]=="较早" else -day,r["row"],r))
            if len(bl)==1 and visits:
                v=min(visits)[3]
                if bl[0]["value"] and v["value"]:
                    sample.append((sid,int(dm[sid]["arm"]==sp["test"]),bl[0]["value"],v["value"],bl[0]["row"],v["row"]))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"input.csv"
            with path.open("w",newline="") as f:
                w=csv.writer(f);w.writerow(["id","arm","base","end","blrow","endrow"]);w.writerows(sample)
            script='a<-commandArgs(TRUE);d<-read.csv(a[1]);m<-lm(I(end-base)~arm+base,d);s<-coef(summary(m))["arm",];ci<-confint(m,"arm",level=as.numeric(a[2]));cat(sprintf("%.15g",c(s[1],ci,s[4],s[2],df.residual(m))),sep="\\n")'
            proc=subprocess.run(["Rscript","--vanilla","-e",script,str(path),str(float(sp["model_conf"])/100)],text=True,capture_output=True,timeout=90)
        if proc.returncode==0:
            est,lo,hi,p,se,df=map(float,proc.stdout.split())
            label=f"第{sp['week']}周{sp['endpoint']}变化量组间差（{sp['ancova_role']}分析，ANCOVA，{sp['test']}减{sp['ref']}）"
            put("3.2",label,[f"{est:.3f}",f"{lo:.3f} 至 {hi:.3f}","<0.0001" if p<.0001 else f"{p:.4f}"])
            model=dict(engine="R stats::lm / confint",n=len(sample),estimate=est,low=lo,high=hi,p=p,se=se,df=df,subjects=[(r[0],r[4],r[5]) for r in sample])
    results={}
    for t in tables:
        key=t["number"]; exp=expected.get(key)
        if not exp:
            results[key]=dict(status="未覆盖",checks=[])
            continue
        checks=[]
        seen=set()
        for row in t["rows"]:
            label=row[0]; seen.add(label)
            for j,(c,v) in enumerate(zip(t["columns"],row[1:])):
                oracle=exp.get(label,[None]*len(t["columns"]))[j]
                checks.append(dict(row=label,column=c,calculated=v,independent=oracle,match=v==oracle))
        for label in set(exp)-seen:
            if key=="1.1" and "disposition" not in specs.get("_group_rules",{}):
                continue
            checks.append(dict(row=label,match=False,issue="计算表缺少独立复算行"))
        results[key]=dict(status="一致" if all(r["match"] for r in checks) else "不一致",checks=checks)
    return {"tables":results,"ancova":model,"sources":{k:v["source"] for k,v in domains.items()},
            "same_day_administrations":[dict(subject=sid,date=d,source_rows=rows,group=actual[sid]) for (sid,d),rows in dose_rows.items() if len(rows)>1],
            "events_in_window":[[e["id"],e["seq"],e["row"]] for e in events],"adjudication_digest":digest(sorted((a,b,v) for (a,b),v in overrides.items()))}



def compare_model(checks, derivation):
    """Compare unrounded statistics and the actual source row pair for every participant."""
    oracle=checks.get("ancova")
    if not oracle:
        return
    fit=derivation["models"]["ancova"]
    comparison=[]
    for field in ("estimate","low","high","p","se","df","subjects"):
        independent=oracle["n" if field=="subjects" else field]
        computed=fit[field]
        comparison.append(dict(metric=field,computed=computed,independent=independent,
                               match=math.isclose(computed,independent,rel_tol=1e-8,abs_tol=1e-10)))
    independent={s:(int(b),int(v)) for s,b,v in oracle["subjects"]}
    mismatches=[]
    def position(x):
        found=re.search(r"(?:第|!.*?)([0-9]+)(?:行)?$",x or "")
        return int(found[1]) if found else None
    for sid,r in derivation["participants"].items():
        last=r["visits"][-1]
        pair=(position(r["baseline"]),position(last["row"])) if last["change"] is not None else None
        if pair!=independent.get(sid):
            mismatches.append(dict(subject=sid,production=pair,independent=independent.get(sid),baseline_source=r["baseline"],endpoint_source=last["row"]))
    oracle.update(statistics_checks=comparison,subject_row_disagreements=mismatches)
    if mismatches or any(not c["match"] for c in comparison):
        checks["tables"]["3.2"]["status"]="不一致"
