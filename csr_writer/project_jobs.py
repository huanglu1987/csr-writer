"""Execute project-specific writing jobs using the existing evidence gates."""
from __future__ import annotations

from .conditional_conclusions import resolve_conditional_conclusions
from .constrained_narration import (
    emit_fact_card_envelope, ingest_constrained_draft, render_verbatim_fact_card,
    write_constrained_artifact,
)
from .cross_chapter import load_constrained_resolved_section, load_resolved_section
from .datapack import build_datapack
from .docx_result import apply_docx_result_to_frozen_shell
from .errors import ConfigurationError
from .group_semantics import _normalize_contract
from .semantic_key import parse_semantic_cell_key
from .io_utils import atomic_write_json, atomic_write_text, load_data
from .project_sources import eligible_record
from .project_skeleton import _formalize_content_section
from .resolve_placeholders import resolve
from .section_writer import draft_section, emit_agent_envelope, ingest_agent_draft
from .tfl_map import check_result_against_frozen_map
from .unified_orchestrator import _content_from_sections


def input_path(name, root, files):
    if not isinstance(name, str) or name not in files:
        raise ConfigurationError(f"资料未登记在本次输入目录：{name!r}")
    return root / name


def project_record(name, root, files, prepared, locations):
    if isinstance(name, str) and name.startswith("@"):
        record = prepared.get(name)
        if not isinstance(record, dict) or set(record) != {"basis", "value"}:
            raise ConfigurationError(f"工作记录缺少内容或原始依据：{name}")
        if not isinstance(record["basis"], list) or not record["basis"]:
            raise ConfigurationError(f"工作记录缺少来源定位：{name}")
        for anchor in record["basis"]:
            if (not isinstance(anchor, dict) or anchor.get("source") not in files
                    or not isinstance(anchor.get("location"), str) or not anchor["location"].strip()):
                raise ConfigurationError(f"工作记录的来源定位无效：{name}")
            source = anchor["source"]
            if source in locations and anchor["location"] not in locations[source]:
                raise ConfigurationError(f"工作记录引用的位置未见于所提取原件：{source} · {anchor['location']}")
        return record["value"]
    return load_data(input_path(name, root, files))


def result_data(setup, root, files, read_record, out):
    frozen = read_record(setup["frozen_map"])
    if set(setup) == {"frozen_map", "rtf_manifest"}:
        name = setup["rtf_manifest"]
        manifest = read_record(name)
        if name.startswith("@"):
            manifest = {**manifest, "sources": [{**s, "path": str(input_path(s["path"], root, files))}
                                                for s in manifest["sources"]]}
            manifest_path = out / "rtf-manifest.json"
            atomic_write_json(manifest_path, manifest)
        else:
            manifest_path = input_path(name, root, files)
        for item in manifest["sources"]:
            target = (manifest_path.parent / item["path"]).resolve()
            if not target.is_relative_to(root) or target.relative_to(root).as_posix() not in files:
                raise ConfigurationError("结果表路径超出本次资料清单")
        pack, _ = build_datapack(manifest_path)
        report = check_result_against_frozen_map(pack, frozen)
    elif set(setup) == {"frozen_map", "shell_datapack", "result_docx"}:
        shell = read_record(setup["shell_datapack"])
        check_result_against_frozen_map(shell, frozen)
        path = input_path(setup["result_docx"], root, files)
        pack, report = apply_docx_result_to_frozen_shell(
            shell, frozen, path, expected_sha256=files[setup["result_docx"]]
        )
    else:
        raise ConfigurationError("结果任务需要冻结映射及 RTF 清单或 DOCX 表壳数据包和结果表")
    return pack, frozen, report


def write_result(job, directory, pack, frozen, facts, records, selected, group_contract=None):
    if set(job) - {"request", "draft", "fragment_locations"}:
        raise ConfigurationError("结果章节任务存在未知字段")
    req = job["request"]
    # The agent can select project text but cannot introduce its own method definition.
    anchors = job["fragment_locations"]
    if set(anchors) != {fragment["source_id"] for fragment in req["source_fragments"]}:
        raise ConfigurationError("章节方法片段与定位清单不一致")
    for fragment in req["source_fragments"]:
        anchor = anchors[fragment["source_id"]]
        if not any(r["location"] == anchor["location"] and fragment["text"] == r["text"].strip()
                   and eligible_record(r) for r in records.get(anchor["source"], [])):
            raise ConfigurationError(f"方法片段与原件不符：{fragment['source_id']}")
        if anchor["source"] not in selected and fragment["purpose"] in {
            "endpoint_definition", "analysis_set", "analysis_method"
        }:
            raise ConfigurationError("结果定义和分析方法须来自所选方案或 SAP")
    if req["section_id"] in {"11.4", "11.6", "11.4.1.1", "11.6.1.1"} and req["tfl_bindings"]:
        if not facts["design"]["controlled"]:
            raise ConfigurationError("两组对比写法与本研究设计不一致")
        if group_contract is None:
            raise ConfigurationError("请核对试验组及对照组在统计原表的列标识")
        contract = _normalize_contract(group_contract)
        for b in req["tfl_bindings"]:
            if b["role"] in {"experimental_result", "experimental_ci", "control_result", "control_ci"}:
                group = contract["binding_roles"].get(b["role"])
                if (not group or contract["groups"][group]["column"]
                        != parse_semantic_cell_key(b["key"]).column):
                    raise ConfigurationError(f"组别与已确认列不符：{b['role']}")
    entity_routes = {x["entity_id"]: x["ich_e3_sections"] for x in frozen["entities"]}
    target = _formalize_content_section(req)["section_id"]
    for binding in req["tfl_bindings"]:
        cell = pack["cells"].get(binding["key"])
        routes = entity_routes.get(binding["source_id"], [])
        if cell is None or not any(
            s == route or s.startswith(route + ".")
            for route in routes for s in (target, req["section_id"])
        ):
            raise ConfigurationError("章节、统计表归属或数值绑定与冻结映射不一致")
    envelope = emit_agent_envelope(req)
    atomic_write_json(directory / "envelope.json", envelope)
    if "draft" in job:
        artifact, report = ingest_agent_draft(envelope, job["draft"], directory)
        atomic_write_json(directory / "writer-guard.json", report)
    else:
        artifact = draft_section(req, directory)
    conditioned = resolve_conditional_conclusions(artifact.markdown, pack, artifact.metadata)
    result = resolve(conditioned.resolved_text, pack, facts, binding_manifest=artifact.metadata,
                     frozen_tfl_map=frozen)
    result.report["conditional_conclusion"] = conditioned.report
    guard = directory / "resolve.json"
    atomic_write_json(guard, result.report)
    if result.status != "pass" or result.resolved_text is None:
        raise ConfigurationError("数值核对未通过，详见本节 resolve.json")
    path = directory / "resolved.md"
    atomic_write_text(path, result.resolved_text)
    section = load_resolved_section(artifact.markdown_path, path, artifact.metadata_path, guard, require_condition_proof=True)
    content = _content_from_sections([section])[0]
    content["variant"] = req["variant"]
    by_key = {binding["key"]: pack["cells"][binding["key"]]["source_locator"]
              for binding in req["tfl_bindings"]}
    atomic_write_json(directory / "source-locations.json",
                      {"cells": by_key, "fragments": anchors})
    return _formalize_content_section(content), section


def write_card(job, directory, files, selected):
    if set(job) - {"card", "draft", "evidence_status"}:
        raise ConfigurationError("事实卡任务存在未知字段")
    card = job["card"]
    status = job["evidence_status"]
    if status not in {"reviewed_project_evidence", "demo_only"}:
        raise ConfigurationError("事实卡需明确证据状态")
    for source in card["source_manifest"]:
        role = source["file_role"].casefold()
        if role in {"protocol", "sap"} and files.get(selected.get(role)) != source["content_sha256"]:
            raise ConfigurationError(f"事实卡未使用本轮选定的{role}：{source['source_id']}")
        if source["content_sha256"] not in files.values():
            raise ConfigurationError(f"事实卡与本次来源版本不符：{source['source_id']}")
    if card["narration"] == "verbatim_list":
        artifact = render_verbatim_fact_card(card)
    else:
        envelope = emit_fact_card_envelope(card)
        atomic_write_json(directory / "envelope.json", envelope)
        artifact = ingest_constrained_draft(envelope, job["draft"])
    md, meta = directory / "section.md", directory / "section.meta.json"
    write_constrained_artifact(artifact, md, meta)
    if card["narration"] == "verbatim_list":
        source_ids = [row["source_id"] for row in card["source_manifest"]]
        paragraphs = [{"text": line[2:], "sources": source_ids, "confidence": "verbatim",
                       "needs_human": True} for line in artifact.markdown.splitlines()
                      if line.startswith("- ")]
        return {"section_id": artifact.section_id, "variant": "verbatim_list", "title": card["title"],
                "reference_entities": [], "evidence_status": status, "paragraphs": paragraphs}, None
    section = load_constrained_resolved_section(
        md, meta, evidence_status="reviewed" if status == "reviewed_project_evidence" else status)
    return _content_from_sections([section])[0], section
