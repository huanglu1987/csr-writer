"""Unified M1-M21 CLI with an optional v1.1 visual preflight layer."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, TextIO

from .datapack import build_datapack_to_files
from .advisory_review import (
    emit_advisory_review_envelope,
    ingest_advisory_review,
    write_advisory_report,
)
from .constrained_narration import (
    ConstrainedNarrationError,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    render_verbatim_fact_card,
    write_constrained_artifact,
)
from .conditional_conclusions import resolve_conditional_conclusions
from .docx_extract import extract_docx_to_file
from .deterministic_sections import (
    build_deterministic_bundle,
    render_deterministic_bundle_docx,
)
from .errors import CSRWriterError, ConfigurationError
from .io_utils import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_text_bundle,
    load_data,
    load_json_unique,
    paths_alias,
    sha256_file,
)
from .cross_chapter import (
    apply_review_decisions,
    build_cross_chapter_report,
    derive_synopsis_manifest,
    load_sections_from_manifest,
    validate_analysis_set_consistency,
    validate_claim_ledger,
    verify_synopsis_copy,
)
from .cross_format_result import apply_rtf_values_to_shell
from .authoring_inventory import (
    build_authoring_inventory,
    render_authoring_inventory_markdown,
)
from .bidirectional_trace import validate_bidirectional_trace
from .group_semantics import validate_group_semantics
from .m4_assembly import (
    CleanOutputBlocked,
    load_review_decisions,
    render_annotated_docx,
    render_clean_and_synopsis,
)
from .m4_qc import build_qc_report
from .module_router import route_modules
from .paths import (
    DEFAULT_FORMAT_SPEC,
    DEFAULT_STRUCTURE,
    DEFAULT_TEMPLATE,
    DEFAULT_TITLE_OVERRIDES,
)
from .project_skeleton import (
    render_omitted_sections_markdown,
    render_project_annotated_skeleton,
    render_project_clean_skeleton,
)
from .protocol_fact_scan import (
    DEFAULT_SCAN_RULES,
    render_protocol_fact_scan,
    scan_protocol_facts,
)
from .qc_format import run_qc_to_file
from .render_docx import render_skeleton
from .resolve_placeholders import resolve
from .review_decisions import (
    apply_decision_template,
    build_decision_ledger,
    build_review_decision_queue,
    compare_review_versions,
    create_decision_record,
    export_decision_template,
    freeze_candidate_with_decisions,
    render_deferred_row_expectations,
    render_review_decision_queue,
    render_version_comparison,
)
from .section_writer import (
    AgentDraftGuardError,
    draft_section,
    emit_agent_envelope,
    ingest_agent_draft,
)
from .shell_mapping import freeze_proposed_shell_map, propose_docx_shell_map
from .study_facts import load_study_facts, render_fact_card
from .template import create_default_template
from .traceability import analyze_tfl_references, render_unreferenced_tfl
from .tfl_map import (
    TFLMapDriftError,
    build_tfl_map_candidate,
    check_result_against_frozen_map,
    freeze_tfl_map,
)
from .visual_qc import (
    DEFAULT_VISUAL_QC_CONFIG,
    DEFAULT_VISUAL_QC_LOCK,
    run_visual_qc,
)


def _layers(value: str) -> tuple[str, ...]:
    layers = tuple(item.strip() for item in value.split(",") if item.strip())
    if not layers:
        raise argparse.ArgumentTypeError("At least one layer is required")
    return layers


def _resolved_input(value: str) -> tuple[str, str]:
    key, separator, content = value.partition("=")
    if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not content.strip():
        raise argparse.ArgumentTypeError("resolved input must be key=non-empty-value")
    return key, content.strip()


def _validate_paths(
    inputs: dict[str, Path],
    outputs: dict[str, Path | None],
    *,
    scope: str,
) -> dict[str, Path]:
    concrete_outputs = {label: path for label, path in outputs.items() if path is not None}
    for output_label, output_path in concrete_outputs.items():
        for input_label, input_path in inputs.items():
            if paths_alias(output_path, input_path):
                raise ConfigurationError(
                    f"{scope} path collision: {output_label} aliases input {input_label}: {output_path}"
                )
    output_items = list(concrete_outputs.items())
    for index, (left_label, left_path) in enumerate(output_items):
        for right_label, right_path in output_items[index + 1 :]:
            if paths_alias(left_path, right_path):
                raise ConfigurationError(
                    f"{scope} path collision: {left_label} aliases {right_label}: {left_path}"
                )
    return concrete_outputs


def _validate_m2_paths(
    inputs: dict[str, Path], outputs: dict[str, Path | None]
) -> dict[str, Path]:
    return _validate_paths(inputs, outputs, scope="M2")


def _manifest_input_paths(
    manifest_path: Path,
) -> tuple[dict[str, Path], ConfigurationError | None]:
    manifest = load_data(manifest_path)
    inputs = {"manifest": manifest_path}
    if not isinstance(manifest, dict):
        return inputs, ConfigurationError("TFL manifest must be an object")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return inputs, ConfigurationError("TFL manifest sources must be a list")
    errors: list[str] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            errors.append(f"TFL manifest source {index} needs a path")
            continue
        inputs[f"source[{index}]"] = (manifest_path.parent / source["path"]).resolve()
    error = ConfigurationError("; ".join(errors)) if errors else None
    return inputs, error


def _archive_existing_outputs(
    outputs: dict[str, Path],
    *,
    reserved_paths: Iterable[Path] = (),
) -> dict[str, str]:
    for label, path in outputs.items():
        if path.is_dir():
            raise ConfigurationError(f"M2 {label} path is a directory: {path}")
    reserved = [*outputs.values(), *reserved_paths]
    moves: list[tuple[str, Path, Path]] = []
    for label, path in outputs.items():
        if not path.exists() and not path.is_symlink():
            continue
        suffix = 0
        while True:
            marker = ".previous" if suffix == 0 else f".previous.{suffix}"
            candidate = path.with_name(f".{path.name}{marker}")
            if (
                not candidate.exists()
                and not candidate.is_symlink()
                and not any(paths_alias(candidate, other) for other in reserved)
            ):
                break
            suffix += 1
        moves.append((label, path, candidate))
        reserved.append(candidate)

    completed: list[tuple[str, Path, Path]] = []
    try:
        for label, path, candidate in moves:
            path.replace(candidate)
            completed.append((label, path, candidate))
    except Exception:
        for _, original, archived in reversed(completed):
            archived.replace(original)
        raise
    return {label: str(archived.resolve()) for label, _, archived in moves}


def _print_archived_outputs(archived: dict[str, str], *, stream: TextIO = sys.stderr) -> None:
    for label, path in archived.items():
        print(f"archived previous {label}: {path}", file=stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csr-writer",
        description=(
            "CSR writer foundations: M1 skeleton, M2 deterministic numeric pipeline, "
            "M3 facts/routing/extraction, M4 consistency/QC, M5 real-shell workflows, "
            "M6 layout inheritance/usability gates, M7 deferred rows/Agent drafting, "
            "M8 reviewer decisions/version reuse, M9 fact-routed structural pruning, "
            "M10-M20 structure, authoring and guard closure, M21 unified composition, "
            "and optional v1.1 visual preflight"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    from . import composition_cli
    composition_cli.register(subparsers)
    draft_project = subparsers.add_parser("draft-project", help="从原始资料目录生成带来源的 CSR 内部审阅初稿")
    draft_project.add_argument("--input-dir", type=Path, required=True)
    draft_project.add_argument("--output-dir", type=Path, help="可选；默认在输入目录旁创建新的 CSR 初稿目录")
    draft_project.add_argument("--sap-reading",type=Path,help="智能体对有效 SAP 原句的结构化解读")
    draft_project.add_argument("--answers",type=Path,help="一次提交有来源、岗位及版本依据的负责人答复")
    draft_project.add_argument("--matches", type=Path, help="宿主智能体对陌生表题的有来源匹配答复")
    draft_project.add_argument("--plan", type=Path, help="同一资料快照的智能体工作计划")

    template = subparsers.add_parser("create-template", help="rebuild the default replaceable DOCX master")
    template.add_argument("--output", type=Path, default=DEFAULT_TEMPLATE)
    template.add_argument("--format-spec", type=Path, default=DEFAULT_FORMAT_SPEC)

    skeleton = subparsers.add_parser("render-skeleton", help="render a title-only ICH E3 skeleton DOCX")
    skeleton.add_argument("--structure", type=Path, default=DEFAULT_STRUCTURE)
    skeleton.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    skeleton.add_argument("--format-spec", type=Path, default=DEFAULT_FORMAT_SPEC)
    skeleton.add_argument("--output", type=Path, required=True)
    skeleton.add_argument("--manifest-output", type=Path)
    skeleton.add_argument("--layers", type=_layers, default=("ich-official",))
    skeleton.add_argument("--title-language", choices=("zh", "en", "bilingual"), default="zh")
    skeleton.add_argument("--header-text", default="临床试验总结报告骨架")
    skeleton.add_argument("--confidentiality", default="机密")

    project_skeleton = subparsers.add_parser(
        "render-project-skeleton",
        help="render fact-routed annotated and clean project skeletons",
    )
    project_skeleton.add_argument("--study-facts", type=Path, required=True)
    project_skeleton.add_argument(
        "--content-manifest",
        type=Path,
        help="optional JSON object with a sections list for the annotated skeleton",
    )
    project_skeleton.add_argument("--publication-reference-map", type=Path)
    project_skeleton.add_argument("--structure", type=Path, default=DEFAULT_STRUCTURE)
    project_skeleton.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    project_skeleton.add_argument("--format-spec", type=Path, default=DEFAULT_FORMAT_SPEC)
    project_skeleton.add_argument(
        "--title-overrides", type=Path, default=DEFAULT_TITLE_OVERRIDES
    )
    project_skeleton.add_argument("--annotated-output", type=Path, required=True)
    project_skeleton.add_argument("--clean-output", type=Path, required=True)
    project_skeleton.add_argument("--annotated-scaffold", type=Path, required=True)
    project_skeleton.add_argument("--clean-scaffold", type=Path, required=True)
    project_skeleton.add_argument("--report", type=Path, required=True)
    project_skeleton.add_argument("--omitted-sections", type=Path, required=True)
    project_skeleton.add_argument(
        "--omission-policy",
        choices=("remove", "retain_not_applicable"),
        default="remove",
    )

    qc = subparsers.add_parser("qc-format", help="run the fixed 21-item OOXML scaffold QC")
    qc.add_argument("--docx", type=Path, required=True)
    qc.add_argument("--structure", type=Path, default=DEFAULT_STRUCTURE)
    qc.add_argument("--format-spec", type=Path, default=DEFAULT_FORMAT_SPEC)
    qc.add_argument("--report", type=Path)
    qc.add_argument("--layers", type=_layers, default=("ich-official",))
    qc.add_argument("--title-language", choices=("zh", "en", "bilingual"), default="zh")

    visual_qc = subparsers.add_parser(
        "visual-qc",
        help="optionally render a DOCX and run A/B visual preflight checks",
    )
    visual_qc.add_argument("--docx", type=Path, required=True)
    visual_qc.add_argument("--work-dir", type=Path, required=True)
    visual_qc.add_argument("--json-report", type=Path, required=True)
    visual_qc.add_argument("--markdown-report", type=Path, required=True)
    visual_qc.add_argument("--config", type=Path, default=DEFAULT_VISUAL_QC_CONFIG)
    visual_qc.add_argument("--config-lock", type=Path, default=DEFAULT_VISUAL_QC_LOCK)
    visual_qc.add_argument(
        "--renderer",
        help="optional exact LibreOffice executable; when supplied it replaces candidate search",
    )

    datapack = subparsers.add_parser("build-datapack", help="parse reviewed RTF sources into datapack.json")
    datapack.add_argument("--manifest", type=Path, required=True)
    datapack.add_argument("--output", type=Path, required=True)
    datapack.add_argument("--parsed-output", type=Path)

    map_candidate = subparsers.add_parser(
        "tfl-map-candidate",
        help="build a value-free pre-lock mapping candidate from a TFL shell datapack",
    )
    map_candidate.add_argument("--shell-datapack", type=Path, required=True)
    map_candidate.add_argument("--route-config", type=Path, required=True)
    map_candidate.add_argument("--output", type=Path, required=True)

    map_freeze = subparsers.add_parser(
        "tfl-map-freeze",
        help="freeze an exactly hashed TFL mapping candidate after human confirmation",
    )
    map_freeze.add_argument("--candidate", type=Path, required=True)
    map_freeze.add_argument("--confirmation", type=Path, required=True)
    map_freeze.add_argument("--output", type=Path, required=True)

    map_apply = subparsers.add_parser(
        "tfl-map-apply",
        help="check post-lock result TFL keys against the frozen shell map",
    )
    map_apply.add_argument("--result-datapack", type=Path, required=True)
    map_apply.add_argument("--frozen-map", type=Path, required=True)
    map_apply.add_argument("--output", type=Path, required=True)

    shell_propose = subparsers.add_parser(
        "tfl-shell-propose",
        help="scan a read-only DOCX shell and write privacy-safe proposals plus a value-free candidate",
    )
    shell_propose.add_argument("--docx", type=Path, required=True)
    shell_propose.add_argument("--expected-sha256", required=True)
    shell_propose.add_argument("--map-version", default="REAL-SHELL-M6-DEID-1")
    shell_propose.add_argument(
        "--layout-reference-config",
        type=Path,
        help="optional configurable layout-reference regex/classifier JSON",
    )
    shell_propose.add_argument("--report", type=Path, required=True)
    shell_propose.add_argument("--shell-datapack-output", type=Path, required=True)
    shell_propose.add_argument("--candidate-output", type=Path, required=True)

    shell_freeze = subparsers.add_parser(
        "tfl-shell-freeze",
        help="freeze a real-shell candidate after every ambiguity batch is explicitly confirmed",
    )
    shell_freeze.add_argument("--report", type=Path, required=True)
    shell_freeze.add_argument("--candidate", type=Path, required=True)
    shell_freeze.add_argument("--batch-confirmation", type=Path, required=True)
    shell_freeze.add_argument("--output", type=Path, required=True)
    shell_freeze.add_argument("--proof-output", type=Path, required=True)

    review_queue = subparsers.add_parser(
        "tfl-review-queue",
        help="reorganise technical shell ambiguities into attributable human decisions",
    )
    review_queue.add_argument("--report", type=Path, required=True)
    review_queue.add_argument("--candidate", type=Path, required=True)
    review_queue.add_argument("--output", type=Path, required=True)
    review_queue.add_argument("--markdown-output", type=Path, required=True)
    review_queue.add_argument("--deferred-rows-output", type=Path, required=True)

    review_decide = subparsers.add_parser(
        "tfl-review-decide", help="create one hash-bound, attributable review decision"
    )
    review_decide.add_argument("--queue", type=Path, required=True)
    review_decide.add_argument("--group-id", required=True)
    review_decide.add_argument("--reviewer-role", required=True)
    review_decide.add_argument("--reviewer-identifier", required=True)
    review_decide.add_argument("--decided-at", required=True)
    review_decide.add_argument(
        "--outcome",
        choices=("confirm_default", "override_default", "defer"),
        required=True,
    )
    review_decide.add_argument(
        "--resolved-input", type=_resolved_input, action="append", default=[]
    )
    review_decide.add_argument(
        "--override-parameter", type=_resolved_input, action="append", default=[]
    )
    review_decide.add_argument("--override-reason")
    review_decide.add_argument("--decision-notes", default="")
    review_decide.add_argument("--template-sha256")
    review_decide.add_argument("--output", type=Path, required=True)

    review_ledger = subparsers.add_parser(
        "tfl-review-ledger", help="validate and bind one decision record per decision group"
    )
    review_ledger.add_argument("--queue", type=Path, required=True)
    review_ledger.add_argument("--record", type=Path, action="append", required=True)
    review_ledger.add_argument("--output", type=Path, required=True)

    review_freeze = subparsers.add_parser(
        "tfl-review-freeze", help="freeze a shell map only after the complete M8 decision ledger"
    )
    review_freeze.add_argument("--report", type=Path, required=True)
    review_freeze.add_argument("--candidate", type=Path, required=True)
    review_freeze.add_argument("--queue", type=Path, required=True)
    review_freeze.add_argument("--ledger", type=Path, required=True)
    review_freeze.add_argument("--output", type=Path, required=True)
    review_freeze.add_argument("--proof-output", type=Path, required=True)

    review_compare = subparsers.add_parser(
        "tfl-review-compare", help="reuse unchanged shell decisions and requeue only version drift"
    )
    review_compare.add_argument("--previous-queue", type=Path, required=True)
    review_compare.add_argument("--previous-ledger", type=Path, required=True)
    review_compare.add_argument("--current-queue", type=Path, required=True)
    review_compare.add_argument("--output", type=Path, required=True)
    review_compare.add_argument("--markdown-output", type=Path, required=True)

    template_export = subparsers.add_parser(
        "tfl-review-template-export", help="export reusable decision defaults without reviewer identity"
    )
    template_export.add_argument("--queue", type=Path, required=True)
    template_export.add_argument("--ledger", type=Path, required=True)
    template_export.add_argument("--template-id", required=True)
    template_export.add_argument("--output", type=Path, required=True)

    template_apply = subparsers.add_parser(
        "tfl-review-template-apply", help="propose imported defaults without silently confirming them"
    )
    template_apply.add_argument("--queue", type=Path, required=True)
    template_apply.add_argument("--template", type=Path, required=True)
    template_apply.add_argument("--output", type=Path, required=True)

    result_apply = subparsers.add_parser(
        "tfl-result-apply-rtf",
        help="apply one RTF result table to exact coordinates in a frozen DOCX-shell map",
    )
    result_apply.add_argument("--shell-datapack", type=Path, required=True)
    result_apply.add_argument("--frozen-map", type=Path, required=True)
    result_apply.add_argument("--entity-id", required=True)
    result_apply.add_argument("--rtf", type=Path, required=True)
    result_apply.add_argument("--output", type=Path, required=True)
    result_apply.add_argument("--report", type=Path, required=True)

    resolver = subparsers.add_parser("resolve", help="run Guards A/B/C and deterministically fill placeholders")
    resolver.add_argument("--draft", type=Path, required=True)
    resolver.add_argument("--datapack", type=Path, required=True)
    resolver.add_argument("--protocol-facts", type=Path, required=True)
    resolver.add_argument(
        "--section-metadata",
        type=Path,
        help="section metadata containing the reviewed binding_manifest for cross-references",
    )
    resolver.add_argument(
        "--frozen-tfl-map",
        type=Path,
        help="optional confirmed TFL map used to authorize non-value table/figure/listing references",
    )
    resolver.add_argument("--output", type=Path, required=True)
    resolver.add_argument("--report", type=Path, required=True)

    facts_card = subparsers.add_parser(
        "facts-card", help="validate frozen study_facts and render a human-review Markdown card"
    )
    facts_card.add_argument("--study-facts", type=Path, required=True)
    facts_card.add_argument("--output", type=Path, required=True)

    router = subparsers.add_parser(
        "route-modules", help="derive capability flags and the 47-module structure deterministically"
    )
    router.add_argument("--study-facts", type=Path, required=True)
    router.add_argument("--output", type=Path, required=True)

    inventory = subparsers.add_parser(
        "authoring-inventory",
        help="classify every routed CSR heading by authoring/playbook depth",
    )
    inventory.add_argument("--study-facts", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--markdown-output", type=Path, required=True)

    extractor = subparsers.add_parser(
        "extract-docx", help="extract accepted-view text and review findings at the OOXML layer"
    )
    extractor.add_argument("--docx", type=Path, required=True)
    extractor.add_argument("--output", type=Path, required=True)

    fact_scan = subparsers.add_parser(
        "scan-protocol-facts",
        help="flag protocol passages that conflict with false/empty study_facts",
    )
    fact_scan.add_argument("--docx", type=Path, required=True)
    fact_scan.add_argument("--study-facts", type=Path, required=True)
    fact_scan.add_argument("--rules", type=Path, default=DEFAULT_SCAN_RULES)
    fact_scan.add_argument("--expected-sha256")
    fact_scan.add_argument("--protocol-role", default="protocol")
    fact_scan.add_argument("--report", type=Path, required=True)
    fact_scan.add_argument("--markdown-output", type=Path, required=True)

    writer = subparsers.add_parser(
        "draft-section", help="draft one isolated M3 section from a reviewed narrow request"
    )
    writer.add_argument("--request", type=Path)
    writer.add_argument("--output-dir", type=Path)
    writer.add_argument(
        "--emit-envelope",
        type=Path,
        help="write a self-contained Agent evidence package instead of drafting",
    )
    writer.add_argument("--envelope", type=Path, help="previously emitted Agent envelope")
    writer.add_argument("--ingest-draft", type=Path, help="Agent-authored strict JSON paragraphs")
    writer.add_argument("--guard-report", type=Path, help="write pass/rejection evidence")
    writer.add_argument(
        "--prior-guard-report",
        type=Path,
        action="append",
        default=[],
        help="hash-chained rejection report from an earlier attempt",
    )

    fact_envelope = subparsers.add_parser(
        "fact-card-envelope",
        help="emit a hash-bound fact card for constrained Agent narration",
    )
    fact_envelope.add_argument("--fact-card", type=Path, required=True)
    fact_envelope.add_argument("--output", type=Path, required=True)

    fact_ingest = subparsers.add_parser(
        "fact-card-ingest",
        help="ingest constrained prose and prove every sentence against fact-card fields",
    )
    fact_ingest.add_argument("--envelope", type=Path, required=True)
    fact_ingest.add_argument("--draft", type=Path, required=True)
    fact_ingest.add_argument("--markdown-output", type=Path, required=True)
    fact_ingest.add_argument("--metadata-output", type=Path, required=True)
    fact_ingest.add_argument("--guard-report", type=Path, required=True)

    fact_verbatim = subparsers.add_parser(
        "fact-card-render-verbatim",
        help="render an extraction:verbatim_list fact card without an LLM",
    )
    fact_verbatim.add_argument("--fact-card", type=Path, required=True)
    fact_verbatim.add_argument("--markdown-output", type=Path, required=True)
    fact_verbatim.add_argument("--metadata-output", type=Path, required=True)

    deterministic = subparsers.add_parser(
        "generate-deterministic-sections",
        help="render all 48 deterministic and four slot-template units without an LLM",
    )
    deterministic.add_argument("--context", type=Path, required=True)
    deterministic.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    deterministic.add_argument("--output-docx", type=Path, required=True)
    deterministic.add_argument("--manifest-output", type=Path, required=True)

    advisory_emit = subparsers.add_parser(
        "advisory-review-envelope",
        help="emit an advisory-only independent CDE-review package",
    )
    advisory_emit.add_argument("--source", type=Path, required=True)
    advisory_emit.add_argument("--output", type=Path, required=True)

    advisory_ingest = subparsers.add_parser(
        "advisory-review-ingest",
        help="ingest review flags without changing prose or making a release decision",
    )
    advisory_ingest.add_argument("--envelope", type=Path, required=True)
    advisory_ingest.add_argument("--review", type=Path, required=True)
    advisory_ingest.add_argument("--output", type=Path, required=True)

    group_qc = subparsers.add_parser(
        "m4-group-semantics",
        help="validate treatment-group roles against reviewed semantic columns and 10.1/11.1 anchors",
    )
    group_qc.add_argument("--datapack", type=Path, required=True)
    group_qc.add_argument("--section-metadata", type=Path, required=True)
    group_qc.add_argument("--contract", type=Path, required=True)
    group_qc.add_argument("--output", type=Path, required=True)

    trace = subparsers.add_parser(
        "m4-unreferenced-tfl",
        help="write reverse-traceability evidence for result tables not referenced by any section",
    )
    trace.add_argument("--datapack", type=Path, required=True)
    trace.add_argument("--section-metadata", type=Path, required=True)
    trace.add_argument("--output", type=Path, required=True)
    trace.add_argument("--report", type=Path, required=True)

    trace_chain = subparsers.add_parser(
        "m4-bidirectional-trace",
        help="validate reviewed protocol-to-SAP-to-TFL-to-CSR paths in both directions",
    )
    trace_chain.add_argument("--manifest", type=Path, required=True)
    trace_chain.add_argument("--output", type=Path, required=True)

    cross = subparsers.add_parser(
        "m4-cross-check",
        help="verify derived Synopsis, analysis-set definitions, and controlled claim ledger",
    )
    cross.add_argument("--sections-manifest", type=Path, required=True)
    cross.add_argument("--review-decisions", type=Path, required=True)
    cross.add_argument("--synopsis-selection", type=Path, required=True)
    cross.add_argument("--analysis-set-contract", type=Path, required=True)
    cross.add_argument("--claim-ledger", type=Path, required=True)
    cross.add_argument("--output", type=Path, required=True)

    assembler = subparsers.add_parser(
        "m4-assemble",
        help="render annotated DOCX and gated clean/Synopsis DOCX outputs",
    )
    assembler.add_argument("--sections-manifest", type=Path, required=True)
    assembler.add_argument("--review-decisions", type=Path, required=True)
    assembler.add_argument("--synopsis-selection", type=Path, required=True)
    assembler.add_argument("--protocol-id", required=True)
    assembler.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    assembler.add_argument("--annotated-output", type=Path, required=True)
    assembler.add_argument("--clean-output", type=Path, required=True)
    assembler.add_argument("--synopsis-output", type=Path, required=True)
    assembler.add_argument("--synopsis-manifest-output", type=Path, required=True)
    assembler.add_argument("--report", type=Path, required=True)

    m4_qc = subparsers.add_parser(
        "m4-qc-report", help="render a bounded M4 QC report without regulatory-readiness claims"
    )
    m4_qc.add_argument("--checks", type=Path, required=True)
    m4_qc.add_argument("--output", type=Path, required=True)
    m4_qc.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    archived_outputs: dict[str, str] = {}
    try:
        if args.command in {"compose-csr","tfl-result-apply-docx","tfl-drift-decide"}:
            from . import composition_cli
            return composition_cli.run(args)
        if args.command == "draft-project":
            from .project_draft import draft_project

            report = draft_project(args.input_dir, args.output_dir, args.plan, matches_path=args.matches,answers_path=args.answers,sap_reading=args.sap_reading)
            print(f"已生成内部审阅初稿：{report['output_dir']}；覆盖情况 {report['coverage']}")
            return 0
        if args.command == "create-template":
            path = create_default_template(args.output, args.format_spec)
            print(f"created master: {path}")
            return 0
        if args.command == "render-skeleton":
            result = render_skeleton(
                args.structure,
                args.template,
                args.format_spec,
                args.output,
                args.layers,
                args.title_language,
                args.header_text,
                args.confidentiality,
                args.manifest_output,
            )
            print(f"rendered {len(result.headings)} headings: {result.output}")
            print(f"sha256: {result.output_sha256}")
            return 0
        if args.command == "render-project-skeleton":
            inputs = {
                "study facts": args.study_facts,
                "structure": args.structure,
                "template": args.template,
                "format spec": args.format_spec,
                "title overrides": args.title_overrides,
            }
            if args.content_manifest is not None:
                inputs["content manifest"] = args.content_manifest
            if args.publication_reference_map is not None:
                inputs["publication reference map"] = args.publication_reference_map
            outputs = {
                "annotated output": args.annotated_output,
                "clean output": args.clean_output,
                "annotated scaffold": args.annotated_scaffold,
                "clean scaffold": args.clean_scaffold,
                "report": args.report,
                "omitted sections": args.omitted_sections,
            }
            _validate_paths(inputs, outputs, scope="M9/M10 project skeleton")
            before = {label: sha256_file(path) for label, path in inputs.items()}
            facts = load_study_facts(args.study_facts)
            routing = route_modules(facts)
            content: list[dict] = []
            if args.content_manifest is not None:
                manifest = load_json_unique(args.content_manifest)
                if not isinstance(manifest, dict) or set(manifest) != {
                    "schema_version",
                    "sections",
                } or not isinstance(manifest["sections"], list):
                    raise ConfigurationError(
                        "project content manifest must contain only schema_version and sections"
                    )
                content = manifest["sections"]
            publication_map = (
                load_json_unique(args.publication_reference_map)
                if args.publication_reference_map is not None
                else None
            )
            annotated = render_project_annotated_skeleton(
                routing,
                content,
                args.annotated_output,
                scaffold_path=args.annotated_scaffold,
                structure_path=args.structure,
                template_path=args.template,
                format_spec_path=args.format_spec,
                omission_policy=args.omission_policy,
                publication_reference_map=publication_map,
                study_facts=facts,
                title_overrides_path=args.title_overrides,
            )
            clean = render_project_clean_skeleton(
                routing,
                args.clean_output,
                scaffold_path=args.clean_scaffold,
                structure_path=args.structure,
                template_path=args.template,
                format_spec_path=args.format_spec,
                omission_policy=args.omission_policy,
                publication_reference_map=publication_map,
                study_facts=facts,
                title_overrides_path=args.title_overrides,
            )
            after = {label: sha256_file(path) for label, path in inputs.items()}
            if before != after:
                raise ConfigurationError("project-skeleton inputs changed during rendering")
            report = {
                "schema_version": "1.0",
                "status": "pass",
                "input_file_sha256": before,
                "active_module_ids": routing["structure"]["active_module_ids"],
                "annotated": annotated,
                "clean": clean,
            }
            atomic_write_text_bundle(
                [
                    (
                        args.report,
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                    ),
                    (
                        args.omitted_sections,
                        render_omitted_sections_markdown(
                            routing["structure"]["section_plan"],
                            omission_policy=args.omission_policy,
                        ),
                    ),
                ]
            )
            print(
                f"rendered fact-routed annotated/clean skeletons; "
                f"omitted={annotated['omitted_official_heading_count']}: {args.report}"
            )
            return 0
        if args.command == "qc-format":
            report = run_qc_to_file(
                args.docx,
                args.format_spec,
                args.structure,
                args.layers,
                args.title_language,
                args.report,
            )
            print(report.format_text(), end="")
            return 0 if report.passed else 1
        if args.command == "visual-qc":
            report = run_visual_qc(
                docx_path=args.docx,
                work_dir=args.work_dir,
                json_report=args.json_report,
                markdown_report=args.markdown_report,
                config_path=args.config,
                lock_path=args.config_lock,
                renderer_override=args.renderer,
            )
            if report["status"] == "not_run":
                print(report["reason"]["message"])
                return 0
            print(
                f"visual preflight {report['status']}: "
                f"A={report['summary']['class_a_count']} "
                f"B={report['summary']['class_b_count']}: {args.markdown_report}"
            )
            return 1 if report["visual_gate_status"] == "blocked" else 0
        if args.command == "build-datapack":
            requested_outputs = {
                "datapack output": args.output,
                "parsed output": args.parsed_output,
            }
            known_outputs = _validate_m2_paths(
                {"manifest": args.manifest},
                requested_outputs,
            )
            try:
                inputs, manifest_error = _manifest_input_paths(args.manifest)
            except (CSRWriterError, OSError, ValueError, KeyError, TypeError):
                archived_outputs = _archive_existing_outputs(
                    known_outputs, reserved_paths=(args.manifest,)
                )
                raise
            outputs = _validate_m2_paths(
                inputs,
                requested_outputs,
            )
            if manifest_error is not None:
                archived_outputs = _archive_existing_outputs(
                    outputs, reserved_paths=inputs.values()
                )
                raise manifest_error
            archived_outputs = _archive_existing_outputs(
                outputs, reserved_paths=inputs.values()
            )
            datapack = build_datapack_to_files(args.manifest, args.output, args.parsed_output)
            print(f"built {len(datapack['tables'])} tables / {len(datapack['cells'])} cells: {args.output}")
            _print_archived_outputs(archived_outputs, stream=sys.stdout)
            return 0
        if args.command == "tfl-map-candidate":
            inputs = {
                "shell datapack": args.shell_datapack,
                "route config": args.route_config,
            }
            _validate_paths(inputs, {"candidate": args.output}, scope="TFL map")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            shell = load_json_unique(args.shell_datapack)
            routes = load_json_unique(args.route_config)
            candidate = build_tfl_map_candidate(shell, routes)
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("TFL shell mapping inputs changed during candidate build")
            candidate["input_file_sha256"] = before_hashes
            atomic_write_json(args.output, candidate)
            print(f"built TFL map candidate for {len(candidate['entities'])} entities: {args.output}")
            return 0
        if args.command == "tfl-map-freeze":
            inputs = {"candidate": args.candidate, "confirmation": args.confirmation}
            _validate_paths(inputs, {"frozen map": args.output}, scope="TFL map")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            candidate = load_json_unique(args.candidate)
            confirmation = load_json_unique(args.confirmation)
            # CLI file-hash provenance is not part of the candidate's signed payload.
            if isinstance(candidate, dict) and "input_file_sha256" in candidate:
                candidate = dict(candidate)
                candidate.pop("input_file_sha256")
            frozen = freeze_tfl_map(candidate, confirmation)
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("TFL map confirmation inputs changed during freeze")
            frozen["input_file_sha256"] = before_hashes
            atomic_write_json(args.output, frozen)
            print(f"froze confirmed TFL map {frozen['map_version']}: {args.output}")
            return 0
        if args.command == "tfl-map-apply":
            inputs = {"result datapack": args.result_datapack, "frozen map": args.frozen_map}
            _validate_paths(inputs, {"drift report": args.output}, scope="TFL map")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            result_datapack = load_json_unique(args.result_datapack)
            frozen = load_json_unique(args.frozen_map)
            if isinstance(frozen, dict) and "input_file_sha256" in frozen:
                frozen = dict(frozen)
                frozen.pop("input_file_sha256")
            try:
                report = check_result_against_frozen_map(result_datapack, frozen)
            except TFLMapDriftError as exc:
                report = dict(exc.report)
                report["input_file_sha256"] = before_hashes
                atomic_write_json(args.output, report)
                print(f"result TFL mapping drift blocked: {args.output}", file=sys.stderr)
                return 3
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("TFL map inputs changed during result comparison")
            report["input_file_sha256"] = before_hashes
            atomic_write_json(args.output, report)
            print(f"result TFL keys match frozen shell map: {args.output}")
            return 0
        if args.command == "tfl-shell-propose":
            inputs = {"real DOCX shell": args.docx}
            if args.layout_reference_config is not None:
                inputs["layout-reference config"] = args.layout_reference_config
            outputs = {
                "proposal report": args.report,
                "privacy-safe shell datapack": args.shell_datapack_output,
                "value-free candidate": args.candidate_output,
            }
            _validate_paths(inputs, outputs, scope="real TFL shell")
            report, shell_datapack, candidate = propose_docx_shell_map(
                args.docx,
                expected_sha256=args.expected_sha256,
                map_version=args.map_version,
                layout_reference_config=args.layout_reference_config,
            )
            atomic_write_text_bundle(
                [
                    (
                        args.report,
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                    (
                        args.shell_datapack_output,
                        json.dumps(
                            shell_datapack, ensure_ascii=False, indent=2, sort_keys=True
                        )
                        + "\n",
                    ),
                    (
                        args.candidate_output,
                        json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )
            metrics = report["metrics"]
            print(
                "proposed "
                f"{metrics['object_level_auto_proposals']} physical objects; "
                f"human batch queue={metrics['human_review_batches']}: {args.report}"
            )
            return 0
        if args.command == "tfl-shell-freeze":
            inputs = {
                "proposal report": args.report,
                "candidate": args.candidate,
                "batch confirmation": args.batch_confirmation,
            }
            outputs = {"frozen map": args.output, "confirmation proof": args.proof_output}
            _validate_paths(inputs, outputs, scope="real TFL shell")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            report = load_json_unique(args.report)
            candidate = load_json_unique(args.candidate)
            confirmation = load_json_unique(args.batch_confirmation)
            frozen, proof = freeze_proposed_shell_map(report, candidate, confirmation)
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("Real-shell confirmation inputs changed during freeze")
            atomic_write_text_bundle(
                [
                    (
                        args.output,
                        json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                    (
                        args.proof_output,
                        json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )
            print(f"froze batch-confirmed shell map: {args.output}")
            return 0
        if args.command == "tfl-review-queue":
            inputs = {"proposal report": args.report, "candidate": args.candidate}
            outputs = {
                "decision queue": args.output,
                "review markdown": args.markdown_output,
                "deferred-row markdown": args.deferred_rows_output,
            }
            _validate_paths(inputs, outputs, scope="M8 review queue")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            queue = build_review_decision_queue(
                load_json_unique(args.report), load_json_unique(args.candidate)
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 review queue inputs changed during read")
            atomic_write_text_bundle(
                [
                    (
                        args.output,
                        json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                    (args.markdown_output, render_review_decision_queue(queue)),
                    (args.deferred_rows_output, render_deferred_row_expectations(queue)),
                ]
            )
            print(
                f"built {queue['ergonomics']['decision_group_count']} reviewer decisions "
                f"for {queue['ergonomics']['unique_review_entity_count']} entities: {args.output}"
            )
            return 0
        if args.command == "tfl-review-decide":
            inputs = {"decision queue": args.queue}
            _validate_paths(inputs, {"decision record": args.output}, scope="M8 decision")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            resolved: dict[str, str] = {}
            for key, value in args.resolved_input:
                if key in resolved:
                    raise ConfigurationError(f"Duplicate resolved input: {key}")
                resolved[key] = value
            override_parameters: dict[str, str] = {}
            for key, value in args.override_parameter:
                if key in override_parameters:
                    raise ConfigurationError(f"Duplicate override parameter: {key}")
                override_parameters[key] = value
            record = create_decision_record(
                load_json_unique(args.queue),
                group_id=args.group_id,
                reviewer_role=args.reviewer_role,
                reviewer_identifier=args.reviewer_identifier,
                decided_at=args.decided_at,
                outcome=args.outcome,
                resolved_inputs=resolved,
                override_parameters=override_parameters or None,
                override_reason=args.override_reason,
                decision_notes=args.decision_notes,
                template_sha256=args.template_sha256,
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 decision queue changed during record creation")
            atomic_write_json(args.output, record)
            print(f"recorded attributable decision {record['record_id']}: {args.output}")
            return 0
        if args.command == "tfl-review-ledger":
            inputs = {"decision queue": args.queue}
            for index, path in enumerate(args.record):
                inputs[f"decision record {index + 1}"] = path
            _validate_paths(inputs, {"decision ledger": args.output}, scope="M8 decision ledger")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            ledger = build_decision_ledger(
                load_json_unique(args.queue),
                [load_json_unique(path) for path in args.record],
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 decision ledger inputs changed during validation")
            atomic_write_json(args.output, ledger)
            print(
                f"built {ledger['status']} decision ledger with "
                f"{ledger['coverage']['recorded_group_count']} records: {args.output}"
            )
            return 0 if ledger["status"] == "complete" else 2
        if args.command == "tfl-review-freeze":
            inputs = {
                "proposal report": args.report,
                "candidate": args.candidate,
                "decision queue": args.queue,
                "decision ledger": args.ledger,
            }
            outputs = {"frozen map": args.output, "freeze proof": args.proof_output}
            _validate_paths(inputs, outputs, scope="M8 reviewed shell freeze")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            frozen, proof = freeze_candidate_with_decisions(
                load_json_unique(args.report),
                load_json_unique(args.candidate),
                load_json_unique(args.queue),
                load_json_unique(args.ledger),
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 freeze inputs changed during validation")
            atomic_write_text_bundle(
                [
                    (args.output, json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
                    (args.proof_output, json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
                ]
            )
            print(f"froze reviewer-confirmed shell map: {args.output}")
            return 0
        if args.command == "tfl-review-compare":
            inputs = {
                "previous queue": args.previous_queue,
                "previous ledger": args.previous_ledger,
                "current queue": args.current_queue,
            }
            outputs = {"comparison": args.output, "comparison markdown": args.markdown_output}
            _validate_paths(inputs, outputs, scope="M8 version comparison")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            comparison = compare_review_versions(
                load_json_unique(args.previous_queue),
                load_json_unique(args.previous_ledger),
                load_json_unique(args.current_queue),
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 version inputs changed during comparison")
            atomic_write_text_bundle(
                [
                    (args.output, json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
                    (args.markdown_output, render_version_comparison(comparison)),
                ]
            )
            print(f"compared shell decision versions: {args.output}")
            return 0
        if args.command == "tfl-review-template-export":
            inputs = {"decision queue": args.queue, "decision ledger": args.ledger}
            _validate_paths(inputs, {"decision template": args.output}, scope="M8 template export")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            template = export_decision_template(
                load_json_unique(args.queue),
                load_json_unique(args.ledger),
                template_id=args.template_id,
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 template inputs changed during export")
            atomic_write_json(args.output, template)
            print(f"exported review decision template: {args.output}")
            return 0
        if args.command == "tfl-review-template-apply":
            inputs = {"decision queue": args.queue, "decision template": args.template}
            _validate_paths(inputs, {"template suggestions": args.output}, scope="M8 template apply")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            application = apply_decision_template(
                load_json_unique(args.queue), load_json_unique(args.template)
            )
            if before_hashes != {label: sha256_file(path) for label, path in inputs.items()}:
                raise ConfigurationError("M8 template inputs changed during application")
            atomic_write_json(args.output, application)
            print(
                f"proposed {application['matched_group_count']} template-assisted decisions; "
                f"automatic decisions=0: {args.output}"
            )
            return 0
        if args.command == "tfl-result-apply-rtf":
            inputs = {
                "shell datapack": args.shell_datapack,
                "frozen map": args.frozen_map,
                "RTF result": args.rtf,
            }
            _validate_paths(
                inputs,
                {"result datapack": args.output, "cross-format report": args.report},
                scope="cross-format TFL result",
            )
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            result, report = apply_rtf_values_to_shell(
                load_json_unique(args.shell_datapack),
                load_json_unique(args.frozen_map),
                args.entity_id,
                args.rtf,
            )
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("Cross-format TFL inputs changed during value application")
            report["input_file_sha256"] = before_hashes
            atomic_write_text_bundle(
                [
                    (
                        args.output,
                        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                    (
                        args.report,
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )
            print(f"applied RTF values without semantic drift: {args.output}")
            return 0
        if args.command == "resolve":
            inputs = {
                "draft": args.draft,
                "datapack": args.datapack,
                "protocol_facts": args.protocol_facts,
            }
            if args.section_metadata is not None:
                inputs["section metadata"] = args.section_metadata
            if args.frozen_tfl_map is not None:
                inputs["frozen TFL map"] = args.frozen_tfl_map
            outputs = _validate_m2_paths(
                inputs,
                {"resolved output": args.output, "guard report": args.report},
            )
            archived_outputs = _archive_existing_outputs(
                outputs, reserved_paths=inputs.values()
            )
            input_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            draft = args.draft.read_text(encoding="utf-8")
            datapack = load_json_unique(args.datapack)
            facts = load_data(args.protocol_facts)
            section_metadata = (
                load_json_unique(args.section_metadata)
                if args.section_metadata is not None
                else None
            )
            frozen_tfl_map = (
                load_json_unique(args.frozen_tfl_map)
                if args.frozen_tfl_map is not None
                else None
            )
            conditional_report = None
            if section_metadata is not None:
                conditioned = resolve_conditional_conclusions(
                    draft,
                    datapack,
                    section_metadata,
                )
                draft = conditioned.resolved_text
                conditional_report = conditioned.report
            result = resolve(
                draft,
                datapack,
                facts,
                binding_manifest=section_metadata,
                frozen_tfl_map=frozen_tfl_map,
            )
            if conditional_report is not None:
                result.report["conditional_conclusion"] = conditional_report
            final_input_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            changed_inputs = [
                label for label in inputs if final_input_hashes[label] != input_hashes[label]
            ]
            if changed_inputs:
                raise ConfigurationError(
                    f"M2 inputs changed while resolving: {', '.join(changed_inputs)}"
                )
            result.report["input_file_sha256"] = input_hashes
            if archived_outputs:
                result.report["archived_previous_outputs"] = dict(archived_outputs)
            if result.status == "pass" and result.resolved_text is not None:
                atomic_write_text_bundle(
                    [
                        (
                            args.report,
                            json.dumps(
                                result.report,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n",
                        ),
                        (args.output, result.resolved_text),
                    ]
                )
                print(f"guards A/B/C passed; resolved output: {args.output}")
                _print_archived_outputs(archived_outputs, stream=sys.stdout)
                return 0
            atomic_write_json(args.report, result.report)
            _print_archived_outputs(archived_outputs)
            print(f"numeric pipeline blocked: {result.status}; report: {args.report}", file=sys.stderr)
            return 2 if result.status == "review_required" else 3
        if args.command == "facts-card":
            _validate_paths(
                {"study_facts": args.study_facts},
                {"fact card": args.output},
                scope="M3",
            )
            before_hash = sha256_file(args.study_facts)
            facts = load_study_facts(args.study_facts)
            card = render_fact_card(facts)
            after_hash = sha256_file(args.study_facts)
            if before_hash != after_hash:
                raise ConfigurationError("M3 study_facts changed while rendering the fact card")
            atomic_write_text(args.output, card)
            print(f"validated study_facts; fact card: {args.output}")
            return 0
        if args.command == "route-modules":
            _validate_paths(
                {"study_facts": args.study_facts},
                {"routing output": args.output},
                scope="M3",
            )
            before_hash = sha256_file(args.study_facts)
            facts = load_study_facts(args.study_facts)
            routed = route_modules(facts)
            after_hash = sha256_file(args.study_facts)
            if before_hash != after_hash:
                raise ConfigurationError("M3 study_facts changed while routing modules")
            routed["input_file_sha256"] = {"study_facts": before_hash}
            atomic_write_json(args.output, routed)
            print(
                f"activated {len(routed['active_modules'])} modules; routing output: {args.output}"
            )
            return 0
        if args.command == "authoring-inventory":
            _validate_paths(
                {"study_facts": args.study_facts},
                {
                    "inventory JSON": args.output,
                    "inventory Markdown": args.markdown_output,
                },
                scope="M11",
            )
            before_hash = sha256_file(args.study_facts)
            facts = load_study_facts(args.study_facts)
            routed = route_modules(facts)
            inventory = build_authoring_inventory(
                routed["structure"]["section_plan"]
            )
            if sha256_file(args.study_facts) != before_hash:
                raise ConfigurationError(
                    "M11 study_facts changed while building the authoring inventory"
                )
            inventory["input_file_sha256"] = {"study_facts": before_hash}
            atomic_write_text_bundle(
                [
                    (
                        args.output,
                        json.dumps(
                            inventory,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                    ),
                    (
                        args.markdown_output,
                        render_authoring_inventory_markdown(inventory),
                    ),
                ]
            )
            print(
                f"classified {inventory['visible_heading_count']} headings; "
                f"full-playbook units={inventory['full_playbook_coverage_units']}: "
                f"{args.output}"
            )
            return 0
        if args.command == "extract-docx":
            report = extract_docx_to_file(args.docx, args.output)
            print(
                f"extracted {report['statistics']['records']} records; "
                f"warnings={len(report['warnings'])}; output: {args.output}"
            )
            return 0
        if args.command == "scan-protocol-facts":
            inputs = {
                "protocol DOCX": args.docx,
                "study facts": args.study_facts,
                "scan rules": args.rules,
            }
            outputs = {
                "scan report": args.report,
                "scan markdown": args.markdown_output,
            }
            _validate_paths(inputs, outputs, scope="M10 protocol fact scan")
            before = {label: sha256_file(path) for label, path in inputs.items()}
            facts = load_study_facts(args.study_facts)
            report = scan_protocol_facts(
                args.docx,
                facts,
                rules_path=args.rules,
                expected_sha256=args.expected_sha256,
                protocol_role=args.protocol_role,
            )
            after = {label: sha256_file(path) for label, path in inputs.items()}
            if before != after:
                raise ConfigurationError("M10 protocol-fact scan inputs changed during read")
            report["input_file_sha256"] = before
            atomic_write_text_bundle(
                [
                    (
                        args.report,
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                    ),
                    (args.markdown_output, render_protocol_fact_scan(report)),
                ]
            )
            print(
                f"scanned protocol against frozen facts; review findings="
                f"{report['metrics']['finding_count']}: {args.report}"
            )
            return 0
        if args.command == "draft-section":
            emit_mode = args.emit_envelope is not None
            ingest_mode = args.ingest_draft is not None or args.envelope is not None
            if emit_mode:
                if (
                    args.request is None
                    or args.output_dir is not None
                    or ingest_mode
                    or args.guard_report is not None
                    or args.prior_guard_report
                ):
                    raise ConfigurationError(
                        "Envelope mode requires only --request and --emit-envelope"
                    )
                _validate_paths(
                    {"section request": args.request},
                    {"Agent envelope": args.emit_envelope},
                    scope="M7",
                )
                before_hash = sha256_file(args.request)
                request = load_json_unique(args.request)
                if not isinstance(request, dict):
                    raise ConfigurationError("Section request JSON must be an object")
                envelope = emit_agent_envelope(request)
                if sha256_file(args.request) != before_hash:
                    raise ConfigurationError("Section request changed while emitting Agent envelope")
                atomic_write_json(args.emit_envelope, envelope)
                print(
                    f"emitted Agent envelope for {envelope['data']['section_id']}: "
                    f"{args.emit_envelope}"
                )
                return 0
            if ingest_mode:
                if (
                    args.request is not None
                    or args.envelope is None
                    or args.ingest_draft is None
                    or args.output_dir is None
                    or args.guard_report is None
                ):
                    raise ConfigurationError(
                        "Ingest mode requires --envelope, --ingest-draft, --output-dir, and --guard-report"
                    )
                envelope_payload = load_json_unique(args.envelope)
                if not isinstance(envelope_payload, dict):
                    raise ConfigurationError("Agent envelope JSON must be an object")
                data = envelope_payload.get("data")
                section_id = data.get("section_id") if isinstance(data, dict) else None
                if not isinstance(section_id, str):
                    raise ConfigurationError("Agent envelope needs a string section_id")
                section_dir = args.output_dir / "sections"
                inputs = {
                    "Agent envelope": args.envelope,
                    "Agent draft": args.ingest_draft,
                    **{
                        f"prior guard report {index}": path
                        for index, path in enumerate(args.prior_guard_report, start=1)
                    },
                }
                _validate_paths(
                    inputs,
                    {
                        "section Markdown": section_dir / f"{section_id}.md",
                        "section metadata": section_dir / f"{section_id}.meta.json",
                        "guard report": args.guard_report,
                    },
                    scope="M7",
                )
                before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
                draft_payload = load_json_unique(args.ingest_draft)
                prior_reports = [
                    load_json_unique(path) for path in args.prior_guard_report
                ]
                if not isinstance(draft_payload, dict) or not all(
                    isinstance(item, dict) for item in prior_reports
                ):
                    raise ConfigurationError("Agent draft and guard reports must be JSON objects")
                try:
                    result, guard_report = ingest_agent_draft(
                        envelope_payload,
                        draft_payload,
                        args.output_dir,
                        prior_guard_reports=prior_reports,
                    )
                except AgentDraftGuardError as exc:
                    atomic_write_json(args.guard_report, exc.report)
                    print(
                        f"Agent draft rejected by section guards: {args.guard_report}",
                        file=sys.stderr,
                    )
                    return 3
                after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
                if before_hashes != after_hashes:
                    raise ConfigurationError("Agent drafting inputs changed during ingestion")
                atomic_write_json(args.guard_report, guard_report)
                print(f"ingested Agent draft for {result.section_id}: {result.markdown_path}")
                print(f"guard evidence: {args.guard_report}")
                return 0
            if (
                args.request is None
                or args.output_dir is None
                or args.guard_report is not None
                or args.prior_guard_report
            ):
                raise ConfigurationError(
                    "Legacy draft mode requires only --request and --output-dir"
                )
            request = load_json_unique(args.request)
            if not isinstance(request, dict):
                raise ConfigurationError("Section request JSON must be an object")
            section_id = request.get("section_id")
            if not isinstance(section_id, str):
                raise ConfigurationError("Section request needs a string section_id")
            section_dir = args.output_dir / "sections"
            _validate_paths(
                {"section request": args.request},
                {
                    "section Markdown": section_dir / f"{section_id}.md",
                    "section metadata": section_dir / f"{section_id}.meta.json",
                },
                scope="M3",
            )
            result = draft_section(request, args.output_dir)
            print(f"drafted section {result.section_id}: {result.markdown_path}")
            print(f"provenance metadata: {result.metadata_path}")
            return 0
        if args.command == "fact-card-envelope":
            _validate_paths(
                {"fact card": args.fact_card},
                {"fact-card envelope": args.output},
                scope="M16 constrained narration",
            )
            before_hash = sha256_file(args.fact_card)
            card = load_json_unique(args.fact_card)
            if not isinstance(card, dict):
                raise ConfigurationError("Fact card JSON must be an object")
            envelope = emit_fact_card_envelope(card)
            if sha256_file(args.fact_card) != before_hash:
                raise ConfigurationError("Fact card changed while emitting envelope")
            atomic_write_json(args.output, envelope)
            print(f"emitted constrained fact-card envelope: {args.output}")
            return 0
        if args.command == "fact-card-ingest":
            inputs = {"fact-card envelope": args.envelope, "Agent draft": args.draft}
            outputs = {
                "section Markdown": args.markdown_output,
                "section metadata": args.metadata_output,
                "guard report": args.guard_report,
            }
            _validate_paths(inputs, outputs, scope="M16 constrained narration")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            envelope = load_json_unique(args.envelope)
            draft = load_json_unique(args.draft)
            if not isinstance(envelope, dict) or not isinstance(draft, dict):
                raise ConfigurationError("Envelope and constrained draft must be JSON objects")
            try:
                artifact = ingest_constrained_draft(envelope, draft)
            except ConstrainedNarrationError as exc:
                if exc.report:
                    atomic_write_json(args.guard_report, exc.report)
                raise
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("Constrained narration inputs changed during ingestion")
            write_constrained_artifact(
                artifact, args.markdown_output, args.metadata_output
            )
            atomic_write_json(args.guard_report, artifact.metadata)
            print(f"ingested constrained narration: {args.markdown_output}")
            return 0
        if args.command == "fact-card-render-verbatim":
            _validate_paths(
                {"fact card": args.fact_card},
                {
                    "section Markdown": args.markdown_output,
                    "section metadata": args.metadata_output,
                },
                scope="M16 verbatim extraction",
            )
            before_hash = sha256_file(args.fact_card)
            card = load_json_unique(args.fact_card)
            if not isinstance(card, dict):
                raise ConfigurationError("Fact card JSON must be an object")
            artifact = render_verbatim_fact_card(card)
            if sha256_file(args.fact_card) != before_hash:
                raise ConfigurationError("Fact card changed during deterministic rendering")
            write_constrained_artifact(
                artifact, args.markdown_output, args.metadata_output
            )
            print(f"rendered verbatim extraction without LLM: {args.markdown_output}")
            return 0
        if args.command == "generate-deterministic-sections":
            inputs = {"generation context": args.context, "DOCX template": args.template}
            outputs = {
                "deterministic DOCX": args.output_docx,
                "deterministic manifest": args.manifest_output,
            }
            _validate_paths(inputs, outputs, scope="M17 deterministic sections")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            context = load_json_unique(args.context)
            if not isinstance(context, dict):
                raise ConfigurationError("Deterministic generation context must be an object")
            bundle = build_deterministic_bundle(context)
            rendering = render_deterministic_bundle_docx(
                bundle, args.output_docx, template_path=args.template
            )
            if {label: sha256_file(path) for label, path in inputs.items()} != before_hashes:
                raise ConfigurationError("Deterministic generation inputs changed during render")
            atomic_write_json(
                args.manifest_output,
                {
                    **bundle,
                    "input_file_sha256": before_hashes,
                    "docx_rendering": rendering,
                },
            )
            print(
                "generated 48 deterministic + 4 template units without an LLM: "
                f"{args.output_docx}"
            )
            return 0
        if args.command == "advisory-review-envelope":
            _validate_paths(
                {"CSR source": args.source},
                {"advisory review envelope": args.output},
                scope="M16 advisory review",
            )
            source_hash = sha256_file(args.source)
            text = args.source.read_text(encoding="utf-8")
            envelope = emit_advisory_review_envelope(
                text, source_label=args.source.name, source_sha256=source_hash
            )
            if sha256_file(args.source) != source_hash:
                raise ConfigurationError("CSR source changed while emitting review envelope")
            atomic_write_json(args.output, envelope)
            print(f"emitted advisory-only CDE review envelope: {args.output}")
            return 0
        if args.command == "advisory-review-ingest":
            inputs = {"review envelope": args.envelope, "review flags": args.review}
            _validate_paths(inputs, {"advisory report": args.output}, scope="M16 advisory review")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            envelope = load_json_unique(args.envelope)
            review = load_json_unique(args.review)
            if not isinstance(envelope, dict) or not isinstance(review, dict):
                raise ConfigurationError("Review envelope and flags must be JSON objects")
            artifact = ingest_advisory_review(envelope, review)
            if {label: sha256_file(path) for label, path in inputs.items()} != before_hashes:
                raise ConfigurationError("Advisory review inputs changed during ingestion")
            write_advisory_report(artifact, args.output)
            print(f"validated advisory flags; release gate unchanged: {args.output}")
            return 0
        if args.command == "m4-group-semantics":
            inputs = {
                "datapack": args.datapack,
                "section metadata": args.section_metadata,
                "group contract": args.contract,
            }
            _validate_paths(inputs, {"group report": args.output}, scope="M4")
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            datapack = load_json_unique(args.datapack)
            metadata = load_json_unique(args.section_metadata)
            contract = load_json_unique(args.contract)
            report = validate_group_semantics(datapack, metadata, contract)
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("M4 group-semantics inputs changed during validation")
            report["input_file_sha256"] = before_hashes
            atomic_write_json(args.output, report)
            print(
                f"validated {report['binding_count_checked']} group-bound roles: {args.output}"
            )
            return 0
        if args.command == "m4-unreferenced-tfl":
            inputs = {"datapack": args.datapack, "section metadata": args.section_metadata}
            _validate_paths(
                inputs,
                {"unreferenced TFL": args.output, "trace report": args.report},
                scope="M4",
            )
            before_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            datapack = load_json_unique(args.datapack)
            metadata = load_json_unique(args.section_metadata)
            report = analyze_tfl_references(datapack, metadata)
            after_hashes = {label: sha256_file(path) for label, path in inputs.items()}
            if before_hashes != after_hashes:
                raise ConfigurationError("M4 traceability inputs changed during validation")
            report["input_file_sha256"] = before_hashes
            atomic_write_text_bundle(
                [
                    (args.output, render_unreferenced_tfl(report)),
                    (
                        args.report,
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )
            print(
                f"checked {report['table_count']} TFLs; "
                f"unreferenced={report['unreferenced_table_count']}: {args.output}"
            )
            return 0
        if args.command == "m4-bidirectional-trace":
            _validate_paths(
                {"trace manifest": args.manifest},
                {"trace report": args.output},
                scope="M4",
            )
            before_hash = sha256_file(args.manifest)
            manifest = load_json_unique(args.manifest)
            report = validate_bidirectional_trace(manifest)
            after_hash = sha256_file(args.manifest)
            if before_hash != after_hash:
                raise ConfigurationError("M4 bidirectional trace manifest changed during validation")
            report["input_file_sha256"] = {"trace manifest": before_hash}
            atomic_write_json(args.output, report)
            print(f"validated {report['path_count']} bidirectional trace paths: {args.output}")
            return 0
        if args.command == "m4-cross-check":
            inputs = {
                "sections manifest": args.sections_manifest,
                "review decisions": args.review_decisions,
                "synopsis selection": args.synopsis_selection,
                "analysis-set contract": args.analysis_set_contract,
                "claim ledger": args.claim_ledger,
            }
            _validate_paths(inputs, {"cross-chapter report": args.output}, scope="M4")
            sections = load_sections_from_manifest(args.sections_manifest)
            decisions = load_review_decisions(args.review_decisions)
            selection_payload = load_json_unique(args.synopsis_selection)
            if not isinstance(selection_payload, dict) or set(selection_payload) != {
                "schema_version",
                "selection",
            } or selection_payload.get("schema_version") != "1.0":
                raise ConfigurationError("Invalid M4 synopsis selection contract")
            clean_paragraphs = apply_review_decisions(sections, decisions)
            synopsis, manifest = derive_synopsis_manifest(
                sections, clean_paragraphs, selection_payload["selection"]
            )
            synopsis_check = verify_synopsis_copy(synopsis, manifest)
            analysis_payload = load_json_unique(args.analysis_set_contract)
            if not isinstance(analysis_payload, dict) or not isinstance(
                analysis_payload.get("records"), list
            ):
                raise ConfigurationError("Invalid M4 analysis-set contract")
            analysis_check = validate_analysis_set_consistency(analysis_payload["records"])
            claim_payload = load_json_unique(args.claim_ledger)
            claim_check = validate_claim_ledger(claim_payload)
            report = build_cross_chapter_report(
                synopsis_check, analysis_check, claim_check
            )
            report["input_file_sha256"] = {
                label: sha256_file(path) for label, path in inputs.items()
            }
            atomic_write_json(args.output, report)
            print(f"cross-chapter status={report['status']}: {args.output}")
            return 0 if report["status"] == "pass" else 1
        if args.command == "m4-assemble":
            inputs = {
                "sections manifest": args.sections_manifest,
                "review decisions": args.review_decisions,
                "synopsis selection": args.synopsis_selection,
                "template": args.template,
            }
            outputs = {
                "annotated DOCX": args.annotated_output,
                "clean DOCX": args.clean_output,
                "Synopsis DOCX": args.synopsis_output,
                "Synopsis manifest": args.synopsis_manifest_output,
                "assembly report": args.report,
            }
            _validate_paths(inputs, outputs, scope="M4")
            sections = load_sections_from_manifest(args.sections_manifest)
            decisions = load_review_decisions(args.review_decisions)
            selection_payload = load_json_unique(args.synopsis_selection)
            if not isinstance(selection_payload, dict) or set(selection_payload) != {
                "schema_version",
                "selection",
            } or selection_payload.get("schema_version") != "1.0":
                raise ConfigurationError("Invalid M4 synopsis selection contract")
            annotated = render_annotated_docx(
                sections,
                args.annotated_output,
                protocol_id=args.protocol_id,
                template_path=args.template,
            )
            try:
                clean = render_clean_and_synopsis(
                    sections,
                    decisions,
                    selection_payload["selection"],
                    args.clean_output,
                    args.synopsis_output,
                    args.synopsis_manifest_output,
                    protocol_id=args.protocol_id,
                    template_path=args.template,
                )
            except CleanOutputBlocked as exc:
                report = {
                    "schema_version": "1.0",
                    "status": "blocked",
                    "annotated": annotated,
                    "clean": {"status": "not_generated", "reason": str(exc)},
                }
                atomic_write_json(args.report, report)
                print(f"annotated output created; clean output blocked: {args.report}", file=sys.stderr)
                return 3
            report = {
                "schema_version": "1.0",
                "status": "pass",
                "annotated": annotated,
                **clean,
            }
            atomic_write_json(args.report, report)
            print(f"rendered annotated/clean/Synopsis outputs: {args.report}")
            return 0
        if args.command == "m4-qc-report":
            inputs = {"QC checks": args.checks}
            _validate_paths(
                inputs, {"QC Markdown": args.output, "QC JSON": args.report}, scope="M4"
            )
            payload = load_json_unique(args.checks)
            if not isinstance(payload, dict) or set(payload) != {"schema_version", "checks"}:
                raise ConfigurationError("M4 QC input must contain schema_version and checks")
            if payload.get("schema_version") != "1.0" or not isinstance(
                payload.get("checks"), dict
            ):
                raise ConfigurationError("Invalid M4 QC input contract")
            markdown, report = build_qc_report(payload["checks"])
            report["input_file_sha256"] = {"QC checks": sha256_file(args.checks)}
            atomic_write_text_bundle(
                [
                    (args.output, markdown),
                    (
                        args.report,
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )
            print(f"rendered bounded M4 QC report: {args.output}")
            return 0
    except (CSRWriterError, OSError, ValueError, KeyError, TypeError) as exc:
        _print_archived_outputs(archived_outputs)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    parser.error(f"Unhandled command: {args.command}")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
