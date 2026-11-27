"""Reference-based MI on discrete trajectories; keep every observed category fixed."""
from itertools import product
import re

import numpy as np
from scipy import special
from .errors import ConfigurationError
from .fcs_models import discontinuations
from .longitudinal_models import pool_imputations
from .subject_analysis import table
from .subject_data import day


def compatible(y, paths):
    return np.all((y[:,None,:]<0) | (y[:,None,:]==paths),axis=2)


def sample_paths(prob, allowed, uniforms):
    cumulative=np.cumsum(allowed*prob,axis=1)
    if np.any(cumulative[:,-1]<=0):
        raise ConfigurationError("类别轨迹条件分布不可估计")
    total=cumulative[:,-1:]
    threshold=np.minimum(uniforms[:,None]*total,np.nextafter(total,0.))
    return (cumulative<=threshold).sum(axis=1)


def controlled(own, ref, paths, ncat, cut, strategy):
    """Keep the own-arm pre-event joint and calibrate the reference conditional association."""
    k=paths.shape[1]
    if strategy=="MAR" or cut==k:
        return own
    if cut==0:
        return ref
    prefixes=np.ravel_multi_index(paths[:,:cut].T,(ncat,)*cut)
    own_prefix=np.bincount(prefixes,weights=own)
    current=ref*own_prefix[prefixes]/np.bincount(prefixes,weights=ref)[prefixes]
    margins=[(prefixes,own_prefix)]
    ratio=np.ones(ncat)
    if strategy=="CIR":
        anchor=paths[:,cut-1]
        ratio=np.bincount(anchor,weights=own)/np.bincount(anchor,weights=ref)
    for j in range(cut,k):
        target=np.bincount(paths[:,j],weights=ref)*ratio
        margins.append((paths[:,j],target/target.sum()))
    for _ in range(1000):
        for idx,target in margins:
            current*= (target/np.bincount(idx,weights=current))[idx]
        if all(np.max(np.abs(np.bincount(idx,weights=current)-target))<1e-10 for idx,target in margins):
            return current
    raise ConfigurationError("参考轨迹分布未达到规定的边际精度")


def impute(y, arm, sites, cuts, cats, responders, strategies, spec):
    m,sweeps,seed=int(spec["m"]),int(spec["sweeps"]),int(spec["seed"])
    prior=float(spec["prior"])
    n,k=y.shape
    if (len(cats)**k>4096 or not 2<=m<=10000 or not 1<=sweeps<=1000 or
            not np.isfinite(prior) or prior<=0):
        raise ConfigurationError("轨迹规模、Dirichlet参数或链设置超出可执行范围")
    paths=np.array(list(product(range(len(cats)),repeat=k)))
    training=y.copy()
    training[(np.arange(k)[None,:]>=cuts[:,None]) & (arm[:,None]==1)]=-1
    train=[]
    for a in (0,1):
        z=training[(arm==a) & np.any(training>=0,axis=1)]
        if not len(z) or np.any(np.all(z<0,axis=0)):
            raise ConfigurationError("组别至少一个拟合访视完全缺少分类观测")
        train.append(compatible(z,paths))
    target=compatible(y,paths)
    whole=np.all(y>=0,axis=1)
    strata=[]
    for site in sorted(set(sites)):
        members=sites==site
        groups=[members & (arm==a) for a in (0,1)]
        if min(int(g.sum()) for g in groups)<2:
            raise ConfigurationError("中心内每组至少需两名随机受试者估计应答率差方差")
        strata.append((members.mean(),groups))
    df=n-2*len(strata)
    draws={s:[] for s in strategies}
    rates={s:np.zeros((2,len(cats))) for s in strategies}
    response=np.isin(np.arange(len(cats)),responders)
    for stream in np.random.SeedSequence(seed).spawn(m):
        rng=np.random.default_rng(stream)
        probs=[]
        for allowed in train:
            prob=np.full(len(paths),1/len(paths))
            for _ in range(sweeps):
                ids=sample_paths(prob,allowed,rng.random(len(allowed)))
                shape=prior+np.bincount(ids,minlength=len(paths))
                u=np.clip(rng.random(len(paths)),np.finfo(float).eps,1-np.finfo(float).eps)
                g=special.gammaincinv(shape,u)
                if not np.isfinite(g).all() or np.any(g<=0):
                    raise ConfigurationError("轨迹后验概率出现数值退化")
                prob=g/g.sum()
            probs.append(prob)
        uniforms=rng.random(n)
        for s in strategies:
            last=y[:,-1].copy()
            for a in (0,1):
                for cut in np.unique(cuts[(arm==a) & ~whole]):
                    idx=(arm==a) & (cuts==cut) & ~whole
                    prob=controlled(probs[a],probs[0],paths,len(cats),cut,s) if a==1 else probs[0]
                    chosen=sample_paths(prob,target[idx],uniforms[idx])
                    filled=paths[chosen]
                    if np.any((y[idx]>=0) & (filled!=y[idx])):
                        raise ConfigurationError("条件抽样改变了已有分类实测")
                    last[idx]=filled[:,-1]
            percentages=np.zeros((2,len(cats)))
            var=0.
            for w,groups in strata:
                for a,group in enumerate(groups):
                    subset=last[group]
                    percentages[a]+=w*np.bincount(subset,minlength=len(cats))/len(subset)
                    var+=w*w*np.var(response[subset],ddof=1)/len(subset)
            rd=np.sum((percentages[1]-percentages[0])[responders])
            draws[s].append([float(100*rd),float(10000*var)])
            rates[s]+=percentages/m
    fits={}
    for s,chain in draws.items():
        values,trace=pool_imputations(chain,df,float(spec["level"])/100,"Barnard-Rubin")
        fits[s]={**values,**trace,"class_probabilities":rates[s].tolist()}
    return fits


def tables(people,domain,stops,treated,arms,spec,protocol):
    from collections import defaultdict
    cats=spec["categories"].split("、");good=spec["response_levels"].split("、")
    methods=spec["strategies"].upper().split("、")
    days=[int(v) for v in spec["cat_days"].split("、")]
    gap=int(spec["margin"]); field=spec["site"].upper()
    if (len(cats)<2 or len(set(cats))!=len(cats) or not set(good)<set(cats) or len(set(good))!=len(good)
        or len(set(methods))!=len(methods) or {spec["test"],spec["ref"]}!=set(arms) or len(arms)!=2
        or days[0]-gap<=1 or any(b-a<=2*gap for a,b in zip(days,days[1:])) or not 0<float(spec["level"])<100):
        raise ConfigurationError("分类终点组别、访视、应答类别或区间与SAP不符")
    marker=re.escape(spec["outcome"])+r"[（(]"+re.escape(spec["code"])+r"[）)]"
    if not any(re.search(marker,r["text"]) for r in protocol):
        raise ConfigurationError("分类终点名称及代码未能与方案核对")
    ids=sorted(s for s,p in people.items() if p["randomized"])
    stop,stop_trace=discontinuations(ids,people,stops,treated)
    arm=np.array([int(people[s]["arm"]==spec["test"]) for s in ids])
    sites=np.array([people[s]["covariates"].get(field,"").strip() for s in ids])
    if any(v.upper() in {"",".","NA","N/A","NAN","NULL","未知","缺失"} for v in sites):
        raise ConfigurationError("分类终点中心列缺失或不唯一")
    raw=defaultdict(list)
    for r in domain["rows"]:
        if r["id"] not in people:
            raise ConfigurationError(f"{r['loc']} 的受试者无主记录")
        if r["cat_code"]==spec["code"]:
            if r["category"] not in cats:
                raise ConfigurationError(f"{r['loc']} 的分类取值未在SAP定义")
            raw[r["id"]].append(r)
    y=np.full((len(ids),len(days)),-1,int)
    retrieved=np.zeros_like(y,bool)
    subjects={}
    for i,s in enumerate(ids):
        origin=day(people[s]["rand_date"],people[s]["loc"])
        records=[((day(r["cat_date"],r["loc"])-origin).days+1,r) for r in raw[s]]
        selected=[]
        for j,d in enumerate(days):
            options=sorted(((abs(t-d),t,r) for t,r in records if abs(t-d)<=gap),key=lambda it:it[:2])
            if options:
                _,t,r=options[0]
                if len(options)>1 and options[1][1]==t:
                    raise ConfigurationError(f"{s} 同一分类访视有重复并列记录")
                y[i,j]=cats.index(r["category"]);retrieved[i,j]=t>stop[i]
                selected.append(dict(target=d,day=t,category=r["category"],source=r["loc"]))
            else:
                selected.append(dict(target=d,category=None))
        subjects[s]=dict(group=people[s]["arm"],site=sites[i],stop=stop_trace[s],visits=selected)
    cuts=np.sum(np.array(days)<=stop[:,None],axis=1)
    fits=impute(y,arm,sites,cuts,cats,[cats.index(c) for c in good],methods,spec)
    result=[];dist=[]
    for s,fit in fits.items():
        result.append([f"次要终点{spec['outcome']} {s}末访应答率差（{spec['test']}减{spec['ref']}）（百分点）",f"{fit['estimate']:.3f}",
                       f"{fit['low']:.3f} 至 {fit['high']:.3f}","<0.0001" if fit["p"]<.0001 else f"{fit['p']:.4f}"])
        for j,c in enumerate(cats):
            dist.append([s+" "+c,*[f"{100*fit['class_probabilities'][int(g==spec['test'])][j]:.2f}%" for g in arms]])
    def by_arm(x):
        return [int(x[arm==int(g==spec["test"])].sum()) for g in arms]
    obs=y>=0
    intermitt=np.any((~obs[:,:-1]) & np.logical_or.accumulate(obs[:,::-1],axis=1)[:,::-1][:,1:],axis=1)
    counts=[["永久停药人数",*by_arm(np.isfinite(stop))],["间歇缺失人数",*by_arm(intermitt)],
            ["停药后分类实测次数",*by_arm(retrieved)],["停药后分类缺失次数",*by_arm(~obs & (np.array(days)>stop[:,None]))]]
    for j,d in enumerate(days):
        counts.append([f"第{d}天实测人数",*by_arm(obs[:,j])])
    note=f"第{days[-1]}天，{spec['test']}减{spec['ref']}。应答为{'、'.join(good)}，按中心入组人数标准化。{spec['m']}条链各{spec['sweeps']}轮，Rubin合并及Barnard–Rubin自由度。"
    ts=[table("3.13.1","次要终点"+spec["outcome"]+"应答率差","ITT",["估计值",f"{spec['level']}%置信区间","P值"],result,note),
        table("3.13.2","次要终点"+spec["outcome"]+"末访类别分布","ITT",arms,dist,"各组列示标准化的插补平均概率。"),
        table("3.13.3","分类终点随访缺失及永久停药后观察","ITT",arms,counts,"实测按实际评估日，缺失按名义访视日确定。")]
    return ts,dict(subjects=subjects,models=fits,categories=cats,reference=spec["ref"],strategies=methods,
                   endpoint=spec["outcome"],days=days,prior=float(spec["prior"]),seed=int(spec["seed"]),sweeps=int(spec["sweeps"]))
