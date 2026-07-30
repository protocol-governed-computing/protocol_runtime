"""
locator.py — Artifact path resolution for failing nodes.

Governed by: Trace Examiner spec §G3

Maps node_id / capability codes to artifact file paths via snapshot artifacts dir.

Snapshot artifacts follow the naming convention: {namespace}__{CODE}.json
Snapshot root is derived from the trace path by the caller (STRUCTURE layout convention):
  trace_path = {workspace}/traces/{id}/{id}.jsonl
  snapshot   = {workspace}/protocol_snapshot
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.examine.classifier import ClassificationResult, FailureClass
from runtime.examine.parser import ParsedTrace, TraceEventDict


def _payload(event: TraceEventDict) -> dict[str, Any]:
    return event.get("payload", {})


def _find_node_start(trace: ParsedTrace, node_id: str) -> TraceEventDict | None:
    """Find node_start event for a given node_id."""
    for event in trace.events:
        if (
            event.get("event_type") == "node_start"
            and _payload(event).get("node_id") == node_id
        ):
            return event
    return None


def _artifacts_dir(snapshot_root: Path, artifact_type: str) -> Path | None:
    """Return the snapshot artifacts dir for the given type, or None if missing."""
    d = snapshot_root / "artifacts" / artifact_type
    return d if d.is_dir() else None


def _find_artifact(snapshot_root: Path, artifact_type: str, code: str) -> str | None:
    """
    Find an artifact file in the snapshot by code suffix match.

    Snapshot artifacts: {namespace}__{CODE}.json (case-insensitive code match).
    Returns the absolute path as a string, or None if not found.
    """
    d = _artifacts_dir(snapshot_root, artifact_type)
    if d is None:
        return None
    suffix_lower = f"__{code.lower()}.json"
    for artifact_path in d.glob("*.json"):
        if artifact_path.name.lower().endswith(suffix_lower):
            return str(artifact_path)
    return None


def locate_artifact(
    trace: ParsedTrace,
    result: ClassificationResult,
    snapshot_root: Path | None = None,
) -> str | None:
    """
    Resolve the failing artifact path from classification result.

    Uses snapshot_root (derived from workspace layout) to find artifacts.
    Returns the absolute path string, or None if unresolvable.

    Args:
        trace: Parsed trace.
        result: Classification result from classifier.
        snapshot_root: Path to the protocol_snapshot directory. If None,
                       artifact resolution is skipped and None is returned.

    Returns:
        Absolute path to the failing artifact, or None.
    """
    if snapshot_root is None:
        return None

    node_id = result.node_id

    # Admission errors fire before DAG entry — "ADMISSION" is a synthetic pre-node identifier.
    # There is no node_start event for it; route directly to the workflow spec.
    if result.failure_class == FailureClass.ADMISSION_ERROR:
        return _find_artifact(snapshot_root, "workflows", trace.workflow_code)

    if node_id is None:
        # Graph structure errors — point to the workflow spec
        if result.failure_class == FailureClass.GRAPH_STRUCTURE_ERROR:
            return _find_artifact(snapshot_root, "workflows", trace.workflow_code)
        return None

    # Look up node_type from node_start event
    node_start = _find_node_start(trace, node_id)
    node_type = _payload(node_start).get("node_type", "") if node_start else ""

    # Route to artifact based on node_type and failure class
    if result.failure_class == FailureClass.EXPRESSION_ERROR:
        return _find_artifact(snapshot_root, "workflows", trace.workflow_code)

    if result.failure_class == FailureClass.BINDING_ERROR:
        if node_id.startswith("CC_"):
            return _find_artifact(snapshot_root, "capability_contracts", node_id)
        return _find_artifact(snapshot_root, "workflows", trace.workflow_code)

    if result.failure_class == FailureClass.CT_STRUCTURE_ERROR:
        error_code = result.error_code
        if error_code in ("CT_ARTIFACT_NOT_FOUND", "CT_VALIDATION_FAILED", "CT_EXECUTION_FAILED"):
            artifact_code = _extract_ct_code(result)
            if artifact_code:
                return _find_artifact(snapshot_root, "capability_transforms", artifact_code)
        if node_id.startswith("CC_"):
            return _find_artifact(snapshot_root, "capability_contracts", node_id)
        return _find_artifact(snapshot_root, "workflows", trace.workflow_code)

    if result.failure_class == FailureClass.CS_RUNTIME_ERROR:
        return _find_artifact(snapshot_root, "runtime_bindings", trace.workflow_code)

    if result.failure_class == FailureClass.SCHEMA_ERROR:
        if node_id.startswith("CC_"):
            return _find_artifact(snapshot_root, "capability_contracts", node_id)
        return _find_artifact(snapshot_root, "workflows", trace.workflow_code)

    if node_type == "intent":
        return _find_artifact(snapshot_root, "intents", node_id)

    if node_type == "capability_contract":
        return _find_artifact(snapshot_root, "capability_contracts", node_id)

    return _find_artifact(snapshot_root, "workflows", trace.workflow_code)


def _extract_ct_code(result: ClassificationResult) -> str | None:
    """Try to extract CT code from error event details."""
    if result.root_event is None:
        return None
    p = _payload(result.root_event)
    details = p.get("details", {})
    if isinstance(details, dict) and "artifact_code" in details:
        return details["artifact_code"]
    node_id = p.get("node_id", "")
    if node_id.startswith("CT_"):
        return node_id
    return None
