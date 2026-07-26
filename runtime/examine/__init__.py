"""
trace_examiner — Post-execution diagnostic module.

Governed by: STRUCTURE_TRACE_SCHEMA_V0 §10, Trace Examiner spec

Public API:
    analyze(trace_path) -> DiagnosticReport

Reads completed JSONL trace, classifies failures deterministically,
resolves artifact paths, generates prescriptive fix hints.

Module boundaries (spec §6):
    - No imports from execution/machine/
    - parser.py imports only json, pathlib
    - locator.py imports only structure.path_registry
    - classifier.py, hint_engine.py, reporter.py — pure functions
"""

from __future__ import annotations

from pathlib import Path

from runtime.examine.parser import parse_trace, ParsedTrace, TraceParseError
from runtime.examine.classifier import classify, FailureClass, STRUCTURAL_FAILURES
from runtime.examine.locator import locate_artifact
from runtime.examine.hint_engine import generate_hint
from runtime.examine.reporter import DiagnosticReport, SideEffectOutcome


def _extract_side_effect_outcomes(trace: ParsedTrace) -> list[SideEffectOutcome]:
    """
    Extract side-effect outcomes from capability dispatch + node_end correlation.

    Identifies CC nodes that dispatched capability contracts and correlates
    them with their node_end status to produce business-visible outcomes.
    """
    dispatched_cc: dict[str, str] = {}  # node_id -> cc_code
    for event in trace.capability_dispatch_events:
        p = event.get("payload", {})
        node_id = p.get("node_id", "")
        cc_code = p.get("cc_code", "")
        if node_id and cc_code:
            dispatched_cc[node_id] = cc_code

    # Correlate with node_end events to get result status
    outcomes: list[SideEffectOutcome] = []
    for event in trace.node_end_events:
        p = event.get("payload", {})
        node_id = p.get("node_id", "")
        if node_id in dispatched_cc:
            outcomes.append(SideEffectOutcome(
                cc_code=dispatched_cc[node_id],
                result_status=p.get("status", "UNKNOWN"),
            ))

    return outcomes


def analyze(trace_path: Path) -> DiagnosticReport:
    """
    Analyze a completed trace file and produce a diagnostic report.

    This is the single public entry point for the Trace Examiner.

    Snapshot root is derived from the trace path per STRUCTURE layout convention:
      trace_path = {workspace}/traces/{trace_id}/{trace_id}.jsonl
      snapshot   = {workspace}/protocol_snapshot

    Args:
        trace_path: Path to a completed .jsonl trace file.

    Returns:
        DiagnosticReport with failure classification, artifact path, and fix hint.

    Raises:
        TraceParseError: If the trace file is missing, empty, or malformed.
    """
    # Derive snapshot root from trace path (STRUCTURE layout: traces/{id}/{id}.jsonl)
    snapshot_root: Path | None = None
    candidate = trace_path.parent.parent.parent / "protocol_snapshot"
    if candidate.is_dir():
        snapshot_root = candidate

    # 1. Parse
    trace = parse_trace(trace_path)

    # 2. Classify
    result = classify(trace)

    # 3. Locate artifact
    artifact_path = None
    if result.failure_class is not None:
        artifact_path = locate_artifact(trace, result, snapshot_root)

    # 4. Generate hint
    fix_hint = generate_hint(trace, result, artifact_path)

    # 5. Extract side-effect outcomes
    side_effect_outcomes = _extract_side_effect_outcomes(trace)

    # 6. Build report
    is_structural = (
        result.failure_class is not None
        and result.failure_class in STRUCTURAL_FAILURES
    )

    return DiagnosticReport(
        execution_id=trace.execution_id,
        workflow_code=trace.workflow_code,
        has_structural_failure=is_structural,
        failure_class=result.failure_class,
        failing_node=result.node_id,
        reason=result.message,
        artifact_path=artifact_path,
        fix_hint=fix_hint,
        side_effect_outcomes=side_effect_outcomes,
    )


__all__ = [
    "analyze",
    "DiagnosticReport",
    "SideEffectOutcome",
    "FailureClass",
    "TraceParseError",
]
