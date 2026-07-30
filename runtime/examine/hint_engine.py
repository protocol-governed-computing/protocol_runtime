"""
hint_engine.py — Prescriptive fix hint generation.

Governed by: Trace Examiner spec §G4

Generates short, concrete, actionable fix hints from failure classification.
Every hint references a specific artifact and field. No generic messages.
Pure function — no external imports.
"""

from __future__ import annotations

from typing import Any

from runtime.examine.classifier import ClassificationResult, FailureClass
from runtime.examine.parser import ParsedTrace, TraceEventDict


def _payload(event: TraceEventDict) -> dict[str, Any]:
    return event.get("payload", {})


def generate_hint(
    trace: ParsedTrace,
    result: ClassificationResult,
    artifact_path: str | None,
) -> str:
    """
    Generate a prescriptive fix hint from classification result.

    Args:
        trace: Parsed trace.
        result: Classification result.
        artifact_path: Resolved artifact path (may be None).

    Returns:
        Concrete, actionable fix hint string.
    """
    fc = result.failure_class
    if fc is None:
        return "No action required"

    node_id = result.node_id or "unknown node"
    error_code = result.error_code or ""
    message = result.message

    if fc == FailureClass.EXPRESSION_ERROR:
        return _hint_expression_error(node_id, message, artifact_path)

    if fc == FailureClass.SCHEMA_ERROR:
        return _hint_schema_error(node_id, message, artifact_path)

    if fc == FailureClass.BINDING_ERROR:
        return _hint_binding_error(node_id, error_code, message, artifact_path)

    if fc == FailureClass.CT_STRUCTURE_ERROR:
        return _hint_ct_error(node_id, error_code, message, artifact_path, result)

    if fc == FailureClass.CS_RUNTIME_ERROR:
        return _hint_cs_error(node_id, message, artifact_path)

    if fc == FailureClass.ADMISSION_ERROR:
        return _hint_admission_error(trace, result, error_code, message, artifact_path)

    if fc == FailureClass.GRAPH_STRUCTURE_ERROR:
        return _hint_graph_error(trace, result, artifact_path)

    if fc == FailureClass.BUSINESS_VIOLATION:
        return f"Business violation at {node_id} — this is domain behavior, not a bug"

    return f"Check {artifact_path or 'workflow artifacts'} for issues at {node_id}"


def _hint_expression_error(
    node_id: str, message: str, artifact_path: str | None
) -> str:
    """Hint for EXPRESSION_RESOLUTION_FAILED."""
    # Try to extract the expression path from the message
    # Message format is typically: "Cannot resolve $.payload.field_name"
    expr = _extract_expression(message)
    if expr:
        field_name = expr.split(".")[-1] if "." in expr else expr
        target = f" in {artifact_path}" if artifact_path else ""
        return (
            f"Add '{field_name}' to input payload or fix expression '{expr}' "
            f"in input_bindings for node {node_id}{target}"
        )
    target = f" — check {artifact_path}" if artifact_path else ""
    return f"Fix unresolved expression in input_bindings for node {node_id}{target}"


def _hint_schema_error(
    node_id: str, message: str, artifact_path: str | None
) -> str:
    """Hint for SCHEMA_VALIDATION_FAILED."""
    target = f" in {artifact_path}" if artifact_path else ""
    return f"Fix schema violation at node {node_id}{target}: {_truncate(message, 100)}"


def _hint_binding_error(
    node_id: str, error_code: str, message: str, artifact_path: str | None
) -> str:
    """Hint for BINDING_RESOLUTION_FAILED."""
    target = f" in {artifact_path}" if artifact_path else ""
    if "missing" in message.lower() or "not found" in message.lower():
        return (
            f"Add missing binding entry for {node_id}{target} — "
            f"check runtime binding and CC pipeline bindings"
        )
    return f"Fix binding resolution for {node_id}{target}: {_truncate(message, 100)}"


def _hint_ct_error(
    node_id: str,
    error_code: str,
    message: str,
    artifact_path: str | None,
    result: ClassificationResult,
) -> str:
    """Hint for CT_* errors."""
    target = f" — check {artifact_path}" if artifact_path else ""

    if error_code == "CT_ARTIFACT_NOT_FOUND":
        artifact_code = _extract_ct_code_from_result(result)
        if artifact_code:
            return f"CT artifact '{artifact_code}' not found{target}. Run build or check atom registration"
        return f"CT artifact not found for {node_id}{target}. Run build or check atom registration"

    if error_code == "CT_VALIDATION_FAILED":
        return f"CT validation failed for {node_id}{target}: {_truncate(message, 100)}"

    if error_code == "CT_EXECUTION_FAILED":
        return f"CT execution failed at {node_id}{target}: {_truncate(message, 80)}"

    if error_code == "CAPABILITY_NOT_FOUND":
        return f"Capability contract '{node_id}' not found in loaded contracts{target}"

    if error_code == "CAPABILITY_DISPATCH_FAILED":
        return f"Capability dispatch failed for {node_id}{target}: {_truncate(message, 80)}"

    return f"Transform error at {node_id}{target}: {_truncate(message, 80)}"


def _hint_cs_error(
    node_id: str, message: str, artifact_path: str | None
) -> str:
    """Hint for CS_EXECUTION_FAILED."""
    target = f" — check runtime binding at {artifact_path}" if artifact_path else ""
    return f"Side-effect execution failed at {node_id}{target}: {_truncate(message, 80)}"


def _hint_admission_error(
    trace: ParsedTrace,
    result: ClassificationResult,
    error_code: str,
    message: str,
    artifact_path: str | None,
) -> str:
    """Hint for ADMISSION_ERROR — missing binding field or admission denial."""
    target = f" — see {artifact_path}" if artifact_path else ""

    if error_code == "ADMISSION_BINDING_MISSING_FIELD":
        # Extract the missing payload field from the structured message.
        # Message format: "Admission binding for 'EV_...' references payload field 'field_name' ..."
        import re
        field_match = re.search(r"payload field '([^']+)'", message)
        event_match = re.search(r"binding for '([^']+)'", message)
        field = field_match.group(1) if field_match else "unknown"
        event_code = event_match.group(1) if event_match else "unknown"

        return (
            f"Admission gate for {trace.workflow_code} requires '{field}' in payload{target}. "
            f"Binding: {event_code} → payload.{field}. "
            f"Ensure the caller forwards '{field}' from the upstream workflow that produced it "
            f"(e.g. via output of the registration step)."
        )

    # Generic admission denial
    return (
        f"Admission denied for {trace.workflow_code}{target}. "
        f"Check admission.requires conditions are satisfied before invoking this workflow. "
        f"Detail: {_truncate(message, 100)}"
    )


def _hint_graph_error(
    trace: ParsedTrace,
    result: ClassificationResult,
    artifact_path: str | None,
) -> str:
    """Hint for GRAPH_STRUCTURE_ERROR."""
    target = f" in {artifact_path}" if artifact_path else ""
    exit_reason = trace.exit_reason_code

    if exit_reason == "NO_TRANSITION":
        # Try to extract from/status from exit_condition message
        condition = trace.exit_condition
        return (
            f"No transition edge found{target}. "
            f"Add missing edge for the result_status in workflow spec. "
            f"Detail: {_truncate(condition, 100)}"
        )

    if exit_reason == "NO_ENTRY_NODE":
        return f"DAG has no entry node{target}. Check workflow spec node definitions"

    if exit_reason == "NODE_NOT_FOUND":
        return f"Referenced node missing from DAG{target}. Check edge targets in workflow spec"

    return f"Graph structure error ({exit_reason}){target}"


# --- Helpers ---


def _extract_expression(message: str) -> str:
    """Try to extract a $.path expression from an error message."""
    # Look for $.payload.xxx or $.capability_result.xxx patterns
    import re

    match = re.search(r'(\$\.\w+(?:\.\w+)*)', message)
    if match:
        return match.group(1)
    return ""


def _extract_ct_code_from_result(result: ClassificationResult) -> str:
    """Try to extract CT code from result details."""
    if result.root_event is None:
        return ""
    p = _payload(result.root_event)
    details = p.get("details", {})
    if isinstance(details, dict) and "artifact_code" in details:
        return details["artifact_code"]
    node_id = p.get("node_id", "")
    if node_id.startswith("CT_"):
        return node_id
    return ""


def _truncate(s: str, max_len: int) -> str:
    """Truncate string with ellipsis if too long."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
