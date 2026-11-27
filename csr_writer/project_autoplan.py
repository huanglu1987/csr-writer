"""Build a replayable internal-draft plan from the current study's original files."""
from collections import defaultdict
from pathlib import Path
import re

from .errors import ConfigurationError
from .io_utils import atomic_write_json, load_json_unique
from .project_sources import choose_sources, eligible_record, method_candidates
from .project_table_match import CAPTION, data_rows, find_layout, study_matches

CODE = r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*\d[A-Za-z0-9_-]*"
LABELED_ID = re.compile(r"(?:方案编号|研究编号|试验编号|protocol\s*(?:number|no\.?|id)|study\s*(?:id|number))\s*[:：=]?\s*("+CODE+r")(?![\w-])",re.I|re.ASCII)
TOKEN_ID = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){2,})(?![A-Za-z0-9_-])")
TITLE = re.compile(r"研究题目|研究标题|试验名称|研究名称|study title",re.I)


def identity(selected, records):
    """Only a chosen protocol can establish identity; an independent SAP may contradict it."""
    ids={}
    for role,source in selected.items():
        rows=[r for r in records.get(source,[]) if eligible_record(r)]
        seen={}
        for r in rows:
            for match in LABELED_ID.finditer(r["text"]):
                seen[match[1].upper()]={"value":match[1],"source":source,"location":r["location"]}
        if not seen:
            for r in rows[:6]:
                for code in TOKEN_ID.findall(r["text"]):
                    if any(c.isdigit() for c in code):
                        seen[code.upper()]={"value":code,"source":source,"location":r["location"]}
        ids[role]=seen
    protocol=ids.get("protocol",{})
    if len(protocol)!=1:
        return None,"研究方案中的研究编号缺失或不唯一，需确定所用方案。"
    key=next(iter(protocol))
    if ids.get("sap") and set(ids["sap"])!={key}:
        return None,"方案与 SAP 的研究编号冲突，须确定归属后才能采用统计结果。"
    return protocol[key],None


# Body ownership depends on the meaning of the table, not the digits in its caption.
# The annex always retains the complete released table including non-highlighted rows.
TABLE_TOPICS = (
    ("flow",r"受试者处置|患者处置|受试者分布|受试者流向|participant disposition|subject disposition|patient disposition","10.1","14.1"),
    ("deviation",r"方案偏离|protocol deviations?","10.2","14.1"),
    ("sets",r"分析人群汇总|分析集分布|analysis populations?|analysis sets?","11.1","14.1"),
    ("baseline",r"人口学|基线特征|人口统计|demograph|baseline characteristics","11.2","14.1"),
    ("compliance",r"依从性|依从率|adherence|compliance","11.3","14.1"),
    ("missing_visits",r"随访缺失及永久停药后观察","11.4.2.2","14.2"),
    ("center",r"纵向模型方差分量|各中心|分中心|by (?:site|cent[er]+)","11.4.2.4","14.2"),
    ("efficacy",r"主要终点|主要疗效|次要终点|次要疗效|关键次要|探索性组间估计|客观缓解|primary (?:endpoint|outcome)|secondary (?:endpoint|outcome)|efficacy","11.4.1","14.2"),
    ("exposure",r"药物暴露|用药暴露|治疗暴露|drug exposure|treatment exposure","12.1","14.3"),
    ("ae_terms",r"系统器官|首选术语|preferred term|system organ|soc.pt","12.2.2","14.3.1"),
    ("consequence",r"不良事件后果|事件处理|停药事件|action taken","12.2.3","14.3.1"),
    ("serious",r"死亡和严重|死亡及严重|严重不良事件|deaths? and serious|serious adverse events?","12.3.1","14.3.2"),
    ("safety",r"安全性概|不良事件概|治疗期间出现的不良事件|不良事件汇总|teae overview|adverse events? overview|overview of (?:treatment.emergent )?adverse events","12.2.1","14.3.1"),
    ("lab",r"实验室|转移酶变化|laboratory|haematology|hematology","12.4.2.1","14.3"),
    ("vitals",r"生命体征|心电图|vital signs|electrocardiogram","12.5","14.3"),
)
DETAIL_ROWS = {
    "safety":[("12.2.3",r"有关|相关|related")],
    "serious":[("12.3.1.1",r"死亡|致死|death"),("12.3.1.2",r"住院|严重|serious|hospital"),("12.3.1.3",r"其他重要|other significant")],
    "consequence":[("12.2.6",r"永久停"),("12.2.7",r"暂时|临时"),("12.2.8",r"退出"),("12.3.1.3",r"非严重|其他重要")],
    "lab":[("12.4.2.2",r"个体变化|个例变化|individual change|change from baseline|变化量"),("12.4.2.3",r"临床重要|临床意义|clinically significant")],
    "vitals":[("12.5.2",r"心电图|ECG"),("12.5.3",r"生命体征|vital")],
}
POP_ALIAS={"FAS":"FAS","全分析集":"FAS","ITT":"ITT","MITT":"MITT","意向治疗集":"ITT","SAF":"SAF","安全性分析集":"SAF","安全集":"SAF","SS":"SS","PPS":"PPS","符合方案集":"PPS"}


def analysis_contract(rows):
    groups=set()
    populations={}
    for r in rows:
        if not eligible_record(r):
            continue
        for match in re.finditer(r"(?:报告组别|统计表组别|治疗组别|治疗组名称)\s*[:：]\s*([^。；;\n]+)",r["text"]):
            groups.add(tuple(re.split(r"\s*[、,，|｜]\s*",match[1].strip())))
        for match in re.finditer(r"(主要疗效|主要终点|安全性)(?:分析人群|分析集)\s*[:：]\s*([A-Za-z]+)\b",r["text"],re.I):
            populations.setdefault("safety" if match[1]=="安全性" else "efficacy",set()).add(match[2].upper())
    return groups,populations


def table_jobs(inventory,out,study,titles,jobs,issues,methods,has_frozen,contract,matches,unmatched):
    tables=[]
    seen_matches=set()
    for src,source in inventory.items():
        if source["role"]!="tfl" or not source.get("extraction"):
            continue
        if has_frozen and source["format"] in {".rtf",".docx"}:
            continue
        data=load_json_unique(out/source["extraction"])
        sheets=data.get("sheets",{})
        if not sheets:
            issues.append(f"统计原件 {src} 未自动识别汇总表结构，请核对原件布局。")
        for sheet,region in sheets.items():
            if sheet in source.get("withheld_sheets",[]) or "rows" not in region:
                continue
            try:
                layout=find_layout(region["rows"],computed=source.get("origin")=="computed_from_subjects")
                if not study or not study_matches(layout,study):
                    raise ConfigurationError("表内研究编号与方案不能唯一对应")
                values,_=data_rows(region["rows"],layout)
                title=CAPTION.sub("",layout.title,count=1)
                topic=next((t for t in TABLE_TOPICS if re.search(t[1],title,re.I)),None)
                match=matches.get((src,sheet))
                if match:
                    seen_matches.add((src,sheet))
                if match:
                    if topic and topic[0]!=match["topic"]:
                        raise ConfigurationError("答复的统计主题与已识别的表义冲突")
                    topic=next(t for t in TABLE_TOPICS if t[0]==match["topic"])
                    counts=[v[0] for _,v in values]
                    if any(counts.count(label)!=1 for label in match["row_labels"]):
                        raise ConfigurationError("答复的摘要指标未唯一匹配原件行标签")
                if not topic:
                    # A named endpoint in the protocol is also sufficient to locate a result.
                    clean=title.strip().rstrip("。")
                    if len(clean)>=4 and any(clean in v["text"] for v in methods.get("9.5.1",[])):
                        topic=next(t for t in TABLE_TOPICS if t[0]=="efficacy")
                    else:
                        unmatched.append({"source":src,"table":sheet,"caption":layout.title,"population":layout.population,
                                          "columns":layout.columns,"row_labels":[v[0] for _,v in values]})
                        raise ConfigurationError("表题需智能体结合方案和 SAP 判读")
                key,_,sid,annex=topic
                if key=="efficacy" and "11.6.1.1" in titles:
                    sid="11.6.1.1" if not re.search(r"次要|secondary",title,re.I) else "11.6.1"
                elif key=="efficacy" and "11.4.1.1" in titles and not re.search(r"次要|secondary",title,re.I):
                    sid="11.4.1.1"
                elif key=="center" and "11.6.2.4" in titles:
                    sid="11.6.2.4"
                if key=="sets" and sid not in titles and "10.3" in titles:
                    sid="10.3"
                annex = {"flow":"14.1.1", "deviation":"14.1.2", "baseline":"14.1.3"}.get(key,annex) if {"14.1.1","14.1.2","14.1.3"} <= set(titles) else annex
                if sid not in titles or annex not in titles:
                    raise ConfigurationError("该研究目录未启用匹配的小节")
                pop=POP_ALIAS.get(layout.population.upper(),layout.population.upper())
                if ((key in {"safety","serious","ae_terms","consequence","exposure","vitals","lab"} and pop in {"FAS","ITT","MITT","PPS"})
                    or (key in {"efficacy","missing_visits"} and pop in {"SAF","SS"})):
                    raise ConfigurationError("表内分析人群与结果用途需要统计确认")
                groups,populations=contract
                prescribed=populations.get("efficacy" if key in {"efficacy","missing_visits"} else "safety" if key in {"safety","serious","ae_terms","consequence","exposure","vitals","lab"} else "")
                if prescribed and prescribed!={pop}:
                    raise ConfigurationError("表内分析人群与 SAP 规定的人群不一致")
                if groups:
                    columns={re.sub(r"\s*[（(][Nn]\s*=.*?[)）]\s*$","",v).strip() for v in layout.columns[1:]}
                    summary=re.compile(r"(?:合计|总计|Total|Overall|差值|率差|估计值|[0-9.]+%\s*(?:CI|置信区间)|置信区间|p值|p.value|estimate|difference)(?:[（(].*[）)])?",re.I)
                    actual={v for v in columns if not summary.fullmatch(v)}
                    if len(groups)!=1 or (actual and actual!=set(next(iter(groups)))):
                        raise ConfigurationError("表内治疗组与 SAP 声明的组别不一致")
                job=dict(kind="table",layout="source",source=src,sheet=sheet,study=study,
                         title=layout.title,columns=layout.columns,population=layout.population)
                tables.append((layout,job,values,key,sid,annex,match))
            except (ValueError,KeyError,IndexError,ConfigurationError) as exc:
                issues.append(f"统计表 {src} / {sheet} 暂未采用。{exc}")
    # Identical source copies are harmless; competing releases of a table are a decision.
    if set(matches)-seen_matches:
        issues.append("智能体匹配答复引用了未通过原件校验的表："+"、".join(f"{s}/{t}" for s,t in sorted(set(matches)-seen_matches)))
    def model_order(entry):
        job=entry[1]
        if inventory[job["source"]].get("origin")=="computed_from_subjects" and entry[3]=="efficacy":
            for rank,role in enumerate(("主要分析","支持性分析","敏感性分析"),1):
                if role in job["title"]:
                    return rank
            return 4
        return 0
    model_slots=[i for i,entry in enumerate(tables) if model_order(entry)]
    ordered=sorted((tables[i] for i in model_slots),key=model_order)
    for index,entry in zip(model_slots,ordered):
        tables[index]=entry
    by_id=defaultdict(list)
    for entry in tables:
        title=entry[0].title
        number=re.search(r"\d+(?:[.\-–－]\d+)*",title)[0]
        by_id[number].append(entry)
    for entries in by_id.values():
        if len({(inventory[e[1]["source"]]["sha256"],e[1]["sheet"]) for e in entries})>1:
            issues.append("同一表号有多个定稿候选，须决定采用版本："+"、".join(e[1]["source"]+"/"+e[1]["sheet"] for e in entries))
            continue
        layout,job,values,key,sid,annex,match=entries[0]
        def selected(rows):
            return [{"row":i,"label":v[0]} for i,v in rows if all(v)]
        if key=="efficacy":
            primary_row=re.compile(r"变化|改变|缓解|应答|发生率|均值差|得分|change|respond|difference|score",re.I)
            ranked=sorted(values,key=lambda item:not bool(primary_row.search(item[1][0])))
        else:
            ranked=values
        highlights=selected(ranked[:3]) if key!="ae_terms" else []
        if match and match["row_labels"]:
            selected_by_label={v[0]:(i,v) for i,v in values}
            highlights=selected([selected_by_label[k] for k in match["row_labels"]])
            if len(highlights)!=len(match["row_labels"]):
                raise ConfigurationError("智能体选择的摘要行含空单元格")
        jobs[sid].append({**job,"highlights":highlights})
        jobs[annex].append({**job,"highlights":[]})
        for detail,rx in DETAIL_ROWS.get(key,[]):
            matches=selected([(i,v) for i,v in values if re.search(rx,v[0],re.I)])
            if detail in titles and detail!=sid and matches:
                jobs[detail].append({**job,"highlights":matches})

# Route administrative statements and existing clinical judgments to their own sections.
DOCUMENT_SECTIONS={
    "ethics":[("5.3",r"签署|知情同意(?:书|过程)|informed consent"),("5.2",r"遵循|GCP|核验|伦理行为"),("5.1",r"批准|批件|伦理委员会|IRB|IEC")],
    "medical":[("13",r"总体|获益.?风险|总评|局限|后续研究|benefit.risk"),("12.3.1.1",r"全因死亡|生存状态核查"),("12.3.1.3",r"其他重要不良事件"),("12.3.3",r"住院风险|个例分析|严重事件分析"),("12.6",r"安全性|胃肠道|风险|耐受性"),("11.4.7",r"疗效|应答|组间差异|效果|症状改善")],
}
CASE = re.compile(r"^(?:受试者\s*|病例\s*)?[A-Za-z]{1,8}[-_]?\d{2,}(?:[（(，,\s：:]|$)",re.I)
ADMIN = {"investigators":"6","amendment":"9.8","references":"15","operations":"1"}
LISTING_DEST={"16.2.6":["11.4.3","11.4.6"],"16.2.7":["12.2.4"],"16.2.8":["12.4.1","12.4.2.2"]}
APPROVED_STATUS = {"已提供","不适用","待签署","待决定"}
# Use project statements on scope when available, including decisions explicitly documented in the SAP.
METHOD_SCOPE={
    "9.5.3":r"主要(?:疗效)?(?:终点|变量|指标)|primary endpoint",
    "9.5.4":r"药物浓度|浓度测定|药代动力学|pharmacokinetic",
    "11.1":r"全分析集|安全性分析集|按随机分组纳入|分析集定义|analysis populations?",
    "11.4.2.1":r"协变量|调整基线|基线与访视交互|covariate","11.4.2.2":r"缺失|缺少疗效|missing data|多重插补|基于MAR使用可用访视",
    "11.4.2.3":r"期中分析|中期分析|interim analys","11.4.2.4":r"治疗随机斜率|单中心|中心效应|多中心分析|随机中心截距|中心随机截距",
    "11.4.2.5":r"多重性|多重比较|multiplicity","11.4.2.6":r"疗效子集|疗效子人群",
    "11.4.2.7":r"等效性|非劣效","11.4.2.8":r"亚组|subgroup",
    "11.4.4":r"剂量.?反应|剂量.?效应|暴露.?效应|浓度.?效应",
    "11.4.5":r"药物.{0,4}相互作用|药物.疾病|drug interaction",
}


def prepare_plan(plan,inventory,records,out,titles,facts,issues,matches=None):
    selected,_=choose_sources(inventory,plan["selected_sources"])
    anchor,problem=identity(selected,records)
    study=facts["protocol_id"] if facts else anchor["value"] if anchor else None
    identity_conflict=(anchor and facts and anchor["value"].upper()!=study.upper()) or (problem and "冲突" in problem)
    if identity_conflict:
        issues.append("研究身份冲突，自动证据绑定暂缓。请核对方案、SAP 和研究事实来源。")
        return
    if not facts and not anchor:
        if any(r["role"]=="tfl" for r in inventory.values()):
            issues.append(problem)
        return
    if anchor:
        plan["study_identity"]=anchor
    methods=method_candidates(selected,records)
    profile={"identity":anchor or {"value":study,"source":plan["study_facts"]},
             "structure":"已登记的项目事实" if facts else "ICH E3 通用目录",
             "protocol":selected.get("protocol"),"sap":selected.get("sap"),"fields":{}}
    for sid,name in [("8","研究目的"),("9.1","研究设计"),("9.3.1","入选人群"),("9.4.1","研究干预"),
                     ("9.5.1","评价指标"),("9.5.8","安全性终点"),("9.5.1.1","访视安排"),("9.7.1","统计方法"),("9.7.2","样本量")]:
        if sid in methods:
            profile["fields"][name]=[{k:r[k] for k in ("source","location","text")} for r in methods[sid]]
    atomic_write_json(out/"研究概况与来源.json",profile)
    protocol=selected.get("protocol")
    if protocol:
        plan["simulation"]=any(re.search(r"仅(?:供|用于).{0,10}演练|模拟研究资料|虚构研究|synthetic (?:study|data)",r["text"],re.I)
                               for r in records.get(protocol,[])[:10] if eligible_record(r))
    jobs=defaultdict(list)
    by_section=defaultdict(list)
    grouped_register=defaultdict(list)
    present=set()
    cover_text=set()
    for src,entry in inventory.items():
        role=entry["role"]
        if entry.get("case_locations"):
            for sid in ("12.3.2","14.3.3"):
                if sid in titles:
                    jobs[sid].append({"kind":"subject_case","source":src,"locations":entry["case_locations"]})
        if (entry["sha256"],role) in present:
            continue
        present.add((entry["sha256"],role))
        text_rows=[r for r in records.get(src,[]) if eligible_record(r)]
        doc_ids={m[1].upper() for r in text_rows for m in LABELED_ID.finditer(r["text"])}
        if doc_ids and doc_ids!={study.upper()}:
            if role not in {"protocol","sap","tfl"}:
                issues.append(f"文件 {src} 标示其他或多个研究编号，已排除。")
            continue
        if role=="figure" and entry["format"]==".png":
            visit=bool(re.search(r"访视|visit|schedule of assessments",src,re.I))
            sid=("9.5.1.1" if "9.5.1.1" in titles else "9.5.1") if visit else "10.1"
            jobs[sid].append({"kind":"figure","source":src})
        for row in text_rows:
            value=row["text"].strip()
            loc=row["location"]
            if role=="register":
                parts=value.split("\t")
                if len(parts)==5 and parts[2] in APPROVED_STATUS:
                    grouped_register[parts[0]].append((parts,src,loc))
                continue
            if not value or value.startswith("#") or value in {"医学审阅意见","医学总结","病例叙述"} or str(row.get("paragraph_style","")).lower().startswith(("heading","title")):
                continue
            targets=[]
            if role=="operations":
                if TITLE.search(value) or study.upper() in value.upper() or re.search(r"申办|研究名称|首例|末例|锁库|数据库|报告日期|研究。?$",value):
                    targets=["1"]
                if re.search(r"依从性|日记卡与发药",value):
                    targets.append("11.3")
            elif role=="deviations" and re.search(r"偏离|漏访|超窗",value):
                targets=["10.2"]
            elif role in ADMIN:
                targets=[ADMIN[role]]
            elif role=="medical" and CASE.search(value):
                targets=["12.3.2","14.3.3"]
            elif role in DOCUMENT_SECTIONS:
                targets=[sid for sid,rx in DOCUMENT_SECTIONS[role] if re.search(rx,value,re.I)][:1]
                if role=="medical" and targets==["12.6"] and "12.7" in titles and "安全性结论" in titles["12.7"]:
                    targets=["12.7"]
                if role=="medical" and targets==["11.4.7"] and "11.6.7" in titles:
                    targets=["11.6.7"]
            if src in selected.values():
                if src==protocol and (TITLE.search(value) or (study.upper() in value.upper() and len(value)<150)):
                    targets+=["1"]
                if len(value)>10:
                    targets.extend(sid for sid,rx in METHOD_SCOPE.items() if re.search(rx,value,re.I))
            for sid in dict.fromkeys(targets):
                if sid in titles:
                    if sid=="1":
                        if value in cover_text:
                            continue
                        cover_text.add(value)
                    kind="medical" if role=="medical" else "method" if src in selected.values() else "record"
                    by_section[sid,kind,src].append(loc)
    for (sid,kind,src),locations in by_section.items():
        jobs[sid].append(dict(kind=kind,source=src,locations=locations))
    if jobs.get("1"):
        jobs["1"].sort(key=lambda j:j.get("source")!=protocol)
    for code, candidates in grouped_register.items():
        if len(candidates)!=1:
            issues.append(f"附录登记编号 {code} 有多项记录，须核对采用行。")
            continue
        parts,src,loc=candidates[0]
        if code in titles and code.startswith("16."):
            jobs[code].append(dict(kind="attachment",source=src,location=loc))
        owners=LISTING_DEST.get(code,[])
        if re.search(r"重要事件清单|住院病例清单|严重事件清单|SAE listing",parts[1],re.I):
            owners=["14.3.2"]
        if re.search(r"异常实验室清单|异常化验清单|abnormal lab",parts[1],re.I):
            owners=["14.3.4","12.4.2.2"]
        for owner in owners:
            if owner in titles:
                job=dict(kind="listing",source=src,location=loc)
                if owner in {"14.3.2","14.3.4"} and parts[3] in inventory and parts[3].lower().endswith(".csv"):
                    fields=inventory[parts[3]].get("columns",[])
                    display=[f for f in fields if f in {"受试者","治疗组","首选术语","级别","结局","基线ALT","末次ALT","单位","临床重要"}]
                    if len(display)>=3 and "受试者" in display:
                        job["display_fields"]=display
                jobs[owner].append(job)
    bind_unregistered_attachments(jobs,titles,inventory,selected,records,study,grouped_register)
    if selected.get("sap") and any(e.get("origin")=="computed_from_subjects" for e in inventory.values()):
        sap_source=selected["sap"]
        active_methods=inventory[sap_source].get("analysis_rule_locations",[])
        if active_methods and "9.7.1" in titles:
            raw_method=[r["location"] for r in methods.get("9.7.1",[]) if r["source"]==sap_source]
            jobs["9.7.1"].append({"kind":"method","source":sap_source,"locations":list(dict.fromkeys(raw_method+active_methods))})
    unmatched=[]
    if "sap" in selected or not any(e["role"]=="sap" for e in inventory.values()):
        contract=analysis_contract(records.get(selected.get("sap"),[]))
        parsed=inventory.get(selected.get("sap"),{}).get("analysis_arms")
        if parsed:
            contract[0].add(tuple(parsed))
        table_jobs(inventory,out,study,titles,jobs,issues,methods,plan["result_setup"],contract,matches or {},unmatched)
    structure_signals = []
    if not facts:
        signal = re.compile(r"实体瘤|客观缓解率|剂量递增(?:研究|设计|队列)|(?:主要研究目的|主要终点).{0,16}(?:药代动力学|生物等效性)|非劣效(?:性)?(?:检验|试验设计)|耐受剂量|dose.escalation study|objective response rate", re.I)
        for sid in ("8","9.1","9.5.1"):
            for fragment in methods.get(sid,[]):
                if signal.search(fragment["text"]):
                    structure_signals.append({k:fragment[k] for k in ("source","location","text")})
    if unmatched or structure_signals:
        task={"source_snapshot":plan["source_snapshot"],"selected_sources":selected,
              "topics":{t[0]:{"body":t[2],"annex":t[3]} for t in TABLE_TOPICS},"unmatched":unmatched}
        if structure_signals:
            task["review_project_structure"]=structure_signals
            issues.append("方案存在专项目的或终点，智能体需按原件核对项目扩展小节；本稿当前采用通用目录。")
        atomic_write_json(out/"待智能体匹配.json",task)
    priorities=["11.6.1.1","11.4.1.1","11.4.1","12.2.1"]
    summary=[]
    for sid in ["1","8","9.1","9.7.1","10.1",*priorities]:
        if sid in methods or any(j["kind"] in {"record","method"} or j.get("highlights") for j in jobs.get(sid,[])):
            summary.append(sid)
    for sid in priorities:
        if any(j.get("highlights") for j in jobs.get(sid,[])):
            jobs["13"].insert(0,dict(kind="body_copy",section=sid,paragraph=1))
    if jobs.get("13") and not any(j["kind"]=="medical" for j in jobs["13"]):
        issues.append("第13章已汇入事实结果，综合获益风险与局限需医学负责人判断。")
    # Include the group contrast and severe events in the synopsis when computed in this run.
    for sid in priorities:
        start=0
        extra=[]
        for job in jobs.get(sid,[]):
            if job.get("kind")!="table" or inventory[job["source"]].get("origin")!="computed_from_subjects":
                continue
            labels=job.get("highlights",[])
            extra.extend(start+i for i,r in enumerate(labels,1) if "（百分点）" in r["label"] or "变化量组间差" in r["label"])
            start+=len(labels)
        if extra and sid in summary:
            summary[summary.index(sid)]={"section":sid,"paragraphs":sorted(set([1,2,*extra]) & set(range(1,start+1)))}
            jobs["13"].extend({"kind":"body_copy","section":sid,"paragraph":i} for i in extra if i!=1)
    serious="12.3.1.2"
    if any(j.get("kind")=="table" and inventory[j["source"]].get("origin")=="computed_from_subjects"
           for j in jobs.get(serious,[])):
        summary.append(serious)
    for sid in priorities:
        if not any(j.get("kind")=="table" and inventory[j["source"]].get("origin")=="computed_from_subjects"
                   for j in jobs.get(sid,[])):
            continue
        safety = sid=="12.2.1"
        target = next((s for s in (("12.7","12.6") if safety else ("11.6.7","11.4.7"))
                       if s in titles and "结论" in titles[s]),None)
        if target and not jobs.get(target):
            jobs[target]=[dict(j) for j in jobs["13"] if j.get("section")==sid]
            if safety and serious in summary:
                jobs[target].append({"kind":"body_copy","section":serious,"paragraph":1})
    plan["working_synopsis"]=summary
    plan["evidence_sections"]=dict(jobs)


def select_effective_sources(inventory,records):
    """Only resolve versions when every other distinct version explicitly identifies itself as historical."""
    choices={}
    for role in ("protocol","sap"):
        candidates=[p for p,r in inventory.items() if r["role"]==role]
        if len({inventory[c]["sha256"] for c in candidates})<=1:
            continue
        effective=[]
        unresolved=[]
        for src in candidates:
            front="\n".join(r["text"].strip() for r in records.get(src,[])[:12] if eligible_record(r))
            statuses=re.findall(r"^(?:文件状态|版本状态|document status)\s*[:：]\s*(.+)$",front,re.M|re.I)
            if len(set(statuses))!=1:
                unresolved.append(src)
            elif statuses[0].casefold() in {"现行有效","已批准且生效","effective","current approved"}:
                effective.append(src)
            elif statuses[0].casefold() not in {"已作废","已废止","已被替代","superseded","withdrawn","草稿","draft"}:
                unresolved.append(src)
        if not unresolved and effective and len({inventory[c]["sha256"] for c in effective})==1:
            choices[role]=effective[0]
    return choices


def human_items(rows,issues):
    decisions=[]
    technical=[]
    def add(action,sections):
        decisions.append({"action":action,"sections":sections})
    medical=[r["section"] for r in rows if r["status"]=="medical_review" or r["needs_human_paragraphs"]]
    if medical:
        add("医学负责人核实病例、因果评价和研究结论，并确认对应正文和附表。",medical)
    if any(r["status"]=="working_synopsis" for r in rows):
        add("正文确定后，由医学及统计负责人确认概要与正文一致。",["2"])
    for msg in dict.fromkeys(issues):
        if "复核转录的医学意见" in msg or "概要须在正文确认后" in msg:
            continue
        if re.search(r"待签署|待决定|多个定稿|采用版本|研究编号冲突|研究身份冲突|研究编号缺失或不唯一|分析人群与结果用途|治疗组与|SAP 规定的人群|源文件存在不同|须核对采用行|综合获益风险与局限需",msg):
            sid=msg.split(" ",1)[0]
            add(msg,[sid] if re.fullmatch(r"\d+(?:\.\d+)*",sid) else [])
        else:
            technical.append(msg)
    return decisions,technical


def bind_unregistered_attachments(jobs,titles,inventory,selected,records,study,registered):
    """An absent filing register need not prevent a reference to an actual local original."""
    roles={"protocol":"16.1.1","amendment":"16.1.1","sap":"16.1.9","ethics":"16.1.3",
           "investigators":"16.1.4","references":"16.1.12","randomization":"16.1.7","deviations":"16.2.2"}
    for src,entry in inventory.items():
        role=entry["role"]
        if role in {"protocol","sap"} and src!=selected.get(role):
            continue
        other_ids={m[1].upper() for r in records.get(src,[]) for m in LABELED_ID.finditer(r["text"])}
        if other_ids and other_ids!={study.upper()}:
            continue
        owners=[]
        if role in roles:
            owners.append(roles[role])
        columns=set(entry.get("columns",[]))
        if columns.intersection({"受试者","受试者编号","USUBJID"}):
            for sid,pattern in [("16.2.7",r"首选术语|AETERM|AEDECOD"),("16.2.8",r"基线ALT|LBTESTCD"),
                                ("16.2.6",r"最佳疗效|第\d+周分钟|疗效结果|QSSTRESN"),("16.2.5",r"实服片数|实际完成天数"),
                                ("16.2.4",r"年龄|AGE")]:
                if any(re.fullmatch(pattern,c,re.I) for c in columns):
                    owners.append(sid)
        for sid in dict.fromkeys(owners):
            if sid not in titles:
                continue
            if sid in registered:
                rows=registered[sid]
                # A provided protocol or ethics letter may have separately filed companion documents.
                if len(rows)!=1 or rows[0][0][2]!="已提供" or rows[0][0][3]==src:
                    continue
                if not (sid=="16.1.1" and role=="amendment" or sid=="16.1.3" and role=="ethics"
                        and re.search(r"知情同意书样本|参与须知与同意样本|informed consent form",
                                      src+" "+" ".join(r["text"] for r in records.get(src,[])[:2]),re.I)):
                    continue
            jobs[sid].append(dict(kind="original_file",source=src))
            for owner in LISTING_DEST.get(sid,[]):
                if owner in titles:
                    jobs[owner].append(dict(kind="original_file",source=src))


def apply_routing_reply(reply, plan, inventory, records):
    """Only admit study facts tied to actual text from the selected protocol and SAP."""
    if reply is None:
        return
    if not isinstance(reply,dict) or set(reply)!={"value","basis"} or not isinstance(reply["basis"],list):
        raise ConfigurationError("研究事实答复需包括项目事实和逐类原文定位")
    from .study_facts import validate_study_facts
    facts=validate_study_facts(reply["value"])
    selected,_=choose_sources(inventory,plan["selected_sources"])
    anchor,_=identity(selected,records)
    if not anchor or facts["protocol_id"].casefold()!=anchor["value"].casefold() or plan.get("study_facts"):
        raise ConfigurationError("研究事实与所选方案身份冲突，或已有项目事实记录")
    locations=[]
    seen=set()
    required={"design","objectives","population","data_domains","analyses"}
    for item in reply["basis"]:
        if not isinstance(item,dict) or set(item)!={"field","source","location","quote"}:
            raise ConfigurationError("研究事实依据须有字段、文件、定位和引文")
        src,loc=item["source"],item["location"]
        if not all(isinstance(item[k],str) and item[k].strip() for k in item) or src not in selected.values() or item["field"] not in required:
            raise ConfigurationError("研究事实依据超出当前方案、SAP 或路由范围")
        candidates=[r["text"] for r in records.get(src,[]) if eligible_record(r) and r["location"]==loc]
        if len(candidates)!=1 or item["quote"] not in candidates[0]:
            raise ConfigurationError("研究事实引文与原件位置不一致")
        seen.add(item["field"])
        locations.append({"source":src,"location":loc,"field":item["field"],"quote":item["quote"]})
    if seen!=required:
        raise ConfigurationError("研究事实缺少设计、目标、人群、数据域或分析依据")
    plan["study_facts"]="@source_supported_study_facts"
    plan["prepared_records"][plan["study_facts"]]={"value":facts,"basis":locations}


def load_matches(path,fingerprint):
    answer=load_json_unique(path)
    if not isinstance(answer,dict) or not {"source_snapshot","tables"} <= answer.keys() or set(answer)-{"source_snapshot","tables","study_facts"} or answer["source_snapshot"]!=fingerprint:
        raise ConfigurationError("智能体匹配答复缺少原始资料快照或字段不符")
    if not isinstance(answer["tables"],list):
        raise ConfigurationError("智能体匹配答复需列出统计表")
    allowed={t[0] for t in TABLE_TOPICS}
    matches={}
    for item in answer["tables"]:
        if not isinstance(item,dict) or set(item)!={"source","table","topic","row_labels"}:
            raise ConfigurationError("匹配答复只接受原表位置、统计主题和源行标签")
        if not all(isinstance(item[k],str) and item[k] for k in ("source","table","topic")):
            raise ConfigurationError("统计原件和主题须为非空文本")
        key=(item["source"],item["table"])
        labels=item["row_labels"]
        if key in matches or item["topic"] not in allowed or not isinstance(labels,list) or not all(isinstance(x,str) and x for x in labels) or len(set(labels))!=len(labels):
            raise ConfigurationError("匹配答复存在重复候选、未知主题或无效标签")
        matches[key]=item
    return matches,answer.get("study_facts")
