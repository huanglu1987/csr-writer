"""Descriptive paired measurements under an explicitly specified two-visit SAP rule."""
from statistics import mean
from .errors import ConfigurationError
from .subject_analysis import table
from .subject_data import day, number, yn


def visit_table(kind, domain, participants, treated, arms, spec):
    prefix="lab" if kind=="lab" else "vs"
    code=spec[prefix+"_code"]
    last=spec[prefix+"_visit"]
    visits=("BL",last)
    if last=="BL":
        raise ConfigurationError("随访与基线标记相同")
    observed={};units=set();trace=[]
    for r in domain["rows"]:
        if r["id"] not in participants:
            raise ConfigurationError(f"{r['loc']} 无对应受试者")
        if r[prefix+"_code"]!=code or r["id"] not in treated or r["visit"] not in visits:
            continue
        sid=r["id"];loc=r["loc"]; key=(sid,r["visit"])
        if key in observed or not r[prefix+"_unit"]:
            raise ConfigurationError(f"{loc} 访视重复或单位缺失")
        units.add(r[prefix+"_unit"])
        date=day(r[prefix+"_date"],loc)
        first=day(treated[sid]["first"],loc)
        if (r["visit"]=="BL" and date>first) or (r["visit"]==last and date<=first):
            raise ConfigurationError(f"{loc} 访视时序无法与给药记录核对")
        value=number(r[prefix+"_value"],loc)
        important=yn(r["clin_sig"],loc) if kind=="lab" else False
        observed[key]=(value,important)
        trace.append({"subject":sid,"group":treated[sid]["actual"],"visit":r["visit"],"value":str(value),"source":loc})
    if len(units)!=1:
        raise ConfigurationError("缺少测量或测量单位不唯一")
    unit=units.pop()
    rows=[]
    def summary(label,by_arm):
        rows.append([label+" 观察人数",*[len(q) for q in by_arm]])
        rows.append([label+" 均值（"+unit+"）",*[f"{mean(q):.2f}" if q else "不可估计" for q in by_arm]])
    for v in visits:
        summary(code+" "+v,[[x[0] for (s,w),x in observed.items() if w==v and treated[s]["actual"]==g] for g in arms])
    ids=[s for s in treated if (s,last) in observed and (s,"BL") in observed]
    summary(code+" 配对变化量",[[observed[s,last][0]-observed[s,"BL"][0] for s in ids if treated[s]["actual"]==g] for g in arms])
    if kind=="lab":
        rows.append(["随访临床重要异常人数",*[sum(w==last and x[1] and treated[s]["actual"]==g for (s,w),x in observed.items()) for g in arms]])
    title="实验室检查" if kind=="lab" else "生命体征"
    return table("5.1" if kind=="lab" else "5.2",title+" "+code,"SAF",arms,rows,
                 "均值使用该访视实测值，变化量仅使用基线和随访均可用者。"),trace
