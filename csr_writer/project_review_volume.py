"""Compose reviewed passages from the selected evidence and record them across Word sections."""
from collections import Counter
import csv
import hashlib

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from .io_utils import atomic_write_json, load_json_unique, sha256_file
from .m4_assembly import validate_docx_references
from .project_skeleton import _bookmark_key, _find_heading_paragraphs, _insert_after


def finish_review(out,report,stats,review,root):
    path=out/"CSR内部审阅初稿.docx"
    doc=Document(path)
    headings=_find_heading_paragraphs(doc)
    sections={r["section"]:r for r in report["sections"]}
    qc=load_json_unique(out/"统计分析/独立复算.json")
    ledger=load_json_unique(out/"统计分析/逐表对账.json")
    selected={v["number"]:v for v in ledger if v.get("selected")}
    tracefile=out/"working-synopsis-trace.json"
    trace=load_json_unique(tracefile) if tracefile.exists() else []
    updates=[]
    summary_additions=[]
    def direct(sid):
        node=headings[_bookmark_key(sid)]._p.getnext()
        while node is not None:
            if node.tag==qn("w:p"):
                if any(n.get(qn("w:name"),"").startswith("_Ref_Sec_") for n in node.findall(qn("w:bookmarkStart"))):
                    break
                yield Paragraph(node,doc)
            node=node.getnext()
    def add(sid,text,ref,*,summary=False):
        if sid not in sections:
            return
        row=sections[sid]
        was_pending=row["status"]=="pending"
        if was_pending:
            for p in list(direct(sid)):
                if p.text.startswith("【待补】"):
                    p._p.getparent().remove(p._p)
            row.update(status="sourced_body",action=None)
        _insert_after(headings[_bookmark_key(sid)],[doc.add_paragraph(text)])
        if ref not in row["sources"]:
            row["sources"].append(ref)
        updates.append(dict(section=sid,text=text,source=ref))
        if summary and "2" in sections and sections["2"]["status"]=="working_synopsis":
            summary_additions.append((sid,text))
            h=hashlib.sha256(text.encode()).hexdigest()
            trace.append(dict(section=sid,text=text,sha256=h))
            sections["2"]["sources"].append(dict(source="本轮正文",location=sid,sha256=h))

    # Work from the adopted display values; an excluded candidate may never populate the narrative.
    def values(key):
        from .project_table_match import data_rows,find_layout
        item=selected[key]["selected"]; src=item["source"];sheet=item["sheet"]
        meta=report.get("derived_sources",{}).get(src) or report["sources"][src]
        cells=load_json_unique(out/meta["extraction"])["sheets"][sheet]["rows"]
        lay=find_layout(cells,computed=src in report.get("derived_sources",{}))
        rows,_=data_rows(cells,lay)
        return lay.columns[1:],dict((vs[0],vs[1:]) for _,vs in rows),dict(source=src,location=sheet)
    def remove_text(sid,predicate):
        if sid in sections:
            for para in list(direct(sid)):
                if predicate(para.text) and not para._p.xpath(".//w:bookmarkStart|.//w:instrText"):
                    para._p.getparent().remove(para._p)

    primary=None
    for key,row in selected.items():
        if "主要终点主要分析" in row["title"]:
            cols,rows,ref=values(key)
            for label,v in rows.items():
                if "变化量组间差" in label and len(v)==3:
                    primary=label+"为 "+v[0]+" 分（"+cols[1]+" "+v[1]+" 分，P "+(v[2] if v[2].startswith("<") else "= "+v[2])+"）。"
                    if key=="3.2" and qc.get("ancova"):
                        primary+=f"末访有可用测量并进入模型的受试者共 {qc['ancova']['n']} 人。"
                    for sid in ("11.4.7","13"):
                        remove_text(sid,lambda txt:txt.startswith(label+"，"))
                    add("11.4.7",primary,ref)
                    add("13",primary,ref)
                    break
    sensitivity=[]
    for key,row in selected.items():
        if "主要终点敏感性分析" in row["title"]:
            _,rr,ref=values(key)
            for label,v in rr.items():
                if "变化量组间差" in label and len(v)==3:
                    sensitivity.append((float(v[0]),label,ref))
    if primary and sensitivity:
        for sid in ("11.4.7","13"):
            for _,label,_ in sensitivity:
                remove_text(sid,lambda txt:txt.startswith(label+"，"))
            lo=min(v[0] for v in sensitivity);hi=max(v[0] for v in sensitivity)
            msg=f"预设敏感性分析的同一评分变化组间差为 {lo:.3f} 至 {hi:.3f} 分。各情景的区间见疗效附表。"
            add(sid,msg,{"source":"统计分析/逐表对账.json","location":[v[2] for v in sensitivity]})
    rules=stats["sap_rules"]["rules"].get("efficacy",{})
    if "ancova" in rules and "3.2" in selected:
        spec=rules["ancova"]; loc=dict(source=stats["sap_rules"]["source"],location=spec["location"])
        if ("covariates" not in rules and qc.get("ancova") and "主要"==spec["parameters"]["ancova_role"]):
            add("11.4.2.1","主要分析采用协方差分析，以随机治疗组和基线评分为自变量，估计末访评分变化量的调整后组间差。",loc)
            n=qc["ancova"]["n"];total=sum(p["randomized"] for p in stats["derivations"]["subjects"].values())
            add("11.4.2.2",f"主要分析采用观察病例。{total} 名随机受试者中，{n} 名有合格的基线及末访评分，余下 {total-n} 名未进入该模型。",loc)
    if "1.1" in selected:
        cols,rr,ref=values("1.1")
        if "已随机人数" in rr:
            counts=rr["已随机人数"]
            actual="实际随机分配为"+"、".join(f"{g} {n} 人" for g,n in zip(cols,counts))+"。"
            for sid in ("9.1","9.4.3","9.7.2"):
                if sections.get(sid,{}).get("status")=="source_excerpt":
                    add(sid,actual,ref)
                    sections[sid].update(status="sourced_body",action=None)
    if "3.2" in selected and qc.get("ancova"):
        role=rules["ancova"]["parameters"]["ancova_role"]
        n=qc["ancova"]["n"]
        _,_,ref=values("3.2")
        if sections.get("9.5.1",{}).get("status")=="source_excerpt":
            add("9.5.1",f"在{role}分析中，共有 {n} 人的基线与末访评分用于估计调整后的治疗组间差。",ref)
            sections["9.5.1"].update(status="sourced_body",action=None)
    # A subject-level deviation register takes precedence over repeating each event in the narrative.
    candidates=[(s,r) for s,r in report["sources"].items() if "DVTERM" in r.get("columns",[]) and "USUBJID" in r.get("columns",[]) and r["format"] in {".csv",".tsv"}]
    if len(candidates)==1 and "10.2" in sections:
        src,meta=candidates[0]
        with (root/src).open(encoding="utf-8-sig",newline="") as f:
            dv=list(csv.DictReader(f,delimiter="\t" if meta["format"]==".tsv" else ","))
        if dv and all(r.get("STUDYID")==stats["study"] and r.get("USUBJID") and r.get("DVTERM") for r in dv):
            ref=dict(source=src,location=f"记录 1–{len(dv)}")
            persons={r["USUBJID"] for r in dv}
            for p in list(direct("10.2")):
                if not p._p.xpath(".//w:bookmarkStart|.//w:instrText") and any(p.text.startswith(s+" ") for s in persons):
                    p._p.getparent().remove(p._p)
            counts=Counter(r["DVTERM"] for r in dv)
            msg=f"共有 {len(persons)} 名受试者记录了 {len(dv)} 次方案偏离，其中"+"、".join(f"{k} {v} 次" for k,v in counts.items())+"。逐例日期与偏离类别见附录。"
            if all(r.get("DVIMPACT","").strip() for r in dv):
                impacts=Counter(r["DVIMPACT"].strip() for r in dv)
                msg+="盲态核查记录的分析影响为"+"、".join(f"“{k}”{v} 次" for k,v in impacts.items())+"。"
            add("10.2",msg,ref)
            add("16.2.2","偏离逐例记录："+"；".join(" ".join([r["USUBJID"],r.get("DVDTC",""),r["DVTERM"]]) for r in sorted(dv,key=lambda r:r["USUBJID"]))+"。",ref)
    for item in ledger:
        if item.get("number")=="2.1" and not item["selected"]:
            add("12.1","暴露表仍有待核对的结果，本稿暂缓列入总暴露人日。",dict(source="统计分析/逐表对账.json",location="2.1"),summary=True)
            if "12.1" in sections:
                sections["12.1"].update(status="decision_required",action="确认两份暴露结果的差异")
    if "2.1" in selected and any(i["id"]=="TFL-2.1" and i["status"]=="answered" for i in review.items):
        cols,rows,ref=values("2.1")
        statement="按实际给药日期，各组总暴露为"+"、".join(f"{a} {v} 人日" for a,v in zip(cols,rows["总用药人日"]))+"。"
        add("12.1",statement,ref,summary=True)
        add("14.3",statement,ref)

    serious=[r for r in stats["derivations"].get("events",[]) if r.get("serious")]
    for event in serious:
        key=f"{event['id']}/{event['event_id']}"
        review.ask("COURSE-"+key.replace("/","-"),"医学负责人","请核实严重事件 "+key+" 的已记录病程、处置和转归。",
                   ["12.3.2","14.3.3","16.3.1"],{"event":{k:event[k] for k in ("id","event_id","source","pt","trigger")},
                   "case":[r for r in stats.get("case_records",[]) if r["id"]==event["id"] and r["seq"]==event["event_id"]]},["核实原记录"])
    outputs=[(r["number"],r["title"],values(r["number"])[1]) for r in selected.values()]
    if primary:
        ref=next((r for k,t,r in outputs if "主要终点主要分析" in t),{})
    else:
        ref=[(k,r) for k,t,r in outputs if "终点" in t]
    alternatives=[(i["id"],i["basis"],i["choices"]) for i in review.items if i["id"].startswith(("AE-","TFL-","COURSE-"))]
    safety_basis=[(k,t,{label:v for label,v in rr.items() if "相关" not in label}) for k,t,rr in outputs if k.startswith(("4.","5."))]
    clinical_sections=[sid for sid in ("12.3.1.1","12.3.1.3","12.3.3","12.6","12.7") if sections.get(sid,{}).get("status")=="medical_review"]
    medical_sources={sid:[{"source":e["source"],"location":e["location"],"hash":report["sources"][e["source"]]["sha256"]}
                           for e in sections[sid]["sources"] if e["source"] in report["sources"]] for sid in clinical_sections}
    review.ask("MED-CONCLUSION","医学负责人","请核实所列小节涉及的死亡、其他重要事件及安全性意见，再给出疗效的临床解释、总体获益风险与局限。",
               ["11.4.7","13","2","14.2",*clinical_sections],{"medical_sources":medical_sources,"primary":ref,"sap":stats["sources"].get(stats["sap_rules"]["source"]),
               "safety":safety_basis,"dependent_choices":alternatives,
               "events_source":stats.get("sources",{}).get(stats.get("domains",{}).get("events",{}).get("source")),
               "serious":[(r["id"],r["pt"],r["serious"]) for r in serious]},["采用所附医学意见"])
    med=review.items[-1]
    if med["status"]=="answered" and alternatives:
        conditions=review.input[med["id"]].get("applies_if",{})
        for key,_,_ in alternatives:
            prerequisite=next(i for i in review.items if i["id"]==key)
            if prerequisite["status"]!="answered" or conditions.get(key)!=prerequisite.get("answer",{}).get("choice"):
                med["status"]="deferred"
        med["answer"]["applies_if"]=conditions
    for i in review.items:
        if i["status"]!="answered" or i["id"].startswith("TFL-"):
            continue
        a=i["answer"]; mark="演练医学答复" if review.mode=="simulation" else "医学复核意见"
        if i["id"].startswith("AE-"):
            ev=i["evidence"]["event"]
            msg=f"{mark}（{a['date']}）：{ev['id']}/{ev['seq']} {ev['pt']}，原事件页记录的相关性为{ev['related']}，复核判定为{a['choice']}。"+a["statement"]
            dest=["12.3.2","12.2.3","12.3.3","13","14.3.3","16.3.1"]
        elif i["id"].startswith("COURSE-"):
            msg=f"{mark}（{a['date']}）："+a["statement"]
            dest=["12.3.2","14.3.3","16.3.1"]
        else:
            msg=f"{mark}（{a['date']}）："+a["statement"]
            dest=["13","11.4.7","14.2"]
        if i["id"].startswith("COURSE-") and all(r["status"]=="answered" for r in review.items if r["id"].startswith("COURSE-")):
            for sid in ("12.3.2","14.3.3"):
                remove_text(sid,lambda txt:txt=="〔逐例记录原文，待医学负责人复核〕")
        if i["id"].startswith("AE-"):
            for sid in ("12.3.2","12.3.3","14.3.3"):
                for para in direct(sid) if sid in sections else []:
                    if para.text==ev.get("narrative") and para.text and not para._p.xpath(".//w:instrText"):
                        para.text="事件首次报告记录："+para.text
                    elif sid=="12.3.3" and ev["pt"] in para.text and "尚待" in para.text and not para.text.startswith("原医学监查记录"):
                        para.text="原医学监查记录："+para.text
        if i["id"]=="MED-CONCLUSION":
            for sid in ("11.4.7","13",*clinical_sections):
                remove_text(sid,lambda txt:txt in {"〔上述为统计复核稿结果，待医学负责人确认解释〕","〔待医学负责人复核〕"})
                if sid in sections and sections[sid]["status"]=="medical_review":
                    sections[sid].update(status="reviewed_medical",action=None)
                    sections[sid]["sources"].append(dict(source="负责人决定.json",location=i["id"],basis=i["basis"]))
            for sid in ("12.6","12.7"):
                if sid in clinical_sections:
                    add(sid,msg,dict(source="负责人决定.json",location=i["id"],basis=i["basis"]))
        source=dict(source="负责人决定.json",location=i["id"],basis=i["basis"])
        for j,sid in enumerate(dest):
            add(sid,msg,source,summary=j==0 and not i["id"].startswith("COURSE-"))
    case_items=[i for i in review.items if i["id"].startswith(("COURSE-","AE-"))]
    if case_items and all(i["status"]=="answered" for i in case_items):
        for sid in ("12.3.2","14.3.3"):
            if sections.get(sid,{}).get("status")=="medical_review":
                sections[sid].update(status="reviewed_medical",action=None)
    if stats.get("decision_updates") and "4.1" in selected:
        cols,rows,ref=values("4.1")
        vals=rows.get("相关性待定的TEAE n/N（%）")
        if vals:
            msg="本次复核后，仍有相关性待定 TEAE 的受试者为"+"、".join(f"{g} {v}" for g,v in zip(cols,vals))+"。"
            add("12.2.3",msg,ref,summary=True)
            add("14.3.1",msg,ref)
    if summary_additions:
        anchor=list(direct("2"))[-1]
        for group,label in [(("12.1",),"治疗暴露"),(("12.3.2","12.2.3"),"重点安全性结果"),(("13",),"医学结论")]:
            additions=[text for sid,text in summary_additions if sid in group]
            if additions:
                parts=[doc.add_paragraph(label,style="CSR Footer"),*[doc.add_paragraph(text) for text in additions]]
                _insert_after(anchor,parts)
                anchor=parts[-1]
    original=report["human_decisions"]
    stats_prompt=next((i["action"] for i in stats["decisions"] if i["action"].startswith("复核分析集")),"")
    if stats_prompt:
        review.ask("STAT-RELEASE","统计负责人",stats_prompt,["10","11","12","14"],
                   {"tables":outputs,"independent":{k:v["status"] for k,v in qc["tables"].items()},
                    "sources":stats["sources"],"holds":[i["id"] for i in review.items if i["id"].startswith("TFL-") and i["status"]!="answered"]},["确认本版统计结果"])
    for i in review.items:
        if i["id"]=="STAT-RELEASE" and i["status"]=="answered" and (any(not r.get("selected") for r in ledger if r.get("number")) or stats["data_queries"]):
            i["status"]="deferred"
            i["action"]+=" 当前仍有暂缓的计算或未解决差异，本版签认待完成。"
    if trace:
        review.ask("SYNOPSIS","医学及统计负责人","请确认本版概要与正文及已采用结果一致。",["2"],
                   {"text":[r["text"] for r in trace],"decisions":[(i["id"],i["status"]) for i in review.items]},["确认本版概要"])
    for i in review.items:
        if i["id"]=="SYNOPSIS" and i["status"]=="answered":
            if any(v["status"]!="answered" for v in review.items if v["id"]!="SYNOPSIS"):
                i["status"]="deferred"
            else:
                for para in direct("2"):
                    if para.text=="内部审阅概要 · 待医学与统计复核":
                        para.text="内部审阅概要 · "+("演练复核" if review.mode=="simulation" else "本版已复核")
    other=[i for i in original if not (i["action"].startswith(("复核分析集","核定相关性仍","复核严重事件病程","医学负责人核实","正文确定后，由医学及统计负责人","第13章已汇入事实结果","结合疗效、安全性及研究局限")))]
    review.save(out)
    report["human_decisions"]=other+[{k:i[k] for k in ("id","role","action","sections","status")} for i in review.items if i["status"]!="answered"]
    report["decision_summary"]={"answered":sum(i["status"]=="answered" for i in review.items),"outstanding":len(report["human_decisions"]),"mode":review.mode}
    report["source_gaps"]=[r for r in report["source_gaps"] if sections[r["section"]]["status"]=="pending"]
    report["coverage"]=dict(Counter(r["status"] for r in sections.values()))
    if trace:
        atomic_write_json(tracefile,trace)
    atomic_write_json(out/"更新定位.json",updates)
    doc.save(path)
    validate_docx_references(path)
    from .word_result_check import check_word
    display=check_word(path,out,report,ledger,qc)
    atomic_write_json(out/"统计分析/Word单元格复核.json",display)
    for item in display:
        if not item["word_matches_source"]:
            report["source_followup"].append(item["caption"]+" 的 Word 附表与采用表未唯一对应，请核对路由和原件。")
    report["docx_sha256"]=sha256_file(path)
