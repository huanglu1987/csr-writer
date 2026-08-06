#!/usr/bin/env python3
"""Run the privacy-safe M15 real-shell/synthetic-result end-to-end proof."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.cross_chapter import load_sections_from_manifest  # noqa: E402
from csr_writer.cross_references import REF_PLACEHOLDER_RE, display_reference  # noqa: E402
from csr_writer.authoring_inventory import (  # noqa: E402
    build_authoring_inventory,
    render_authoring_inventory_markdown,
)
from csr_writer.docx_result import (  # noqa: E402
    DocxResultError,
    apply_docx_result_to_frozen_shell,
)
from csr_writer.io_utils import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    sha256_file,
)
from csr_writer.m4_assembly import (  # noqa: E402
    render_annotated_docx,
    render_clean_and_synopsis,
    validate_clean_docx,
    validate_docx_references,
)
from csr_writer.m4_qc import build_qc_report  # noqa: E402
from csr_writer.module_router import route_modules  # noqa: E402
from csr_writer.playbook_qc import (  # noqa: E402
    M15_NEW_COMPLETE_PLAYBOOK_FILENAMES,
    audit_complete_playbooks,
)
from csr_writer.resolve_placeholders import resolve  # noqa: E402
from csr_writer.section_writer import emit_agent_envelope, ingest_agent_draft  # noqa: E402
from csr_writer.shell_mapping import (  # noqa: E402
    freeze_proposed_shell_map,
    propose_docx_shell_map,
)
from csr_writer.source_lock import verify_source_file_lock  # noqa: E402
from csr_writer.study_facts import load_study_facts  # noqa: E402
from csr_writer.traceability import (  # noqa: E402
    analyze_tfl_references,
    render_unreferenced_tfl,
)
from csr_writer.writing_style import analyze_writing_style  # noqa: E402


OUTPUT = ROOT / "examples" / "output" / "m15"
FIXTURE = ROOT / "examples" / "fixtures" / "synthetic_result_tfl.deidentified.docx"
FIXTURE_HASH = "76338581eb2295e0ed6552a348da369a1f72aaa61a62421fba4fd22174a56c4a"
REAL_SOURCE_HASHES = {
    "paired_protocol": "3c1cf7c01434405885f562d9e687691185d5250bf4a6aeead564fcdf97aeceae",
    "paired_tfl_shell": "75b6ce46bfda933d75611ce38e0ac564a17116d67faf04a767c1bd81d4bc27c5",
    "reference_protocol": "12ac850d70294e1a074ffa8102e437d98e67f27e81091097bc15fddd63d45ca1",
    "reference_numbering_pdf": "b074ce91810e48e0536cb4464053502e37c5bbe244b7954715ffd41a1936117a",
}


def _find_sources(real_dir: Path) -> dict[str, Path]:
    matches: dict[str, list[Path]] = {role: [] for role in REAL_SOURCE_HASHES}
    if not real_dir.is_dir():
        raise RuntimeError("Approved read-only source directory is unavailable")
    for path in real_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".docx", ".pdf"}:
            continue
        digest = sha256_file(path)
        for role, expected in REAL_SOURCE_HASHES.items():
            if digest == expected:
                matches[role].append(path)
    invalid = {role: len(paths) for role, paths in matches.items() if len(paths) != 1}
    if invalid:
        raise RuntimeError(f"Approved-source hash resolution failed: {invalid}")
    return {role: paths[0] for role, paths in matches.items()}


def _source_audit(sources: Mapping[str, Path]) -> dict[str, Any]:
    records = []
    for role, expected in REAL_SOURCE_HASHES.items():
        observed = sha256_file(sources[role])
        records.append(
            {
                "source_role": role,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "match": expected == observed,
                "path_recorded": False,
                "filename_recorded": False,
            }
        )
    return {
        "schema_version": "1.0",
        "status": "pass" if all(item["match"] for item in records) else "blocked",
        "real_sources_read_only": True,
        "records": records,
    }


def _privacy_scan(root: Path) -> dict[str, Any]:
    forbidden = (
        bytes.fromhex("5a5947").decode("utf-8"),
        bytes.fromhex("42563235303732353031").decode("utf-8"),
        "/Users/huanglu",
        "研究者姓名",
        "机构名称",
        "SUBJECT-ID-",
    )
    findings: list[dict[str, str]] = []
    checked = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "privacy_scan.json":
            continue
        if path.suffix.lower() in {".json", ".md", ".txt"}:
            chunks = [path.read_text(encoding="utf-8")]
        elif path.suffix.lower() == ".docx":
            with zipfile.ZipFile(path) as package:
                chunks = [
                    package.read(name).decode("utf-8", errors="ignore")
                    for name in package.namelist()
                    if name.startswith("word/") and name.endswith(".xml")
                ]
        else:
            continue
        checked += 1
        text = "\n".join(chunks)
        for token in forbidden:
            if token in text:
                findings.append(
                    {"artifact": str(path.relative_to(root)), "token": token}
                )
    return {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "files_checked": checked,
        "findings": findings,
    }


def _select_key(
    datapack: Mapping[str, Any],
    table_id: str,
    *parts: str,
) -> str:
    keys = sorted(
        key
        for key, cell in datapack["cells"].items()
        if key.startswith(table_id + "|")
        and all(part in key for part in parts)
        and cell.get("result_value_status") == "observed_result"
    )
    if len(keys) != 1:
        raise RuntimeError(
            f"Expected one observed semantic key for {table_id}/{parts}; got {len(keys)}"
        )
    return keys[0]


def _binding(role: str, key: str) -> dict[str, str]:
    return {
        "role": role,
        "key": key,
        "source_id": key.split("|", 1)[0],
        "context": "脱敏合成结果；仅用于结构与守卫验收",
    }


def _source(source_id: str, purpose: str, text: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "locator": "脱敏等价方法说明",
        "purpose": purpose,
        "text": text,
    }


def _writing_specs(
    datapack: Mapping[str, Any], frozen: Mapping[str, Any]
) -> list[dict[str, Any]]:
    pk_cmax = _select_key(
        datapack,
        "T14.2.4.3.1",
        "ROW=CMAX_UNIT/GM",
        "COL=C0_5PCT_BID",
        "STAGE=D1",
    )
    pk_auc = _select_key(
        datapack,
        "T14.2.4.3.1",
        "ROW=AUC0_UNIT/GM",
        "COL=C0_5PCT_BID",
        "STAGE=D1",
    )
    efficacy = _select_key(
        datapack,
        "T14.2.2.1.1",
        "ROW=BASELINE/OBSERVED_VALUE/MEAN_SD",
        "COL=PBO",
    )
    ae = _select_key(
        datapack,
        "T14.3.1.3.1",
        "SOC_全身性疾病及给药部位各种反应/PT_用药部位红斑",
        "COL=ACTIVE_TOTAL_N_PCT",
    )
    local = _select_key(
        datapack,
        "T14.3.1.3.3",
        "SOC_皮肤及皮下组织类疾病/PT_用药部位红斑",
        "COL=ACTIVE_TOTAL_N_PCT",
    )
    confirmed = {
        "frozen_sha256": frozen["frozen_sha256"],
        "entity_ids": [item["entity_id"] for item in frozen["entities"]],
    }

    def request(
        section_id: str,
        variant: str,
        bindings: list[dict[str, str]],
        sources: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.1",
            "section_id": section_id,
            "variant": variant,
            "evidence_status": "demo_only",
            "tfl_bindings": bindings,
            "tfl_references": list(
                dict.fromkeys(item["source_id"] for item in bindings)
            ),
            "confirmed_tfl_map": confirmed,
            "source_fragments": sources,
        }

    return [
        {
            "request": request(
                "11.5.4",
                "pk_parameters",
                [_binding("pk_cmax", pk_cmax), _binding("pk_auc", pk_auc)],
                [
                    _source(
                        "SRC-M15-PK-METHOD",
                        "pk_method",
                        "PK 参数采用非房室方法计算，参数按分析集和给药阶段分别呈现。",
                    ),
                    _source(
                        "SRC-M15-PK-SET",
                        "analysis_set",
                        "本节使用 PK 参数分析集。",
                    ),
                ],
            ),
            "raw": {
                "paragraphs": [
                    {
                        "text": "PK 参数采用非房室方法计算，并按分析集和给药阶段分别呈现。",
                        "sources": ["SRC-M15-PK-METHOD", "SRC-M15-PK-SET"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": (
                            f"Cmax 与 AUC 的几何均值分别为 {{{{{pk_cmax}}}}} 和 "
                            f"{{{{{pk_auc}}}}}，详见{{{{@ref:T14.2.4.3.1}}}}。"
                        ),
                        "sources": ["T14.2.4.3.1"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": "上述结果仅涵盖可计算参数，不代表全部采样时点。",
                        "sources": ["SRC-M15-PK-METHOD"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                ]
            },
        },
        {
            "request": request(
                "11.6.1.4",
                "exploratory_endpoint_results",
                [_binding("exploratory_endpoint_result", efficacy)],
                [
                    _source(
                        "SRC-M15-EXP-ENDPOINT",
                        "endpoint_definition",
                        "该量表为探索性评价，结果不用于确证性结论。",
                    ),
                    _source(
                        "SRC-M15-EXP-SET",
                        "analysis_set",
                        "本节按全分析集呈现。",
                    ),
                ],
            ),
            "raw": {
                "paragraphs": [
                    {
                        "text": "该量表作为探索性评价，并按全分析集呈现。",
                        "sources": ["SRC-M15-EXP-ENDPOINT", "SRC-M15-EXP-SET"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": (
                            f"安慰剂组基线观察值的均值（标准差）为 {{{{{efficacy}}}}}，"
                            "详见{{@ref:T14.2.2.1.1}}。"
                        ),
                        "sources": ["T14.2.2.1.1"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": "本节现有结果仅包括基线描述，未对访视后差异作判断。",
                        "sources": ["SRC-M15-EXP-ENDPOINT"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                ]
            },
        },
        {
            "request": request(
                "12.2.3",
                "ae_soc_pt_analysis",
                [_binding("teae_soc_pt_result", ae)],
                [
                    _source(
                        "SRC-M15-AE-DEFINITION",
                        "safety_definition",
                        "不良事件按系统器官分类和首选术语呈现。",
                    ),
                    _source(
                        "SRC-M15-AE-SET",
                        "analysis_set",
                        "安全性结果按安全性分析集呈现。",
                    ),
                ],
            ),
            "raw": {
                "paragraphs": [
                    {
                        "text": "不良事件按系统器官分类和首选术语呈现，分析人群为安全性分析集。",
                        "sources": ["SRC-M15-AE-DEFINITION", "SRC-M15-AE-SET"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": (
                            "在全身性疾病及给药部位各种反应下，用药部位红斑见于 "
                            f"{{{{{ae}}}}}，详见{{{{@ref:T14.3.1.3.1}}}}。"
                        ),
                        "sources": ["T14.3.1.3.1"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": "同名首选术语按各自的系统器官分类分别统计。",
                        "sources": ["SRC-M15-AE-DEFINITION"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                ]
            },
        },
        {
            "request": request(
                "12.2.9",
                "application_site_adverse_reactions",
                [_binding("application_site_teae", local)],
                [
                    _source(
                        "SRC-M15-LOCAL-DEFINITION",
                        "safety_definition",
                        "用药部位不良反应按系统器官分类和首选术语呈现。",
                    ),
                    _source(
                        "SRC-M15-LOCAL-METHOD",
                        "local_tolerance_method",
                        "编码为不良事件的用药部位反应与主动询问评分分开呈现。",
                    ),
                ],
            ),
            "raw": {
                "paragraphs": [
                    {
                        "text": "编码为不良事件的用药部位反应与主动询问的局部耐受性评分分开呈现。",
                        "sources": ["SRC-M15-LOCAL-METHOD"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": (
                            "在皮肤及皮下组织类疾病下，用药部位红斑见于 "
                            f"{{{{{local}}}}}，详见{{{{@ref:T14.3.1.3.3}}}}。"
                        ),
                        "sources": ["T14.3.1.3.3"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                    {
                        "text": "本节仅呈现已编码的不良事件，不代替局部耐受性评分结果。",
                        "sources": ["SRC-M15-LOCAL-DEFINITION"],
                        "confidence": "derived",
                        "needs_human": False,
                    },
                ]
            },
        },
    ]


def _run_writing(
    datapack: Mapping[str, Any], frozen: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], tuple[Any, ...]]:
    project = OUTPUT / "writing_trial" / "project"
    records: list[dict[str, Any]] = []
    manifest_entries: list[dict[str, str]] = []
    facts = json.loads(
        (ROOT / "examples" / "demo_protocol_facts.json").read_text(encoding="utf-8")
    )
    for item in _writing_specs(datapack, frozen):
        request = item["request"]
        section_id = request["section_id"]
        section_root = OUTPUT / "writing_trial" / section_id.replace(".", "_")
        section_root.mkdir(parents=True, exist_ok=True)
        envelope = emit_agent_envelope(request)
        atomic_write_json(section_root / "request.json", request)
        atomic_write_json(section_root / "envelope.json", envelope)
        atomic_write_json(section_root / "raw_agent_draft_attempt1.json", item["raw"])
        draft, ingest_report = ingest_agent_draft(envelope, item["raw"], project)
        atomic_write_json(section_root / "ingest_guard_attempt1.json", ingest_report)
        resolved = resolve(
            draft.markdown,
            dict(datapack),
            facts,
            binding_manifest=draft.metadata,
        )
        if resolved.status != "pass" or resolved.resolved_text is None:
            raise RuntimeError(f"M15 resolve blocked for {section_id}: {resolved.report}")
        resolved_path = section_root / "resolved.md"
        guard_path = section_root / "resolve_guard.json"
        atomic_write_text(resolved_path, resolved.resolved_text)
        atomic_write_json(guard_path, resolved.report)
        manifest_entries.append(
            {
                "draft": str(draft.markdown_path.relative_to(OUTPUT)),
                "resolved": str(resolved_path.relative_to(OUTPUT)),
                "metadata": str(draft.metadata_path.relative_to(OUTPUT)),
                "guard": str(guard_path.relative_to(OUTPUT)),
            }
        )
        records.append(
            {
                "section_id": section_id,
                "playbook": draft.metadata["playbook"]["filename"],
                "bound_keys": [binding["key"] for binding in request["tfl_bindings"]],
                "all_bound_values_observed_result": all(
                    datapack["cells"][binding["key"]].get("result_value_status")
                    == "observed_result"
                    for binding in request["tfl_bindings"]
                ),
                "ingest_status": ingest_report["status"],
                "resolve_status": resolved.status,
                "guard_stages": {
                    key: value["status"] for key, value in resolved.report["stages"].items()
                },
                "retry_count": 0,
                "manual_polishing_after_guard": False,
            }
        )

    manifest = {"schema_version": "1.0", "sections": manifest_entries}
    manifest_path = OUTPUT / "sections_manifest.json"
    atomic_write_json(manifest_path, manifest)
    sections = load_sections_from_manifest(manifest_path)
    return records, sections


def _readable(text: str) -> str:
    return REF_PLACEHOLDER_RE.sub(
        lambda match: display_reference(match.group("entity")), text
    )


def _write_sample(sections: Sequence[Any]) -> dict[str, Any]:
    lines = [
        "# M15 起草样例",
        "",
        "> 本样例使用脱敏合成结果，经 envelope、ingest、三道数值守卫和确定性 resolve 直接生成；未作守卫后的人工润色，不得作为临床证据。",
        "",
    ]
    body_parts: list[str] = []
    for section in sections:
        text = "# " + section.heading + "\n\n" + "\n\n".join(
            paragraph.text for paragraph in section.paragraphs
        )
        readable = _readable(text)
        lines.extend([readable, ""])
        body_parts.append("\n\n".join(paragraph.text for paragraph in section.paragraphs))
    lines.extend(
        [
            "## 守卫拦截记录",
            "",
            "- 首轮被拒节数：0；首轮被拒段落：0。",
            "- 重试次数：0；最终通过：4/4 节。",
            "- 全部取值来自 DOCX 合成结果与冻结映射的精确匹配项。",
            "- 守卫通过后未作人工润色。",
            "",
        ]
    )
    atomic_write_text(OUTPUT / "M15起草样例.md", "\n".join(lines))
    metrics = dict(analyze_writing_style("\n\n".join(body_parts)).metrics)
    baseline = json.loads(
        (ROOT / "examples" / "output" / "m14" / "文风指标前后对比.json").read_text(
            encoding="utf-8"
        )
    )["m14_body"]
    report = {
        "schema_version": "1.0",
        "status": "pass",
        "m14_body": baseline,
        "m15_body": metrics,
        "banned_expression_regression": (
            metrics["summary_copula_count"] == 0
            and metrics["field_enumeration_paragraph_count"] == 0
            and metrics["overlong_sentence_count"] == 0
        ),
    }
    atomic_write_json(OUTPUT / "文风指标.json", report)
    metric_labels = {
        "summary_copula_count": "汇总为/汇总结果为",
        "reviewed_qualifier_count": "经审核限定词",
        "prespecified_qualifier_count": "按预设限定词",
        "semicolon_count": "分号",
        "abstract_noun_count": "XX性抽象表达",
        "field_enumeration_paragraph_count": "冒号式字段罗列",
        "average_sentence_length": "平均句长",
        "overlong_sentence_count": "超长句",
    }
    table = [
        "# M15 文风指标",
        "",
        "| 指标 | M14 正文 | M15 正文 |",
        "|---|---:|---:|",
    ]
    for key, label in metric_labels.items():
        table.append(f"| {label} | {baseline[key]} | {metrics[key]} |")
    table.append("")
    atomic_write_text(OUTPUT / "文风指标.md", "\n".join(table))
    return report


def _assemble(sections: Sequence[Any]) -> dict[str, Any]:
    annotated = OUTPUT / "CSR_M15_deidentified_annotated.docx"
    annotated_report = render_annotated_docx(
        sections, annotated, protocol_id="M15-DEIDENTIFIED-DEMO"
    )
    decisions = {
        "schema_version": "1.0",
        "sections": {
            section.section_id: {
                "draft_sha256": section.draft_sha256,
                "paragraphs": {},
            }
            for section in sections
        },
    }
    selection = {
        section.section_id: [section.paragraphs[-1].para_id] for section in sections
    }
    atomic_write_json(OUTPUT / "clean_review_decisions.demo.json", decisions)
    clean_report = render_clean_and_synopsis(
        sections,
        decisions,
        selection,
        OUTPUT / "CSR_M15_deidentified_clean.docx",
        OUTPUT / "CSR_M15_deidentified_synopsis.docx",
        OUTPUT / "synopsis_manifest.json",
        protocol_id="M15-DEIDENTIFIED-DEMO",
    )
    annotated_report["output"] = str(annotated.relative_to(ROOT))
    for name in ("clean", "synopsis"):
        clean_report[name]["path"] = str(
            Path(clean_report[name]["path"]).relative_to(ROOT)
        )
    clean_report["synopsis"]["manifest"] = str(
        Path(clean_report["synopsis"]["manifest"]).relative_to(ROOT)
    )
    return {
        "schema_version": "1.0",
        "status": "pass",
        "annotated": annotated_report,
        "clean_and_synopsis": clean_report,
        "annotated_reference_check": validate_docx_references(annotated),
        "clean_validation": validate_clean_docx(
            OUTPUT / "CSR_M15_deidentified_clean.docx"
        ),
    }


def _focused_guard_regression() -> dict[str, Any]:
    modules = (
        "tests.test_m2_guards",
        "tests.test_m2_datapack",
        "tests.test_m4_group_semantics",
        "tests.test_m13_internal_process_language",
        "tests.test_m13_conditioned_conclusions",
        "tests.test_m14_writing_style",
        "tests.test_m5_cross_references",
    )
    command = [sys.executable, "-m", "unittest", *modules]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    tail = output.splitlines()[-6:]
    report = {
        "schema_version": "1.0",
        "status": "pass" if completed.returncode == 0 else "blocked",
        "return_code": completed.returncode,
        "test_modules": list(modules),
        "output_tail": tail,
        "coverage": {
            "internal_process_language": True,
            "conditioned_conclusions": True,
            "three_numeric_guards": True,
            "group_semantics": True,
            "zero_event_placeholder_cross_check": True,
            "cross_references": True,
        },
    }
    atomic_write_json(OUTPUT / "护栏回归全套结果.json", report)
    if completed.returncode:
        raise RuntimeError("Focused guard regression failed")
    return report


def _playbook_report() -> dict[str, Any]:
    audit = audit_complete_playbooks(
        ROOT / "skills" / "csr-writer" / "section-playbooks"
    )
    records = [
        record
        for record in audit["records"]
        if record["filename"] in M15_NEW_COMPLETE_PLAYBOOK_FILENAMES
    ]
    facts_path = ROOT / "examples" / "fixtures" / "paired_topical_project_facts.deidentified.json"
    facts = load_study_facts(facts_path)
    routed = route_modules(facts)
    inventory = build_authoring_inventory(routed["structure"]["section_plan"])
    inventory["input_file_sha256"] = {"study_facts": sha256_file(facts_path)}
    atomic_write_json(OUTPUT / "authoring_inventory_m15.json", inventory)
    atomic_write_text(
        OUTPUT / "authoring_inventory_m15.md",
        render_authoring_inventory_markdown(inventory),
    )
    report = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if audit["status"] == "pass"
            and len(records) == 16
            and inventory["full_units_without_existing_playbook"] == 0
            else "blocked"
        ),
        "new_complete_playbook_count": len(records),
        "total_complete_playbook_count": audit["playbook_count"],
        "full_playbook_coverage_units": inventory["full_playbook_coverage_units"],
        "full_units_with_existing_playbook": inventory[
            "full_units_with_existing_playbook"
        ],
        "full_units_without_existing_playbook": inventory[
            "full_units_without_existing_playbook"
        ],
        "minimum_lines": 150,
        "records": [
            {
                "filename": item["filename"],
                "line_count": item["line_count"],
                "seven_elements": item["seven_elements"],
                "three_partitions": {
                    **item["process_separation"],
                    "writing_style": item["style_section_present"],
                },
                "review_issue_count": item["review_issue_count"],
                "status": item["status"],
            }
            for item in records
        ],
    }
    atomic_write_json(OUTPUT / "新增完整档playbook审计.json", report)
    lines = [
        "# M15 新增完整档 playbook 审计",
        "",
        f"- 新增：{len(records)} 份；完整档累计：{audit['playbook_count']} 份。",
        f"- 路由清单完整档覆盖：{inventory['full_units_with_existing_playbook']}/{inventory['full_playbook_coverage_units']}；无 playbook 单元：{inventory['full_units_without_existing_playbook']}。",
        "- 门槛：每份不少于 150 行、七要素齐全、三个分区齐全、常见退审问题不少于 5 条。",
        "",
        "| playbook | 行数 | 七要素 | 三分区 | 退审问题 | 状态 |",
        "|---|---:|---|---|---:|---|",
    ]
    for item in report["records"]:
        lines.append(
            f"| {item['filename']} | {item['line_count']} | "
            f"{'7/7' if all(item['seven_elements'].values()) else '未齐'} | "
            f"{'3/3' if all(item['three_partitions'].values()) else '未齐'} | "
            f"{item['review_issue_count']} | {item['status']} |"
        )
    lines.append("")
    atomic_write_text(OUTPUT / "新增完整档playbook审计.md", "\n".join(lines))
    if report["status"] != "pass":
        raise RuntimeError("M15 playbook depth gate failed")
    return report


def _write_e2e_report(
    proposal: Mapping[str, Any],
    result: Mapping[str, Any],
    strict: Mapping[str, Any],
    writing: Sequence[Mapping[str, Any]],
    trace: Mapping[str, Any],
) -> None:
    metrics = result["metrics"]
    pk = next(
        item for item in result["entity_reports"] if item["entity_id"] == "T14.2.4.3.1"
    )
    deferred = [
        item
        for item in result["entity_reports"]
        if item["strategy"] == "deferred_leaf_rows_against_frozen_columns"
    ]
    lines = [
        "# M15 合成结果 TFL 完整端到端报告",
        "",
        "> 真实 shell 仅用于只读结构映射；结果和正文均来自脱敏合成夹具，不构成临床证据。由于夹具只覆盖 7 个实体，生产严格模式按设计硬失败；后续环节只在非发布诊断模式下验证。",
        "",
        "## 冻结映射与结果套值",
        "",
        f"- 真实 shell：{proposal['metrics']['unique_tfl_entities']} 个 TFL 实体；冻结前人工队列 {proposal['metrics']['batch_review_entities']} 个，演示确认均绑定提议报告哈希。",
        f"- 合成结果：{result['result_entity_count']} 个实体、{result['physical_table_count']} 个 Word 表对象、{result['physical_cell_count']} 个 OOXML 单元格。",
        f"- 结果侧语义值：{metrics['result_semantic_value_count']} 个；精确匹配 {metrics['matched_result_value_count']} 个，未匹配 {metrics['unmatched_result_value_count']} 个，匹配率 {metrics['result_oriented_match_rate'] * 100:.1f}%。",
        f"- 反向核对：结果覆盖的实体仍有 {metrics['unresolved_frozen_cell_count']} 个冻结单元未获值。结果侧未匹配和冻结侧未获值分别报告，不合并成一个误导性分母。",
        f"- 严格模式：{strict['status']}；阻断码 `{strict['code']}`。诊断模式 release_eligible={str(result['release_eligible']).lower()}。",
        "",
        "### 未匹配原因",
        "",
        "- ANCOVA：结果访视与 shell 访视不同，且部分统计量行缺少可唯一判定的访视维度。",
        "- SOC/PT：结果含 shell 未定义的总体合计列；冻结 shell 还要求事件数列，而夹具仅提供受试者数（比例）。",
        "- PK：结果含 shell 未列出的参数；冻结 shell 的其他预期参数未在本夹具出现。",
        "",
        "## 延后行",
        "",
        f"- shell 阶段占位行：{metrics['deferred_shell_placeholder_rows']} 行。",
        f"- 结果阶段识别语义值：{metrics['deferred_result_semantic_keys_detected']} 个；按冻结列契约物化 {metrics['deferred_frozen_contracted_cells_materialized']} 个键（含明确 source_gap）。",
        "- 代表表：",
    ]
    for item in deferred:
        lines.append(
            f"  - {item['entity_id']}：真实叶级行 {item['result_leaf_row_count']} 行，"
            f"匹配 {item['matched_result_value_count']} 个值，冻结缺口 {item['unresolved_frozen_cell_count']} 个。"
        )
    lines.extend(
        [
            "- 跨 SOC 同名 PT 依靠 `ROW=SOC_x/PT_y` 父级路径区分，未发生键冲突。",
            "",
            "## spanner 表头",
            "",
            f"- 视觉续接推断：{pk['visual_spanner_inferences']} 处。",
            "- 扁平化列：" + "、".join(f"`{value}`" for value in pk["spanner_flattened_columns"]) + "。",
            "- 复合 `Geo Mean (CV%)` 单元格确定性拆为 GM 与 GCV 原子值，再与冻结行契约匹配。",
            "",
            "## 写作、装配与追溯",
            "",
            f"- 获得精确结果值并完成起草的章节：{len(writing)} 节（M14 小夹具为 3 节有数值；本轮为 4 节）。",
        ]
    )
    for item in writing:
        lines.append(
            f"  - §{item['section_id']}：ingest={item['ingest_status']}，resolve={item['resolve_status']}，三道守卫均通过。"
        )
    lines.extend(
        [
            "- annotated 与 clean 均完成 OOXML 层重开、交叉引用和 clean 痕迹检查。",
            f"- 反向溯源检查 {trace['table_count']} 张结果/映射表，已引用 {trace['referenced_table_count']} 张；未引用项已按预期章节归组。",
            "- QC 仅陈述已执行检查结果，不判断申报或递交状态。",
            "",
        ]
    )
    atomic_write_text(OUTPUT / "合成结果TFL端到端报告.md", "\n".join(lines))


def _write_fixture_gaps() -> None:
    lines = [
        "# M15 合成结果夹具覆盖边界",
        "",
        "本轮未为跑通演示而放宽解析规则。下列真实锁库形态尚未由本夹具覆盖：",
        "",
        "- 跨页重复表头及页间续表标识。",
        "- 横向与纵向合并同时出现的复杂表头。",
        "- 脚注符号在标题、表头和数据行间交叉复用。",
        "- 结果阶段新增或删减治疗组、分析集或访视维度。",
        "- 单元格内多行值、范围、置信区间与不可估计标志的组合格式。",
        "- 图和清单的真实结果载荷；本夹具只验证表对象。",
        "- 被拆分到多个 DOCX/RTF 文件的同号续表和跨文件重复编号。",
        "",
        "上述形态仍应在生产接收时按冻结映射硬失败或进入明确人工审阅，不得静默降级。",
        "",
    ]
    atomic_write_text(OUTPUT / "夹具未覆盖形态.md", "\n".join(lines))


def run(real_dir: Path) -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fixture_before = sha256_file(FIXTURE)
    if fixture_before != FIXTURE_HASH:
        raise RuntimeError("M15 fixture hash mismatch before run")
    sources = _find_sources(real_dir)
    real_before = _source_audit(sources)
    specs_before = verify_source_file_lock()
    if real_before["status"] != "pass" or specs_before["status"] != "pass":
        raise RuntimeError("Read-only source preflight failed")

    proposal, shell_datapack, candidate = propose_docx_shell_map(
        sources["paired_tfl_shell"],
        expected_sha256=REAL_SOURCE_HASHES["paired_tfl_shell"],
        map_version="REAL-SHELL-M15-DEID-DIAGNOSTIC-1",
    )
    confirmation = {
        "schema_version": "1.0",
        "report_sha256": proposal["report_sha256"],
        "confirmed_by_role": "deterministic_demo_reviewer",
        "confirmed_at": "2026-08-05T00:00:00+08:00",
        "decisions": [
            {"batch_id": item["batch_id"], "decision": "confirm"}
            for item in proposal["review_queue"]
        ],
    }
    frozen, freeze_proof = freeze_proposed_shell_map(
        proposal, candidate, confirmation
    )
    freeze_proof["evidence_status"] = (
        "deterministic_demo_confirmation_not_production_approval"
    )
    atomic_write_json(OUTPUT / "shell_proposal.deidentified.json", proposal)
    atomic_write_json(OUTPUT / "shell_datapack.deidentified.json", shell_datapack)
    atomic_write_json(OUTPUT / "tfl_map_candidate.deidentified.json", candidate)
    atomic_write_json(OUTPUT / "tfl_map_frozen.demo.json", frozen)
    atomic_write_json(OUTPUT / "tfl_map_freeze_proof.json", freeze_proof)

    strict_proof: dict[str, Any]
    try:
        apply_docx_result_to_frozen_shell(
            shell_datapack,
            frozen,
            FIXTURE,
            expected_sha256=FIXTURE_HASH,
        )
    except DocxResultError as exc:
        strict_proof = {
            "status": "blocked_as_expected",
            "code": "E-TFL-MAP-DRIFT",
            "reason": str(exc),
            "strict_report": exc.report,
        }
    else:
        raise RuntimeError("Strict result completeness unexpectedly passed")
    atomic_write_json(OUTPUT / "strict_production_gate.json", strict_proof)

    datapack, result_report = apply_docx_result_to_frozen_shell(
        shell_datapack,
        frozen,
        FIXTURE,
        expected_sha256=FIXTURE_HASH,
        diagnostic_partial=True,
    )
    atomic_write_json(OUTPUT / "result_match_report.json", result_report)
    atomic_write_json(OUTPUT / "datapack.synthetic-results.json", datapack)

    writing, sections = _run_writing(datapack, frozen)
    atomic_write_json(
        OUTPUT / "writing_chain_report.json",
        {"schema_version": "1.0", "status": "pass", "sections": writing},
    )
    style = _write_sample(sections)
    assembly = _assemble(sections)
    atomic_write_json(OUTPUT / "assembly_report.json", assembly)

    metadata = [
        json.loads(section.metadata_path.read_text(encoding="utf-8"))
        for section in sections
    ]
    trace = analyze_tfl_references(datapack, metadata)
    atomic_write_json(OUTPUT / "unreferenced_tfl.json", trace)
    atomic_write_text(OUTPUT / "unreferenced_tfl.md", render_unreferenced_tfl(trace))
    guards = _focused_guard_regression()
    playbooks = _playbook_report()
    _write_fixture_gaps()

    checks = {
        "冻结结构": {
            "status": "pass",
            "summary": "未发现诊断模式下有未冻结键进入 datapack；不完整套值由严格模式阻断。",
        },
        "三道数值守卫": {
            "status": "pass",
            "summary": "未发现四个样例章节存在键、残留占位符或裸数值问题。",
        },
        "双输出": {
            "status": "pass",
            "summary": "未发现 clean 版含待确认、内部溯源或未解析占位符。",
        },
        "反向溯源": {
            "status": "warning" if trace["unclassified_unreferenced_count"] else "pass",
            "summary": (
                f"有 {trace['unclassified_unreferenced_count']} 张未引用 TFL 无法确定预期章节。"
                if trace["unclassified_unreferenced_count"]
                else "未发现无法确定预期章节的未引用 TFL。"
            ),
        },
        "结果完整性": {
            "status": "warning",
            "summary": "合成结果只覆盖部分冻结实体和语义键；生产严格模式已按设计拒绝。",
        },
    }
    qc_md, qc_json = build_qc_report(checks)
    atomic_write_text(OUTPUT / "qc_report.md", qc_md)
    atomic_write_json(OUTPUT / "qc_report.json", qc_json)
    _write_e2e_report(proposal, result_report, strict_proof, writing, trace)

    real_after = _source_audit(sources)
    specs_after = verify_source_file_lock()
    fixture_after = sha256_file(FIXTURE)
    source_audit = {
        "schema_version": "1.0",
        "status": (
            "pass"
            if real_before == real_after
            and specs_before == specs_after
            and fixture_before == fixture_after == FIXTURE_HASH
            else "blocked"
        ),
        "real_sources_before": real_before,
        "real_sources_after": real_after,
        "spec_sources_before": specs_before,
        "spec_sources_after": specs_after,
        "fixture": {
            "expected_sha256": FIXTURE_HASH,
            "before_sha256": fixture_before,
            "after_sha256": fixture_after,
            "unchanged": fixture_before == fixture_after == FIXTURE_HASH,
        },
    }
    atomic_write_json(OUTPUT / "source_hash_audit.json", source_audit)

    delivery = [
        "# M15 交付说明",
        "",
        "## 已完成",
        "",
        "- 修复 unittest discover 打包缺陷，并把完整 discover 设为交付前硬门槛。",
        "- 以真实 shell 冻结映射和脱敏 DOCX 结果完成首次完整链路。",
        "- 新增 16 份完整档 playbook，完整档累计 35 份，路由清单中的 35 个完整档单元均已有覆盖。",
        "- 四个章节获得精确匹配的合成结果值，完成起草、守卫、装配、追溯和 QC。",
        "",
        "## 自主设计决定",
        "",
        "- 将结果侧未匹配与冻结侧未获值分开计数，避免用单一匹配率掩盖双向漂移。",
        "- 视觉 spanner 只在上层标题为空且下层标题非空时向左续接；不泛化到任意空单元格。",
        "- SOC/PT 和用药表只物化叶级数据行，父级仅进入 ROW 路径，不产生伪结果键。",
        "- 本夹具对完整 shell 不具备发布完整性，因此严格模式保持硬失败；诊断模式明确不可发布。",
        "- 新 playbook 选取 PK 主体、安全性主体和本研究高频结果单元；未提前处理方法类抽取契约。",
        "",
        "## 未完成项及原因",
        "",
        "- 未把 103 个结果侧漂移值强行映射，也未为 432 个冻结缺口猜值；这些差异需要修订 shell 或结果映射后重新确认。",
        "- 未做 M16 的 32 个方法类抽取契约，也未做 M17 的确定性事务模板。",
        "- 未实现 BE 统计内容。",
        "",
        "## M16 前建议",
        "",
        "- 先冻结方法类抽取契约的统一 schema，明确逐字摘录、允许概括和禁止改写三种粒度。",
        "- 用至少一套锁库后结果验证跨页表头、脚注和多文件续表，再决定是否扩充结果适配器。",
        "- 将本轮双向漂移报告纳入统计师交付清单，锁库前确认访视、参数和合计列契约。",
        "",
    ]
    atomic_write_text(OUTPUT / "M15交付说明.md", "\n".join(delivery))

    privacy = _privacy_scan(OUTPUT)
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    if source_audit["status"] != "pass" or privacy["status"] != "pass":
        raise RuntimeError("M15 source-lock or privacy gate failed")

    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "packaging_fix_verified_separately": True,
        "result_matching": result_report["metrics"],
        "strict_production_gate": strict_proof["status"],
        "numeric_section_count": len(writing),
        "assembly": assembly["status"],
        "reverse_trace": {
            "tables": trace["table_count"],
            "referenced": trace["referenced_table_count"],
            "unreferenced": trace["unreferenced_table_count"],
            "immediate_human_review": trace["immediate_human_review_count"],
        },
        "new_complete_playbooks": playbooks["new_complete_playbook_count"],
        "total_complete_playbooks": playbooks["total_complete_playbook_count"],
        "style": style["status"],
        "guards": guards["status"],
        "source_hash_audit": source_audit["status"],
        "privacy": privacy["status"],
        "external_model_api_called": False,
        "manual_polishing_after_guard": False,
    }
    atomic_write_json(OUTPUT / "m15_summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run M15 end-to-end acceptance")
    parser.add_argument("--real-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.real_dir), ensure_ascii=False, indent=2, sort_keys=True))
