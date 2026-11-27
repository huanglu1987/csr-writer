"""Bayesian reference-based MI for continuous changes with common or arm-specific unstructured covariance."""
import numpy as np
from scipy import linalg, stats
from .errors import ConfigurationError
from .longitudinal_models import ols, pool_imputations


def patterns(y):
    masks, idx = np.unique(np.isnan(y), axis=0, return_inverse=True)
    return [(np.flatnonzero(idx==i),np.flatnonzero(mask),np.flatnonzero(~mask))
            for i,mask in enumerate(masks) if mask.any()]


def conditional(y, mean, sigma, groups, z):
    result = y.copy()
    for rows, mis, obs in groups:
        at = np.ix_(rows,mis)
        mu = mean[at].copy()
        variance = sigma[np.ix_(mis,mis)].copy()
        if len(obs):
            coef = linalg.solve(sigma[np.ix_(obs,obs)],sigma[np.ix_(obs,mis)],assume_a="pos")
            mu += (y[np.ix_(rows,obs)]-mean[np.ix_(rows,obs)])@coef
            variance -= sigma[np.ix_(mis,obs)]@coef
        result[at] = mu + z[at] @ np.linalg.cholesky((variance+variance.T)/2).T
    return result


def trajectory(own, reference, on, strategy):
    if strategy=="CIR":
        # For discontinuation before visit one, change at baseline is zero in both arms.
        last = on.sum(axis=1)-1
        row = np.arange(len(last))
        retained = np.where(last>=0,own[row,last]-reference[row,last],0.)
        reference = reference+retained[:,None]
    return np.where(on,own,reference)


def transition_covariance(own, ref, cut):
    if cut==0:
        return ref
    if cut==len(own):
        return own
    pre=own[:cut,:cut]
    coef=linalg.solve(ref[:cut,:cut],ref[:cut,cut:],assume_a="pos")
    cross=pre@coef
    post=ref[cut:,cut:]-ref[cut:,:cut]@coef+coef.T@cross
    return np.block([[pre,cross],[cross.T,post]])


def training_model(design,y):
    p,k = design.shape[1],y.shape[1]
    df = len(y)-p
    if df<=k-1:
        raise ConfigurationError("参考组插补的协方差后验不可估计")
    observed = np.isfinite(y)
    if np.min(observed.astype(int).T@observed.astype(int))<=1:
        raise ConfigurationError("参考组插补缺少访视间共同观测")
    start = y.copy()
    for j in range(k):
        ok = observed[:,j]
        beta,*_ = ols(design[ok],y[ok,j])
        start[~ok,j] = design[~ok]@beta
    xx = np.linalg.inv(design.T@design)
    left = np.linalg.cholesky(xx)
    projection = xx@design.T
    return design,y,df,start,left,projection,patterns(y)


def posterior(model,sweeps,rng):
    design,y,df,start,left,projection,train_groups=model
    p,k=design.shape[1],y.shape[1]
    filled = start.copy()
    for step in range(sweeps):
        bhat = projection@filled
        residual = filled-design@bhat
        sigma = np.atleast_2d(stats.invwishart.rvs(df=df,scale=residual.T@residual,random_state=rng))
        b = bhat + left@rng.standard_normal((p,k))@np.linalg.cholesky(sigma).T
        if step+1<sweeps:
            filled = conditional(y,design@b,sigma,train_groups,rng.standard_normal(y.shape))
    return b,sigma


def fit(arm, baseline, change, on, level, spec, extra=None):
    m, sweeps, seed = map(int,(spec["ref_m"],spec["ref_sweeps"],spec["ref_seed"]))
    if not (2<=m<=10000 and 1<=sweeps<=1000):
        raise ConfigurationError("参考组插补的链数或迭代轮数超出支持范围")
    methods = spec["strategies"].upper().split("、")
    x = np.column_stack([np.ones(len(arm)),arm,baseline])
    if extra is not None:
        x = np.column_stack([x,extra])
    xref = x.copy()
    xref[:,1] = 0
    # Own-arm on-treatment means; the reference arm follows MAR throughout.
    training = np.where(on | (arm[:,None]==0),change,np.nan)
    keep = np.isfinite(training).any(axis=1)
    design,y = x[keep],training[keep]
    separate=spec.get("ref_design","").startswith("各治疗组")
    reduced=np.delete(x,1,axis=1)
    if separate:
        models=[training_model(reduced[keep & (arm==a)],training[keep & (arm==a)]) for a in (0,1)]
    else:
        models=[training_model(design,y)]
    target_groups=patterns(change)
    n_on=on.sum(axis=1)
    blocks=[(a,cut,np.flatnonzero((arm==a)&(n_on==cut))) for a in (0,1) for cut in np.unique(n_on[arm==a])]
    bound=[(a,cut,idx,patterns(change[idx])) for a,cut,idx in blocks] if separate else []
    draws = {name:[] for name in methods}
    for stream in np.random.SeedSequence(seed).spawn(m):
        rng = np.random.default_rng(stream)
        if separate:
            (br,sr),(bt,st)=[posterior(mod,sweeps,rng) for mod in models]
            ref=reduced@br
            own=np.where(arm[:,None]==1,reduced@bt,ref)
        else:
            b,sigma=posterior(models[0],sweeps,rng)
            own,ref=x@b,xref@b
        noise = rng.standard_normal(change.shape)
        for name in methods:
            mu = trajectory(own,ref,on,name)
            if separate:
                completed=change.copy()
                for a,cut,idx,groups in bound:
                    local=transition_covariance(st,sr,cut) if a else sr
                    completed[idx]=conditional(change[idx],mu[idx],local,groups,noise[idx])
            else:
                completed = conditional(change,mu,sigma,target_groups,noise)
            bfit,vc,rss,complete_df = ols(x,completed[:,-1])
            draws[name].append([float(bfit[1]),float(vc[1,1]*rss/complete_df)])
    out = {}
    for name, chain in draws.items():
        values, trace = pool_imputations(chain,complete_df,level,spec["ref_df"])
        trace.update(subjects=len(x),training_subjects=len(y),training_by_visit=np.isfinite(y).sum(axis=0).tolist(),
                     seed=seed,sweeps=sweeps,reference_arm=spec["ref"],strategy=name,
                     observed_conditioning="all available including post-discontinuation",
                     covariance="arm-specific, reference conditional post-stop" if separate else "common unstructured",posterior="flat B, inverse-Wishart with n-p df")
        out[name]=(values,trace)
    return out
