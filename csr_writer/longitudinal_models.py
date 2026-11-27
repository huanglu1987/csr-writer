"""Two-arm continuous outcomes. Model parameters and missing-data rules come from the SAP."""
from collections import defaultdict

import numpy as np
from scipy import linalg, optimize, stats
from .errors import ConfigurationError


def ols(x, y):
    if len(y) <= x.shape[1] or np.linalg.matrix_rank(x) != x.shape[1]:
        raise ConfigurationError("回归设计不满秩或缺少残差自由度")
    q,r=np.linalg.qr(x,mode="reduced")
    beta=linalg.solve_triangular(r,q.T @ y)
    ri=linalg.solve_triangular(r,np.eye(len(r)))
    df=len(y)-len(beta)
    rss=float(np.sum((y-x@beta)**2))
    if rss <= np.finfo(float).eps:
        raise ConfigurationError("回归模型无可估计的残差变异")
    return beta,ri@ri.T,rss,df


def inference(estimate, se, df, level):
    if se<=0 or not np.isfinite([estimate,se]).all() or not df>0:
        raise ConfigurationError("效应或推断方差不可估计")
    dist=stats.norm if np.isinf(df) else stats.t(df)
    width=float(dist.ppf((1+level)/2))*se
    return dict(estimate=float(estimate),se=float(se),df=None if np.isinf(df) else float(df),
                low=float(estimate-width),high=float(estimate+width),p=float(2*dist.sf(abs(estimate/se))))


def ancova(arm,base,change,level,extra=None):
    x=np.column_stack([np.ones(len(base)),arm,base])
    if extra is not None:
        x=np.column_stack([x,extra])
    keep=np.isfinite(change[:,-1])
    b,vc,rss,df=ols(x[keep],change[keep,-1])
    return inference(b[1],np.sqrt(vc[1,1]*rss/df),df,level),{"subjects":int(keep.sum())}


def multiple_imputation(arm,base,change,level,spec):
    m,seed=int(spec["m"]),int(spec["seed"])
    if m<2 or m>10000:
        raise ConfigurationError("插补次数须为2至10000之间的整数")
    missing=np.isnan(change)
    if np.any(missing[:,:-1] & ~missing[:,1:]):
        raise ConfigurationError("存在间歇性缺失，不能执行SAP所列单调回归插补")
    rng=np.random.Generator(np.random.PCG64(seed))
    x=np.column_stack([np.ones(len(base)),arm,base])
    draws=[]
    for _ in range(m):
        completed=change.copy()
        for j in range(change.shape[1]):
            mask=missing[:,j]
            if not mask.any():
                continue
            predictors=np.column_stack([x,completed[:,:j]])
            beta,vc,rss,df=ols(predictors[~mask],completed[~mask,j])
            # Document the stream and fixed coefficient order for cross-platform replay.
            u=np.clip(rng.random(1+len(beta)+int(mask.sum())),np.nextafter(0.,1.),np.nextafter(1.,0.))
            sd=np.sqrt(rss/stats.chi2.ppf(u[0],df))
            bdraw=beta+sd*(np.linalg.cholesky(vc)@stats.norm.ppf(u[1:1+len(beta)]))
            completed[mask,j]=predictors[mask]@bdraw+sd*stats.norm.ppf(u[1+len(beta):])
        b,vc,rss,df_complete=ols(x,completed[:,-1])
        draws.append([float(b[1]),float(vc[1,1]*rss/df_complete)])
    values,trace=pool_imputations(draws,df_complete,level,spec["mi_df"])
    trace.update(subjects=len(base),seed=seed,stream="PCG64 / inverse CDF / sorted subject IDs / visit order",
                 missing_per_visit=missing.sum(axis=0).tolist())
    return values,trace


def pool_imputations(draws,df_complete,level,df_rule):
    m=len(draws)
    estimates,variances=np.asarray(draws).T
    between=estimates.var(ddof=1)
    within=variances.mean()
    total=within+(1+1/m)*between
    fraction=(1+1/m)*between/total
    df_old=(m-1)/(fraction**2) if fraction else np.inf
    df_obs=(df_complete+1)/(df_complete+3)*df_complete*(1-fraction)
    df=df_old if df_rule=="Rubin" else 1/(1/df_old+1/df_obs)
    return inference(estimates.mean(),np.sqrt(total),df,level),dict(m=m,within=float(within),
        between=float(between),mcse=float(np.sqrt(between/m)),completed_estimates_and_variances=draws)


def mmrm(arm,base,change,level,spec,extra=None):
    """Profile REML over Cholesky factors of the common visit covariance, grouping missing patterns."""
    n,k=change.shape
    intercept=np.eye(k)
    design=np.stack([np.column_stack([intercept,arm[i]*intercept,
                    base[i]*intercept if spec["base_inter"] else np.full(k,base[i])]) for i in range(n)])
    if extra is not None:
        design=np.concatenate([design,np.broadcast_to(extra[:,None,:],(n,k,extra.shape[1]))],axis=2)
    by_pattern=defaultdict(list)
    for i,row in enumerate(change):
        where=tuple(np.flatnonzero(np.isfinite(row)))
        if where:
            by_pattern[where].append(i)
    blocks=[]
    pair_counts=np.zeros((k,k),dtype=int)
    for pattern,ids in by_pattern.items():
        ix=np.array(pattern)
        blocks.append((ix,design[ids][:,ix,:],change[ids][:,ix]))
        pair_counts[np.ix_(ix,ix)]+=len(ids)
    if not blocks or pair_counts.min()<2:
        raise ConfigurationError("部分访视对缺少重复观察，非结构化协方差不可估计")
    p=design.shape[-1]
    if np.linalg.matrix_rank(np.concatenate([x.reshape(-1,p) for _,x,_ in blocks]))!=p:
        raise ConfigurationError("MMRM固定效应设计不满秩")
    il=np.tril_indices(k)
    diag=il[0]==il[1]

    def evaluate(theta, fitted=False):
        L=np.zeros((k,k)); L[il]=theta
        L[np.diag_indices(k)]=np.exp(theta[diag])
        cov=L@L.T
        info=np.zeros((p,p)); rhs=np.zeros(p); logdet=0.; cache=[]
        for ix,x,y in blocks:
            v=cov[np.ix_(ix,ix)]
            chol=linalg.cho_factor(v,lower=True)
            vi=linalg.cho_solve(chol,np.eye(len(ix)))
            vx=vi@x
            info+=np.einsum('nij,nik->jk',x,vx)
            rhs+=np.einsum('nij,ni->j',vx,y)
            logdet+=len(y)*2*np.log(np.diag(chol[0])).sum()
            cache.append((ix,x,y,vi,vx))
        cf=linalg.cho_factor(info,lower=True)
        var=linalg.cho_solve(cf,np.eye(p))
        beta=linalg.cho_solve(cf,rhs)
        if fitted:
            return beta,var,cov,cache
        logdet+=2*np.log(np.diag(cf[0])).sum()
        scatter=np.zeros_like(cov)
        for ix,x,y,vi,vx in cache:
            res=y-x@beta
            vr=res@vi
            logdet+=np.sum(vr*res)
            scatter[np.ix_(ix,ix)]+=len(y)*vi-np.einsum('nij,nkj->ik',vx@var,vx)-vr.T@vr
        grad=(scatter@L)[il]
        grad[diag]*=np.diag(L)
        return .5*logdet,grad

    scale=np.nanstd(change,axis=0)
    if np.any(~np.isfinite(scale)) or np.any(scale<=0):
        raise ConfigurationError("随访变化无可估计变异")
    start=np.zeros(len(diag));start[diag]=np.log(scale)
    fit=optimize.minimize(evaluate,start,jac=True,method="L-BFGS-B",
            options={"ftol":1e-13,"gtol":1e-7,"maxiter":600,"maxls":40})
    if not fit.success or np.max(np.abs(fit.jac))>0.003:
        raise ConfigurationError("MMRM未收敛，须复核模型和数据")
    beta,var,cov,cache=evaluate(fit.x,True)
    if np.linalg.cond(cov)>1e9:
        raise ConfigurationError("MMRM访视协方差接近奇异")
    contrast=np.zeros(p);contrast[2*k-1]=1.
    variance=float(contrast@var@contrast)
    df=np.inf
    unadjusted=float(np.sqrt(variance))
    kr=spec["inference"] in {"Kenward-Roger","Kenward-Roger（直接协方差参数，期望信息）"}
    if not kr and spec["inference"] not in {"渐近正态","期望信息Satterthwaite"}:
        raise ConfigurationError("MMRM推断规则未识别")
    if kr or spec["inference"]=="期望信息Satterthwaite":
        # Expected REML information: tr(P D_i P D_j)/2. Grouped traces avoid N-by-N matrices.
        dims=list(zip(*il))
        d_cov=np.zeros((len(dims),k,k))
        for h,(r,c) in enumerate(dims):
            d_cov[h,r,c]=d_cov[h,c,r]=1
        d_info=np.zeros((len(dims),p,p))
        linear=np.zeros((len(dims),len(dims)))
        adjust=np.zeros_like(linear)
        contrast_projection=var@contrast
        kr_q=np.zeros_like(linear)
        for ix,x,y,vi,vx in cache:
            ds=d_cov[:,ix][:,:,ix]
            dv=ds@vi
            if kr:
                projected=np.einsum("aij,nj->ani",ds,vx@contrast_projection)
                kr_q+=np.einsum("ani,bni->ab",projected,projected@vi)
            linear+=len(y)*np.einsum('aij,bji->ab',dv,dv)
            for a,D in enumerate(ds):
                vdx=D@vx
                d_info[a]+=np.einsum('nij,nik->jk',vx,vdx)
                for b,E in enumerate(ds):
                    adjust[a,b]+=np.einsum('nij,nij->',vx@var,(E@vi)@vdx)
        vd=var@d_info
        fisher=.5*(linear-adjust-adjust.T+np.einsum('aij,bji->ab',vd,vd))
        gradient=np.einsum('i,aij,j->a',var@contrast,d_info,var@contrast)
        denominator=gradient@linalg.cho_solve(linalg.cho_factor(fisher),gradient)
        df=2*variance**2/denominator
        if kr:
            # Direct unique elements of UN are linear covariance parameters (zero second derivatives).
            # For a scalar contrast the KR moment df simplifies to 2/A2 and the F scale is one.
            projected=d_info@contrast_projection
            adjustment=kr_q-projected@var@projected.T
            weights=linalg.cho_solve(linalg.cho_factor(fisher),np.eye(len(dims)))
            variance+=2*float(np.sum(weights*adjustment))
            if not variance>0:
                raise ConfigurationError("Kenward–Roger校正方差不可估计")
    return inference(contrast@beta,np.sqrt(variance),df,level),dict(subjects=sum(len(b[2]) for b in blocks),
        records=int(np.isfinite(change).sum()),visits_observed=np.isfinite(change).sum(axis=0).tolist(),
        unadjusted_se=unadjusted,inference=spec["inference"],
        covariance_parameterization="direct UN elements; expected REML information" if kr else None,
        covariance=cov.tolist(),reml_iterations=int(fit.nit),max_abs_gradient=float(np.max(np.abs(fit.jac))))
