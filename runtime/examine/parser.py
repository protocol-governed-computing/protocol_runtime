"""
parser.py — JSONL trace loader with strict field validation.

Governed by: STRUCTURE_TRACE_SCHEMA_V0

Reads a completed trace JSONL file and returns a ParsedTrace.
Validates required fields per schema — rejects malformed events.
Imports only json and pathlib — no execution/machine imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Type alias — raw dict from JSONL line
TraceEventDict = dict[str, Any]

# ─── Required fields per SCHEMA_TRACE_EVENT_V0 ───
# Duplicated as constants (spec §6: no imports from execution/machine/)

# Version pin — examiner rejects traces produced by incompatible schema versions.
# Increment when execution_start or workflow_complete payload shape changes.
_EXPECTED_TRACE_SCHEMA_VERSION = "TRACE_SCHEMA_V0"

# Every event must have these top-level fields
_REQUIRED_TOP_LEVEL = ("event_type", "timestamp", "execution_id", "sequence")

# Per-event-type required payload fields (from schema allOf conditionals)
_REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    "execution_start": ("workflow_code", "trace_schema_version"),
    "node_start": ("node_id", "node_type"),
    "node_end": ("node_id", "status", "duration_ms"),
    "workflow_complete": ("status", "duration_ms", "exit_condition", "exit_reason_code"),
    "capability_dispatch": ("cc_code", "node_id"),
    "transform_start": ("ct_code",),
    "transform_end": ("ct_code", "duration_ms"),
    "context_snapshot": ("context_hash", "sequence"),
    "error": ("error_code", "message", "node_category"),
}


@dataclass
class ParsedTrace:
    """
    Structured representation of a completed trace.

    Extracted from JSONL trace file after execution.
    """

    execution_id: str
    workflow_code: str
    trace_schema_version: str  # from execution_start.payload.trace_schema_version
    events: list[TraceEventDict]
    status: str  # from workflow_complete.payload.status
    exit_reason_code: str  # from workflow_complete.payload.exit_reason_code
    exit_condition: str  # from workflow_complete.payload.exit_condition

    # Convenience: pre-indexed subsets
    error_events: list[TraceEventDict] = field(default_factory=list)
    node_end_events: list[TraceEventDict] = field(default_factory=list)
    capability_dispatch_events: list[TraceEventDict] = field(default_factory=list)
    workflow_complete_event: TraceEventDict | None = None


class TraceParseError(Exception):
    """Raised when a trace file cannot be parsed or contains invalid events."""

    pass


def _validate_event(event: TraceEventDict, line_num: int, trace_path: Path) -> None:
    """
    Validate required fields on a single trace event.

    Checks:
      1. Top-level required fields (event_type, timestamp, execution_id, sequence)
      2. Per-event-type required payload fields from SCHEMA_TRACE_EVENT_V0

    Raises:
        TraceParseError on missing required field.
    """
    # Top-level required fields
    for field_name in _REQUIRED_TOP_LEVEL:
        if field_name not in event:
            raise TraceParseError(
                f"Event at line {line_num} missing required field '{field_name}' "
                f"in {trace_path}"
            )

    # Per-event-type payload validation
    event_type = event["event_type"]
    required_payload_fields = _REQUIRED_PAYLOAD.get(event_type)
    if required_payload_fields is not None:
        payload = event.get("payload")
        if payload is None:
            raise TraceParseError(
                f"Event '{event_type}' at line {line_num} missing 'payload' "
                f"in {trace_path}"
            )
        for field_name in required_payload_fields:
            if field_name not in payload:
                raise TraceParseError(
                    f"Event '{event_type}' at line {line_num} missing required "
                    f"payload field '{field_name}' in {trace_path}"
                )


def parse_trace(trace_path: Path) -> ParsedTrace:
    """
    Parse a JSONL trace file into a ParsedTrace.

    Args:
        trace_path: Path to the .jsonl trace file.

    Returns:
        ParsedTrace with all events loaded and indexed.

    Raises:
        TraceParseError: If trace is missing, empty, or structurally invalid.
    """
    if not trace_path.exists():
        raise TraceParseError(f"Trace file not found: {trace_path}")

    events: list[TraceEventDict] = []
    error_events: list[TraceEventDict] = []
    node_end_events: list[TraceEventDict] = []
    capability_dispatch_events: list[TraceEventDict] = []
    workflow_complete_event: TraceEventDict | None = None

    with open(trace_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise TraceParseError(
                    f"Invalid JSON at line {line_num} in {trace_path}: {e}"
                )

            _validate_event(event, line_num, trace_path)
            events.append(event)

            event_type = event.get("event_type")
            if event_type == "error":
                error_events.append(event)
            elif event_type == "node_end":
                node_end_events.append(event)
            elif event_type == "capability_dispatch":
                capability_dispatch_events.append(event)
            elif event_type == "workflow_complete":
                workflow_complete_event = event

    if not events:
        raise TraceParseError(f"Empty trace file: {trace_path}")

    # Enforce monotonic sequence ordering (spec §5: earliest by sequence wins)
    # Events must be in sequence order for deterministic classification.
    # If file order doesn't match sequence order, sort explicitly.
    sequences = [e.get("sequence", 0) for e in events]
    if sequences != sorted(sequences):
        events.sort(key=lambda e: e.get("sequence", 0))
        # Re-index after sort
        error_events = [e for e in events if e.get("event_type") == "error"]
        node_end_events = [e for e in events if e.get("event_type") == "node_end"]
        capability_dispatch_events = [e for e in events if e.get("event_type") == "capability_dispatch"]

    # Extract execution_id from first event
    execution_id = events[0].get("execution_id", "")
    if not execution_id:
        raise TraceParseError(f"First event missing execution_id in {trace_path}")

    # Extract workflow_code and trace_schema_version from execution_start
    workflow_code = ""
    trace_schema_version = ""
    for event in events:
        if event.get("event_type") == "execution_start":
            payload = event.get("payload", {})
            workflow_code = payload.get("workflow_code", "")
            trace_schema_version = payload.get("trace_schema_version", "")
            break

    if not workflow_code:
        raise TraceParseError(f"No execution_start event with workflow_code in {trace_path}")

    if trace_schema_version != _EXPECTED_TRACE_SCHEMA_VERSION:
        raise TraceParseError(
            f"Trace schema version mismatch in {trace_path}: "
            f"expected '{_EXPECTED_TRACE_SCHEMA_VERSION}', got '{trace_schema_version}'"
        )

    # Extract final status from workflow_complete
    if workflow_complete_event is None:
        raise TraceParseError(f"No workflow_complete event in {trace_path}")

    wc_payload = workflow_complete_event.get("payload", {})
    status = wc_payload.get("status", "")
    exit_reason_code = wc_payload.get("exit_reason_code", "")
    exit_condition = wc_payload.get("exit_condition", "")

    if not status:
        raise TraceParseError(f"workflow_complete missing status in {trace_path}")
    if not exit_reason_code:
        raise TraceParseError(f"workflow_complete missing exit_reason_code in {trace_path}")

    return ParsedTrace(
        execution_id=execution_id,
        workflow_code=workflow_code,
        trace_schema_version=trace_schema_version,
        events=events,
        status=status,
        exit_reason_code=exit_reason_code,
        exit_condition=exit_condition,
        error_events=error_events,
        node_end_events=node_end_events,
        capability_dispatch_events=capability_dispatch_events,
        workflow_complete_event=workflow_complete_event,
    )
