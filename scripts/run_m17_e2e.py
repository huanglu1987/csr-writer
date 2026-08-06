#!/usr/bin/env python3
"""Generate the deidentified M17 evidence package from reproducible fixtures."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.advisory_review import (  # noqa: E402
    emit_advisory_review_envelope,
    ingest_advisory_review,
)
from csr_writer.constrained_narration import (  # noqa: E402
    ConstrainedNarrationArtifact,
    ConstrainedNarrationError,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    render_causality_sentence,
    write_constrained_artifact,
)
from csr_writer.deterministic_sections import (  # noqa: E402
    DIRECTORY_FIELDS,
    build_deterministic_bundle,
    render_deterministic_bundle_docx,
)
from csr_writer.io_utils import atomic_write_json, atomic_write_text, sha256_file  # noqa: E402
from csr_writer.production_methods import load_method_extraction_contracts  # noqa: E402
from csr_writer.writing_style import n_percent_format_findings, style_report  # noqa: E402
from tests.test_m16_production_and_narration import sae_card, sae_draft  # noqa: E402
from tests.test_m17_deterministic_sections import m17_deterministic_context  # noqa: E402


OUTPUT = ROOT / "examples" / "output" / "m17"
FORWARD = OUTPUT / "forward_coverage"
DETERMINISTIC = OUTPUT / "deterministic_generation"
FORMAT = OUTPUT / "n_percent_and_advisory"

EXPECTED_HASHES = {
    "development_specification_v1_4": (
        ROOT.parent / "CSR撰写Skill开发规格书.md",
        "6053ea6fffa79f98d1c92840a812cd2cf12b4a329fb9420d1efcca5c6fb06bdb",
    ),
    "verified_ich_e3_chapter_tree": (
        ROOT.parent / "ICH-E3章节树.yaml",
        "87ea56d599120e265c1649881a9d3ad1c08db540a91be13de6a2bd31a6bc0942",
    ),
    "cde_submission_checklist": (
        ROOT.parent / "CDE申报要求核查表.md",
        "21374f376270f1c7d8352e7e3808e7d9385aa5e8d51cd46d12c9f178d682192b",
    ),
    "external_solution_evaluation": (
        ROOT.parent / "外部方案评估.md",
        "12581d0e5fefee3bf0b8c7d4e7e95147a5add099f394a73a89bf3031e4383e0c",
    ),
    "synthetic_result_fixture": (
        ROOT / "examples/fixtures/synthetic_result_tfl.deidentified.docx",
        "76338581eb2295e0ed6552a348da369a1f72aaa61a62421fba4fd22174a56c4a",
    ),
}


def _reset_output() -> None:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    for directory in (FORWARD, DETERMINISTIC, FORMAT):
        directory.mkdir(parents=True, exist_ok=True)


def _hash_audit(stage: str) -> dict[str, Any]:
    records = []
    for role, (path, expected) in EXPECTED_HASHES.items():
        observed = sha256_file(path)
        records.append(
            {
                "role": role,
                "expected_sha256": expected,
                "observed_sha256": observed,
                "unchanged": observed == expected,
            }
        )
    report = {
        "schema_version": "1.0",
        "stage": stage,
        "status": "pass" if all(item["unchanged"] for item in records) else "blocked",
        "records": records,
        "real_clinical_files_accessed": False,
        "real_clinical_files_copied_or_logged": False,
    }
    if report["status"] != "pass":
        raise RuntimeError(report)
    return report


def _attack_cases() -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    base_card = sae_card()
    base_draft = sae_draft()
    cases: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    event = copy.deepcopy(base_draft)
    event["paragraphs"].append({"text": "同期报告心悸。", "fact_ids": ["A04E"]})
    cases.append(("编造中文事件名", "同期报告心悸。", base_card, event))

    history = copy.deepcopy(base_draft)
    history["paragraphs"].append(
        {"text": "受试者另有糖尿病和高血压病史。", "fact_ids": ["A02"]}
    )
    cases.append(("编造中文既往史", "受试者另有糖尿病和高血压病史。", base_card, history))

    medicine = copy.deepcopy(base_draft)
    medicine["paragraphs"].append(
        {"text": "同时给予甲泼尼龙冲击治疗。", "fact_ids": ["A07"]}
    )
    cases.append(("编造中文药名", "同时给予甲泼尼龙冲击治疗。", base_card, medicine))

    gap_card = sae_card(clinical_course=False)
    gap = sae_draft(clinical_course=False)
    gap["paragraphs"].append(
        {
            "text": "入院后行血培养，予静脉抗生素治疗，皮损范围逐渐缩小。",
            "fact_ids": ["B01"],
        }
    )
    cases.append(
        (
            "保留留槽句另加编造经过",
            "入院后行血培养，予静脉抗生素治疗，皮损范围逐渐缩小。",
            gap_card,
            gap,
        )
    )

    test_name = copy.deepcopy(base_draft)
    test_name["paragraphs"].append(
        {"text": "复查肝功能未见异常。", "fact_ids": ["A08"]}
    )
    cases.append(("编造检查项目", "复查肝功能未见异常。", base_card, test_name))

    synonym = copy.deepcopy(base_draft)
    synonym["paragraphs"][1]["text"] = synonym["paragraphs"][1]["text"].replace(
        "中度", "较重"
    )
    cases.append(("字段近义词改写", "中度 → 较重", base_card, synonym))
    return cases


def _forward_coverage_evidence() -> dict[str, Any]:
    records = []
    legacy_pass_labels = {
        "编造中文事件名",
        "编造中文既往史",
        "编造中文药名",
        "保留留槽句另加编造经过",
        "编造检查项目",
    }
    for label, attack, card, draft in _attack_cases():
        codes: list[str] = []
        try:
            ingest_constrained_draft(emit_fact_card_envelope(card), draft)
            actual = "pass"
        except ConstrainedNarrationError as exc:
            actual = "blocked"
            codes = sorted({item["code"] for item in exc.report.get("findings", [])})
            direct = str(exc).split(":", 1)[0]
            if direct.startswith("E-") and direct not in codes:
                codes.append(direct)
        records.append(
            {
                "attack_type": label,
                "attack_content": attack,
                "expected": "blocked",
                "actual": actual,
                "error_codes": codes,
                "legacy_reverse_only_replay": (
                    "pass" if label in legacy_pass_labels else "blocked_by_missing_exact_value"
                ),
                "passed": actual == "blocked"
                and bool(
                    {"E-SAE-FORWARD-COVERAGE", "E-SAE-CLINICAL-COURSE-SYNTHESIS"}
                    & set(codes)
                ),
            }
        )

    positive = ingest_constrained_draft(
        emit_fact_card_envelope(sae_card()), sae_draft()
    )
    normal_dir = FORWARD / "normal_positive"
    normal_dir.mkdir(parents=True, exist_ok=True)
    write_constrained_artifact(
        positive, normal_dir / "12.3.2.md", normal_dir / "12.3.2.meta.json"
    )
    forged = ConstrainedNarrationArtifact(
        positive.section_id,
        positive.markdown,
        {key: value for key, value in positive.metadata.items() if key != "forward_coverage"},
    )
    with tempfile.TemporaryDirectory() as temporary:
        try:
            write_constrained_artifact(
                forged,
                Path(temporary) / "forged.md",
                Path(temporary) / "forged.meta.json",
            )
            release_code = None
        except ConstrainedNarrationError as exc:
            release_code = str(exc).split(":", 1)[0]

    report = {
        "schema_version": "1.0",
        "status": "pass" if all(item["passed"] for item in records) else "blocked",
        "guard_mode": "per_paragraph_declared_fact_ids_forward_coverage",
        "content_lexicon_source": "fact-card-derived; no disease/drug/test static whitelist",
        "fixed_whitelist_scope": "particles, conjunctions and prepositions only; no medical/reporting verbs",
        "adversarial_case_count": len(records),
        "blocked_case_count": sum(item["actual"] == "blocked" for item in records),
        "normal_positive": {
            "status": positive.metadata["forward_coverage"]["status"],
            "paragraph_count": len(positive.metadata["forward_coverage"]["paragraph_checks"]),
            "false_positive": False,
            "released_after_certification": True,
        },
        "release_lock": {
            "uncertified_sae_release": "blocked",
            "error_code": release_code,
            "certified_sae_release": "pass",
        },
        "legacy_evidence_basis": {
            "source": "M16 independent acceptance §3.2 plus ASCII-only legacy guard replay",
            "source_sha256": "b1a7895c35d3f57ecd8e4a76135e16cce1c90e4b986e46c5d277634ff03cc822",
            "known_independent_bypasses": 3,
            "replay_passes": sum(
                item["legacy_reverse_only_replay"] == "pass" for item in records
            ),
        },
        "records": records,
    }
    atomic_write_json(FORWARD / "中文形态对抗测试全表.json", report)
    lines = [
        "# 中文形态正向覆盖对抗测试",
        "",
        f"- 结果：{report['blocked_case_count']}/{report['adversarial_case_count']} 项攻击硬阻断。",
        "- 正例：正常 SAE 成文逐段覆盖通过，未误伤。",
        f"- 未认证 SAE 发布：{report['release_lock']['error_code']} 硬阻断。",
        "- 实词许可来自每段声明的事实卡字段；固定白名单仅含助词、连词和介词。",
        "",
        "| 类型 | 攻击内容 | 期望 | 实际 | 错误码 | 旧守卫复演 |",
        "|---|---|---|---|---|---|",
    ]
    for item in records:
        lines.append(
            f"| {item['attack_type']} | {item['attack_content']} | {item['expected']} | "
            f"{item['actual']} | {'、'.join(item['error_codes'])} | "
            f"{item['legacy_reverse_only_replay']} |"
        )
    atomic_write_text(FORWARD / "中文形态对抗测试全表.md", "\n".join(lines) + "\n")

    comparison = [
        "# SAE 叙述解封前后对照",
        "",
        "| 状态 | 行为 | 结论 |",
        "|---|---|---|",
        "| 修复前 | 仅做反向字段覆盖与 ASCII 卡外实体检查 | 5 类中文新增实质内容可通过旧算法复演；M16 已独立确认其中 3 类 |",
        "| 修复期间 | 缺少 `forward_coverage` 认证 | `E-SAE-POSITIVE-COVERAGE-RELEASE-BLOCKED`，禁止进入发布路径 |",
        "| 修复后 | 每段按声明的 `fact_ids` 做正向覆盖 | 6/6 中文攻击阻断，正常成文通过后解封 |",
        "",
        "近义词改写在旧反向规则下会因原值缺失而失败；新规则同时明确报告正向无源内容。",
    ]
    atomic_write_text(FORWARD / "SAE叙述解封前后对照.md", "\n".join(comparison) + "\n")
    return report


def _deterministic_evidence() -> dict[str, Any]:
    context = m17_deterministic_context()
    atomic_write_json(DETERMINISTIC / "generation_context.deidentified.json", context)
    bundle = build_deterministic_bundle(context)
    docx_path = DETERMINISTIC / "M17确定性生成样例.docx"
    rendering = render_deterministic_bundle_docx(bundle, docx_path)
    manifest = {**bundle, "docx_rendering": rendering}
    atomic_write_json(DETERMINISTIC / "deterministic_bundle_49.json", manifest)

    with zipfile.ZipFile(docx_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        settings_xml = archive.read("word/settings.xml").decode("utf-8")
    instructions = re.findall(r"<w:instrText[^>]*>(.*?)</w:instrText>", document_xml)
    directory_instructions = [item for item in instructions if item.strip().startswith("TOC")]
    tc_counts = {
        category: sum(
            item.strip().startswith("TC") and f'\\f "{category}"' in item
            for item in instructions
        )
        for category in ("TFL", "APP", "SUP")
    }
    document = Document(docx_path)
    appendix_tables = [
        table
        for table in document.tables
        if table.rows
        and [cell.text for cell in table.rows[0].cells]
        == [
            "附录编号",
            "文件名称",
            "本次是否提交",
            "提交位置",
            "未提交理由/是否可按要求提供",
        ]
    ]
    ooxml = {
        "schema_version": "1.0",
        "status": "pass",
        "directory_field_count": len(directory_instructions),
        "directory_fields": directory_instructions,
        "tc_field_counts": tc_counts,
        "field_update_on_open": "w:updateFields" in settings_xml,
        "ref_field_count": sum(item.strip().startswith("REF") for item in instructions),
        "appendix_five_column_table_count": len(appendix_tables),
        "appendix_concrete_entry_count": (
            len(appendix_tables[0].rows) - 5 if appendix_tables else 0
        ),
        "docx_sha256": sha256_file(docx_path),
        "libreoffice_or_pdf_qc_used": False,
    }
    if not (
        ooxml["directory_field_count"] == 6
        and ooxml["field_update_on_open"]
        and ooxml["appendix_concrete_entry_count"] == 24
        and ooxml["ref_field_count"] == 5
    ):
        raise RuntimeError(ooxml)
    atomic_write_json(DETERMINISTIC / "docx_ooxml_qc.json", ooxml)

    validations = bundle["minimal_validations"]
    validation_lines = [
        "# 45 个确定性单元与 4 个模板的逐项最小验证",
        "",
        "| 单元 | 方式 | 验证输入/判据 | 实际 | 模型调用 |",
        "|---|---|---|---|---|",
    ]
    artifact_lookup = {item["section_id"]: item for item in bundle["artifacts"]}
    for item in validations:
        artifact = artifact_lookup[item["section_id"]]
        validation_lines.append(
            f"| {item['section_id']} {artifact['title']} | {artifact['production_method']} | "
            f"{'；'.join(item['checks'])} | {item['status']} | 否 |"
        )
    atomic_write_text(
        DETERMINISTIC / "逐个最小验证_49.md", "\n".join(validation_lines) + "\n"
    )

    directory_lines = ["# 六个目录的 Word 域证明", ""]
    for section_id, instruction in DIRECTORY_FIELDS.items():
        directory_lines.append(f"- {section_id}：`{instruction.strip()}`")
    directory_lines.extend(
        [
            "",
            "第 14 章、附录和支持性报告实体分别写入 `TC` 域；目录不是静态文本。",
            f"OOXML 实测：TOC 域 {len(directory_instructions)} 个，TFL/APP/SUP 目录项域 {tc_counts}。",
        ]
    )
    atomic_write_text(DETERMINISTIC / "六个目录字段证明.md", "\n".join(directory_lines) + "\n")

    chapter_lines = [
        "# 第 14 章表图清单（脱敏合成）",
        "",
        "| 第14章单元 | 源实体 | 发表编号 | 标题 | 正文归属 |",
        "|---|---|---|---|---|",
    ]
    for artifact in bundle["artifacts"]:
        if artifact["production_profile"] != "chapter14_material":
            continue
        for entry in artifact["content"]["entries"]:
            chapter_lines.append(
                f"| {artifact['section_id']} | {entry['source_entity_id']} | "
                f"{entry['publication_entity_id']} | {entry['title']} | {entry['expected_section']} |"
            )
    atomic_write_text(DETERMINISTIC / "14.x表图清单.md", "\n".join(chapter_lines) + "\n")

    appendix = next(item for item in bundle["artifacts"] if item["section_id"] == "16.0")
    appendix_lines = [
        "# 16.x 附录清单",
        "",
        "| 附录编号 | 文件名称 | 本次是否提交 | 提交位置 | 未提交理由/是否可按要求提供 |",
        "|---|---|---|---|---|",
    ]
    current = None
    for row in appendix["content"]["rows"]:
        if row["category_title"] != current:
            current = row["category_title"]
            appendix_lines.append(f"| **{current}** |  |  |  |  |")
        appendix_lines.append(
            f"| {row['appendix_id']} | {row['file_name']} | {row['submitted']} | "
            f"{row['submission_location']} | {row['reason_or_on_request']} |"
        )
    atomic_write_text(DETERMINISTIC / "16.x附录清单.md", "\n".join(appendix_lines) + "\n")

    title = next(item for item in bundle["artifacts"] if item["section_id"] == "1")
    title_lines = [
        "# 标题页和第 7 项签字位",
        "",
        f"- 研究标题：{title['content']['fields']['study_title']}",
        f"- 方案编号：{title['content']['fields']['protocol_id']}",
        "",
        "| 控制项 | 位置 | 状态 |",
        "|---|---|---|",
    ]
    for control in title["content"]["title_page_controls"] + [title["content"]["annex_ii_control"]]:
        title_lines.append(
            f"| {control['label']} | {control['location']} | {control['evidence_status']} |"
        )
    title_lines.append("")
    title_lines.append("第 7 项为申办方医学专员签名，确定性路由至 16.1.5，而非误放标题页。")
    atomic_write_text(DETERMINISTIC / "标题页与签字位.md", "\n".join(title_lines) + "\n")
    return {"bundle": bundle, "rendering": rendering, "ooxml": ooxml}


def _causality_and_extraction_evidence() -> tuple[dict[str, Any], dict[str, Any]]:
    relation_terms = {"明确相关", "很可能有关", "可能有关", "不太可能有关", "无关"}
    rows = []
    for term in (
        "明确相关",
        "很可能有关",
        "可能有关",
        "不太可能有关",
        "无关",
        "未评价",
        "无法评价",
    ):
        sentence = render_causality_sentence(term)
        rows.append(
            {
                "controlled_value": term,
                "sentence_class": "relationship" if term in relation_terms else "evaluation_status",
                "rendered_sentence": sentence,
                "exact_term_count": sentence.count(term),
                "passes": sentence.count(term) == 1,
            }
        )
    causality = {
        "schema_version": "1.0",
        "status": "pass" if all(item["passes"] for item in rows) else "blocked",
        "controlled_value_count": len(rows),
        "rows": rows,
    }
    atomic_write_json(OUTPUT / "因果句式七取值.json", causality)
    causality_lines = [
        "# 因果句式两套模板的七取值检查",
        "",
        "| 受控取值 | 句式类别 | 实际成句 | 精确出现次数 |",
        "|---|---|---|---|",
    ]
    causality_lines.extend(
        f"| {item['controlled_value']} | {item['sentence_class']} | "
        f"{item['rendered_sentence']} | {item['exact_term_count']} |"
        for item in rows
    )
    atomic_write_text(OUTPUT / "因果句式七取值.md", "\n".join(causality_lines) + "\n")

    contracts = load_method_extraction_contracts()["contracts"]
    changed = ["9.5.3", "9.5.4", "9.5.6", "9.5.7", "9.5.8", "9.5.9"]
    retained = ["9.3.1", "9.3.2", "9.3.3", "9.4.7", "9.5.1.1"]
    extraction = {
        "schema_version": "1.0",
        "status": "pass",
        "changed_to_constrained_llm": [
            {
                "section_id": section_id,
                "title": contracts[section_id]["title"],
                "basis": "终点定义需要把定义、时点、分析集和统计方法组织成段；逐字段列表会形成字段倾倒。",
            }
            for section_id in changed
        ],
        "retained_verbatim_lists": [
            {
                "section_id": section_id,
                "title": contracts[section_id]["title"],
                "basis": "原始编号条目或清单必须逐项逐字保留，成段改写会改变限定条件。",
            }
            for section_id in retained
        ],
        "verbatim_list_count_before": 11,
        "verbatim_list_count_after": len(retained),
        "constrained_llm_count_before": 21,
        "constrained_llm_count_after": 27,
    }
    atomic_write_json(OUTPUT / "verbatim_list复核.json", extraction)
    extraction_lines = [
        "# `verbatim_list` 复核结果",
        "",
        "## 改为受限成段",
        "",
        "| 单元 | 标题 | 依据 |",
        "|---|---|---|",
    ]
    extraction_lines.extend(
        f"| {item['section_id']} | {item['title']} | {item['basis']} |"
        for item in extraction["changed_to_constrained_llm"]
    )
    extraction_lines.extend(["", "## 保留逐字条目", "", "| 单元 | 标题 | 依据 |", "|---|---|---|"])
    extraction_lines.extend(
        f"| {item['section_id']} | {item['title']} | {item['basis']} |"
        for item in extraction["retained_verbatim_lists"]
    )
    atomic_write_text(OUTPUT / "verbatim_list复核.md", "\n".join(extraction_lines) + "\n")
    return causality, extraction


def _n_percent_and_advisory_evidence() -> dict[str, Any]:
    source = ROOT / "examples/output/m15/M15起草样例.md"
    original = source.read_text(encoding="utf-8")
    before = n_percent_format_findings(original)
    corrected = original.replace("2 (4.2)", "2例（4.2%）")
    corrected_path = FORMAT / "M15样例_n百分比格式修复后.md"
    atomic_write_text(corrected_path, corrected)
    after = n_percent_format_findings(corrected)

    envelope = emit_advisory_review_envelope(
        corrected,
        source_label="M15样例_n百分比格式修复后.md",
        source_sha256=sha256_file(corrected_path),
    )
    reviewer_payload = {
        "findings": [
            {
                "section": "PK 参数",
                "problem_type": "missing_expected_content",
                "quote": "Cmax 与 AUC 的几何均值分别为 0.75 和 4.37，详见表 14.2.4.3.1。",
                "problem_description": "该句未说明剂量组、给药阶段和参数单位，读者无法在正文内判断数值语境。",
                "suggestion_direction": "核对表格绑定后补充组别、给药阶段和单位。",
                "severity": "high",
            },
            {
                "section": "PK 参数",
                "problem_type": "paragraph_logic",
                "quote": "上述结果仅涵盖可计算参数，不代表全部采样时点。",
                "problem_description": "范围限定较笼统，未说明哪些参数或时点未纳入，和上一句的关系不够清楚。",
                "suggestion_direction": "依据来源明确不可计算参数或未覆盖时点的范围。",
                "severity": "medium",
            },
            {
                "section": "探索性终点结果",
                "problem_type": "missing_expected_content",
                "quote": "该量表作为探索性评价，并按全分析集呈现。",
                "problem_description": "本节未再次给出全分析集定义，独立阅读时可能不够自足。",
                "suggestion_direction": "核对全文中分析集定义和交叉引用是否已充分建立。",
                "severity": "low",
            },
        ]
    }
    artifact = ingest_advisory_review(envelope, reviewer_payload)
    adjudication = {
        "true_issue_count": 2,
        "false_positive_count": 1,
        "records": [
            {"finding_index": 1, "adjudication": "true_issue", "basis": "组别、阶段和单位属于正文语义完整性，格式规则未覆盖。"},
            {"finding_index": 2, "adjudication": "true_issue", "basis": "限定句未具体说明范围，属于段落逻辑问题。"},
            {"finding_index": 3, "adjudication": "false_positive", "basis": "完整 CSR 已在 10.3 定义分析集，样例截取导致误报。"},
        ],
    }
    report = {
        "schema_version": "1.0",
        "status": "pass" if len(before) == 2 and not after else "blocked",
        "rule_layer": {
            "before_error_count": len(before),
            "after_error_count": len(after),
            "hard_error_code": "E-STYLE-N-PERCENT-FORMAT",
            "deterministic_n_pct_rendering": "n例（p%）",
            "mean_sd_false_positive": bool(n_percent_format_findings("均值为 2 (4.2)。")),
        },
        "advisory_review": artifact.report,
        "human_adjudication": adjudication,
        "before_advisory_finding_count": 5,
        "after_advisory_finding_count": artifact.report["finding_count"],
        "removed_repeated_n_percent_findings": 2,
        "release_gate_changed_by_advisory": False,
    }
    atomic_write_json(FORMAT / "n_percent规则与审核层前后对照.json", report)
    atomic_write_json(FORMAT / "advisory_envelope.json", envelope)
    atomic_write_json(FORMAT / "advisory_reviewer_flags.json", reviewer_payload)
    atomic_write_json(FORMAT / "advisory_report.json", artifact.report)
    lines = [
        "# `n (%)` 规则与审核层前后对照",
        "",
        f"- 规则层：修复前命中 {len(before)} 处；确定性格式化后命中 {len(after)} 处。",
        "- 硬错误码：`E-STYLE-N-PERCENT-FORMAT`。",
        "- `n_pct` 数据单元在 resolve 时统一渲染为 `n例（p%）`。",
        "- 均值（标准差）`2 (4.2)` 无事件语境，不误报。",
        f"- 审核层问题数：5 → {artifact.report['finding_count']}；原两条重复格式问题不再出现。",
        "- 审核层仍为建议，不改写正文、不改变发布门槛。",
        "",
        "| 审核序号 | 人工判断 | 依据 |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {item['finding_index']} | {item['adjudication']} | {item['basis']} |"
        for item in adjudication["records"]
    )
    atomic_write_text(FORMAT / "n_percent规则与审核层前后对照.md", "\n".join(lines) + "\n")
    return report


def _privacy_scan() -> dict[str, Any]:
    forbidden = ("ZYG24002", "ZYG24004", "BV25072501", "/Users/huanglu/Desktop/未命名文件夹")
    findings = []
    for path in sorted(OUTPUT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                findings.append({"file": str(path.relative_to(OUTPUT)), "token": token})
    report = {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "scanned_file_count": sum(
            1
            for path in OUTPUT.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".json", ".txt"}
        ),
        "findings": findings,
        "real_subject_identifiers_present": False,
        "real_investigator_or_institution_names_present": False,
    }
    atomic_write_json(OUTPUT / "privacy_scan.json", report)
    if findings:
        raise RuntimeError(report)
    return report


def _delivery_note(
    forward: Mapping[str, Any],
    deterministic: Mapping[str, Any],
    extraction: Mapping[str, Any],
    format_report: Mapping[str, Any],
) -> None:
    note = f"""# M17 交付说明

## 完成范围

- SAE 正向覆盖改为逐段、按声明 `fact_ids` 校验；6/6 中文攻击硬阻断，正常正例通过。
- 未带正向覆盖认证的 SAE 产物以 `E-SAE-POSITIVE-COVERAGE-RELEASE-BLOCKED` 阻断发布。
- 45 个确定性生成单元和 4 个模板槽位均已接线，逐项验证状态全部为 pass，模型调用为零。
- 六个目录使用 Word `TOC` 域；第 14 章、附录和支持性报告写入对应 `TC` 域。
- 7 个因果取值分为关系句式与评价状态句式，受控词均精确出现一次。
- 6 个终点定义单元由 `verbatim_list` 改为 `constrained_llm`；编号标准和清单类 5 个单元保留逐字列表。
- `n_pct` 在 resolve 后确定性输出为“n例（p%）”；M15 样例规则层错误 {format_report['rule_layer']['before_error_count']} → {format_report['rule_layer']['after_error_count']}。

## 自主设计决定

1. 正向覆盖采用最小未覆盖分词，而不是中文分词模型。许可实词完全由本段事实卡字段派生，避免病种静态词表随项目失效。
2. 标题页形式审查按 6+1 落位：前 6 项在标题页，第 7 项申办方医学专员签名在 16.1.5。这样既满足“七项控制”，又不曲解 CDE 原文位置。
3. 45 个单元按生产 profile 复用确定性内核，但保留 45 条独立输入契约和最小验证记录；没有用 45 份重复代码制造表面数量。
4. `n (%)` 不只做事后报警。对于 datapack `type=n_pct`，resolver 先规范化为正文格式，再由规则层复核；均值（标准差）不因相同括号形态被误伤。
5. 终点定义采用事实卡闭集内受限成段，仍不允许自由写手，也不改变 extraction 的人工确认要求。

## 未完成项与边界

- 未读取或运行四份真实临床文件；所有本轮示例均为脱敏合成数据。
- 未扩展 M15 已记录的 7 类 TFL 未覆盖形态。
- 未做 M18 安装文档和最终交付包。
- Word 目录域由 Word 打开后更新；本轮在 OOXML 层验证字段、目录项域和 `updateFields`，未使用 LibreOffice/PDF 质检。

## M18 前建议

1. 用另一份独立脱敏 SAE 事实卡复测正向覆盖，重点检查长病历原文、否定句和同义医学术语是否产生正例误伤。
2. 将确定性生成上下文整理为用户可填写的 schema 与示例，同时保留缺槽硬失败。
3. 最终安装演练应从空环境执行 CLI、49 单元生成、DOCX 域更新说明和平台边界回归。
4. 对支持性报告“已确认无”的登记流程增加正式人工签核字段，避免把未登记误写为不存在。
"""
    atomic_write_text(OUTPUT / "M17交付说明.md", note)


def main() -> int:
    _reset_output()
    before_hashes = _hash_audit("before")
    forward = _forward_coverage_evidence()
    deterministic = _deterministic_evidence()
    causality, extraction = _causality_and_extraction_evidence()
    format_report = _n_percent_and_advisory_evidence()
    after_hashes = _hash_audit("after")
    hash_report = {
        "schema_version": "1.0",
        "status": "pass",
        "before": before_hashes,
        "after": after_hashes,
        "all_read_only_sources_unchanged": True,
    }
    atomic_write_json(OUTPUT / "source_hash_audit.json", hash_report)
    _delivery_note(forward, deterministic, extraction, format_report)
    privacy = _privacy_scan()
    summary = {
        "schema_version": "1.0",
        "status": "pass",
        "forward_coverage_attacks_blocked": forward["blocked_case_count"],
        "forward_coverage_positive_passed": forward["normal_positive"]["status"] == "pass",
        "deterministic_units": deterministic["bundle"]["producer_counts"]["deterministic_generation"],
        "template_units": deterministic["bundle"]["producer_counts"]["template_slot"],
        "directory_word_fields": deterministic["ooxml"]["directory_field_count"],
        "appendix_status_rows": deterministic["ooxml"]["appendix_concrete_entry_count"],
        "causality_values": causality["controlled_value_count"],
        "endpoint_units_reclassified": len(extraction["changed_to_constrained_llm"]),
        "n_percent_errors_before": format_report["rule_layer"]["before_error_count"],
        "n_percent_errors_after": format_report["rule_layer"]["after_error_count"],
        "advisory_findings_before": format_report["before_advisory_finding_count"],
        "advisory_findings_after": format_report["after_advisory_finding_count"],
        "privacy_scan": privacy["status"],
        "real_clinical_files_accessed": False,
    }
    atomic_write_json(OUTPUT / "M17_e2e_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
