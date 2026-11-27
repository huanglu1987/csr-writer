"""SAP-specified fixed covariates, normal FCS and post-imputation delta pattern mixtures."""
import re
import numpy as np
from scipy import special, stats
from .errors import ConfigurationError
from .longitudinal_models import ols, pool_imputations
from .subject_data import day, number, yn


def column_names(text):
    if not text or text=="无":
        return []
    names=[v.strip().upper() for v in text.split("、")]
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*|[\u3400-\u9fff]+",v) for v in names):
        raise ConfigurationError("协变量名称需要与基线数据列逐一对应")
    return names


def fixed_covariates(ids,people,spec):
    cats=column_names(spec.get("cat_cols"))
    nums=column_names(spec.get("num_cols"))
    if len(set(cats+nums))!=len(cats+nums):
        raise ConfigurationError("协变量重复或类型冲突")
    design=[];metadata={}
    for field in cats+nums:
        vals=[people[s].get("covariates",{}).get(field,"") for s in ids]
        if any(v.strip().upper() in {"",".","NA","N/A","NAN","NULL","未知","缺失"} for v in vals):
            raise ConfigurationError(f"{field} 缺少明确的逐人协变量值或源列不唯一")
        if field in cats:
            levels=sorted(set(vals))
            if len(levels)<2:
                raise ConfigurationError(f"分类协变量 {field} 仅有一个水平")
            design.extend([np.array([v==label for v in vals],float) for label in levels[1:]])
            metadata[field]={"type":"categorical","levels":levels,"reference":levels[0]}
        else:
            x=np.array([float(number(v,people[s]["loc"])) for s,v in zip(ids,vals)])
            if not np.isfinite(x).all():
                raise ConfigurationError(f"{field} 为非有限值")
            design.append(x-x.mean())
            metadata[field]={"type":"continuous","center":float(x.mean())}
    return np.column_stack(design) if design else None,metadata


def discontinuations(ids,people,domain,treated):
    source={}
    for r in (domain or {}).get("rows",[]):
        sid=r["id"]
        if sid not in people or sid in source:
            raise ConfigurationError(f"{r['loc']} 永久停药记录重复或受试者不符")
        source[sid]=r
    stop=[];trace={}
    for sid in ids:
        p=people[sid]
        records=([p] if "stop_yn" in p and "stop_date" in p else [])+([source[sid]] if sid in source else [])
        if not records or len({(r["stop_yn"],r["stop_date"]) for r in records})>1:
            raise ConfigurationError(f"{sid} 永久停药记录缺少或互相冲突")
        r=records[0];loc=r["loc"];flag=yn(r["stop_yn"],loc)
        if flag!=bool(r["stop_date"]):
            raise ConfigurationError(f"{loc} 永久停药标志与日期不符")
        offset=np.inf
        if flag:
            if treated is not None and sid not in treated:
                raise ConfigurationError(f"{loc} 永久停药记录与未实际给药状态冲突")
            date=day(r["stop_date"],loc)
            offset=(date-day(p["rand_date"],p["loc"])).days+1
            if offset<1 or (treated is not None and sid in treated and date<day(treated[sid]["last"],loc)):
                raise ConfigurationError(f"{loc} 永久停药与随机或后续给药日期不符")
        stop.append(offset)
        trace[sid]={"day":int(offset) if flag else None,"source":loc}
    return np.array(stop),trace


def parse_deltas(text):
    pairs=[]
    for scenario in text.split("、"):
        match=re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))/([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*",scenario)
        if not match:
            raise ConfigurationError("MNAR两组偏移量未规定或无法成对解释")
        pair=tuple(float(v) for v in match.groups())
        if pair in pairs or not np.isfinite(pair).all():
            raise ConfigurationError("MNAR偏移情景重复或非有限值")
        pairs.append(pair)
    return pairs


def fcs(arm,base,change,level,spec,extra=None,*,mask=None,deltas=(),in_chain=None):
    m=int(spec["fcs_m"]);sweeps=int(spec["sweeps"]);seed=int(spec["fcs_seed"])
    if not (2<=m<=10000 and 1<=sweeps<=1000):
        raise ConfigurationError("FCS链数或迭代轮数超出支持范围")
    missing=np.isnan(change)
    if np.any(missing.all(axis=0)):
        raise ConfigurationError("FCS至少一个访视全缺失，无法拟合条件模型")
    if (deltas or in_chain is not None) and (mask is None or mask.shape!=missing.shape or np.any(mask & ~missing)):
        raise ConfigurationError("MNAR偏移必须仅作用于指定缺失单元格")
    x=np.column_stack([np.ones(len(base)),arm,base])
    if extra is not None:
        x=np.column_stack([x,extra])
    init=np.where(missing,np.nanmean(change,axis=0),change)
    visits=np.arange(change.shape[1])
    draws={key:[] for key in [None,*deltas]}
    chain_means=[]
    streams=np.random.SeedSequence(seed).spawn(m)
    for stream in streams:
        rng=np.random.Generator(np.random.PCG64(stream))
        yy=init.copy()
        checkpoints=[]
        for sweep in range(sweeps):
            for j in visits:
                absent=missing[:,j]
                if not absent.any():
                    continue
                predictors=np.column_stack([x,yy[:,visits!=j]])
                beta,vc,rss,df=ols(predictors[~absent],yy[~absent,j])
                u=np.clip(rng.random(1+len(beta)+int(absent.sum())),np.nextafter(0.,1.),np.nextafter(1.,0.))
                sd=np.sqrt(rss/stats.chi2.ppf(u[0],df))
                coefficients=beta+sd*(np.linalg.cholesky(vc)@special.ndtri(u[1:1+len(beta)]))
                yy[absent,j]=predictors[absent]@coefficients+sd*special.ndtri(u[1+len(beta):])
                if in_chain is not None:
                    offsets = np.where(arm==1,in_chain[0],in_chain[1])
                    yy[absent,j] += offsets[absent] * mask[absent,j]
            if sweep in (0,sweeps//2,sweeps-1):
                checkpoints.append([float(yy[missing[:,j],j].mean()) if missing[:,j].any() else None for j in visits])
        chain_means.append(checkpoints)
        for key in draws:
            endpoint=yy[:,-1]
            if key is not None:
                # A pattern-mixture shift after the MAR chain, without feedback into its conditional fits.
                shifted=yy+mask*np.where(arm==1,key[0],key[1])[:,None]
                endpoint=shifted[:,-1]
            beta,vc,rss,df_complete=ols(x,endpoint)
            draws[key].append([float(beta[1]),float(vc[1,1]*rss/df_complete)])
    result={}
    for key,chain in draws.items():
        value,trace=pool_imputations(chain,df_complete,level,spec["fcs_df"])
        trace.update(subjects=len(base),seed=seed,sweeps=sweeps,analysis_df=df_complete)
        if in_chain is not None:
            trace.update(delta_test=in_chain[0],delta_reference=in_chain[1],offset_timing="each conditional draw before next visit",
                         eligible_per_visit=mask.sum(axis=0).tolist())
        if key is None:
            trace.update(missing_per_visit=missing.sum(axis=0).tolist(),initialization="observed visit mean",
                         stream="independent PCG64 SeedSequence child per chain, inverse CDF",
                         imputed_means_by_chain=chain_means,diagnostic_sweeps=sorted({1,sweeps//2+1,sweeps}))
        else:
            trace.update(delta_test=key[0],delta_reference=key[1],endpoint_shifted_by_arm=[
                int(np.sum(mask[:,-1] & (arm==a) & (delta!=0))) for a,delta in zip((1,0),key)])
        result[key]=(value,trace)
    return result
