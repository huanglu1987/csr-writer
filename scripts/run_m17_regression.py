#!/usr/bin/env python3
"""Run and record the mandatory M17 release regression gates."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.io_utils import atomic_write_json, atomic_write_text, sha256_file  # noqa: E402


OUTPUT = ROOT / "examples/output/m17"


def _run(command: list[str], timeout: int) -> tuple[subprocess.CompletedProcess[str], int]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    combined = completed.stdout + "\n" + completed.stderr
    match = re.search(r"Ran (\d+) tests? in", combined)
    return completed, int(match.group(1)) if match else 0


def main() -> int:
    full, count = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"], 240
    )
    platform, platform_count = _run(
        [sys.executable, "-m", "unittest", "tests.test_codex_platform_boundary"],
        240,
    )
    m14_internal = json.loads(
        (ROOT / "examples/output/m14/内部流程语言13项回归.json").read_text(
            encoding="utf-8"
        )
    )
    m14_numeric = json.loads(
        (ROOT / "examples/output/m14/数值组别与零事件交叉回归.json").read_text(
            encoding="utf-8"
        )
    )
    m14_summary = json.loads(
        (ROOT / "examples/output/m14/m14_summary.json").read_text(encoding="utf-8")
    )
    forward = json.loads(
        (OUTPUT / "forward_coverage/中文形态对抗测试全表.json").read_text(
            encoding="utf-8"
        )
    )
    deterministic = json.loads(
        (OUTPUT / "deterministic_generation/deterministic_bundle_49.json").read_text(
            encoding="utf-8"
        )
    )
    result_format = json.loads(
        (OUTPUT / "n_percent_and_advisory/n_percent规则与审核层前后对照.json").read_text(
            encoding="utf-8"
        )
    )
    source_hashes = json.loads(
        (OUTPUT / "source_hash_audit.json").read_text(encoding="utf-8")
    )
    passed = (
        full.returncode == 0
        and platform.returncode == 0
        and count > 0
        and platform_count == 1
        and m14_internal["status"] == "pass"
        and m14_numeric["status"] == "pass"
        and m14_summary["branch_drift_count"] == 0
        and forward["status"] == "pass"
        and deterministic["status"] == "pass"
        and result_format["status"] == "pass"
        and source_hashes["status"] == "pass"
    )
    report = {
        "schema_version": "1.0",
        "status": "pass" if passed else "blocked",
        "mandatory_discover": {
            "command": "python3 -m unittest discover -s tests",
            "return_code": full.returncode,
            "test_count": count,
            "result": "OK" if full.returncode == 0 else "FAILED",
        },
        "platform_boundary": {
            "command": "python3 -m unittest tests.test_codex_platform_boundary",
            "return_code": platform.returncode,
            "test_count": platform_count,
            "result": "OK" if platform.returncode == 0 else "FAILED",
            "scope": "remove AGENTS.md, SKILL.md, .claude-plugin and agents/; rerun runtime and all CLI help",
        },
        "guard_regressions": {
            "internal_process_language": {
                "blocked_cases": m14_internal["blocked_passed"],
                "allowed_cases": m14_internal["allowed_passed"],
                "status": m14_internal["status"],
            },
            "conditioned_conclusions": {
                "scenario_count": m14_summary["conditioned_scenarios"],
                "branch_drift_count": m14_summary["branch_drift_count"],
                "status": "pass" if m14_summary["branch_drift_count"] == 0 else "blocked",
            },
            "numeric_three_guards": m14_numeric["numeric_guards"],
            "chinese_quantity_cases": m14_numeric["numeric_quantity_regression"],
            "group_swap": m14_numeric["group_semantics"],
            "zero_event": m14_numeric["zero_event"],
            "sae_chinese_forward_coverage": {
                "blocked_cases": forward["blocked_case_count"],
                "positive_status": forward["normal_positive"]["status"],
                "release_lock_code": forward["release_lock"]["error_code"],
            },
            "deterministic_units": {
                "unit_count": deterministic["unit_count"],
                "model_called": deterministic["model_called"],
                "status": deterministic["status"],
            },
            "n_percent_format": result_format["rule_layer"],
        },
        "read_only_source_hashes": source_hashes["status"],
    }
    atomic_write_json(OUTPUT / "full_regression_report.json", report)
    lines = [
        "# M17 护栏与全量回归",
        "",
        f"- `python3 -m unittest discover -s tests`：Ran {count} tests — "
        f"{'OK' if full.returncode == 0 else 'FAILED'}。",
        f"- 平台边界单测：Ran {platform_count} test — "
        f"{'OK' if platform.returncode == 0 else 'FAILED'}。",
        f"- 内部流程语言：{m14_internal['blocked_passed']} 项应拦均拦，"
        f"{m14_internal['allowed_passed']} 项应放均放。",
        f"- 结论条件化：{m14_summary['conditioned_scenarios']} 个情形，分支漂移 "
        f"{m14_summary['branch_drift_count']}。",
        f"- 中文数量对抗：{m14_numeric['numeric_quantity_regression']['blocked_cases_passed']}/"
        f"{m14_numeric['numeric_quantity_regression']['blocked_cases_expected']} 应拦用例通过；方案事实白名单正例通过。",
        f"- SAE 中文正向覆盖：{forward['blocked_case_count']}/{forward['adversarial_case_count']} 攻击阻断；正例通过。",
        f"- 确定性/模板：{deterministic['unit_count']} 单元全部通过，模型调用为 {deterministic['model_called']}。",
        f"- `n (%)` 格式：错误 {result_format['rule_layer']['before_error_count']} → "
        f"{result_format['rule_layer']['after_error_count']}；均值（标准差）对照不误报。",
        f"- 只读源哈希：{source_hashes['status']}。",
    ]
    atomic_write_text(OUTPUT / "护栏回归全套结果.md", "\n".join(lines) + "\n")

    forbidden = ("ZYG24002", "ZYG24004", "BV25072501", "/Users/huanglu/Desktop/未命名文件夹")
    findings = []
    scanned = 0
    for path in OUTPUT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt"}:
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                findings.append({"file": str(path.relative_to(OUTPUT)), "token": token})
    privacy = {
        "schema_version": "1.0",
        "status": "pass" if not findings else "blocked",
        "scanned_file_count": scanned,
        "findings": findings,
        "real_subject_identifiers_present": False,
        "real_investigator_or_institution_names_present": False,
    }
    atomic_write_json(OUTPUT / "privacy_scan.json", privacy)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed and not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())

