"""Prepare review-only statistical outputs and their raw-data derivations for draft-project."""
from collections import Counter
from statistics import mean, stdev
import csv
import re
from zipfile import BadZipFile

from .errors import ConfigurationError
from .io_utils import atomic_write_json, sha256_file
from .project_autoplan import identity
from .project_sources import choose_sources, eligible_record
from .project_table_match import COMPUTED_STATUS
from .sap_rules import additional_scopes, parse_sap
from .subject_analysis import efficacy, exposure, percentage, safety, subjects, table
from .subject_data import collect, number

BOOK="统计分析/统计复核表.xlsx"


def publish(out,tables,study,inventory):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from .project_evidence import read_workbook
    if BOOK in inventory:
        raise ConfigurationError("统计复核表输出相对路径与输入原件重名")
    book=Workbook()
    book.remove(book.active)
    for t in tables:
        ws=book.create_sheet(t["number"])
        rows=[[t["title"]],[f"研究编号：{study}｜状态：{COMPUTED_STATUS}｜分析集：{t['population']}"],
              ["指标",*t["columns"]],*t["rows"],["注："+t["note"]]]
        for i,row in enumerate(rows,1):
            for j,v in enumerate(row,1):
                c=ws.cell(i,j)
                c.value=str(v)
                c.data_type="s"
                if i in {1,3}:
                    c.font=Font(bold=True,color="17394F")
                    c.fill=PatternFill("solid",fgColor="E1F0F2")
        ws.freeze_panes="B4"
        for col in range(1,len(t["columns"])+2):
            ws.column_dimensions[get_column_letter(col)].width=52 if col==1 else 27
    path=out/BOOK
    path.parent.mkdir(exist_ok=True)
    book.save(path)
    extraction="统计分析/表格单元格.json"
    extracted,_=read_workbook(path,"tfl")
    atomic_write_json(out/extraction,extracted)
    inventory[BOOK]={"role":"tfl","origin":"computed_from_subjects","format":".xlsx","sha256":sha256_file(path),
                     "extraction":extraction,"status":"extracted","analysis_manifest":"统计分析/分析留痕.json"}


def run_subject_analysis(root,out,inventory,records,plan,review=None,sap_reading=None):
    external_tfl=any(r["role"]=="tfl" and not (r["format"] in {".csv",".tsv"} and
                     {str(c).upper() for c in r.get("columns",[])} & {"USUBJID","SUBJID","受试者编号"}) for r in inventory.values())
    if external_tfl:
        from .subject_data import ALIASES, DOMAINS
        def is_dm(cols):
            names={str(c).upper() for c in cols}
            return all(names.intersection(ALIASES[k]) for k in DOMAINS["subjects"])
        raw=any(is_dm(r.get("columns",[])) for r in inventory.values())
        if not raw:
            from openpyxl import load_workbook
            for name,r in inventory.items():
                if r["format"]==".xlsx" and r.get("status")!="unreadable":
                    wb=load_workbook(root/name,read_only=True)
                    try:
                        raw=any(is_dm(next(ws.values,())) for ws in wb)
                    finally:
                        wb.close()
                    if raw:
                        break
        if not raw:
            return None
    if plan.get("result_setup"):
        return None
    if not any(r["format"] in {".csv",".tsv",".xlsx",".xpt",".sas7bdat"} for r in inventory.values()):
        return None
    selected,_=choose_sources(inventory,plan["selected_sources"])
    anchor,problem=identity(selected,records)
    audit={"status":"pending","source_snapshot":plan["source_snapshot"],"decisions":[],"data_queries":[],
           "sources":{},"tables":[],"derivations":{},"sap_rules":{}}
    def decision(role,action,sections):
        audit["decisions"].append({"role":role,"action":action,"sections":sections})
    def lock(issue,sections):
        decision("统计负责人",issue,sections)
    def hold(message,sections):
        # Route resolvable programming/input problems away from the sign-off queue.
        if any(marker in message for marker in ("与同节","未规定","未定义","协议冲突","与 SAP 不符","未能唯一核对","参数不一致")):
            lock(message,sections)
        else:
            audit["data_queries"].append(message)
    def finish():
        (out/"统计分析").mkdir(exist_ok=True)
        atomic_write_json(out/"统计分析/分析留痕.json",audit)
        return audit
    if not anchor or not selected.get("sap"):
        lock(problem or "确定本研究适用的 SAP 后生成统计表。",["11","12"])
        return finish()
    study=anchor["value"]
    if review is None:
        from .project_answers import Decisions
        review=Decisions(None)
    simulated=any(re.search("虚构研究|模拟研究|仅用于.{0,5}演练",r["text"]) for r in records[selected["protocol"]])
    review.bind(study,simulated)
    overrides={}
    sap=selected["sap"]
    sapid,_=identity({"protocol":sap},records)
    if not sapid or sapid["value"].upper()!=study.upper():
        lock("SAP 未能与方案研究编号唯一对应。",["11","12"])
        return finish()
    audit["study"]=study
    audit["sources"]={s:inventory[s]["sha256"] for s in selected.values()}
    unresolved = any(isinstance(w,dict) and w.get("code")=="SOURCE_HAS_UNACCEPTED_REVISIONS"
                     for s in selected.values() for w in inventory[s].get("warnings",[]))
    reviewed_rule = any(r.get("has_review_comment") and re.search(r"研究编号|分析集|分析|ITT|SAF|TEAE|终点|应答|基线|访视窗|加重|给药|救援",r["text"],re.I)
                        for s in selected.values() for r in records.get(s,[]))
    if unresolved or reviewed_rule:
        audit["data_queries"].append("方案或 SAP 有未接受修订或与分析相关的批注，须核对有效条款后再计算。")
        return finish()
    reading=[]
    unresolved_sap=[]
    if sap_reading:
        from .io_utils import load_json_unique
        reply=load_json_unique(sap_reading)
        if reply.get("source")!=sap or reply.get("sha256")!=inventory[sap]["sha256"]:
            raise ConfigurationError("SAP 解读引用的文件或版本与有效原件不同")
        reading=reply["clauses"]
    values,rule_evidence,rule_errors=parse_sap(records[sap],reading,unresolved_sap)
    if unresolved_sap or rule_errors:
        atomic_write_json(out/"待智能体解读SAP.json",dict(source=sap,sha256=inventory[sap]["sha256"],clauses=unresolved_sap,issues=rule_errors))
    if values.get("groups",{}).get("arms") and "groups" not in rule_errors:
        inventory[sap]["analysis_arms"]=values["groups"]["arms"].split("、")
    audit["data_queries"].extend(additional_scopes(records[sap]))
    audit["sap_rules"]={"source":sap,"rules":rule_evidence}
    inventory[sap]["analysis_rule_locations"]=list(dict.fromkeys(a["location"] for rules in rule_evidence.values() for r in rules.values() for a in r.get("anchors",[r])))
    try:
        cov_spec=values.get("efficacy",{})
        fields=[f.strip().upper() for key in ("cat_cols","num_cols","center_col","rb_cat_cols","rb_num_cols") for f in cov_spec.get(key,"").split("、") if f and f!="无"]
        if values.get("categorical",{}).get("site"):
            fields.append(values["categorical"]["site"].upper())
        domains,data_errors=collect(root,inventory,study,covariates=list(dict.fromkeys(fields)))
    except (ImportError, OSError, UnicodeError, csv.Error, BadZipFile, ValueError) as exc:
        audit["data_queries"].append(f"读取逐例文件失败：{exc}")
        return finish()
    audit["domains"]={k:{p:v[p] for p in ("source","sheet","columns")} for k,v in domains.items()}
    audit["sources"].update({v["source"]:inventory[v["source"]]["sha256"] for v in domains.values()})
    for dataset in domains.values():
        inventory[dataset["source"]]["analyzed_subject_data"]=True
    def require(domain):
        if domain in data_errors:
            raise ConfigurationError(data_errors[domain])
        return domains[domain]
    try:
        if "groups" in rule_errors:
            raise ConfigurationError(rule_errors["groups"])
        arms=[g.strip() for g in re.split(r"[、,，]",values["groups"]["arms"])]
        if len(arms)<2 or any(not g for g in arms) or len(set(arms))!=len(arms):
            raise ConfigurationError("SAP 组别缺失或重复")
        people=subjects(require("subjects"),arms)
    except ConfigurationError as exc:
        hold("分析集及分组暂缓。"+str(exc),["10.1","11","12"])
        return finish()
    rand=Counter(p["arm"] for p in people.values() if p["randomized"])
    audit["derivations"]["subjects"]={sid:{"randomized":p["randomized"],"group":p["arm"],"source":p["loc"]} for sid,p in people.items()}
    tables=[table("1.1","受试者处置与随机分配","随机人群",arms,
                  [["已随机人数",*[rand[g] for g in arms]]],
                  f"受试者主记录中共有 {len(people)} 人，其中 {sum(not p['randomized'] for p in people.values())} 人未随机。")]
    if "disposition" in rule_evidence["groups"]:
        if all(p.get("end_status") in {"完成","提前结束"} for p in people.values() if p["randomized"]):
            for status in ("完成","提前结束"):
                tables[0]["rows"].append([status,*[str(sum(p["randomized"] and p["arm"]==g and p["end_status"]==status
                                                          for p in people.values())) for g in arms]])
        else:
            audit["data_queries"].append("处置表暂仅列随机人数；需补足逐例完成状态。")
    if "demographics" in rule_evidence["groups"]:
        try:
            cols=[]
            for g in arms:
                members=[p for p in people.values() if p["randomized"] and p["arm"]==g]
                ages=[number(p.get("age",""),p["loc"]) for p in members]
                if any(p.get("sex","") not in {"F","M","女","男"} for p in members) or any(a<0 for a in ages):
                    raise ConfigurationError("年龄或性别编码缺失或未知")
                cols.append([f"{mean(ages):.1f}",f"{stdev(ages):.1f}" if len(ages)>1 else "不可估计",
                             percentage(sum(p["sex"] in {"F","女"} for p in members),len(members))])
            demo=[[label,*v] for label,v in zip(("平均年龄（岁）","年龄样本标准差（岁）","女性 n/N（%）"),zip(*cols))]
            tables.append(table("1.3","人口学基线特征","随机人群",arms,demo,"按随机分配汇总。"))
        except ConfigurationError as exc:
            audit["data_queries"].append("人口学特征表暂缓。"+str(exc))
    try:
        treated=exposure(people,require("exposure"),arms)
        audit["derivations"]["exposure"]=treated
        rows=[["ITT（随机组）",*[rand[g] for g in arms]],
              ["SAF（实际用药组）",*[sum(v["actual"]==g for v in treated.values()) for g in arms]]]
        tables.append(table("1.2","分析人群汇总","随机人群",arms,rows,"ITT 按随机分配；SAF 按实际暴露。"))
        if "exposure" in rule_evidence["safety"] and "safety" not in rule_errors:
            tables.append(table("2.1","治疗暴露与用药天数","SAF",arms,
                [["实际用药人数",*rows[1][1:]],["总用药人日",*[sum(v["days"] for v in treated.values() if v["actual"]==g) for g in arms]]],
                "仅计算正剂量日期；同一人同日多条给药记录合并为一人日。"))
    except ConfigurationError as exc:
        treated=None
        hold("实际给药集或分组暂不能确定。"+str(exc),["12.1","12.2"])
    try:
        if "efficacy" in rule_errors:
            raise ConfigurationError(rule_errors["efficacy"])
        protocol=[r for r in records.get(selected["protocol"],[]) if eligible_record(r)]
        spec=values["efficacy"]
        if "model_conf" in spec:
            from .model_tables import model_tables
            t,rows,model_errors=model_tables(people,require("scores"),arms,spec,protocol,
                stops=domains.get("stops"),treated=treated,stop_issue=data_errors.get("stops",""))
            for error in model_errors:
                hold(error,["11.4.1","14.2"])
        else:
            t,rows=efficacy(people,require("scores"),arms,spec,protocol)
        tables.extend(t)
        audit["derivations"]["efficacy"]=rows
    except ConfigurationError as exc:
        hold("主要疗效表暂缓。"+str(exc),["11.4.1","14.2"])
    if treated is not None:
        try:
            if "safety" in rule_errors:
                raise ConfigurationError(rule_errors["safety"])
            t,events=safety(people,treated,require("events"),arms,values["safety"])
            serious_ids={(e["id"],e["event_id"]) for e in events if e.get("serious")}
            audit["case_records"]=[dict(r) for r in domains["events"]["rows"] if (r["id"],r["seq"]) in serious_ids]
            for ev in events:
                if not ev["included"] or ev["related"]!="待定":
                    continue
                row=next(r for r in domains["events"]["rows"] if r["id"]==ev["id"] and r["seq"]==ev["event_id"])
                options=[*re.split("[、，,及和]",values["safety"]["related_terms"]),"无关","待评估"]
                reply=review.ask("AE-"+ev["id"]+"-"+ev["event_id"],"医学负责人",
                    f"请记录研究者对 {ev['id']}/{ev['event_id']} {ev['pt']} 的相关性评价及依据。",
                    ["12.2.3","12.3.2","14.3.3","16.3.1","2"],{"event":dict(row),"rule":rule_evidence["safety"]["related_terms"]},options)
                if reply and reply["choice"]!="待评估":
                    row["related"]=reply["choice"]
                    overrides[row["id"],row["seq"]]=row["related"]
            if overrides:
                t,events=safety(people,treated,require("events"),arms,values["safety"])
            tables.extend(t)
            audit["derivations"]["events"]=events
            uncertain=[e for e in events if e.get("included") and e["related"]=="待定"]
            serious=[e for e in events if e.get("serious")]
            case_rows=[]
            for r in domains["events"]["rows"]:
                if r.get("narrative") and (r["id"],r["seq"]) in {(e["id"],e["event_id"]) for e in serious}:
                    case_rows.append({"text":r["narrative"],"location":r["loc"]+" / "+domains["events"]["columns"]["narrative"],
                                      "kind":"subject_case"})
            if case_rows:
                source=domains["events"]["source"]
                records.setdefault(source,[]).extend(case_rows)
                inventory[source]["case_locations"]=[r["location"] for r in case_rows]
            medical=[]
            if uncertain:
                medical.append("核定相关性仍待评估的事件（"+"、".join(f"{r['id']}/{r['event_id']}" for r in uncertain)+"）")
            if serious:
                medical.append("复核严重事件病程、处置及转归（"+"、".join(f"{r['id']}/{r['event_id']}" for r in serious)+"）")
            if medical:
                decision("医学负责人","；".join(medical)+"。",["12.2.3","12.3.2"])
        except ConfigurationError as exc:
            hold("安全性事件表暂缓。"+str(exc),["12.2","12.3"])
    for method,domain_name in (("cox","time_event"),("cmh","task")):
        if method in rule_errors:
            hold("次要分析暂缓。"+rule_errors[method],["11.4.1","14.2"])
        elif rule_evidence.get(method):
            from .secondary_models import fit_secondary
            try:
                t,trace=fit_secondary(method,people,require(domain_name),arms,values[method],
                        [r for r in records.get(selected["protocol"],[]) if eligible_record(r)])
                tables.append(t)
                audit["derivations"][method]=trace
            except (ConfigurationError,OverflowError) as exc:
                hold("次要分析暂缓。"+str(exc),["11.4.1","14.2"])
    if "categorical" in rule_errors:
        hold("分类终点分析暂缓。"+rule_errors["categorical"],["11.4.1","14.2"])
    elif rule_evidence.get("categorical"):
        try:
            import numpy as np
            from .categorical_reference import tables as category_tables
            stop_error=data_errors.get("stops","")
            if stop_error and not stop_error.startswith("缺少唯一可用的"):
                raise ConfigurationError(stop_error)
            if treated is None:
                raise ConfigurationError("无法核实停药与实际给药记录")
            t,trace=category_tables(people,require("categorical"),domains.get("stops"),treated,arms,values["categorical"],
                                    [r for r in records.get(selected["protocol"],[]) if eligible_record(r)])
            tables.extend(t)
            audit["derivations"]["categorical"]=trace
        except ImportError:
            hold("分类终点分析需要模型统计依赖 NumPy、SciPy。",["11.4.1","14.2"])
        except (ConfigurationError,ValueError,ArithmeticError) as exc:
            hold("分类终点分析暂缓。"+str(exc),["11.4.1","14.2"])
    if "descriptors" in rule_errors:
        hold("补充描述统计暂缓。"+rule_errors["descriptors"],["12.4","12.5"])
    elif treated is not None:
        from .visit_summary import visit_table
        for kind in rule_evidence.get("descriptors",{}):
            try:
                t,trace=visit_table(kind,require(kind),people,treated,arms,values["descriptors"])
                tables.append(t)
                audit["derivations"][kind]=trace
            except ConfigurationError as exc:
                hold("补充描述统计暂缓。"+str(exc),["12.4","12.5"])
    try:
        publish(out,tables,study,inventory)
    except (ImportError,ConfigurationError) as exc:
        audit["data_queries"].append("未生成可核对的统计复核表："+str(exc))
        return finish()
    audit.update(status="review_draft",workbook=BOOK,workbook_sha256=inventory[BOOK]["sha256"],
                 tables=[{"sheet":t["number"],"title":t["title"],"population":t["population"],"rows":len(t["rows"])} for t in tables])
    from .independent_qc import verify,compare_model
    from .project_reconcile import reconcile
    try:
        checks=verify(root,domains,study,arms,{**values,"_group_rules":rule_evidence["groups"]},tables,overrides)
        compare_model(checks,audit["derivations"].get("efficacy",{}))
    except (ArithmeticError,ValueError,KeyError,IndexError,OSError) as exc:
        checks={"tables":{},"issue":"独立计算未完成："+str(exc)}
        audit["data_queries"].append(checks["issue"])
    atomic_write_json(out/"统计分析/独立复算.json",checks)
    cutoffs={d for src,parts in records.items() if src in selected.values() or inventory.get(src,{}).get("role")!="tfl"
             for r in parts for d in re.findall(r"数据截止日[：:]\s*(\d{4}-\d{2}-\d{2})",r["text"])}
    cutoff=next(iter(cutoffs)) if len(cutoffs)==1 else "资料存在多个截止日" if cutoffs else None
    ledger=reconcile(out,inventory,BOOK,tables,study,cutoff,checks["tables"],review)
    for row in ledger:
        if row.get("number")=="2.1":
            row["same_day_administrations"]=checks.get("same_day_administrations",[])
    atomic_write_json(out/"统计分析/逐表对账.json",ledger)
    audit["reconciliation"]=[{k:r.get(k) for k in ("number","status","selected","independent")} for r in ledger]
    audit["decision_updates"]=[dict(subject=s,event=q,related=v) for (s,q),v in overrides.items()]
    audit["has_external"]=external_tfl
    for item in ledger:
        if item.get("independent")=="不一致":
            audit["data_queries"].append(item["title"]+" 独立复算不一致，暂缓采用。")
    message="复核分析集、逐例推导及所执行的 SAP 规则，决定是否签认为正式统计输出。"
    fitted=audit["derivations"].get("efficacy",{}).get("models",{})
    if audit["derivations"].get("categorical"):
        message+="结合各类别实测分布，评价参考组轨迹、停药后假设、插补误差及应答率差的解释。"
    if "mixed" in fitted:
        message+="审阅SAP指定的中心效应、两组残差协方差和治疗对比自由度。"
    if fitted:
        message+="核准治疗对比方向、访视窗及实际采用的模型。"
        if "mmrm" in fitted:
            message+="核对MMRM的协方差、收敛及推断方式。"
            if fitted["mmrm"].get("kr_convention_explicit") is False:
                message+="SAP仅指定Kenward–Roger，请确认采用直接协方差参数和期望信息的计算约定。"

        if "fcs" in fitted:
            message+="复核固定效应的协变量编码、FCS链的诊断和缺失假设。"
        if any(k.startswith("mnar") for k in fitted):
            message+="核定预设MNAR偏移情景与永久停药后缺失的适用范围，并解释情景间差异。"
        if {"rmmrm","rancova"}.intersection(fitted):
            message+="结合各中心样本量，审阅随机截距方差、末访中心内相关及自由度估计。"
        if {"J2R","CIR"}.intersection(fitted):
            strategies="、".join(k for k in ("J2R","CIR") if k in fitted)
            message+=f"结合停药后实测资料，解释{strategies}轨迹假设及其与主要分析的差异。"
        if any(k.startswith("nar") for k in fitted):
            message+="核对轮内偏移的方向及临床含义，并解释对后续访视插补的影响。"
        if "mi" in fitted:
            message+="核对缺失模式、MAR依据、插补次数及合并的不确定性。"
    if treated is not None:
        switches=sum(v["actual"]!=people[s]["arm"] for s,v in treated.items())
        message+=f"本轮含 {sum(rand.values())-len(treated)} 名随机后未用药者及 {switches} 名实际用药组与随机组不同者。"
    decision("统计负责人",message,["10","11","12","14"])
    return finish()
