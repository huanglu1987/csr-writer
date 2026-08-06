"""Fail-closed protocol -> SAP -> TFL -> CSR trace-chain validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from .errors import CSRWriterError


NODE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
TRACE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
LAYERS = ("protocol", "sap", "tfl", "csr")
RELATIONS = {
    ("protocol", "sap"): "specified_by",
    ("sap", "tfl"): "implemented_by",
    ("tfl", "csr"): "reported_in",
}


class BidirectionalTraceError(CSRWriterError):
    """A trace chain is malformed or incomplete."""

    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        self.report = {
            "schema_version": "1.0",
            "status": "blocked",
            "issues": [{"code": code, "message": message, **details}],
        }
        super().__init__(f"{code}: {message}")


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise BidirectionalTraceError(
            "E-TRACE-CHAIN-SHAPE",
            f"{label} keys must be exactly {sorted(expected)}",
            missing=sorted(expected - set(value)),
            extra=sorted(set(value) - expected),
        )


def validate_bidirectional_trace(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every declared four-layer chain from both directions.

    The manifest contains reviewed locators only.  It never infers a protocol or
    SAP link from similar wording, which keeps missing evidence visible.
    """

    if not isinstance(manifest, Mapping):
        raise BidirectionalTraceError("E-TRACE-CHAIN", "Trace manifest must be an object")
    _exact_keys(manifest, {"schema_version", "nodes", "links", "required_paths"}, "manifest")
    if manifest.get("schema_version") != "1.0":
        raise BidirectionalTraceError("E-TRACE-CHAIN-VERSION", "Unsupported schema_version")

    raw_nodes = manifest.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise BidirectionalTraceError("E-TRACE-CHAIN-NODES", "nodes must be a non-empty list")
    nodes: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping):
            raise BidirectionalTraceError("E-TRACE-CHAIN-NODE", f"nodes[{index}] must be an object")
        _exact_keys(raw, {"id", "layer", "locator", "label"}, f"nodes[{index}]")
        if not all(isinstance(raw.get(key), str) and raw[key].strip() for key in raw):
            raise BidirectionalTraceError("E-TRACE-CHAIN-NODE", f"nodes[{index}] fields must be non-empty strings")
        node_id = raw["id"]
        if NODE_ID_RE.fullmatch(node_id) is None or node_id in nodes:
            raise BidirectionalTraceError("E-TRACE-CHAIN-NODE", f"Invalid or duplicate node id: {node_id}")
        if raw["layer"] not in LAYERS:
            raise BidirectionalTraceError("E-TRACE-CHAIN-LAYER", f"Invalid node layer: {raw['layer']}")
        nodes[node_id] = {key: raw[key] for key in ("id", "layer", "locator", "label")}

    raw_links = manifest.get("links")
    if not isinstance(raw_links, list):
        raise BidirectionalTraceError("E-TRACE-CHAIN-LINKS", "links must be a list")
    links: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_links):
        if not isinstance(raw, Mapping):
            raise BidirectionalTraceError("E-TRACE-CHAIN-LINK", f"links[{index}] must be an object")
        _exact_keys(raw, {"from", "to", "relation"}, f"links[{index}]")
        source, target, relation = raw.get("from"), raw.get("to"), raw.get("relation")
        if not all(isinstance(value, str) for value in (source, target, relation)):
            raise BidirectionalTraceError("E-TRACE-CHAIN-LINK", f"links[{index}] fields must be strings")
        if source not in nodes or target not in nodes:
            raise BidirectionalTraceError("E-TRACE-CHAIN-LINK", "Link references an unknown node", source=source, target=target)
        layer_pair = (nodes[source]["layer"], nodes[target]["layer"])
        expected_relation = RELATIONS.get(layer_pair)
        if relation != expected_relation:
            raise BidirectionalTraceError(
                "E-TRACE-CHAIN-DIRECTION",
                "Link must connect adjacent layers in the forward direction",
                source=source,
                target=target,
                observed_relation=relation,
                expected_relation=expected_relation,
            )
        edge = (source, target, relation)
        if edge in links:
            raise BidirectionalTraceError("E-TRACE-CHAIN-LINK", "Duplicate trace link", edge=edge)
        links.add(edge)

    raw_paths = manifest.get("required_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise BidirectionalTraceError("E-TRACE-CHAIN-PATHS", "required_paths must be a non-empty list")
    seen_trace_ids: set[str] = set()
    used_nodes: set[str] = set()
    path_reports: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_paths):
        if not isinstance(raw, Mapping):
            raise BidirectionalTraceError("E-TRACE-CHAIN-PATH", f"required_paths[{index}] must be an object")
        _exact_keys(raw, {"trace_id", *LAYERS}, f"required_paths[{index}]")
        trace_id = raw.get("trace_id")
        if not isinstance(trace_id, str) or TRACE_ID_RE.fullmatch(trace_id) is None or trace_id in seen_trace_ids:
            raise BidirectionalTraceError("E-TRACE-CHAIN-PATH", f"Invalid or duplicate trace_id: {trace_id}")
        seen_trace_ids.add(trace_id)
        forward = [raw[layer] for layer in LAYERS]
        for layer, node_id in zip(LAYERS, forward, strict=True):
            if not isinstance(node_id, str) or node_id not in nodes:
                raise BidirectionalTraceError("E-TRACE-CHAIN-PATH", f"{trace_id} references unknown {layer} node", node_id=node_id)
            if nodes[node_id]["layer"] != layer:
                raise BidirectionalTraceError("E-TRACE-CHAIN-LAYER", f"{trace_id} places {node_id} in the wrong layer", expected=layer, observed=nodes[node_id]["layer"])
        required_edges = [
            (forward[position], forward[position + 1], RELATIONS[(LAYERS[position], LAYERS[position + 1])])
            for position in range(len(LAYERS) - 1)
        ]
        missing_edges = [edge for edge in required_edges if edge not in links]
        if missing_edges:
            raise BidirectionalTraceError("E-TRACE-CHAIN-GAP", f"{trace_id} has a missing reviewed link", missing_edges=missing_edges)
        used_nodes.update(forward)
        path_reports.append(
            {
                "trace_id": trace_id,
                "forward": forward,
                "reverse_from_csr": list(reversed(forward)),
                "reverse_from_tfl": list(reversed(forward[:3])),
                "status": "pass",
            }
        )

    orphan_nodes = sorted(set(nodes) - used_nodes)
    used_edges = {
        edge
        for path in raw_paths
        for edge in (
            (path["protocol"], path["sap"], "specified_by"),
            (path["sap"], path["tfl"], "implemented_by"),
            (path["tfl"], path["csr"], "reported_in"),
        )
    }
    extra_links = sorted(links - used_edges)
    if orphan_nodes or extra_links:
        raise BidirectionalTraceError(
            "E-TRACE-CHAIN-ORPHAN",
            "Every reviewed node/link must belong to a required path",
            orphan_nodes=orphan_nodes,
            extra_links=extra_links,
        )

    return {
        "schema_version": "1.0",
        "status": "pass",
        "manifest_sha256": _canonical_hash(manifest),
        "path_count": len(path_reports),
        "paths": sorted(path_reports, key=lambda item: item["trace_id"]),
        "checks": {
            "protocol_to_sap_to_tfl_to_csr": "pass",
            "csr_to_tfl_to_sap_to_protocol": "pass",
            "tfl_to_sap_to_protocol": "pass",
            "no_orphan_reviewed_nodes_or_links": "pass",
        },
        "issues": [],
    }
