"""Prespecified two-arm Cox/Breslow and uncorrected CMH analyses from observed events and task records."""
from collections import Counter, defaultdict
from math import exp, log, sqrt
import re
from .errors import ConfigurationError
from .subject_analysis import table
from .subject_data import day, yn


def fit_secondary(method, people, domain, arms, spec, protocol):
    try:
        from scipy import optimize, stats
    except ImportError as exc:
        raise ConfigurationError("Cox 和 CMH 分析需要 models 依赖 SciPy") from exc
    test,ref=spec["test"],spec["ref"]
    level=float(spec["level"])/100
    if len(arms)!=2 or {test,ref}!=set(arms) or not 0<level<1:
        raise ConfigurationError("次要分析组别或置信水平参数不一致")
    source_name=re.escape(spec["outcome"])+r"[（(]"+re.escape(spec["code"])+r"[）)]"
    if not any(re.search(source_name,r["text"]) for r in protocol):
        raise ConfigurationError("次要终点名称和代码未能与方案唯一核对")
    field="event_code" if method=="cox" else "task_code"
    raw=defaultdict(list)
    for r in domain["rows"]:
        if r["id"] not in people:
            raise ConfigurationError(f"{r['loc']}无对应受试者")
        if r[field]==spec["code"]:
            raw[r["id"]].append(r)
    if not raw:
        raise ConfigurationError("缺少规定指标的原始记录")
    trace={};numeric={}
    for sid,p in sorted(people.items()):
        if not p["randomized"]:
            continue
        origin=day(p["rand_date"],p["loc"])
        tr={"group":p["arm"],"group_source":p["loc"]}
        rows=raw[sid]
        if method=="cox":
            if len(rows)!=1 or int(spec["cut"])<1:
                raise ConfigurationError(f"{sid} 缺少唯一的随访确认或截止日不符")
            r=rows[0]; last=(day(r["last_known"],r["loc"])-origin).days+1
            ev=(day(r["event_date"],r["loc"])-origin).days+1 if r["event_date"] else None
            end=min(int(spec["cut"]),last)
            if end<1 or (ev is not None and not 1<=ev<=last):
                raise ConfigurationError(f"{r['loc']} 首次事件与末次确认日期冲突")
            event=ev is not None and ev<=end
            tr.update(time=ev if event else end,event=event,source=r["loc"])
        else:
            target=int(spec["target"]);margin=int(spec["margin"])
            if not p.get("stratum") or target-margin<=1:
                raise ConfigurationError(f"{sid} 随机登记层或评估窗不明确")
            valid=[r for r in rows if abs((day(r["task_date"],r["loc"])-origin).days+1-target)<=margin]
            if len(valid)>1:
                raise ConfigurationError(f"{sid} 同一访视窗有多个任务记录")
            tr.update(stratum=p["stratum"],observed=bool(valid),success=yn(valid[0]["task_done"],valid[0]["loc"]) if valid else False,
                      source=valid[0]["loc"] if valid else None)
        trace[sid]=tr
    if method=="cox":
        counts=Counter((v["time"],v["group"],v["event"]) for v in trace.values())
        times=sorted({v["time"] for v in trace.values() if v["event"]})
        sets=[]
        for t in times:
            risks=[sum(n for (u,g,_),n in counts.items() if g==a and u>=t) for a in (test,ref)]
            events=[counts[t,a,True] for a in (test,ref)]
            sets.append((*risks,*events))
        def derivatives(coef):
            score=0.;info=0.
            for nt,nr,dt,dr in sets:
                prob=nt*exp(coef)/(nr+nt*exp(coef))
                score+=dt-(dt+dr)*prob
                info+=(dt+dr)*prob*(1-prob)
            return score,info
        if not sets or derivatives(-30)[0]<=0 or derivatives(30)[0]>=0:
            raise ConfigurationError("Cox事件不足或完全分离，风险比不可估计")
        beta=optimize.brentq(lambda b:derivatives(b)[0],-30,30)
        se=1/sqrt(derivatives(beta)[1]);pval=2*stats.norm.sf(abs(beta/se))
        label="首次事件风险比";note="Breslow法处理同日事件。"
        numeric["events"]=sum(v["event"] for v in trace.values())
    else:
        strata=defaultdict(Counter)
        for v in trace.values():
            strata[v["stratum"]][v["group"],v["success"]]+=1
        num=den=dif=var=t1=t2=t3=0.
        for cells in strata.values():
            a,b,c,d=[cells[g,s] for g in (test,ref) for s in (True,False)]
            n=a+b+c+d
            if n<2:
                raise ConfigurationError("分层记录含单例层，需统计确认")
            r=a*d/n;s=b*c/n
            num+=r;den+=s
            dif+=a-(a+b)*(a+c)/n
            var+=(a+b)*(c+d)*(a+c)*(b+d)/(n*n*(n-1))
            t1+=(a+d)*r/n;t2+=((a+d)*s+(b+c)*r)/n;t3+=(b+c)*s/n
        if min(num,den,var)<=0:
            raise ConfigurationError("分层信息不足，无法估计共同优势比")
        beta=log(num/den);se=sqrt(.5*(t1/num**2+t2/(num*den)+t3/den**2))
        pval=stats.chi2.sf(dif**2/var,1)
        label="分层共同优势比";note="共同优势比采用RBG区间，P值来自无连续性校正的CMH检验。"
        numeric["missing"]=sum(not v["observed"] for v in trace.values())
    width=stats.norm.ppf((1+level)/2)*se
    ratio,lo,hi=map(exp,(beta,beta-width,beta+width))
    numeric.update(estimate=ratio,low=lo,high=hi,p=float(pval),subjects=len(trace))
    row=[f"{spec['outcome']}{label}（{test}相对{ref}）",f"{ratio:.3f}",f"{lo:.3f} 至 {hi:.3f}",
         "<0.0001" if pval<.0001 else f"{pval:.4f}"]
    tab=table("3.4" if method=="cox" else "3.5","次要终点 "+label,"ITT",["估计值",f"{100*level:g}%置信区间","P值"],[row],note)
    return tab,{"participants":trace,"result":numeric}
