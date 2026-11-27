"""Produce one internal review volume and a source-bound continuation plan."""
from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from .cross_chapter import apply_review_decisions, derive_synopsis_manifest
from .deterministic_sections import build_deterministic_bundle
from .errors import CSRWriterError, ConfigurationError
from .io_utils import atomic_write_json, atomic_write_text, load_data, sha256_file
from .m4_assembly import validate_docx_references
from .module_router import route_modules
from .paths import DEFAULT_FORMAT_SPEC, DEFAULT_STRUCTURE, DEFAULT_TEMPLATE
from .production_methods import load_production_method_registry
from .project_navigation import prepare_draft_navigation
from .project_evidence import assemble_evidence
from .project_jobs import project_record, result_data, write_card, write_result
from .project_skeleton import (
    DIRECTORY_TITLES, _bookmark_key, _find_heading_paragraphs,
    _insert_after, _section_sort_key, render_project_skeleton,
)
from .project_sources import (
    METHOD_TARGETS, choose_sources, discover, eligible_record, method_candidates,
    read_sources, snapshot_id,
)
from .project_tables import insert_exhibits, result_exhibits
from .protocol_fact_scan import render_protocol_fact_scan, scan_protocol_facts
from .publication_refs import validate_publication_reference_map
from .render_docx import _atomic_save_docx, render_skeleton
from .review_decisions import build_review_decision_queue, render_review_decision_queue
from .shell_mapping import propose_docx_shell_map
from .study_facts import render_fact_card, validate_study_facts
from .structure import flatten_sections, load_chapter_tree


PLAN_DEFAULTS = {"study_identity": None, "evidence_sections": {}, "working_synopsis": [], "simulation": False, "prepared_records": {}, "selected_sources": {}, "source_selections": {}, "study_facts": None,
                 "group_contract": None, "result_setup": None, "result_sections": [], "fact_cards": [],
                 "publication_map": None, "synopsis_review": None, "deterministic_context": None}
ADMIN_ROLES = {"5.1": {"ethics"}, "5.2": {"ethics"}, "5.3": {"ethics"},
               "6": {"investigators"}, "9.8": {"amendment"}, "15": {"references"}}
CHAPTER_NEEDS = {
    "1": "核对研究标识、申办方、研究起止日期、报告版本与签字登记。",
    "2": "待正文确认后，按已确认段落派生概要。",
    "4": "核对研究采用的缩略语及全称。",
    "5": "核对伦理批件、知情同意书版本及实际执行记录。",
    "6": "补充研究者、中心和职责登记。",
    "7": "核对方案背景及引用文献。",
    "8": "核对方案的研究目的及层级。",
    "9": "核对现行方案、修订案和 SAP 的对应原文。",
    "10": "核对受试者处置、偏离、分析集及对应统计表。",
    "11": "核对终点、分析集、治疗组与定稿统计输出。",
    "12": "核对暴露、安全性结果表及相应医学记录。",
    "13": "结合已确认正文、研究局限和医学负责人意见撰写。",
    "14": "核对表图清单、定稿结果和发表编号。",
    "15": "核对实际引用文献。",
    "16": "核对附件实物、版本、可提供性和提交位置。",
}
LABELS = {"sourced_body": "根据原件和定稿统计表生成正文", "medical_review":"有来源的医学初稿，待医学复核",
          "source_attachment":"已定位实际附件或个例清单", "not_applicable":"有依据的不适用", "decision_required":"已有资料，待真实签认或决定",
          "source_figure":"原始研究流程图", "statistical_table":"原始统计附表", "working_synopsis":"从本轮正文复制的审阅版概要", "container": "下设小节", "directory": "目录", "pending": "待资料或判断",
          "source_excerpt": "原文摘录待核对", "guarded_body": "已生成受控正文", "deterministic": "已按已核资料生成",
          "registered_method": "已填研究事实，待复核", "result_table": "已纳入定稿统计表，待综述", "copied_synopsis": "已从确认正文派生", "study_identifiers": "已填研究标识"}


def _plan(path, fingerprint):
    initial = {"schema_version": "1", "source_snapshot": fingerprint, **PLAN_DEFAULTS}
    if path is None:
        return initial
    plan = load_data(path)
    if not isinstance(plan, dict) or set(plan) - set(initial) or plan.get("schema_version") != "1":
        raise ConfigurationError("工作计划版本或字段错误")
    if plan.get("source_snapshot") != fingerprint:
        raise ConfigurationError("原始资料版本已变化，请重新清点并核对受影响的决定")
    for field, expected in (("evidence_sections", dict), ("working_synopsis", list), ("simulation", bool), ("prepared_records", dict), ("selected_sources", dict), ("source_selections", dict),
                            ("result_sections", list), ("fact_cards", list)):
        if field in plan and not isinstance(plan[field], expected):
            raise ConfigurationError(f"工作计划字段类型无效：{field}")
    return {**initial, **plan}


def _discover_prepared(plan, files):
    """Conventional filenames denote supplied project records, never generated approvals."""
    def unique(*names):
        matches = [p for p in files if Path(p).name.casefold() in names]
        return matches[0] if len(matches) == 1 else None

    plan["group_contract"] = unique("group_contract.confirmed.json")
    plan["study_facts"] = unique("study_facts.confirmed.json")
    plan["publication_map"] = unique("publication_reference_map.confirmed.json")
    frozen = unique("frozen_tfl_map.json")
    rtf = unique("result_tfl_manifest.json", "result_tfl_manifest.yaml", "result_tfl_manifest.yml")
    if frozen and rtf:
        plan["result_setup"] = {"frozen_map": frozen, "rtf_manifest": rtf}
    return plan


def _shell_queues(root, inventory, out, issues):
    for n, (name, row) in enumerate(inventory.items(), 1):
        if row["role"] != "shell" or row["format"] != ".docx":
            continue
        try:
            report, pack, candidate = propose_docx_shell_map(
                root / name, expected_sha256=row["sha256"], map_version=row["sha256"][:16])
            directory = out / "shells" / str(n)
            for key, value in (("proposal", report), ("datapack", pack), ("candidate", candidate)):
                atomic_write_json(directory / f"{key}.json", value)
            row["mapping_candidate"] = f"shells/{n}/candidate.json"
            if not report["review_queue"]:
                continue
            queue = build_review_decision_queue(report, candidate)
        except CSRWriterError as exc:
            issues.append(f"表壳 {name} 需要检查结构。{exc}")
            continue
        atomic_write_json(directory / "queue.json", queue)
        atomic_write_text(directory / "待确认.md", render_review_decision_queue(queue))
        row["mapping_queue"] = f"shells/{n}/待确认.md"


def _selected_excerpts(plan, selected, inventory, records, issues):
    excerpts = method_candidates(selected, records)

    def override(sid, anchors):
        if not isinstance(anchors, list):
            raise ConfigurationError("须使用原文定位列表")
        if sid not in METHOD_TARGETS and sid not in ADMIN_ROLES:
            raise ConfigurationError(f"不能通过原文摘录填充 §{sid}")
        parts = []
        for anchor in anchors:
            if not isinstance(anchor, dict) or set(anchor) != {"source", "location"}:
                raise ConfigurationError("原文定位需要 source 和 location")
            source = anchor["source"]
            if source not in inventory:
                raise ConfigurationError("摘录来源不存在")
            if source not in selected.values() and inventory[source]["role"] not in ADMIN_ROLES.get(sid, set()):
                raise ConfigurationError(f"§{sid} 摘录文件类型不适用")
            if sid in ADMIN_ROLES and sid not in METHOD_TARGETS and inventory[source]["role"] not in ADMIN_ROLES[sid]:
                raise ConfigurationError(f"§{sid} 需要对应的实际记录")
            matches = [r for r in records.get(source, []) if r["location"] == anchor["location"]
                       and eligible_record(r) and r["text"].strip()]
            if len(matches) != 1:
                raise ConfigurationError(f"§{sid} 摘录原文位置无效")
            parts.append({**anchor, "text": matches[0]["text"].strip(), "role": inventory[source]["role"]})
        return parts

    for sid, anchors in plan["source_selections"].items():
        try:
            excerpts[sid] = override(sid, anchors)
        except (ConfigurationError, KeyError, TypeError, ValueError) as exc:
            excerpts.pop(sid, None)
            issues.append(f"§{sid} 原文摘录未纳入。{exc}")
    return excerpts


def _registered_methods(facts):
    if not facts:
        return {}
    excerpts = {}
    size = facts["population"]["planned_sample_size"]
    if size:
        excerpts["9.7.2"] = [(f"计划纳入 {size} 例受试者。", "population.planned_sample_size")]
    for index, entry in enumerate(facts["analyses"]["analysis_sets"]):
        excerpts[f"10.3.{index + 1}"] = [
            (f"{entry['name']}（{entry['id']}）的定义为：{entry['definition']}。",
             f"analyses.analysis_sets[{index}].definition")]
    methods = [(f"主要终点为{entry['name']}。方案规定的分析方法为{entry['analysis_method']}。",
                f"objectives.primary_endpoints[{i}]")
               for i, entry in enumerate(facts["objectives"]["primary_endpoints"])
               if entry.get("domain") == "efficacy" and entry.get("name") and entry.get("analysis_method")]
    if methods:
        excerpts["9.5.3"] = methods
    return excerpts


def _write_volume(root, files, out, plan, inventory, records, issues, *, automatic=False, matches=None):
    selected, selection_issues = choose_sources(inventory, plan["selected_sources"])
    issues.extend(selection_issues)
    from .project_autoplan import identity
    if automatic:
        _, problem = identity(selected,records)
        if problem and "冲突" in problem:
            selected.pop("sap",None)
    excerpts = _selected_excerpts(plan, selected, inventory, records, issues)
    locations = {source: {r["location"] for r in rows if eligible_record(r)}
                 for source, rows in records.items() if rows}

    def read_record(name):
        return project_record(name, root, files, plan["prepared_records"], locations)

    facts = None
    if plan["study_facts"]:
        try:
            facts = validate_study_facts(read_record(plan["study_facts"]))
        except (CSRWriterError, KeyError, TypeError, ValueError) as exc:
            issues.append(f"本轮暂缓按研究事实裁剪目录。{exc}")
    routing = route_modules(facts) if facts else None
    if facts:
        issues.extend(routing.get("warnings", []))
        atomic_write_text(out / "研究事实卡.md", render_fact_card(facts))
        protocol = selected.get("protocol")
        if protocol and protocol.lower().endswith(".docx") and inventory[protocol]["status"] == "extracted":
            scan = scan_protocol_facts(root / protocol, facts, expected_sha256=files[protocol])
            atomic_write_json(out / "protocol-facts-scan.json", scan)
            if scan["findings"]:
                atomic_write_text(out / "方案事实核对.md", render_protocol_fact_scan(scan))
                issues.append(f"方案与研究事实有 {len(scan['findings'])} 条待判读信号，见《方案事实核对》。")
    if not facts and not automatic and not plan.get("study_identity"):
        issues.append("核对结构化研究事实后可按本研究设计裁剪目录；本轮保留通用目录。")
    publication = None
    if plan["publication_map"]:
        try:
            publication = validate_publication_reference_map(read_record(plan["publication_map"]))
        except (CSRWriterError, KeyError, TypeError, ValueError) as exc:
            issues.append(f"本轮发表编号未采用。{exc}")
    content, admitted = {}, []
    pack = frozen = None
    if plan["result_setup"]:
        try:
            pack, frozen, report = result_data(plan["result_setup"], root, files, read_record, out)
            atomic_write_json(out / "result-match.json", report)
        except (CSRWriterError, ValueError, KeyError, TypeError) as exc:
            issues.append(f"统计结果未纳入；需核对冻结映射和结果原件。{exc}")
    has_project_jobs = any(
        isinstance(job, dict) and isinstance(job.get("request", job), dict)
        and job.get("request", job).get("evidence_status") == "reviewed_project_evidence"
        for job in [*plan["result_sections"], *plan["fact_cards"]]
    )
    for kind in ("result_sections", "fact_cards"):
        for n, job in enumerate(plan[kind], 1):
            directory = out / "sections" / f"{kind}-{n}"
            try:
                if not isinstance(job, dict):
                    raise ConfigurationError("章节任务需要使用对象格式")
                evidence = job["request"]["evidence_status"] if kind == "result_sections" else job["evidence_status"]
                if evidence == "demo_only" and has_project_jobs:
                    raise ConfigurationError("演练材料不能并入含真实项目证据的正文")
                if evidence == "source_gap":
                    raise ConfigurationError("来源仍有缺口，本节保留待补状态")
                if not facts:
                    raise ConfigurationError("须先提供与本研究对应的结构化研究事实")
                if kind == "result_sections":
                    if pack is None:
                        raise ConfigurationError("结果表尚未通过映射核对")
                    groups = read_record(plan["group_contract"]) if plan["group_contract"] else None
                    item, section = write_result(job, directory, pack, frozen, facts, records,
                                                 set(selected.values()), groups)
                else:
                    item, section = write_card(job, directory, files, selected)
                sid = item["section_id"]
                section_plan = routing["structure"]["section_plan"]
                active = set(section_plan["retained_official_section_ids"]) | {
                    r["section_id"] for r in section_plan["formal_extensions"]}
                if sid not in active or sid in content:
                    raise ConfigurationError(f"§{sid} 未激活或存在重复任务")
                if item["reference_entities"] and not publication:
                    raise ConfigurationError("请提供已确认的发表编号映射")
                if publication:
                    known = {x["source_entity_id"] for x in publication["entries"]}
                    if set(item["reference_entities"]) - known:
                        raise ConfigurationError("发表编号映射未覆盖本节引用")
                content[sid] = item
                if section:
                    admitted.append(section)
            except (CSRWriterError, ValueError, KeyError, TypeError) as exc:
                issues.append(f"{kind} 第 {n} 项未纳入正文。{exc}")
    synopsis = []
    if plan["synopsis_review"]:
        try:
            review = read_record(plan["synopsis_review"])
            if not all(str(review.get(k) or "").strip() for k in ("reviewed_by", "reviewed_at")):
                raise ConfigurationError("概要缺少正文审阅记录")
            for section in admitted:
                if review["resolved_sha256"].get(section.section_id) != section.resolved_sha256:
                    raise ConfigurationError("正文已变化，须更新对应概要来源")
            clean = apply_review_decisions(admitted, review["review_decisions"])
            synopsis, proof = derive_synopsis_manifest(admitted, clean, review["selection"])
            if any("{{" in p or "}}" in p for p in synopsis):
                raise ConfigurationError("概要所选段落含需转换的发表引用")
            atomic_write_json(out / "synopsis-trace.json", proof)
        except (CSRWriterError, KeyError, ValueError, TypeError) as exc:
            synopsis = []
            issues.append(f"概要未派生。{exc}")

    bundle = None
    if plan["deterministic_context"]:
        try:
            if not (facts and synopsis and publication and frozen):
                raise ConfigurationError("完整模板单元需要研究事实、已确认概要来源、冻结映射和发表编号")
            context = read_record(plan["deterministic_context"])
            if any(context["study_facts"].get(k) != v for k, v in facts.items()):
                raise ConfigurationError("模板资料与本研究事实不一致")
            identifiers = context["study_facts"].get("study_identifiers", {})
            if any(identifiers.get(k) != facts[k] for k in ("study_title", "protocol_id")):
                raise ConfigurationError("标题页研究标识不一致")
            context.update(confirmed_clean_body=clean, frozen_tfl_map=frozen,
                           publication_reference_map=publication)
            candidate_bundle = build_deterministic_bundle(context)
            copy_unit = next(a for a in candidate_bundle["artifacts"] if a["section_id"] == "2")
            if [p["text"] for p in copy_unit["content"]["paragraphs"]] != list(synopsis):
                raise ConfigurationError("模板概要选择与本轮已确认正文不一致")
            bundle = candidate_bundle
            atomic_write_json(out / "deterministic-sections.json", bundle)
        except (CSRWriterError, KeyError, ValueError, TypeError) as exc:
            issues.append(f"完整模板单元未纳入。{exc}")
    deterministic_ids = {a["section_id"] for a in (bundle or {}).get("artifacts", [])}
    path = out / "CSR内部审阅初稿.docx"
    if routing:
        rendered = render_project_skeleton(
            routing, list(content.values()), path, scaffold_path=out / "scaffold.docx",
            mode="working_draft", study_facts=facts, publication_reference_map=publication,
            deterministic_bundle=bundle)
        (out / "scaffold.docx").unlink()
        atomic_write_json(out / "routing.json", routing)
        titles = {r["section_id"]: r.get("render_title", r["title"]) for r in
                  routing["structure"]["section_plan"]["official_sections"] if r["retained"]}
        titles.update({r["section_id"]: r["title"] for r in routing["structure"]["section_plan"]["formal_extensions"]})
        titles.update(DIRECTORY_TITLES)
        titles["16.0"] = "附录状态清单"
        if set(titles) != set(rendered["retained_section_ids"]):
            raise ConfigurationError("成稿目录与项目路由不一致")
    else:
        tree = load_chapter_tree(DEFAULT_STRUCTURE)
        titles = {n.id: n.zh for n in flatten_sections(tree.select_layers(("ich-official",)))}
        render_skeleton(DEFAULT_STRUCTURE, DEFAULT_TEMPLATE, DEFAULT_FORMAT_SPEC, path,
                        header_text="临床试验总结报告（内部审阅初稿）")
    from .project_autoplan import identity, prepare_plan
    if not routing and automatic:
        for extended in ("9.5.1.1","9.5.8"):
            if extended in excerpts:
                excerpts.setdefault("9.5.1", []).extend(excerpts.pop(extended))
    if automatic:
        prepare_plan(plan,inventory,records,out,titles,facts,issues,matches)
    result_identity = facts
    if plan.get("study_identity"):
        verified_identity,_ = identity(selected,records)
        if verified_identity != plan["study_identity"]:
            raise ConfigurationError("研究编号定位与当前方案或 SAP 不一致")
        if not result_identity:
            result_identity = {"protocol_id":verified_identity["value"]}
    registered_methods = _registered_methods(facts)
    exhibits = result_exhibits(pack, frozen, publication, inventory, titles, out, issues, root)
    doc = Document(path)
    headings = _find_heading_paragraphs(doc)
    for sid in titles:
        title = headings[_bookmark_key(sid)].text.strip()
        if title.startswith(sid + " "):
            titles[sid] = title[len(sid) + 1:]
    cover = headings[_bookmark_key("1")].insert_paragraph_before()
    cover.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cover.paragraph_format.first_line_indent = Pt(0)
    title_run = cover.add_run("临床试验报告 · 内部审阅初稿")
    title_run.bold = True
    title_run.font.size = Pt(18)
    if plan["simulation"]:
        headings[_bookmark_key("1")].insert_paragraph_before("模拟研究 · 仅供 CSR 撰写演练")
    supplements, composed = assemble_evidence(doc, headings, titles, plan, inventory, records, out, issues, result_identity, root, selected)
    if any(r["role"] == "amendment" for r in inventory.values()) and "9.8" not in supplements:
        issues.append("已登记修订案，核对生效日期、适用人群及原方案受影响段落。")
    if not pack and not any(kind == "table" for item in supplements.values() for _, _, _, kind in item["navigation"]):
        issues.append("结果章尚需定稿 TFL 及已确认的分析集、组别和表格映射。")
    parents = {sid.rsplit(".", 1)[0] for sid in titles if "." in sid}
    registry = load_production_method_registry()["assignments"]
    rows = []
    for sid in sorted(titles, key=_section_sort_key):
        chapter = sid.split(".")[0]
        heading = headings[_bookmark_key(sid)]
        paras = []
        if sid in exhibits:
            insert_exhibits(doc, heading, exhibits[sid])
        if sid in content:
            state = "guarded_body"
        elif sid in supplements:
            state = supplements[sid]["status"]
            if state == "source_figure" and sid in excerpts:
                for part in excerpts[sid]:
                    paras.append(doc.add_paragraph(part["text"]))
                    supplements[sid]["sources"].append({"source":part["source"],"location":part["location"]})
                state = "sourced_body"
        elif sid in exhibits:
            state = "result_table"
        elif sid in deterministic_ids:
            state = "copied_synopsis" if sid == "2" else "deterministic"
        elif sid == "1" and facts:
            state = "study_identifiers"
            paras = [doc.add_paragraph(facts["study_title"]), doc.add_paragraph(f"方案编号 {facts['protocol_id']}"),
                     doc.add_paragraph(CHAPTER_NEEDS["1"])]
        elif sid == "2" and synopsis:
            state = "copied_synopsis"
            paras = [doc.add_paragraph(p) for p in synopsis]
        elif sid in excerpts and excerpts[sid]:
            state = "source_excerpt"
            for part in excerpts[sid]:
                p = doc.add_paragraph(part["text"])
                locator = re.sub(r"^word/document\.xml:/w:document/w:body/w:p\[(\d+)\]$", r"正文第\1段", part["location"])
                paras.extend((p, doc.add_paragraph(
                    f"原文待核对 · {part['source']} · {locator}", style="CSR Footer")))
        elif sid in registered_methods:
            state = "registered_method"
            for sentence, locator in registered_methods[sid]:
                paras.extend((doc.add_paragraph(sentence),
                              doc.add_paragraph(f"研究事实 · {plan['study_facts']} · {locator}", style="CSR Footer")))
        elif chapter == "3" or sid == "16.0":
            state = "directory"
        elif sid in parents:
            state = "container"
        else:
            state = "pending"
            paras = [doc.add_paragraph("【待补】" + CHAPTER_NEEDS[chapter])]
        if paras:
            _insert_after(heading, paras)
        rows.append({"section": sid, "title": titles[sid], "status": state,
                     "production_method": registry.get(sid, {}).get("production_method"),
                     "action": CHAPTER_NEEDS.get(chapter) if state in {"pending", "source_excerpt", "study_identifiers", "result_table", "registered_method"} else None,
                     "sources": [{k: p[k] for k in ("source", "location")} for p in excerpts.get(sid, [])]
                     if state == "source_excerpt" else
                     [{"source": plan["study_facts"], "location": loc} for _, loc in registered_methods[sid]]
                     if state == "registered_method" else [],
                     "result_tables": [item[0]["source_entity_id"] for item in exhibits.get(sid, [])],
                     "needs_human_paragraphs": [i for i, p in enumerate(content.get(sid, {}).get("paragraphs", []), 1)
                                                if p["needs_human"]]})
    for row in rows:
        if row["section"] in supplements:
            row["sources"] += supplements[row["section"]]["sources"]
    if plan["working_synopsis"] and not synopsis and not bundle:
        import hashlib
        copies, trace = [], []
        body_text = {p.text for p in doc.paragraphs}
        for selection in plan["working_synopsis"]:
            sid = selection.get("section") if isinstance(selection, dict) else selection
            if not isinstance(sid, str) or sid not in titles:
                issues.append("概要引用了未启用的章节。")
                continue
            # Copy only the live, source-bound factual paragraphs. Medical conclusions stay in their sections.
            if sid in excerpts and sid not in supplements and sid not in content:
                texts = [part["text"] for part in excerpts[sid][:1]]
            elif supplements.get(sid, {}).get("status") == "sourced_body":
                texts = composed[sid] if isinstance(selection, dict) else composed[sid][:2]
            else:
                issues.append(f"内部概要未采用 {sid}，缺少可复制的事实正文。")
                continue
            if isinstance(selection, dict):
                indices = selection.get("paragraphs")
                if not isinstance(indices, list) or not indices or any(type(i) is not int or not 1 <= i <= len(texts) for i in indices):
                    issues.append(f"内部概要引用的 {sid} 段落不存在。")
                    continue
                texts = [texts[i-1] for i in indices]
            copies.append(doc.add_paragraph(titles[sid], style="CSR Footer"))
            for sentence in texts:
                if sentence not in body_text:
                    raise ConfigurationError("概要来源句未写入本轮正文")
                copies.append(doc.add_paragraph(sentence))
                trace.append({"section":sid, "text":sentence, "sha256":hashlib.sha256(sentence.encode()).hexdigest()})
        if copies:
            anchor = headings[_bookmark_key("2")]
            node = anchor._p.getnext()
            if node is not None and "【待补】" in "".join(node.itertext()):
                node.getparent().remove(node)
            _insert_after(anchor, [doc.add_paragraph("内部审阅概要 · 待医学与统计复核"), *copies])
            atomic_write_json(out/"working-synopsis-trace.json", trace)
            row = next(r for r in rows if r["section"]=="2")
            row.update(status="working_synopsis", action=None, sources=[{"source":"本轮正文", "location":t["section"], "sha256":t["sha256"]} for t in trace])
            issues.append("概要须在正文确认后完成医学与统计复核。")
    prepare_draft_navigation(doc, titles, exhibits, [item for s in supplements.values() for item in s["navigation"]])
    doc.core_properties.identifier = plan["source_snapshot"]
    _atomic_save_docx(doc, path)
    verified = Document(path)
    if (verified.core_properties.identifier != plan["source_snapshot"] or
        set(_find_heading_paragraphs(verified)) != {_bookmark_key(s) for s in titles}):
        raise ConfigurationError("输出 Word 回读时章节或来源快照不一致")
    validate_docx_references(path)
    return rows, selected, sha256_file(path)


def draft_project(source_dir, output_dir=None, plan_path=None, *, matches_path=None, answers_path=None, sap_reading=None):
    root = Path(source_dir).resolve()
    base = root.with_name(root.name + "_CSR初稿") if output_dir is None else Path(output_dir).absolute()
    destination = base
    index = 1
    while output_dir is None and (destination.exists() or destination.is_symlink()):
        index += 1
        destination = base.with_name(f"{base.name}_{index}")
    if (destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(root)
            or root.is_relative_to(destination.resolve())):
        raise ConfigurationError("请使用独立于输入资料的新输出目录；已有稿件会保留")
    files, skipped = discover(root)
    plan = _plan(plan_path, snapshot_id(files))
    matches = routing_reply = None
    if matches_path:
        from .project_autoplan import load_matches
        if plan_path:
            raise ConfigurationError("请使用自动匹配答复或完整工作计划中的一种")
        matches,routing_reply = load_matches(matches_path,plan["source_snapshot"])
    if plan_path is None:
        plan = _discover_prepared(plan, files)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".csr-draft-") as tmp:
        out = Path(tmp) / "volume"
        out.mkdir()
        issues = []
        inventory, records = read_sources(root, files, out)
        for rel, row in inventory.items():
            if row.get("issue"):
                issues.append(f"文件 {rel} 提取失败。{row['issue']}")
            if row.get("warnings"):
                issues.append(f"文件 {rel} 有修订、批注或提取提示，详见 {row['extraction']}。")
        if plan_path is None:
            from .project_autoplan import apply_routing_reply, select_effective_sources
            plan["selected_sources"] = select_effective_sources(inventory,records)
            apply_routing_reply(routing_reply,plan,inventory,records)
        from .subject_tables import run_subject_analysis
        from .project_answers import Decisions
        review=Decisions(answers_path)
        statistics = run_subject_analysis(root,out,inventory,records,plan,review,sap_reading)
        _shell_queues(root, inventory, out, issues)
        rows, selected, document_hash = _write_volume(root, files, out, plan, inventory, records, issues, automatic=plan_path is None, matches=matches)
        report = {"schema_version": "1", "status": "internal_review_draft",
                  "source_snapshot": plan["source_snapshot"], "sources": {p:inventory[p] for p in files}, "skipped": skipped,
                  "selected_sources": selected, "sections": rows,
                  "coverage": dict(Counter(r["status"] for r in rows)),
                  "review_items": issues, "docx_sha256": document_hash}
        from .project_autoplan import human_items
        report["human_decisions"],report["source_followup"] = human_items(rows,issues)
        if statistics is not None:
            report["subject_analysis"]={"status":statistics["status"],"manifest":"统计分析/分析留痕.json","tables":statistics["tables"]}
            if statistics.get("workbook"):
                name=statistics["workbook"]
                report["derived_sources"]={name:inventory[name]}
            if statistics.get("workbook"):
                # Consolidate repeated medical and statistical reviews into one decision per responsibility.
                grouped=[item for item in report["human_decisions"]
                         if item["action"].startswith(("医学负责人核实","第13章已汇入事实结果"))]
                medical=next((item for item in statistics["decisions"] if item["role"]=="医学负责人"),None)
                if grouped and medical is None:
                    medical={"role":"医学负责人","action":"","sections":[]}
                    statistics["decisions"].append(medical)
                if medical is not None and grouped:
                    medical["sections"]=list(dict.fromkeys(medical["sections"]+[s for i in grouped for s in i["sections"]]+["13"]))
                    medical["action"]+="结合疗效、安全性及研究局限，确认医学结论。"
                    report["human_decisions"]=[i for i in report["human_decisions"] if i not in grouped]
            report["human_decisions"].extend(statistics["decisions"])
            report["source_followup"].extend(statistics["data_queries"])
        report["source_gaps"] = [{"section":r["section"],"title":r["title"],"need":r["action"]}
                                 for r in rows if r["status"]=="pending"]
        if plan.get("study_identity"):
            report["study_identity"] = plan["study_identity"]
        if (out/"待智能体匹配.json").is_file():
            report["agent_work"] = "待智能体匹配.json"
        if statistics and statistics.get("workbook"):
            from .project_review_volume import finish_review
            finish_review(out,report,statistics,review,root)
        elif answers_path:
            raise ConfigurationError("当前资料未形成可绑定的统计结果，未采用集中答复")
        atomic_write_json(out / "draft-report.json", report)
        atomic_write_json(out / "project-plan.json", plan)
        _review_sheet(out, report)
        if discover(root) != (files, skipped):
            raise ConfigurationError("制作期间原始资料发生变化，需重新运行")
        # mkdir reserves the final name exclusively, including on concurrent runs.
        while True:
            try:
                destination.mkdir()
                break
            except FileExistsError:
                if output_dir is not None:
                    raise ConfigurationError("输出目录已存在，请保留已有稿件")
                index += 1
                destination = base.with_name(f"{base.name}_{index}")
        try:
            os.replace(out, destination)
        except OSError:
            destination.rmdir()
            raise
    return {**report,"output_dir":str(destination)}


def _review_sheet(out, report):
    lines = ["# CSR 初稿审阅清单", "", "## 本轮覆盖", ""]
    labels = {**LABELS,"reviewed_medical":"已按本版医学答复更新"}
    if report.get("derived_sources"):
        labels.update(sourced_body="依据原件及统计复核表生成正文",statistical_table="统计实体表（计算结果待复核）")
        lines.extend(["本稿逐表记录已采用的定稿结果、补算结果、暂缓项与独立复算状态，见「统计分析/逐表对账.json」。计算来源和逐例记录见「统计分析/分析留痕.json」。",""])
    lines.extend(f"- {labels[k]}　{v} 节" for k, v in report["coverage"].items())
    lines.extend(["", "## 需要人工决定或签认", ""])
    import json
    decisions_file=out/"负责人决定.json"
    entries={r["id"]:r for r in json.loads(decisions_file.read_text())["items"]} if decisions_file.exists() else {}
    ledger_file=out/"统计分析/逐表对账.json"
    ledger=json.loads(ledger_file.read_text()) if ledger_file.exists() else []
    if entries:
        lines.extend(["可在同一份「集中答复模板.json」中填写多项答复。每项列明答复人、日期、选择与判断依据，未答复项可以留空。",""])
    for item in report["human_decisions"]:
        refs="、".join(item["sections"])
        lines.append("- "+(item["role"]+"　" if "role" in item else "")+item["action"]+(f"（{refs}）" if refs else ""))
        entry=entries.get(item.get("id"))
        if entry:
            lines.append("  - 编号 "+entry["id"]+"。可选决定为"+"、".join(entry["choices"])+"。")
            event=entry["evidence"].get("event")
            if event:
                lines.append("  - 依据 "+str(event.get("loc") or event.get("source"))+"。")
            for r in ledger:
                if entry["id"]=="TFL-"+str(r.get("number")):
                    for source in r.get("external",[]):
                        for d in source["differences"]:
                            lines.append("  - "+(f"{source['source']} {d['external_cell']} 为 {d['external']}，逐例结果 {d['calculated_cell']} 为 {d['calculated']}。" if d.get("external_cell") and "calculated_cell" in d else str(d)))
                    if r.get("same_day_administrations"):
                        lines.append("  - 同日多次给药记录："+ "、".join(x["subject"]+" "+x["date"]+" 原始行 "+"/".join(map(str,x["source_rows"])) for x in r["same_day_administrations"])+"。")
    if not report["human_decisions"]:
        lines.append("本轮未识别需裁决的冲突或签认项目。")
    if report["source_followup"]:
        lines.extend(["","## 资料与自动处理限制",""])
        lines.extend("- "+item for item in report["source_followup"])
    if report["source_gaps"]:
        lines.extend(["","## 尚缺依据的章节",""])
        grouped = {}
        for r in report["source_gaps"]:
            grouped.setdefault(r["need"],[]).append(r["section"]+" "+r["title"])
        lines.extend("- "+need+" "+ "、".join(ids) for need,ids in grouped.items())
    lines.extend(["", "## 输入资料", ""])
    status_labels = {"indexed": "仅登记", "extracted": "已提取", "unreadable": "提取失败"}
    lines.extend(f"- {name}（{('已用于逐例统计核查' if row.get('analyzed_subject_data') else status_labels[row['status']])}）" +
                 (f"　表壳审阅表：{row['mapping_queue']}" if row.get("mapping_queue") else "")
                 for name, row in report["sources"].items())
    atomic_write_text(out / "集中审阅清单.md", "\n".join(lines) + "\n")
