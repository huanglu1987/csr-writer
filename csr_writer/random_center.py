"""Two-arm Gaussian endpoint or repeated-measures REML with prespecified center effects and arm-specific covariance."""
import numpy as np
from scipy import linalg, optimize
from .errors import ConfigurationError
from .longitudinal_models import inference


def fit(arm, baseline, changes, centers, level, *, endpoint=False, slope=False, heterogeneous=False):
    ymat = changes[:, -1:] if endpoint else changes
    who, visit = np.where(np.isfinite(ymat))
    y = ymat[who, visit]
    k = ymat.shape[1]
    has_center = centers is not None
    if slope and not has_center:
        raise ConfigurationError("随机治疗斜率须指定中心")
    levels, group = np.unique(centers[who] if has_center else who, return_inverse=True)
    if has_center and (len(levels) < 2 or len(np.unique(who)) <= len(levels)):
        raise ConfigurationError("中心间或中心内重复数不足，随机截距与残差无法区分")
    v = np.eye(k)[visit]
    x = np.column_stack([v, v * arm[who, None], v * baseline[who, None]])
    if np.linalg.matrix_rank(x) != x.shape[1] or len(y) <= x.shape[1]:
        raise ConfigurationError("随机中心模型固定效应不可估计")
    contrast = np.eye(x.shape[1])[2*k-1]
    # The variance components are linear in the entries of the marginal covariance.
    lower = np.tril_indices(k)
    basis = np.zeros((len(lower[0]), k, k))
    for b, i, j in zip(basis, *lower):
        b[i,j] = b[j,i] = 1.
    if slope and sum(len(set(arm[who[group==h]]))==2 for h in range(len(levels)))<3:
        raise ConfigurationError("可比较两个治疗组的中心不足，无法估计随机治疗斜率")
    arm_blocks = (0,1) if heterogeneous else (None,)
    for a in arm_blocks:
        observed = np.isfinite(ymat if a is None else ymat[arm==a]).astype(int)
        if np.min(observed.T@observed)<2:
            raise ConfigurationError("组内成对访视观测不足，非结构化协方差不可估计")
    blocks = []
    for h in range(len(levels)):
        idx = np.flatnonzero(group == h)
        sub, t = who[idx], visit[idx]
        same = sub[:, None] == sub
        treatment = arm[sub].astype(float)
        derivatives = [same * b[t[:,None], t] * (1 if a is None else (treatment==a)[:,None])
                       for a in arm_blocks for b in basis]
        if has_center:
            derivatives.append(np.ones(same.shape))
        if slope:
            derivatives.extend([treatment[:,None]+treatment, np.outer(treatment,treatment)])
        blocks.append((idx,np.array(derivatives)))

    dimensions = [k]*len(arm_blocks) + ([2] if slope else [])
    triangles = [np.tril_indices(size) for size in dimensions]
    scalar_tau = has_center and not slope
    def compute(theta, details=False):
        choleskies, matrices, raw = [], [], []
        offset = 0
        for size,tri in zip(dimensions,triangles):
            factor = np.zeros((size,size))
            count = len(tri[0])
            factor[tri] = theta[offset:offset+count]
            matrix = factor@factor.T
            choleskies.append(factor)
            matrices.append(matrix)
            raw.extend(matrix[tri])
            offset += count
        if scalar_tau:
            raw.append(theta[-1])
        pars = np.array(raw)
        xtvx = np.zeros((x.shape[1],x.shape[1]))
        xtvy = np.zeros(x.shape[1])
        ll = 0.
        partial = []
        for idx, ds in blocks:
            vv = np.einsum('i,ijk->jk',pars,ds)
            cf = linalg.cho_factor(vv,lower=True)
            vi = linalg.cho_solve(cf,np.eye(len(idx)))
            vx = vi @ x[idx]
            xtvx += x[idx].T @ vx
            xtvy += vx.T @ y[idx]
            ll += 2*np.log(np.diag(cf[0])).sum()
            partial.append((idx,ds,vi,vx))
        cb = linalg.cho_factor(xtvx,lower=True)
        cov = linalg.cho_solve(cb,np.eye(len(xtvy)))
        beta = cov @ xtvy
        ll += 2*np.log(np.diag(cb[0])).sum()
        grad = np.zeros(len(pars))
        for idx,ds,vi,vx in partial:
            r = y[idx] - x[idx] @ beta
            vr = vi @ r
            ll += r @ vr
            grad += np.einsum('ijk,jk->i',ds,vi-vx@cov@vx.T-np.outer(vr,vr)) / 2
        if details:
            return beta,cov,matrices,partial
        jac = []
        offset = 0
        for factor,tri in zip(choleskies,triangles):
            gs = np.zeros_like(factor)
            count=len(tri[0])
            gs[tri] = grad[offset:offset+count]
            jac.extend(((gs+gs.T)@factor)[tri])
            offset += count
        if scalar_tau:
            jac.append(grad[-1])
        return ll/2, np.asarray(jac)

    scale = max(float(np.std(y)),1.e-3)
    init = np.concatenate([(np.eye(size)*scale*(.35 if slope and n==len(arm_blocks) else 1))[tri]
                           for n,(size,tri) in enumerate(zip(dimensions,triangles))])
    bounds = [(scale*1.e-6,None) if i==j else (None,None)
              for tri in triangles for i,j in zip(*tri)]
    if scalar_tau:
        init=np.r_[init,scale**2/4]
        bounds.append((0.,None))
    objective=compute
    log_diag=np.array([i==j for tri in triangles for i,j in zip(*tri)]+([False] if scalar_tau else []))
    transform=slope or heterogeneous
    if transform:
        init[log_diag]=np.log(init[log_diag])
        bounds=[(np.log(scale)-10,np.log(scale)+6) if change else bound for bound,change in zip(bounds,log_diag)]
        def objective(parameters):
            raw=parameters.copy()
            raw[log_diag]=np.exp(raw[log_diag])
            try:
                value,jac=compute(raw)
            except np.linalg.LinAlgError:
                return np.inf,np.zeros_like(raw)
            jac[log_diag]*=raw[log_diag]
            return value,jac
    opt = optimize.minimize(objective,init,jac=True,bounds=bounds,method="L-BFGS-B",
                            options={"ftol":1.e-13,"gtol":1.e-7,"maxiter":450,"maxls":50})
    if not opt.success:
        raise ConfigurationError("随机中心模型REML未收敛")
    if transform:
        if np.max(np.abs(opt.jac))>1.e-3:
            opt=optimize.minimize(objective,opt.x,jac=True,method="BFGS",options={"gtol":1.e-6,"maxiter":500})
            if not np.isfinite(opt.fun) or np.max(np.abs(opt.jac))>1.e-3:
                raise ConfigurationError("REML标准化梯度未达到收敛精度")
        opt.x[log_diag]=np.exp(opt.x[log_diag])
    beta,cov,matrices,partial = compute(opt.x,True)
    if (scalar_tau and opt.x[-1] <= scale**2*1.e-8) or any(np.linalg.eigvalsh(v)[0]<scale**2*1.e-8 for v in matrices):
        raise ConfigurationError("REML方差落在边界，预设的内部点自由度推断不适用")
    # Expected REML information in independent center blocks, including the shared
    # fixed-effect projection. This avoids allocating a study-wide covariance matrix.
    dim = len(init)
    first = np.zeros((dim,dim))
    mixed = np.zeros_like(first)
    fixed = np.zeros((dim,len(beta),len(beta)))
    for idx,ds,vi,vx in partial:
        vd = vi @ ds
        first += np.einsum('iab,jba->ij',vd,vd)
        du = ds @ vx
        mixed += np.einsum('iap,jap->ij',du @ cov,vi @ du)
        fixed += vx.T @ du
    small = cov @ fixed
    info = (first-mixed-mixed.T+np.einsum('iab,jba->ij',small,small))/2
    grad = (cov@contrast) @ fixed @ (cov@contrast)
    var = float(contrast @ cov @ contrast)
    df = 2*var**2/(grad @ linalg.solve(info,grad,assume_a="pos"))
    values = inference(contrast@beta,np.sqrt(var),df,level)
    trace = dict(subjects=len(np.unique(who)),records=len(y),inference="REML, expected-information Satterthwaite")
    if heterogeneous:
        trace["residual_by_arm"]={name:matrices[i].tolist() for i,name in enumerate(("reference","test"))}
    else:
        trace["residual_covariance"]=matrices[0].tolist()
    if has_center:
        g = matrices[-1] if slope else np.array([[float(opt.x[-1])]])
        trace["center_variance"]=float(g[0,0])
        trace["center_analyzed_counts"]={str(v):int(len(np.unique(who[group==i]))) for i,v in enumerate(levels)}
        if slope:
            trace["center_random_covariance"]=g.tolist()
        icc={}
        for arm_code,name in enumerate(("reference","test")):
            z=np.array([1.,arm_code]) if slope else np.ones(1)
            shared=float(z@g@z)
            icc[name]=shared/(shared+matrices[arm_code if heterogeneous else 0][-1,-1])
        if slope or heterogeneous:
            trace["endpoint_icc_by_arm"]=icc
        else:
            trace["endpoint_icc"]=icc["reference"]
    return values, trace
