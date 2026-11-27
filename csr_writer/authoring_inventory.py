"""Deterministic authoring-depth inventory for a routed CSR skeleton."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .project_skeleton import DIRECTORY_TITLES, _section_sort_key
from .production_methods import (
    audit_authoring_unit_coverage,
    load_production_method_registry,
    production_assignment,
)
from .section_writer import PLAYBOOK_FILENAMES
from .title_overrides import load_title_overrides, localized_title


LIGHTWEIGHT_RESULT_SUFFIXES = {
    "11.4.3",
    "11.4.6",
    "12.2.2",
    "12.2.4",
    "12.3.1",
    "12.4.1",
}


# Complete M12 references that are intentionally not exposed as isolated
# section-writer contracts yet.  Section 13 must remain a downstream derivation
# from confirmed body text; the other entries need binding-role contracts before
# they can safely move from guidance into the executable writer registry.
COMPLETE_REFERENCE_PLAYBOOKS = {
    "11.3": ("s11-3-treatment-compliance.md", "complete_reference"),
    "12.3.1.1": (
        "s12-3-1-deaths-serious-significant-listings.md",
        "shared_complete_reference",
    ),
    "12.3.1.2": (
        "s12-3-1-deaths-serious-significant-listings.md",
        "shared_complete_reference",
    ),
    "12.3.1.3": (
        "s12-3-1-deaths-serious-significant-listings.md",
        "shared_complete_reference",
    ),
    "12.3.2": ("s12-3-2-sae-narratives-boundary.md", "human_boundary_reference"),
    "12.3.3": ("s12-3-3-serious-event-analysis.md", "complete_reference"),
    "12.4.2.1": (
        "s12-4-2-laboratory-evaluation.md",
        "shared_complete_reference",
    ),
    "12.4.2.2": (
        "s12-4-2-laboratory-evaluation.md",
        "shared_complete_reference",
    ),
    "12.4.2.3": (
        "s12-4-2-laboratory-evaluation.md",
        "shared_complete_reference",
    ),
    "13": ("s13-discussion-overall-conclusions.md", "derived_from_confirmed_body"),
}


def _existing_playbook(section_id: str) -> tuple[str | None, str]:
    if section_id == "13":
        return COMPLETE_REFERENCE_PLAYBOOKS[section_id]
    if section_id in PLAYBOOK_FILENAMES:
        return PLAYBOOK_FILENAMES[section_id], "exact_writer"
    if section_id in COMPLETE_REFERENCE_PLAYBOOKS:
        return COMPLETE_REFERENCE_PLAYBOOKS[section_id]
    candidates = [
        (candidate, filename)
        for candidate, filename in PLAYBOOK_FILENAMES.items()
        if candidate[0].isdigit()
        and section_id.startswith(candidate + ".")
    ]
    if not candidates:
        return None, "none"
    _candidate, filename = max(candidates, key=lambda item: len(item[0]))
    return filename, "shared_branch_reference"


def _classification(section_id: str, *, is_leaf: bool) -> dict[str, Any]:
    chapter = section_id.split(".", 1)[0]
    if not is_leaf:
        return {
            "type": "容器",
            "reason": "仅组织下级标题，本身不形成独立起草单元。",
            "playbook_depth": "无需独立 playbook",
            "estimated_lines": 0,
            "ai_savings_percent": 0,
        }
    if chapter in {"11", "12", "13"}:
        lightweight = section_id in LIGHTWEIGHT_RESULT_SUFFIXES
        if lightweight:
            return {
                "type": "结果类",
                "reason": "以个体列表或表格索引为主，内容由确定性装配承担。",
                "playbook_depth": "轻量",
                "estimated_lines": 25,
                "ai_savings_percent": 85,
            }
        conclusion_ids = {"11.4.7", "11.5.10", "11.6.7", "12.7"}
        savings = 35 if section_id in conclusion_ids or chapter == "13" else 65
        return {
            "type": "结果类",
            "reason": "需要从 TFL 提炼结果并受数值、语义红线和医学解释边界约束。",
            "playbook_depth": "完整",
            "estimated_lines": 200,
            "ai_savings_percent": savings,
        }
    if chapter in {"9", "10"}:
        automatic = section_id.startswith("10.3.")
        return {
            "type": "方法类",
            "reason": (
                "分析集名称和定义可从已确认 facts/SAP 确定性生成。"
                if automatic
                else "主要任务是忠实转述方案/SAP，重点约束遗漏和推断。"
            ),
            "playbook_depth": "中等",
            "estimated_lines": 70,
            "ai_savings_percent": 90 if automatic else 60,
        }
    if chapter == "14":
        return {
            "type": "事务类",
            "reason": "第 14 章由已确认发表映射和表图装配确定性生成。",
            "playbook_depth": "轻量模板/自动生成",
            "estimated_lines": 15,
            "ai_savings_percent": 95,
        }
    if chapter in {"15", "16"} or chapter in {str(value) for value in range(1, 9)}:
        return {
            "type": "事务类",
            "reason": "签名、目录、伦理声明、参考文献或附件状态以模板和登记表为主。",
            "playbook_depth": "轻量模板/自动生成",
            "estimated_lines": 20,
            "ai_savings_percent": 80,
        }
    return {
        "type": "事务类",
        "reason": "内容以确定性目录或登记信息为主。",
        "playbook_depth": "轻量模板/自动生成",
        "estimated_lines": 20,
        "ai_savings_percent": 75,
    }


def build_authoring_inventory(
    section_plan: Mapping[str, Any],
    *,
    title_overrides: Mapping[str, Any] | None = None,
    production_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    overrides = title_overrides or load_title_overrides()
    official = list(section_plan["official_sections"])
    extensions = list(section_plan["formal_extensions"])
    title_by_id = {
        str(item["section_id"]): str(item.get("render_title", item["title"]))
        for item in official
        if item.get("retained")
    }
    title_by_id.update(
        {
            str(item["section_id"]): str(item["title"])
            for item in extensions
            if item.get("retained")
        }
    )
    title_by_id.update(DIRECTORY_TITLES)
    applied_title_overrides: list[dict[str, str]] = []
    for section_id, title in list(title_by_id.items()):
        localized, record = localized_title(section_id, title, overrides)
        title_by_id[section_id] = localized
        if record is not None:
            applied_title_overrides.append(record)
    section_ids = sorted(title_by_id, key=_section_sort_key)
    registry = production_registry or load_production_method_registry()
    # Active leaves outside legacy production profiles can receive verified internal
    # draft tables, method excerpts, or pending text; this grants no free-text writer.
    if production_registry is None:
        assignments = dict(registry["assignments"])
        for sid in section_ids:
            if sid in assignments or any(child.startswith(sid+".") for child in section_ids):
                continue
            if not sid.startswith(("9.", "10.3.", "11.", "12.")):
                continue
            assignments[sid] = {
                "section_id":sid, "production_method":"extraction", "production_profile":"working_draft_sources",
                "narration":"source_bound", "production_reason":"仅允许内部稿采用原件、已核定统计表或标示缺口。",
                "source":{"declared":True,"items":["project_sources"],"missing_source_policy":"pending"},
                "production_spec":{"release_eligible":False},
            }
        registry = {**registry, "assignments":assignments}
    rows: list[dict[str, Any]] = []
    for section_id in section_ids:
        is_leaf = not any(
            candidate.startswith(section_id + ".") for candidate in section_ids
        )
        category = _classification(section_id, is_leaf=is_leaf)
        playbook, playbook_scope = _existing_playbook(section_id)
        ownership = production_assignment(section_id, registry) if is_leaf else None
        rows.append(
            {
                "section_id": section_id,
                "title": title_by_id[section_id],
                "is_authoring_unit": is_leaf,
                **category,
                "existing_playbook": playbook,
                "existing_playbook_scope": playbook_scope,
                "production_method": (
                    ownership["production_method"] if ownership is not None else None
                ),
                "production_profile": (
                    ownership["production_profile"] if ownership is not None else None
                ),
                "production_reason": (
                    ownership["production_reason"] if ownership is not None else None
                ),
                "narration": ownership["narration"] if ownership is not None else None,
                "source": ownership["source"] if ownership is not None else None,
                "production_spec": (
                    ownership["production_spec"] if ownership is not None else None
                ),
            }
        )
    authoring_units = [item for item in rows if item["is_authoring_unit"]]
    ownership_audit = audit_authoring_unit_coverage(
        [item["section_id"] for item in authoring_units], registry, routed_subset=True
    )
    if ownership_audit["status"] != "pass":
        raise ValueError(f"Production ownership registry does not match route: {ownership_audit}")
    type_counts = Counter(item["type"] for item in authoring_units)
    depth_counts = Counter(item["playbook_depth"] for item in authoring_units)
    full_units = [item for item in authoring_units if item["playbook_depth"] == "完整"]
    existing_full = [item for item in full_units if item["existing_playbook"]]
    exact_writer_full = [
        item
        for item in full_units
        if item["existing_playbook_scope"] == "exact_writer"
    ]
    return {
        "schema_version": "2.0",
        "status": "pass",
        "counting_rule": (
            "每个可见标题均列入清单；只有没有可见子节的叶节点计为独立起草单元，"
            "容器标题不虚增 playbook 数。"
        ),
        "visible_heading_count": len(rows),
        "authoring_unit_count": len(authoring_units),
        "container_count": len(rows) - len(authoring_units),
        "title_overrides": {
            "config_sha256": overrides.get("config_sha256"),
            "structure_sha256": overrides.get("structure_sha256"),
            "applied": applied_title_overrides,
        },
        "type_counts": dict(sorted(type_counts.items())),
        "depth_counts": dict(sorted(depth_counts.items())),
        "production_method_counts": ownership_audit["production_method_counts"],
        "production_method_audit": ownership_audit,
        "full_playbook_coverage_units": len(full_units),
        "full_units_with_existing_playbook": len(existing_full),
        "full_units_with_exact_writer": len(exact_writer_full),
        "full_units_without_existing_playbook": len(full_units) - len(existing_full),
        "distinct_existing_playbook_files": sorted(
            {item["existing_playbook"] for item in rows if item["existing_playbook"]}
        ),
        "rows": rows,
    }


def render_authoring_inventory_markdown(inventory: Mapping[str, Any]) -> str:
    lines = [
        "# CSR 章节写作分层清单",
        "",
        f"> 计数口径：{inventory['counting_rule']}",
        "",
        "## 汇总",
        "",
        f"- 可见标题：{inventory['visible_heading_count']}；独立起草单元：{inventory['authoring_unit_count']}；容器标题：{inventory['container_count']}。",
        f"- 完整 playbook 覆盖单元：**{inventory['full_playbook_coverage_units']}**；其中已有共享或精确 playbook 可复用 {inventory['full_units_with_existing_playbook']}，已精确接入写手 {inventory['full_units_with_exact_writer']}，尚无相关 playbook {inventory['full_units_without_existing_playbook']}。",
        f"- 类型：{inventory['type_counts']}。",
        f"- 深度：{inventory['depth_counts']}。",
        f"- 产出方式：{inventory['production_method_counts']}。",
        "",
        "## 逐节清单",
        "",
        "| 节 | 标题 | 类型 | 产出方式 | 叙述方式 | 源声明 | 产出理由 | 具体规格 | 已有 playbook / 接入范围 | 预估行数 | AI 可省力比例 |",
        "|---|---|---|---|---|---|---|---|---|---:|---:|",
    ]
    for item in inventory["rows"]:
        lines.append(
            "| {section_id} | {title} | {type} / {playbook_depth} | {method} | {narration} | {source} | {production_reason} | {production_spec} | {playbook} | {lines} | {saving}% |".format(
                section_id=item["section_id"],
                title=str(item["title"]).replace("|", "\\|"),
                type=item["type"],
                playbook_depth=item["playbook_depth"],
                reason=str(item["reason"]).replace("|", "\\|"),
                method=item["production_method"] or "—",
                narration=item["narration"] or "—",
                source=(
                    str(item["source"]).replace("|", "\\|")
                    if item["source"]
                    else "—"
                ),
                production_reason=(
                    str(item["production_reason"]).replace("|", "\\|")
                    if item["production_reason"]
                    else "—"
                ),
                production_spec=(
                    str(item["production_spec"]).replace("|", "\\|")
                    if item["production_spec"]
                    else "—"
                ),
                playbook=(
                    f"{item['existing_playbook']} / {item['existing_playbook_scope']}"
                    if item["existing_playbook"]
                    else "—"
                ),
                lines=item["estimated_lines"],
                saving=item["ai_savings_percent"],
            )
        )
    lines.append("")
    return "\n".join(lines)
