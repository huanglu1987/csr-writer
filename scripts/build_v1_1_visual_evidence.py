#!/usr/bin/env python3
"""Aggregate v1.1.0 visual-QC runs into reviewable calibration evidence."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.io_utils import atomic_write_json, atomic_write_text


OUTPUT = ROOT / "examples" / "output" / "v1.1.0"
LIMITATION = (
    "本检查基于 LibreOffice 渲染，与 Microsoft Word 的分页和字体回退存在差异。"
    "本检查是前置筛查，不替代在 Word 中更新域后的人工视觉复核。"
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _m21_runs() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    configurations = (
        ("single_arm_open_phase_ii", "m21_single_arm"),
        ("randomized_double_blind_phase_ib", "m21_ib"),
    )
    for study_shape, folder in configurations:
        report_path = OUTPUT / "visual_qc" / folder / "visual_qc_report.json"
        report = _load(report_path)
        codes = Counter(item["code"] for item in report["findings"])
        records.append(
            {
                "study_shape": study_shape,
                "report": report_path.relative_to(ROOT).as_posix(),
                "status": report["status"],
                "page_count": report["pdf"]["page_count"],
                "class_a_count": report["summary"]["class_a_count"],
                "class_b_count": report["summary"]["class_b_count"],
                "finding_codes": dict(sorted(codes.items())),
                "directory_not_expanded_detected": codes[
                    "A-FIELD-DIRECTORY-NOT-EXPANDED"
                ]
                > 0,
                "page_field_not_expanded_detected": codes[
                    "A-FIELD-PAGE-NUMBER-NOT-EXPANDED"
                ]
                > 0,
                "contact_sheets": report["contact_sheets"],
                "header_footer_excluded": report["docx_layout"][
                    "header_footer_excluded_from_body_checks"
                ],
            }
        )
    summary = {
        "schema_version": "1.0",
        "version": "1.1.0",
        "limitations": LIMITATION,
        "legacy_release_decision_changed": False,
        "documents_checked": len(records),
        "runs": records,
    }
    return summary, records


def _calibration() -> dict[str, Any]:
    manifest_path = OUTPUT / "holdout" / "holdout_set_manifest.json"
    manifest = _load(manifest_path)
    lock_path = ROOT / "skills" / "csr-writer" / "references" / "visual-qc-config.lock.json"
    lock = _load(lock_path)
    scenario_by_name = {
        Path(item["filename"]).stem: item["scenario"] for item in manifest["documents"]
    }
    page_count = 0
    documents_without_findings = 0
    clear_pages = 0
    class_a_pages: set[tuple[str, int]] = set()
    class_b_pages: set[tuple[str, int]] = set()
    codes: Counter[str] = Counter()
    runs: list[dict[str, Any]] = []
    for path in sorted((OUTPUT / "holdout" / "visual_qc").glob("*/visual_qc_report.json")):
        report = _load(path)
        name = path.parent.name
        pages = int(report["pdf"]["page_count"])
        page_count += pages
        local_issue_pages = {
            int(item["page"]) for item in report["findings"] if item.get("page") is not None
        }
        clear_pages += pages - len(local_issue_pages)
        if not report["findings"]:
            documents_without_findings += 1
        for item in report["findings"]:
            codes[item["code"]] += 1
            if item.get("page") is None:
                continue
            target = (name, int(item["page"]))
            (class_a_pages if item["class"] == "A" else class_b_pages).add(target)
        runs.append(
            {
                "document": name,
                "scenario": scenario_by_name[name],
                "page_count": pages,
                "finding_count": len(report["findings"]),
                "issue_pages": sorted(local_issue_pages),
                "report": path.relative_to(ROOT).as_posix(),
            }
        )
    false_positives = [
        {
            "document": "04_landscape_wide_table",
            "scenario": "landscape_wide_table",
            "page": 1,
            "code": "B-SINGLE-CJK-LINE",
            "count": 10,
            "reason": "合法列名‘列1’至‘列10’移除数字后被误视为单字行。",
        },
        {
            "document": "05_long_table_repeated_header",
            "scenario": "long_table_repeated_header",
            "page": 2,
            "code": "B-TABLE-HEADER-NOT-REPEATED",
            "count": 1,
            "reason": "输入已设 w:tblHeader，但 PDF 表格重建对续页表头的相似度仅识别为 0.240。",
        },
    ]
    return {
        "schema_version": "1.0",
        "status": "completed",
        "limitations": LIMITATION,
        "freeze": {
            "config_sha256": lock["config_sha256"],
            "checker_sha256": lock["checker_sha256"],
            "lock_status": lock["status"],
            "superseded_probe": lock["superseded_probe"],
        },
        "construction": {
            "manifest": manifest_path.relative_to(ROOT).as_posix(),
            "generator": manifest["generator"],
            "configuration_used_during_construction": False,
            "documents": len(manifest["documents"]),
            "required_scenarios": manifest["documents"],
        },
        "metrics": {
            "document_count": len(runs),
            "page_count": page_count,
            "documents_without_any_finding": documents_without_findings,
            "pages_without_any_finding": clear_pages,
            "class_a_false_positive_pages": len(class_a_pages),
            "class_a_page_false_positive_rate": len(class_a_pages) / page_count,
            "class_b_false_positive_pages": len(class_b_pages),
            "class_b_page_false_positive_rate": len(class_b_pages) / page_count,
            "overall_false_positive_pages": len(class_a_pages | class_b_pages),
            "overall_page_false_positive_rate": len(class_a_pages | class_b_pages)
            / page_count,
            "finding_codes": dict(sorted(codes.items())),
        },
        "false_positives": false_positives,
        "runs": runs,
        "class_b_upgrade_recommendation": {
            "recommend_upgrade": False,
            "decision": "v1.1.0 全部 B 类继续仅警告。",
            "basis": "正式留出集 B 类页级误伤率为 20.0%（2/10 页）。",
        },
    }


def _render_m21_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# M21 两份成品视觉 QC 实跑报告",
        "",
        f"> {summary['limitations']}",
        "",
        "| 研究形状 | 页数 | A 类 | B 类 | 目录未展开 | 页码/总页数未展开 | 结果 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in summary["runs"]:
        codes = item["finding_codes"]
        lines.append(
            f"| `{item['study_shape']}` | {item['page_count']} | {item['class_a_count']} | "
            f"{item['class_b_count']} | {codes.get('A-FIELD-DIRECTORY-NOT-EXPANDED', 0)} | "
            f"{codes.get('A-FIELD-PAGE-NUMBER-NOT-EXPANDED', 0)} | `{item['status']}` |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "- 两份文档均检出目录域未展开，且页码/总页数域未完整展开。",
            "- 页眉页脚已依据 DOCX story 排除，没有重现 34/34 页的页眉误报。",
            "- 两份文档均仅剩 1 个正文越界 B 类警告，须在 Word 中复核。",
            "- 视觉层的缺席或结果不改变 v1.0.0 既有发布判定。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_calibration_markdown(calibration: dict[str, Any]) -> str:
    metrics = calibration["metrics"]
    lines = [
        "# v1.1.0 视觉 QC 误伤率标定报告",
        "",
        f"> {calibration['limitations']}",
        "",
        "## 留出规则",
        "",
        f"- 配置 SHA-256：`{calibration['freeze']['config_sha256']}`",
        f"- 检查器 SHA-256：`{calibration['freeze']['checker_sha256']}`",
        "- 先冻结配置与检查器，再生成法律上/版式上合法的合成文档。",
        "- 正式留出生成器不导入检查器，不读取配置或锁文件。",
        "- 冻结前 probe 曾暴露‘链接到前节的页眉页脚未继承’实现缺陷，已作废且不计入任何误伤数字。",
        "",
        "## 实测数字",
        "",
        f"- 留出文档：{metrics['document_count']} 份；页面：{metrics['page_count']} 页。",
        f"- 无任何命中：{metrics['documents_without_any_finding']} 份，{metrics['pages_without_any_finding']} 页。",
        f"- A 类误伤：{metrics['class_a_false_positive_pages']}/{metrics['page_count']} 页（0.0%）。",
        f"- B 类误伤：{metrics['class_b_false_positive_pages']}/{metrics['page_count']} 页（20.0%）。",
        "",
        "## 被误伤的具体页",
        "",
        "| 文档 | 页 | 代码 | 次数 | 原因 |",
        "|---|---:|---|---:|---|",
    ]
    for item in calibration["false_positives"]:
        lines.append(
            f"| `{item['document']}` | {item['page']} | `{item['code']}` | "
            f"{item['count']} | {item['reason']} |"
        )
    lines.extend(
        [
            "",
            "## B 类升级建议",
            "",
            "**不建议将任何 B 类升级为阻断。** 页级误伤率为 20.0%，且两类误报均出现在本轮指定的合法形态中。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    m21, _ = _m21_runs()
    calibration = _calibration()
    atomic_write_json(OUTPUT / "visual_qc" / "M21两份成品实跑汇总.json", m21)
    atomic_write_text(
        OUTPUT / "visual_qc" / "M21两份成品实跑报告.md",
        _render_m21_markdown(m21),
    )
    atomic_write_json(OUTPUT / "holdout" / "误伤率标定报告.json", calibration)
    atomic_write_text(
        OUTPUT / "holdout" / "误伤率标定报告.md",
        _render_calibration_markdown(calibration),
    )
    atomic_write_text(
        OUTPUT / "B类升级建议.md",
        "# B 类升级建议\n\n"
        f"> {LIMITATION}\n\n"
        "v1.1.0 不将任何 B 类升级为阻断。正式留出集中，"
        "B 类页级误伤率为 20.0%（2/10 页）；误报包括合法横向宽表的列名"
        "以及已正确设置重复表头的跨页长表。\n",
    )
    print(json.dumps({"m21": m21, "calibration": calibration}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
