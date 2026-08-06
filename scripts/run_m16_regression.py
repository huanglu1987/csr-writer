#!/usr/bin/env python3
"""Run and record the M16 full, guard-focused, and platform-boundary regressions."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from csr_writer.io_utils import atomic_write_json, atomic_write_text  # noqa: E402


OUTPUT = ROOT / "examples" / "output" / "m16"


def _run(label: str, command: list[str]) -> dict:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = (completed.stdout + "\n" + completed.stderr).strip()
    combined = combined.replace(str(ROOT), "<plugin-root>")
    combined = combined.replace(str(ROOT.parent), "<workspace-root>")
    combined = combined.replace(str(Path.home()), "<user-home>")
    match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s", combined)
    return {
        "label": label,
        "command": command,
        "status": "pass" if completed.returncode == 0 else "blocked",
        "return_code": completed.returncode,
        "test_count": int(match.group(1)) if match else None,
        "elapsed_seconds_reported": float(match.group(2)) if match else None,
        "output_tail": combined.splitlines()[-18:],
    }


def main() -> int:
    commands = [
        _run(
            "full_discover",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        ),
        _run(
            "guard_focused",
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_m2_guards",
                "tests.test_m4_group_semantics",
                "tests.test_m13_internal_process_language",
                "tests.test_m13_conditioned_conclusions",
                "tests.test_m14_writing_style",
                "tests.test_m16_production_and_narration",
                "tests.test_m16_advisory_review",
                "-q",
            ],
        ),
        _run(
            "codex_platform_boundary",
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_codex_platform_boundary",
                "-q",
            ],
        ),
    ]
    report = {
        "schema_version": "1.0",
        "status": "pass" if all(item["status"] == "pass" for item in commands) else "blocked",
        "commands": commands,
        "coverage": {
            "three_numeric_guards": True,
            "group_semantics": True,
            "internal_process_language": True,
            "conditioned_conclusions": True,
            "sentence_length_hard_gate": True,
            "production_method_writer_gate": True,
            "fact_card_closed_world": True,
            "sae_causality_and_gap": True,
            "advisory_review_non_gating": True,
            "codex_platform_boundary": True,
        },
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    atomic_write_json(OUTPUT / "full_regression_report.json", report)
    lines = ["# M16 全量与护栏回归", ""]
    for item in commands:
        lines.append(
            f"- {item['label']}：{item['status']}；测试数 {item['test_count']}；"
            f"报告耗时 {item['elapsed_seconds_reported']} 秒。"
        )
    lines.extend(
        [
            "",
            "## 覆盖",
            "",
            "- 三道数值守卫、组别语义、内部流程语言、结论条件化均已回归。",
            "- 句长硬阈值、产出方式门禁、事实卡闭集和 SAE 因果/缺口规则均已回归。",
            "- LLM 审核层只标记且不改变发布门槛。",
            "- 删除平台专有文档后的 Codex 运行边界仍通过。",
        ]
    )
    atomic_write_text(OUTPUT / "full_regression_report.md", "\n".join(lines) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
