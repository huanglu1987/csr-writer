"""Bind model inputs to the effective protocol and SAP; keep unsupported fits out of the Word draft."""
import re
from .errors import ConfigurationError
from .subject_analysis import endpoint_rows, table


def model_tables(people, domain, arms, spec, protocol, *, stops=None,treated=None,stop_issue=""):
    try:
        import numpy as np
        import scipy
        from . import longitudinal_models as fits
    except ImportError as exc:
        raise ConfigurationError("模型统计需要安装工具的 models 依赖（NumPy、SciPy）") from exc
    days=[int(x) for x in re.split(r"[、，及和与 ]+",spec["days"].strip()) if x]
    margin=int(spec["margin"])
    final=int(spec["target"])
    level=float(spec["model_conf"])/100
    if (not days or days[-1]!=final or days[0]-margin<=1 or
            any(b-a<=2*margin for a,b in zip(days,days[1:])) or
            [final-margin,final+margin]!=[int(spec["start"]),int(spec["end"])] or
            not 0<level<1 or len(arms)!=2 or {spec["test"],spec["ref"]}!=set(arms)):
        raise ConfigurationError("模型访视窗、终点、对比组或区间参数不一致")
    by_subject={}
    for target in days:
        local={**spec,"start":str(target-margin),"end":str(target+margin),"target":str(target)}
        rows=endpoint_rows(people,domain,arms,local,protocol)
        for sid,r in rows.items():
            if r["baseline_value"] is None:
                raise ConfigurationError(f"{sid}缺少预设模型要求的基线，需核对原始记录")
            item=by_subject.setdefault(sid,{"source":r["source"],"group":r["group"],"baseline":r["baseline"],
                     "baseline_value":r["baseline_value"],"visits":[]})
            item["visits"].append({"target":target,"row":r["followup"],"observed_day":next((q["day"] for q in r.get("measurements",[]) if q["row"]==r["followup"]),None),"value":r["followup_value"],"change":r["change"]})
    ids=sorted(by_subject)
    arm=np.array([int(by_subject[s]["group"]==spec["test"]) for s in ids])
    base=np.array([float(by_subject[s]["baseline_value"]) for s in ids])
    base-=base.mean()
    change=np.array([[float(v["change"]) if v["change"] is not None else np.nan for v in by_subject[s]["visits"]] for s in ids])
    from .fcs_models import fixed_covariates, discontinuations, parse_deltas, fcs
    tables=[];results={};errors=[];settings={}
    names={"mmrm":"MMRM","ancova":"ANCOVA","mi":"多重插补后ANCOVA","fcs":"MAR FCS后ANCOVA"}
    if spec.get("inference","").startswith("Kenward-Roger"):
        names["mmrm"]="Kenward–Roger MMRM"
    adjust=set(spec.get("adjust_models","").lower().split("、"))-{""}
    extra=None
    adjustment_error=None
    if adjust:
        try:
            extra,settings["covariates"]=fixed_covariates(ids,people,spec)
            settings["adjusted_models"]=sorted(adjust)
            for sid in ids:
                by_subject[sid]["covariates"]={k:people[sid]["covariates"][k] for k in settings["covariates"]}
        except ConfigurationError as exc:
            adjustment_error=exc
    def offsets(first,second,numbers):
        if {spec[first],spec[second]}!=set(arms):
            raise ConfigurationError("偏移组别未与研究组唯一对应")
        pairs=parse_deltas(spec[numbers])
        return pairs if spec[first]==spec["test"] else [(b,a) for a,b in pairs]

    mask=None;stop=None;deltas=[];within=[]
    names.update(rmmrm="随机中心截距MMRM",rancova="随机中心截距ANCOVA",rbmi="参考组插补")
    if "mixed_role" in spec:
        names["mixed"]=spec["center_form"]+"、"+spec["residual_form"]+"非结构化协方差MMRM"
    if spec.get("missing_table") or any(k+"_role" in spec for k in ("mnar","nar","rbmi")):
        try:
            if stop_issue and not stop_issue.startswith("缺少唯一可用的"):
                raise ConfigurationError(stop_issue)
            if stop_issue and stops is None and not all("stop_yn" in people[s] and "stop_date" in people[s] for s in ids):
                raise ConfigurationError("缺少唯一可核实的永久停药记录")
            stop,trace=discontinuations(ids,people,stops,treated)
            mask=np.isnan(change) & (np.array(days)[None,:]>stop[:,None])
            for i,sid in enumerate(ids):
                by_subject[sid]["permanent_stop"]=trace[sid]
                for j,record in enumerate(by_subject[sid]["visits"]):
                    record["post_stop_missing"]=bool(mask[i,j])
                    record["observed_after_stop"]=bool(record["change"] is not None and
                        record["observed_day"] is not None and record["observed_day"]>stop[i])
            settings["mnar_scope"]={"visits":days,"eligible_missing_per_visit":mask.sum(axis=0).tolist(),
                "permanently_stopped":int(np.isfinite(stop).sum()),"preserved_observed_after_stop":sum(
                    v["observed_after_stop"] for r in by_subject.values() for v in r["visits"])}
        except ConfigurationError as exc:
            mask=None;stop=None
            errors.append(f"依赖永久停药记录的敏感性分析暂缓。{exc}")
    if mask is not None:
        for method,keys in (("mnar",("delta_first","delta_second","deltas")),("nar",("nar_first","nar_second","nar_deltas"))):
            if method+"_role" in spec:
                try:
                    (deltas if method=="mnar" else within).extend(offsets(*keys))
                except ConfigurationError as exc:
                    errors.append(f"{method}偏移暂缓。{exc}")
    methods=sorted((k for k in names if k+"_role" in spec),key=lambda k:("主要","支持性","敏感性").index(spec[k+"_role"]))
    pending=[]
    for k in methods:
        role=spec[k+"_role"]+"分析"
        name=names[k]
        try:
            if k in adjust and adjustment_error:
                raise adjustment_error
            fixed=extra if k in adjust else None
            if k in {"rmmrm","rancova","mixed"}:
                from .random_center import fit
                center=None
                if k!="mixed" or spec["center_form"]!="无中心随机效应":
                    field=spec["center_col"].upper()
                    center=np.array([people[s]["covariates"].get(field,"").strip() for s in ids])
                    if any(v.upper() in {"",".","NA","N/A","NAN","NULL","未知","缺失"} for v in center):
                        raise ConfigurationError("存在未确定的中心标识")
                    for sid,site in zip(ids,center):
                        by_subject[sid]["random_center"]=str(site)
                values,trace=fit(arm,base,change,center,level,endpoint=k=="rancova",
                                 slope=k=="mixed" and "随机斜率" in spec["center_form"],
                                 heterogeneous=k=="mixed" and spec["residual_form"].startswith("按治疗组"))
                if center is not None:
                    trace["center_column"]=field
            elif k=="rbmi":
                if stop is None:
                    continue
                from .reference_imputation import fit
                if "rb_cat_cols" in spec:
                    fixed,settings["reference_covariates"]=fixed_covariates(ids,people,{"cat_cols":spec["rb_cat_cols"],"num_cols":spec["rb_num_cols"]})
                    for sid in ids:
                        by_subject[sid].setdefault("covariates",{}).update({f:people[sid]["covariates"][f] for f in settings["reference_covariates"]})
                on=np.array(days)[None,:]<=stop[:,None]
                for strategy,(value,detail) in fit(arm,base,change,on,level,spec,fixed).items():
                    pending.append((strategy,role,strategy+"参考组插补",value,detail))
                continue
            elif k=="mmrm":
                values,trace=fits.mmrm(arm,base,change,level,spec,fixed)
                if "kr_convention_explicit" in spec:
                    trace["kr_convention_explicit"]=spec["kr_convention_explicit"]
            elif k=="ancova":
                values,trace=fits.ancova(arm,base,change,level,fixed)
            elif k=="fcs":
                scenarios=fcs(arm,base,change,level,spec,fixed,mask=mask,deltas=deltas)
                values,trace=scenarios.pop(None)
                for i,(pair,(val,details)) in enumerate(scenarios.items(),1):
                    label=f"MNAR Δ（{spec['test']}{pair[0]:+g}分，{spec['ref']}{pair[1]:+g}分）"
                    pending.append((f"mnar{i}","敏感性分析",label,val,details))
            else:
                values,trace=fits.multiple_imputation(arm,base,change,level,spec)
            pending.append((k,role,name,values,trace))
            if k=="fcs":
                for i,pair in enumerate(within,1):
                    try:
                        val,detail=fcs(arm,base,change,level,spec,fixed,mask=mask,in_chain=pair)[None]
                        label=f"轮内偏移FCS（{spec['test']}{pair[0]:+g}分，{spec['ref']}{pair[1]:+g}分）"
                        pending.append((f"nar{i}","敏感性分析",label,val,detail))
                    except (ConfigurationError,ValueError,np.linalg.LinAlgError,FloatingPointError,OverflowError) as exc:
                        errors.append(f"轮内偏移FCS情景{pair}暂缓。{exc}")
        except (ConfigurationError,ValueError,np.linalg.LinAlgError,FloatingPointError,OverflowError) as exc:
            errors.append(f"{role}（{name}）暂缓。{exc}")
            if k=="fcs" and (deltas or within):
                errors.append("MNAR敏感性分析暂缓。基础FCS未能生成完整插补数据。")
    codes={"mixed":"3.12.1","mmrm":"3.1","ancova":"3.2","mi":"3.3","fcs":"3.6","rmmrm":"3.9.1","rancova":"3.9.2","J2R":"3.10.1","CIR":"3.10.2"}
    for k,role,name,values,trace in sorted(pending,key=lambda row:("主要分析","支持性分析","敏感性分析").index(row[1])):
        results[k]={"role":role,**values,**trace}
        label=f"第{spec['week']}周{spec['endpoint']}变化量组间差（{role}，{name}，{spec['test']}减{spec['ref']}）"
        stat=[label,f"{values['estimate']:.3f}",f"{values['low']:.3f} 至 {values['high']:.3f}",
              "<0.0001" if values['p']<.0001 else f"{values['p']:.4f}"]
        note=f"单位为分。纳入{trace['subjects']}人。"
        if k=="mmrm":
            note+=f"共同非结构化协方差，REML，{spec['inference']}推断。使用{trace['records']}次基线后观察。"
        elif k=="fcs":
            note+=f"MAR正态回归FCS，{trace['m']}条独立链各{trace['sweeps']}轮，按Rubin规则及{spec['fcs_df']}自由度合并。"
        elif k.startswith("mnar"):
            note+=f"MAR插补后，仅调整永久停药后的缺失值。两组末访实际偏移人数分别为{trace['endpoint_shifted_by_arm'][0]}和{trace['endpoint_shifted_by_arm'][1]}。"
        elif k=="mixed":
            note+=f"按实际访视拟合{trace['records']}次观察。REML，期望信息Satterthwaite自由度。"
            if "center_random_covariance" in trace:
                note+=f"两组末访中心内相关系数分别为{trace['endpoint_icc_by_arm']['test']:.3f}（试验）与{trace['endpoint_icc_by_arm']['reference']:.3f}（参考）。"
                note+=f"中心间治疗斜率标准差{trace['center_random_covariance'][1][1]**.5:.3f}分。"
        elif k in {"rmmrm","rancova"}:
            note+=f"中心随机截距方差{trace['center_variance']:.3f}，末访中心内相关系数{trace['endpoint_icc']:.3f}。REML，期望信息Satterthwaite推断。"
        elif k in {"J2R","CIR"}:
            note+=f"参考组为{spec['ref']}，按全部实测值条件插补。{trace['m']}条独立数据增广链各{trace['sweeps']}轮，Rubin合并，{spec['ref_df']}自由度。"
            if trace["covariance"].startswith("arm-specific"):
                note+="两组分别估计协方差，停药后借用参考组条件关系。"
        elif k.startswith("nar"):
            note+=f"每次条件抽样后调整停药后缺失值，更新值进入后续模型。{trace['m']}条独立链各{trace['sweeps']}轮，Rubin合并，{spec['fcs_df']}自由度。"
        elif k=="mi":
            note+=f"单调正态回归插补{spec['m']}次，Rubin合并，{spec['mi_df']}自由度。"
        else:
            note+="终点观察病例，调整基线，按残差自由度作t推断。"
        if k in {"fcs","J2R","CIR"} or k.startswith(("mnar","nar")):
            note+=f" 插补Monte Carlo标准误{trace['mcse']:.4f}分。"
        if k in adjust or (k.startswith(("mnar","nar")) and "fcs" in adjust):
            note+="额外调整变量为"+"、".join(settings["covariates"])+"。"
        if k in {"J2R","CIR"} and settings.get("reference_covariates"):
            note+="额外调整变量为"+"、".join(settings["reference_covariates"])+"。"
        note+=f" 标准误{values['se']:.4f}。"
        if values["df"] is not None:
            note+=f"自由度{values['df']:.2f}。"
        tables.append(table(codes[k] if k in codes else ("3.11."+k[3:] if k.startswith("nar") else "3.7."+k[4:]),f"主要终点{role}（{name}）",
                            "ITT",["估计值",f"{100*level:g}%置信区间","P值"],[stat],note))
    if tables and not any(r["role"]=="主要分析" for r in results.values()):
        for t in tables:
            t["rows"][0][0]="预设主要分析结果待补。"+t["rows"][0][0]
    if "mixed" in results:
        fit=results["mixed"]
        diagnostics=[]
        if "center_random_covariance" in fit:
            g=fit["center_random_covariance"]
            diagnostics.extend([[label,f"{value:.4f}"] for label,value in zip(
                ("中心随机截距方差","中心截距与治疗斜率协方差","中心治疗斜率方差"),(g[0][0],g[1][0],g[1][1]))])
        elif "center_variance" in fit:
            diagnostics.append(["中心随机截距方差",f"{fit['center_variance']:.4f}"])
        sigmas=fit.get("residual_by_arm",{"common":fit.get("residual_covariance")})
        for group,sigma in sigmas.items():
            name={"reference":spec["ref"],"test":spec["test"],"common":"两组合并"}[group]
            for j,target in enumerate(days):
                for i in range(j+1):
                    diagnostics.append([f"{name} 第{days[i]}与第{target}天残差协方差",f"{sigma[i][j]:.4f}"])
        tables.append(table("3.12.2","纵向模型方差分量","ITT",["估计值"],diagnostics,
                            "协方差及方差的单位为分的平方。治疗随机斜率模型以参考组为零编码。"
                            if "center_random_covariance" in fit else "协方差及方差的单位为分的平方。"))
    center_fit=next((results[k] for k in ("rmmrm","rancova","mixed") if k in results and "center_column" in results[k]),None)
    if center_fit:
        center_rows=[]
        for site in sorted({r["random_center"] for r in by_subject.values()}):
            members=np.array([by_subject[s]["random_center"]==site for s in ids])
            center_rows.append([str(site),*[int(np.sum(members & (arm==int(g==spec["test"])))) for g in arms]])
        tables.append(table("3.9.3","各中心随机入组人数","ITT",arms,center_rows,
            "按随机分配登记。中心随机截距方差及末访中心内相关系数见相应的模型表。"))
    if spec.get("missing_table") and mask is not None:
        obs=np.isfinite(change)
        def by_group(values):
            return [int(values[arm==int(g==spec["test"])].sum()) for g in arms]
        intermittent=np.any((~obs[:,:-1]) & np.logical_or.accumulate(obs[:,::-1],axis=1)[:,::-1][:,1:],axis=1)
        observed_post=np.array([[r["observed_after_stop"] for r in by_subject[s]["visits"]] for s in ids])
        rows=[["永久提前停药人数",*by_group(np.isfinite(stop))],["间歇缺失人数",*by_group(intermittent)],
              ["永久停药后实测次数",*by_group(observed_post)],["永久停药后名义访视缺失次数",*by_group(mask)]]
        for j,d in enumerate(days):
            rows.extend([[f"第{d}天访视实测人数",*by_group(obs[:,j])],[f"第{d}天访视缺失人数",*by_group(~obs[:,j])]])
        tables.append(table("3.8","随访缺失及永久停药后观察","ITT",arms,rows,
            "间歇缺失指某次随访缺测，之后又取得评分。停药后的实测按实际测量日，缺失按名义访视日。"))
    return tables,{"participants":by_subject,"models":results,"settings":settings,"numpy":np.__version__,"scipy":scipy.__version__},errors
