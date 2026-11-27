"""Prespecified responder and exposure-window analyses. Each decision retains its raw row locator."""
from collections import Counter, defaultdict
from decimal import Decimal
from math import hypot, sqrt
from statistics import NormalDist, mean, stdev

from .errors import ConfigurationError
from .subject_data import day, number, yn


def wilson(x, n, level):
    z=NormalDist().inv_cdf((1+level/100)/2)
    p=x/n
    den=1+z*z/n
    center=(p+z*z/(2*n))/den
    radius=z/den*sqrt(p*(1-p)/n+z*z/(4*n*n))
    return max(0.,center-radius),min(1.,center+radius)


def percentage(x,n):
    return f"{x}/{n}（{100*x/n:.1f}%）" if n else "不可估计（分母为0）"


def table(code, title, population, columns, rows, note):
    return {"number":code,"title":f"表 {code} {title}","population":population,
            "columns":columns,"rows":[[str(x) for x in row] for row in rows],"note":note}


def subjects(domain,arms):
    people={}
    for r in domain["rows"]:
        sid,loc=r["id"],r["loc"]
        if sid in people:
            raise ConfigurationError(f"{loc} 受试者主记录重复")
        randomized=yn(r["rand"],loc)
        if randomized:
            if r["arm"] not in arms:
                raise ConfigurationError(f"{loc} 随机组与 SAP 不符")
            day(r["rand_date"],loc)
        elif r["arm"] or r["rand_date"]:
            raise ConfigurationError(f"{loc} 筛选未随机标志与分组或日期冲突")
        people[sid]={**r,"randomized":randomized}
    if not people or any(not any(p["randomized"] and p["arm"]==g for p in people.values()) for g in arms):
        raise ConfigurationError("预设治疗组缺少随机受试者；核对随机化导出是否完整")
    return people


def exposure(people,domain,arms):
    by_subject=defaultdict(list)
    for row in domain["rows"]:
        p=people.get(row["id"])
        if p is None:
            raise ConfigurationError(f"{row['loc']} 无对应受试者")
        n=number(row["dose"],row["loc"])
        if n<0:
            raise ConfigurationError(f"{row['loc']} 出现负剂量")
        if not n:
            continue
        d=day(row["dose_date"],row["loc"])
        if not p["randomized"] or d<day(p["rand_date"],p["loc"]) or row["actual"] not in arms:
            raise ConfigurationError(f"{row['loc']} 实际治疗信息与研究人群或时序冲突")
        by_subject[row["id"]].append((d,row["actual"],row["loc"]))
    for sid,p in people.items():
        if p["randomized"] and sid not in by_subject and "dosed" not in p:
            raise ConfigurationError(f"{p['loc']} 无给药记录，也缺少确认未用药的原始标志")
        if p["randomized"] and "dosed" in p and yn(p["dosed"],p["loc"]) != (sid in by_subject):
            raise ConfigurationError(f"{p['loc']} 实际用药标志与给药明细不一致")
    treated={}
    for sid,rows in by_subject.items():
        arms_taken={g for _,g,_ in rows}
        if len(arms_taken)!=1:
            raise ConfigurationError(f"{sid} 实际接受了不同治疗，SAP 未定义安全性归组")
        dates=sorted(set(d for d,_,_ in rows))
        treated[sid]={"actual":arms_taken.pop(),"first":dates[0].isoformat(),"last":dates[-1].isoformat(),
                      "days":len(dates),"source_rows":[loc for _,_,loc in rows]}
    return treated


def endpoint_rows(people, domain, arms, spec, protocol):
    from .sap_rules import RULES, canonical_sentence
    import re
    method=spec["method"]
    week,target,start,end,base_days=[int(spec[k]) for k in ("week","target","start","end","baseline_days")]
    lo,hi=[Decimal(spec[k]) for k in ("low","high")]
    if not (week>0 and 1<start<=target<=end and lo<hi):
        raise ConfigurationError("终点量表或访视窗参数不一致")
    if method=="response":
        cutoff=int(spec["rescue_day"])
        threshold=Decimal(spec["threshold"])
        conf,rd_conf=[float(spec[k]) for k in ("confidence","rd_confidence")]
        if not (0<cutoff<=end and 0<threshold<=hi-lo and 0<conf<100 and 0<rd_conf<100
                and {spec["test"],spec["ref"]}==set(arms) and len(arms)==2):
            raise ConfigurationError("应答阈值、置信水平或率差方向参数不一致")
    definitions=[]
    for r in protocol:
        for m in re.finditer(RULES["efficacy"][method], canonical_sentence(r["text"]), re.I):
            definitions.append(m.groupdict())
    if not definitions or any(any(d[k].strip()!=spec[k] for k in d) for d in definitions):
        raise ConfigurationError("方案与 SAP 的终点名称、代码、周次、变化方向或阈值未能唯一核对")
    if method=="response" and not {"rescue","rescue_date"}<=next(iter(people.values())).keys():
        raise ConfigurationError("逐例资料缺少救援使用状态或首次日期")
    scores=defaultdict(list)
    for row in domain["rows"]:
        if row["id"] not in people:
            raise ConfigurationError(f"{row['loc']} 评分无对应受试者")
        if row["test"]!=spec["code"]:
            continue
        d=day(row["meas_date"],row["loc"])
        if not row["visit"]:
            raise ConfigurationError(f"{row['loc']} 测量缺失访视标识")
        score=number(row["value"],row["loc"]) if row["value"] else None
        if score is not None and not lo<=score<=hi:
            raise ConfigurationError(f"{row['loc']} 评分超出 SAP 范围")
        scores[row["id"]].append((d,row["visit"],score,row["loc"]))
    if not scores:
        raise ConfigurationError("没有找到方案指定指标的测量记录")
    subjects_trace={}
    for sid,p in people.items():
        if not p["randomized"]:
            continue
        origin=day(p["rand_date"],p["loc"])
        baseline=[]; visits=[]; inspected=[]
        for d,visit,value,loc in scores[sid]:
            offset=(d-origin).days
            inspected.append({"row":loc,"day":offset+1,"visit":visit,"value":str(value) if value is not None else None})
            if visit.upper()=="BL" and -base_days<=offset<=0:
                baseline.append((offset+1,value,loc))
            elif visit.upper()!="BL" and start<=offset+1<=end:
                visits.append((offset+1,value,loc))
        if len(baseline)>1:
            raise ConfigurationError(f"{sid} 有多条合格基线，SAP 要求唯一")
        visits.sort(key=lambda rec:(abs(rec[0]-target),rec[0] if spec["tie"]=="较早" else -rec[0]))
        if len(visits)>1 and visits[0][0]==visits[1][0]:
            raise ConfigurationError(f"{sid} 最优访视日出现两条测量，SAP 未规定同日取舍")
        rescue_day=None
        if method=="response":
            rescued=yn(p["rescue"],p["loc"])
            if bool(p["rescue_date"])!=rescued:
                raise ConfigurationError(f"{p['loc']} 救援状态与日期不一致")
            rescue_day=(day(p["rescue_date"],p["loc"])-origin).days+1 if rescued else None
            if rescue_day is not None and rescue_day<1:
                raise ConfigurationError(f"{p['loc']} 救援早于随机")
        b=baseline[0][1] if baseline else None
        f=visits[0][1] if visits else None
        change=f-b if b is not None and f is not None else None
        if method=="change":
            status="观察病例" if change is not None else "缺失未纳入"
        elif rescue_day is not None and rescue_day<=cutoff:
            status="救援按未应答"
        elif change is None:
            status="缺失按未应答"
        else:
            improvement=change if spec["direction"]=="上升" else -change
            status="应答" if improvement>=threshold else "观察未应答"
        subjects_trace[sid]={"group":p["arm"],"source":p["loc"],"status":status,
            "baseline":baseline[0][2] if baseline else None,"followup":visits[0][2] if visits else None,
            "baseline_value":str(b) if b is not None else None,"followup_value":str(f) if f is not None else None,
            "change":str(change) if change is not None else None,"rescue_day":rescue_day,"measurements":inspected}
    return subjects_trace


def efficacy(people, domain, arms, spec, protocol):
    subjects_trace=endpoint_rows(people,domain,arms,spec,protocol)
    method=spec["method"]
    week=int(spec["week"])
    if method=="change":
        cols=[]
        for g in arms:
            obs=[Decimal(r["change"]) for r in subjects_trace.values() if r["group"]==g and r["change"] is not None]
            cols.append([f"{mean(obs):.2f}" if obs else "不可估计",f"{stdev(obs):.2f}" if len(obs)>1 else "不可估计",len(obs)])
        rows=[[label,*vals] for label,vals in zip(("随访相对基线平均变化（分）","变化量样本标准差（分）","观察病例人数"),zip(*cols))]
        return [table("3.1",f"主要终点 第{week}周"+spec["endpoint"]+"变化量","ITT",arms,rows,
                      "ITT按随机组，仅对成对观察值作描述，保留救援后测量。")],subjects_trace
    conf,rd_conf=[float(spec[k]) for k in ("confidence","rd_confidence")]
    counts={g:Counter(r["status"] for r in subjects_trace.values() if r["group"]==g) for g in arms}
    n={g:sum(counts[g].values()) for g in arms}
    x={g:counts[g]["应答"] for g in arms}
    def interval(bounds):
        return f"{100*bounds[0]:.1f}% 至 {100*bounds[1]:.1f}%"
    rows=[[f"第{week}周应答 n/N（%）",*[percentage(x[g],n[g]) for g in arms]],
          [f"应答率双侧{conf:g}% Wilson置信区间",*[interval(wilson(x[g],n[g],conf)) for g in arms]]]
    rows.extend([k,*[counts[g][k] for g in arms]] for k in ("观察未应答","救援按未应答","缺失按未应答"))
    a,b=spec["test"],spec["ref"]
    pa,pb=x[a]/n[a],x[b]/n[b]
    la,ua=wilson(x[a],n[a],rd_conf); lb,ub=wilson(x[b],n[b],rd_conf)
    diff=pa-pb
    bounds=(diff-hypot(pa-la,ub-pb),diff+hypot(ua-pa,pb-lb))
    results=[table("3.1","主要终点 "+spec["endpoint"]+"应答率","ITT",arms,rows,
                   f"按随机组；救援与缺失处理依 SAP。两者同时发生时，未应答原因优先列为救援。"),
             table("3.2","主要终点应答率差","ITT",["估计值",f"{rd_conf:g}%置信区间"],
                   [[f"{a}减{b}（百分点）",f"{100*diff:.1f}",f"{100*bounds[0]:.1f} 至 {100*bounds[1]:.1f}"]],
                   f"单位为百分点；双侧{rd_conf:g}% Newcombe 无连续性校正区间。")]
    return results,subjects_trace


def safety(people,treated,domain,arms,spec):
    follow,grade_cut=int(spec["follow"]),int(spec["grade"])
    if any("ae_complete" not in people[s] or not yn(people[s]["ae_complete"],people[s]["loc"]) for s in treated):
        raise ConfigurationError("存在不良事件收集完成状态未核实的安全集受试者")
    import re
    positive={x.strip() for x in re.split("[、，,及和]",spec["related_terms"]) if x.strip()}
    if not positive or positive & {"无关","待定","待评估","未知"}:
        raise ConfigurationError("SAP 中相关类别与无关或未判定分类重叠")
    events=[]; seen=set()
    for row in domain["rows"]:
        sid,loc=row["id"],row["loc"]
        if sid not in people or not row["seq"] or (sid,row["seq"]) in seen:
            raise ConfigurationError(f"{loc} 事件编号重复、缺失或受试者不存在")
        seen.add((sid,row["seq"]))
        onset=day(row["onset"],loc)
        worse=day(row["worse"],loc) if row["worse"] else None
        if worse and worse<onset:
            raise ConfigurationError(f"{loc} 加重早于发生日期")
        entry={"id":sid,"event_id":row["seq"],"source":loc,"included":False}
        events.append(entry)
        if sid not in treated:
            entry["reason"]="未实际用药"
            continue
        tr=treated[sid]; first=day(tr["first"],loc); last=day(tr["last"],loc)
        trigger=worse if onset<first else onset
        if trigger is None or trigger<first or (trigger-last).days>follow:
            entry["reason"]="观察窗外或治疗前事件未在窗内加重"
            continue
        serious,fatal=yn(row["serious"],loc),yn(row["fatal"],loc)
        if not row["pt"] or row["grade"] not in {"1","2","3","4","5"} or (fatal and not serious):
            raise ConfigurationError(f"{loc} 首选术语、分级或致死/严重标记需核对")
        rel=row["related"]
        category="相关" if rel in positive else {"无关":"无关","待评估":"待定","待定":"待定","未知":"待定","":"待定"}.get(rel)
        if category is None:
            raise ConfigurationError(f"{loc} 相关性分类未经映射")
        entry.update(included=True,group=tr["actual"],pt=row["pt"],grade=int(row["grade"]),serious=serious,
                     fatal=fatal,related=category,trigger=trigger.isoformat(),reason="治疗前事件加重" if onset<first else "窗内新发")
    included=[e for e in events if e["included"]]
    n=Counter(v["actual"] for v in treated.values())
    def cases(label,predicate):
        return [label,*[percentage(len({e["id"] for e in included if e["group"]==g and predicate(e)}),n[g]) for g in arms]]
    overview=[cases("至少一次TEAE n/N（%）",lambda e:True),
              ["TEAE事件数",*[sum(e["group"]==g for e in included) for g in arms]],
              cases(f"{grade_cut}级及以上TEAE n/N（%）",lambda e:e["grade"]>=grade_cut),
              cases("已经判定药物相关的TEAE n/N（%）",lambda e:e["related"]=="相关"),
              cases("相关性待定的TEAE n/N（%）",lambda e:e["related"]=="待定")]
    results=[table("4.1","不良事件概要","SAF",arms,overview,"按实际用药组；人数在各行内去重，事件数逐条累计。已判相关与待定可涉及同一人。"),
             table("4.3","死亡和严重不良事件（TEAE窗内）","SAF",arms,
                   [cases("致死TEAE n/N（%）",lambda e:e["fatal"]),cases("严重TEAE n/N（%）",lambda e:e["serious"])],
                   "限治疗期间出现或加重的事件；致死TEAE按事件记录的致死标记汇总。")]
    terms=sorted({e["pt"] for e in included})
    if terms:
        results.append(table("4.2","按首选术语汇总不良事件","SAF",arms,
                            [cases(pt,lambda e,pt=pt:e["pt"]==pt) for pt in terms],"同一受试者在同一首选术语下仅计一次。"))
    return results,events
