"""Human decision queue and traceable refreeze for shell/result entity drift."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from docx import Document

from .datapack import validate_datapack
from .docx_result import _read_result_entities
from .errors import CSRWriterError
from .io_utils import sha256_file
from .shell_mapping import TITLE_RE, _build_cell_level_table
from .tfl_map import build_tfl_map_candidate, freeze_tfl_map


class TFLDriftDecisionError(CSRWriterError):
    """A drift decision is absent, stale, or cannot be refrozen safely."""


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _title_index(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    source = Path(path)
    if not source.is_file():
        raise TFLDriftDecisionError(f"E-TFL-DRIFT-SOURCE: missing {source}")
    result: dict[str, str] = {}
    document = Document(source)
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        match = TITLE_RE.match(text)
        if match is None:
            continue
        prefix = {"表": "T", "图": "F", "清单": "L"}[match.group(1)]
        result[prefix + match.group(2)] = text
    return result


def build_drift_decision_queue(
    drift_report: Mapping[str, Any],
    frozen_map: Mapping[str, Any],
    *,
    shell_path: str | Path | None = None,
    result_path: str | Path | None = None,
    result_route_proposals: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Turn strict shell/result drift into explicit human decisions."""

    if not isinstance(drift_report, Mapping) or not isinstance(frozen_map, Mapping):
        raise TFLDriftDecisionError("E-TFL-DRIFT-QUEUE-CONTRACT")
    missing = drift_report.get("missing_result_entities")
    extra = drift_report.get("unmatched_entities")
    if not isinstance(missing, list) or not isinstance(extra, list):
        raise TFLDriftDecisionError("E-TFL-DRIFT-QUEUE-CONTRACT")
    if not missing and not extra:
        raise TFLDriftDecisionError("E-TFL-DRIFT-QUEUE-EMPTY")
    frozen_entities = {
        str(item["entity_id"]): item for item in frozen_map.get("entities", [])
    }
    entity_reports = {
        str(item["entity_id"]): item
        for item in drift_report.get("entity_reports", [])
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }
    shell_titles = _title_index(shell_path)
    result_titles = _title_index(result_path)
    result_entities = {}
    if result_path is not None:
        result_entities = {
            item.entity_id: item for item in _read_result_entities(Path(result_path))[0]
        }
    route_proposals = dict(result_route_proposals or {})
    decisions: list[dict[str, Any]] = []
    for entity_id in sorted(missing):
        frozen = frozen_entities.get(entity_id)
        if frozen is None:
            raise TFLDriftDecisionError(
                f"E-TFL-DRIFT-EVIDENCE: missing frozen entity {entity_id}"
            )
        report = entity_reports.get(entity_id, {})
        decisions.append(
            {
                "decision_id": f"DRIFT-{len(decisions) + 1:03d}",
                "direction": "shell_only",
                "entity_id": entity_id,
                "title": shell_titles.get(entity_id, "原 shell 标题未写入隐私安全映射产物"),
                "title_sha256": hashlib.sha256(
                    shell_titles.get(entity_id, "").encode("utf-8")
                ).hexdigest(),
                "semantic_value_positions": len(frozen.get("semantic_keys", [])),
                "affected_sections": list(frozen.get("ich_e3_sections", [])),
                "strict_effect": {
                    "unresolved_frozen_cell_count": int(
                        report.get("unresolved_frozen_cell_count", 0)
                    ),
                    "release_status_before_decision": "blocked",
                },
                "question": "该 shell 表在结果稿中消失，是预期变更还是结果文件错误？",
                "options": ["expected_change", "result_error"],
                "default": "result_error",
            }
        )
    for entity_id in sorted(extra):
        result_entity = result_entities.get(entity_id)
        if result_path is not None and result_entity is None:
            raise TFLDriftDecisionError(
                f"E-TFL-DRIFT-EVIDENCE: result entity {entity_id} cannot be parsed"
            )
        value_positions = 0
        if result_entity is not None:
            _table, cells, _proof = _build_cell_level_table(
                result_entity.parsed,
                entity_id=entity_id,
                population=result_entity.population,
                dimensions=result_entity.dimensions,
                source_id=f"sha256:{sha256_file(result_path)}",
            )
            value_positions = len(cells)
        proposed_sections = list(route_proposals.get(entity_id, []))
        decisions.append(
            {
                "decision_id": f"DRIFT-{len(decisions) + 1:03d}",
                "direction": "result_only",
                "entity_id": entity_id,
                "title": result_titles.get(entity_id, "结果标题未提供"),
                "title_sha256": hashlib.sha256(
                    result_titles.get(entity_id, "").encode("utf-8")
                ).hexdigest(),
                "semantic_value_positions": value_positions,
                "affected_sections": proposed_sections,
                "strict_effect": {
                    "unmapped_result_entity_count": 1,
                    "release_status_before_decision": "blocked",
                },
                "question": "该表只出现在结果稿，是预期新增还是结果文件错误？",
                "options": ["expected_change", "result_error"],
                "default": "result_error",
            }
        )
    body = {
        "schema_version": "1.0",
        "status": "awaiting_human_decisions",
        "interface": "csr-writer-tfl-drift-decisions-v1",
        "frozen_map_sha256": frozen_map.get("frozen_sha256"),
        "drift_report_sha256": _stable_hash(drift_report),
        "decision_count": len(decisions),
        "decisions": decisions,
        "release_eligible": False,
        "automation_policy": "no drift decision is inferred or auto-accepted",
    }
    return {**body, "queue_sha256": _stable_hash(body)}


def _validate_decision_record(
    queue: Mapping[str, Any], record: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    expected_keys = {
        "schema_version",
        "queue_sha256",
        "decided_by_role",
        "decided_at",
        "decisions",
    }
    if not isinstance(record, Mapping) or set(record) != expected_keys:
        raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-CONTRACT")
    queue_body = {key: value for key, value in queue.items() if key != "queue_sha256"}
    if queue.get("queue_sha256") != _stable_hash(queue_body):
        raise TFLDriftDecisionError("E-TFL-DRIFT-QUEUE-HASH")
    if record.get("schema_version") != "1.0" or record.get("queue_sha256") != queue.get(
        "queue_sha256"
    ):
        raise TFLDriftDecisionError("E-TFL-DRIFT-STALE-DECISION")
    role = record.get("decided_by_role")
    timestamp = record.get("decided_at")
    if not isinstance(role, str) or re.fullmatch(r"[a-z][a-z0-9_]*", role) is None:
        raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-ROLE")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-TIME")
    expected = {str(item["decision_id"]): item for item in queue.get("decisions", [])}
    observed: dict[str, Mapping[str, Any]] = {}
    raw_decisions = record.get("decisions")
    if not isinstance(raw_decisions, list):
        raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-CONTRACT")
    for index, item in enumerate(raw_decisions):
        if not isinstance(item, Mapping) or set(item) != {
            "decision_id",
            "decision",
            "reason",
            "ich_e3_sections",
        }:
            raise TFLDriftDecisionError(
                f"E-TFL-DRIFT-DECISION-CONTRACT: item {index}"
            )
        decision_id = item.get("decision_id")
        if not isinstance(decision_id, str) or decision_id in observed:
            raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-DUPLICATE")
        if item.get("decision") not in {"expected_change", "result_error"}:
            raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-VALUE")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-REASON")
        sections = item.get("ich_e3_sections")
        if not isinstance(sections, list) or not all(
            isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)*", value)
            for value in sections
        ):
            raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-ROUTE")
        queue_item = expected.get(decision_id)
        if queue_item is None:
            raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-UNKNOWN")
        if (
            queue_item["direction"] == "result_only"
            and item["decision"] == "expected_change"
            and not sections
        ):
            raise TFLDriftDecisionError("E-TFL-DRIFT-DECISION-ROUTE")
        observed[decision_id] = item
    if set(observed) != set(expected):
        raise TFLDriftDecisionError(
            "E-TFL-DRIFT-DECISION-INCOMPLETE: every queue item requires a decision"
        )
    return observed


def reconcile_expected_drift(
    queue: Mapping[str, Any],
    decision_record: Mapping[str, Any],
    shell_datapack: Mapping[str, Any],
    frozen_map: Mapping[str, Any],
    result_path: str | Path,
) -> dict[str, Any]:
    """Create a new value-free candidate only after explicit drift decisions."""

    observed = _validate_decision_record(queue, decision_record)
    queue_items = {str(item["decision_id"]): item for item in queue["decisions"]}
    return_to_data = [
        queue_items[decision_id]
        for decision_id, item in observed.items()
        if item["decision"] == "result_error"
    ]
    if return_to_data:
        return {
            "schema_version": "1.0",
            "status": "blocked_return_to_data",
            "release_eligible": False,
            "blocking_code": "E-TFL-DRIFT-RESULT-ERROR",
            "queue_sha256": queue["queue_sha256"],
            "decision_record_sha256": _stable_hash(decision_record),
            "return_to_data_entities": [item["entity_id"] for item in return_to_data],
        }

    revised = copy.deepcopy(dict(shell_datapack))
    validate_datapack(revised)
    result_source = Path(result_path)
    result_sha = sha256_file(result_source)
    result_source_id = f"sha256:{result_sha}"
    result_entities = {
        item.entity_id: item for item in _read_result_entities(result_source)[0]
    }
    remove_entities = {
        queue_items[decision_id]["entity_id"]
        for decision_id, item in observed.items()
        if queue_items[decision_id]["direction"] == "shell_only"
    }
    add_decisions = [
        (queue_items[decision_id], item)
        for decision_id, item in observed.items()
        if queue_items[decision_id]["direction"] == "result_only"
    ]
    revised["tables"] = [
        item for item in revised["tables"] if item["table_id"] not in remove_entities
    ]
    revised["cells"] = {
        key: value
        for key, value in revised["cells"].items()
        if key.split("|", 1)[0] not in remove_entities
    }
    if all(item.get("source_id") != result_source_id for item in revised["sources"]):
        revised["sources"].append(
            {
                "source_id": result_source_id,
                "filename": "reviewed-result-structure.docx",
                "manifest_path": "human-adjudicated drift refreeze",
                "format": "docx-result-structure-refreeze-v1",
                "decode_replacements": 0,
            }
        )
    route_by_entity = {
        str(item["entity_id"]): list(item["ich_e3_sections"])
        for item in frozen_map.get("entities", [])
        if item["entity_id"] not in remove_entities
    }
    materialized: list[dict[str, Any]] = []
    for queue_item, decision in add_decisions:
        entity_id = str(queue_item["entity_id"])
        entity = result_entities.get(entity_id)
        if entity is None:
            raise TFLDriftDecisionError(
                f"E-TFL-DRIFT-RESULT-ENTITY-MISSING: {entity_id}"
            )
        try:
            table, cells, proof = _build_cell_level_table(
                entity.parsed,
                entity_id=entity_id,
                population=entity.population,
                dimensions=entity.dimensions,
                source_id=result_source_id,
            )
        except Exception as exc:
            raise TFLDriftDecisionError(
                f"E-TFL-DRIFT-REPARSE-REQUIRED: {entity_id}: {exc}"
            ) from exc
        if any(item["table_id"] == entity_id for item in revised["tables"]):
            raise TFLDriftDecisionError(f"E-TFL-DRIFT-DUPLICATE: {entity_id}")
        revised["tables"].append(table)
        revised["cells"].update(cells)
        route_by_entity[entity_id] = list(decision["ich_e3_sections"])
        materialized.append(
            {
                "entity_id": entity_id,
                "semantic_cell_count": len(cells),
                "ingest_strategy": table["ingest_strategy"],
                "semantic_proof": proof,
            }
        )
    revised["tables"].sort(key=lambda item: item["table_id"])
    revised["mapping_manifest"] = {
        "filename": "human-adjudicated-drift.json",
        "sha256": _stable_hash(decision_record),
    }
    validate_datapack(revised)
    route_config = {
        "schema_version": "1.0",
        "map_version": f"{frozen_map['map_version']}.drift-{queue['queue_sha256'][:8]}",
        "numbering_system": copy.deepcopy(frozen_map["numbering_system"]),
        "routes": [
            {"entity_id": entity_id, "ich_e3_sections": route_by_entity[entity_id]}
            for entity_id in sorted(route_by_entity)
        ],
    }
    candidate = build_tfl_map_candidate(revised, route_config)
    body = {
        "schema_version": "1.0",
        "status": "awaiting_refreeze_confirmation",
        "release_eligible": False,
        "queue_sha256": queue["queue_sha256"],
        "decision_record_sha256": _stable_hash(decision_record),
        "previous_frozen_sha256": frozen_map.get("frozen_sha256"),
        "retired_entities": sorted(remove_entities),
        "added_entities": materialized,
        "revised_shell_datapack": revised,
        "candidate": candidate,
    }
    return {**body, "reconciliation_sha256": _stable_hash(body)}


def freeze_reconciled_tfl_map(
    reconciliation: Mapping[str, Any], confirmation: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze a reconciled candidate and bind its complete decision ancestry."""

    if reconciliation.get("status") != "awaiting_refreeze_confirmation":
        raise TFLDriftDecisionError("E-TFL-DRIFT-REFREEZE-STATE")
    body = {
        key: value
        for key, value in reconciliation.items()
        if key != "reconciliation_sha256"
    }
    if reconciliation.get("reconciliation_sha256") != _stable_hash(body):
        raise TFLDriftDecisionError("E-TFL-DRIFT-RECONCILIATION-HASH")
    frozen = freeze_tfl_map(reconciliation["candidate"], confirmation)
    provenance_body = {
        "schema_version": "1.0",
        "status": "pass",
        "interface": "csr-writer-tfl-map-provenance-chain-v1",
        "previous_frozen_sha256": reconciliation["previous_frozen_sha256"],
        "drift_queue_sha256": reconciliation["queue_sha256"],
        "decision_record_sha256": reconciliation["decision_record_sha256"],
        "reconciliation_sha256": reconciliation["reconciliation_sha256"],
        "candidate_sha256": reconciliation["candidate"]["candidate_sha256"],
        "new_frozen_sha256": frozen["frozen_sha256"],
        "retired_entities": reconciliation["retired_entities"],
        "added_entities": [
            item["entity_id"] for item in reconciliation["added_entities"]
        ],
        "confirmation": copy.deepcopy(dict(confirmation)),
    }
    provenance = {
        **provenance_body,
        "provenance_sha256": _stable_hash(provenance_body),
    }
    return frozen, provenance

