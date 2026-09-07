"""
classifier.py — Deterministic failure classification.

Governed by: STRUCTURE_TRACE_SCHEMA_V0 §10, Trace Examiner spec §4-5

Classification is rule-based and deterministic — driven by structured
trace fields only. Never string-parse error messages.

Rules are applied in spec-defined order. First match wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from runtime.examine.parser import ParsedTrace, TraceEventDict


class FailureClass(Enum):
    """Failure classification categories per spec §4."""

    BUSINESS_VIOLATION = "BUSINESS_VIOLATION"
    EXPRESSION_ERROR = "EXPRESSION_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    CT_STRUCTURE_ERROR = "CT_STRUCTURE_ERROR"
    BINDING_ERROR = "BINDING_ERROR"
    CS_RUNTIME_ERROR = "CS_RUNTIME_ERROR"
    GRAPH_STRUCTURE_ERROR = "GRAPH_STRUCTURE_ERROR"
    ADMISSION_ERROR = "ADMISSION_ERROR"


# Failure classes that trigger escalation (exit(1)) in authoring mode
STRUCTURAL_FAILURES = frozenset(FailureClass) - {FailureClass.BUSINESS_VIOLATION}


@dataclass
class ClassificationResult:
    """Result of classifying a trace."""

    failure_class: FailureClass | None
    root_event: TraceEventDict | None  # The earliest causal event
    is_structural: bool  # True if failure_class is not BUSINESS_VIOLATION
    node_id: str | None  # Failing node if identifiable
    error_code: str | None  # Structured error code if from error event
    message: str  # Human-readable reason


def _payload(event: TraceEventDict) -> dict[str, Any]:
    """Extract payload from event, defaulting to empty dict."""
    return event.get("payload", {})


def _classify_error_event(event: TraceEventDict) -> FailureClass:
    """
    Classify a single error event per spec §4 rules 1-2, 4-5, 8-9.

    Rules applied in order:
      Rule 1: error_code == EXPRESSION_RESOLUTION_FAILED → EXPRESSION_ERROR
      Rule 2: error_code == SCHEMA_VALIDATION_FAILED → SCHEMA_ERROR
      Rule 4: node_category == CT → CT_STRUCTURE_ERROR
      Rule 5: node_category == CS → CS_RUNTIME_ERROR
      Rule 8: error_code == BINDING_RESOLUTION_FAILED → BINDING_ERROR
      Rule 9: catch-all error → CT_STRUCTURE_ERROR
    """
    p = _payload(event)
    error_code = p.get("error_code", "")
    node_category = p.get("node_category", "")

    # Rule 1
    if error_code == "EXPRESSION_RESOLUTION_FAILED":
        return FailureClass.EXPRESSION_ERROR

    # Rule 2
    if error_code == "SCHEMA_VALIDATION_FAILED":
        return FailureClass.SCHEMA_ERROR

    # Rule 4
    if node_category == "CT":
        return FailureClass.CT_STRUCTURE_ERROR

    # Rule 5
    if node_category == "CS":
        return FailureClass.CS_RUNTIME_ERROR

    # Rule 8
    if error_code == "BINDING_RESOLUTION_FAILED":
        return FailureClass.BINDING_ERROR

    # Admission errors (pre-DAG enforcement failures)
    if error_code in ("ADMISSION_BINDING_MISSING_FIELD", "ADMISSION_DENIED"):
        return FailureClass.ADMISSION_ERROR

    # Rule 9: catch-all structural
    return FailureClass.CT_STRUCTURE_ERROR


def _is_business_violation(event: TraceEventDict, trace: ParsedTrace) -> bool:
    """
    Check Rule 3: node_end with NACK status on intent node.

    We need to look up the node_type from the corresponding node_start event.
    """
    p = _payload(event)
    if event.get("event_type") != "node_end":
        return False
    if p.get("status") != "NACK":
        return False

    # Determine node_type from matching node_start event
    node_id = p.get("node_id", "")
    for ev in trace.events:
        if (
            ev.get("event_type") == "node_start"
            and _payload(ev).get("node_id") == node_id
        ):
            if _payload(ev).get("node_type") == "intent":
                return True
            break

    return False


def classify(trace: ParsedTrace) -> ClassificationResult:
    """
    Classify a parsed trace per spec §4-5.

    Scans events in trace order. Applies classification rules.
    Returns on first structural match (single-root diagnosis, V0 constraint).

    If no error event exists, falls back to workflow_complete authority (§5A).

    Args:
        trace: Parsed trace from parser.parse_trace()

    Returns:
        ClassificationResult with failure class, root event, and metadata.
    """
    # Fast path: successful execution — but scan for unhappy-path exits
    if trace.status == "SUCCESS":
        unhappy = _detect_unhappy_path(trace)
        if unhappy is not None:
            return unhappy
        return ClassificationResult(
            failure_class=None,
            root_event=None,
            is_structural=False,
            node_id=None,
            error_code=None,
            message="Execution completed successfully",
        )

    # Scan events in sequence order for root cause (spec §5).
    # Parser guarantees events are sorted by sequence number.
    # First structural match wins (single-root, V0).
    for event in trace.events:
        event_type = event.get("event_type")
        p = _payload(event)

        # Rule 3: business violation check (node_end with NACK on intent)
        if event_type == "node_end" and p.get("status") == "NACK":
            if _is_business_violation(event, trace):
                return ClassificationResult(
                    failure_class=FailureClass.BUSINESS_VIOLATION,
                    root_event=event,
                    is_structural=False,
                    node_id=p.get("node_id"),
                    error_code=None,
                    message=f"Business violation at {p.get('node_id', 'unknown')}: NACK",
                )

        # Rules 1, 2, 4, 5, 8, 9: error event classification
        if event_type == "error":
            failure_class = _classify_error_event(event)
            return ClassificationResult(
                failure_class=failure_class,
                root_event=event,
                is_structural=failure_class in STRUCTURAL_FAILURES,
                node_id=p.get("node_id"),
                error_code=p.get("error_code"),
                message=p.get("message", "Unknown error"),
            )

        # Also check node_end with failure status (spec §5 criterion 2)
        # Schema defines lowercase "failed"; also accept uppercase for robustness
        if event_type == "node_end" and p.get("status", "").lower() in ("failed", "failure", "error"):
            # No error event preceded this — classify as generic structural
            return ClassificationResult(
                failure_class=FailureClass.CT_STRUCTURE_ERROR,
                root_event=event,
                is_structural=True,
                node_id=p.get("node_id"),
                error_code=None,
                message=f"Node {p.get('node_id', 'unknown')} ended with {p.get('status')}",
            )

    # §5A — Workflow Complete Authority
    # No error event found, but workflow failed. Use exit_reason_code.
    wc = trace.workflow_complete_event
    if wc and trace.status in ("FAILURE", "ABORT", "TIMEOUT"):
        return _classify_from_workflow_complete(trace)

    # No failure detected despite non-SUCCESS status
    return ClassificationResult(
        failure_class=None,
        root_event=None,
        is_structural=False,
        node_id=None,
        error_code=None,
        message=f"Workflow ended with status {trace.status} but no classifiable failure found",
    )


_HAPPY_STATUSES = frozenset({"SUCCESS", "ACK", "completed"})


def _detect_unhappy_path(trace: ParsedTrace) -> ClassificationResult | None:
    """
    Detect unhappy-path exits on SUCCESS workflows.

    Even when the workflow completes successfully (valid EXIT path), a
    capability contract ending with NOT_FOUND, VIOLATION, etc. may indicate
    an early-exit path. We flag it as BUSINESS_VIOLATION only when the
    non-success CC status led directly to an EXIT/TERMINAL node — meaning
    the workflow terminated early rather than continuing normally.
    """
    # Build ordered list of node transitions from trace events
    node_sequence: list[tuple[str, str, str]] = []  # (node_id, status, node_type)
    node_types: dict[str, str] = {}

    for event in trace.events:
        et = event.get("event_type")
        p = _payload(event)
        if et == "node_start":
            node_types[p.get("node_id", "")] = p.get("node_type", "")
        elif et == "node_end":
            nid = p.get("node_id", "")
            node_sequence.append((nid, p.get("status", ""), node_types.get(nid, "")))

    # Scan for CC nodes with non-happy status that are immediately followed by EXIT
    for i, (node_id, status, node_type) in enumerate(node_sequence):
        if node_type != "capability_contract":
            continue
        if status in _HAPPY_STATUSES:
            continue

        # Check if the very next node is EXIT or TERMINAL
        if i + 1 < len(node_sequence):
            next_node_id = node_sequence[i + 1][0]
            next_node_type = node_types.get(next_node_id, "")
            if next_node_id in ("EXIT", "TERMINAL") or next_node_type == "exit":
                # Find the original node_end event for reporting
                for event in trace.node_end_events:
                    if _payload(event).get("node_id") == node_id:
                        return ClassificationResult(
                            failure_class=FailureClass.BUSINESS_VIOLATION,
                            root_event=event,
                            is_structural=False,
                            node_id=node_id,
                            error_code=None,
                            message=(
                                f"{node_id} returned {status} — workflow took "
                                f"early-exit path. Check payload data matches "
                                f"expected state"
                            ),
                        )

    return None


def _classify_from_workflow_complete(trace: ParsedTrace) -> ClassificationResult:
    """
    Classify failure from workflow_complete event (§5A authority).

    Used when no error events exist but workflow_complete indicates failure.
    Classification is based on exit_reason_code.
    """
    wc = trace.workflow_complete_event
    p = _payload(wc) if wc else {}
    exit_reason_code = trace.exit_reason_code

    # Rules 6, 7: graph structure errors
    if exit_reason_code in ("NO_TRANSITION", "NO_ENTRY_NODE", "NODE_NOT_FOUND", "EXIT_NOT_FOUND"):
        return ClassificationResult(
            failure_class=FailureClass.GRAPH_STRUCTURE_ERROR,
            root_event=wc,
            is_structural=True,
            node_id=None,
            error_code=None,
            message=f"Graph structure error: {exit_reason_code}",
        )

    if exit_reason_code == "TIMEOUT":
        return ClassificationResult(
            failure_class=None,
            root_event=wc,
            is_structural=False,
            node_id=None,
            error_code=None,
            message="Execution timed out",
        )

    if exit_reason_code == "ABORT":
        return ClassificationResult(
            failure_class=None,
            root_event=wc,
            is_structural=False,
            node_id=None,
            error_code=None,
            message="Execution aborted by policy",
        )

    # Business/policy rejections
    if exit_reason_code in (
        "ADMISSION_DENIED",
        "GOVERNANCE_VIOLATION",
        "EXIT_VIOLATION",
        "EXIT_REJECTED",
        "EXIT_ALREADY_EXISTS",
    ):
        return ClassificationResult(
            failure_class=FailureClass.BUSINESS_VIOLATION,
            root_event=wc,
            is_structural=False,
            node_id=None,
            error_code=None,
            message=f"Policy rejection: {exit_reason_code}",
        )

    # Backend/infrastructure failure
    if exit_reason_code == "EXIT_BACKEND_ERROR":
        return ClassificationResult(
            failure_class=FailureClass.CS_RUNTIME_ERROR,
            root_event=wc,
            is_structural=True,
            node_id=None,
            error_code=None,
            message=f"Backend error at exit: {trace.exit_condition}",
        )

    # EXECUTION_ERROR or unknown
    return ClassificationResult(
        failure_class=FailureClass.CT_STRUCTURE_ERROR,
        root_event=wc,
        is_structural=True,
        node_id=None,
        error_code=None,
        message=f"Execution failed: {trace.exit_condition}",
    )
