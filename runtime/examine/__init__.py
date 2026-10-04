"""
trace_examiner — what a completed run did, read from its trace alone.

Public API:
    analyze(trace_path) -> DiagnosticReport

Reads a trace written to `SCHEMA_TRACE_EVENT_V2` and reports the path the run took, node by node,
the contracts it ran and their results, the events it announced, the non-deterministic results it
recorded, and whether it completed or failed structurally. It needs no snapshot: the trace names
the snapshot it ran under, and everything reported is in the trace.
"""

from __future__ import annotations

from pathlib import Path

from runtime.examine.parser import ParsedTrace, TraceParseError, parse_trace
from runtime.examine.reporter import DiagnosticReport


def analyze(trace_path: Path) -> DiagnosticReport:
    trace = parse_trace(trace_path)
    complete = trace.of("WF_COMPLETE")
    routes = trace.of("WF_ROUTE")
    ending = routes[-1]["detail"].get("to_node") if routes and complete else None

    results: dict[str, str | None] = {}
    for e in trace.of("CC_COMPLETE"):
        results[e["detail"].get("node", "")] = e["result_status"]
    contracts = [(e["detail"].get("node", ""), e["detail"].get("cc_fqdn", ""),
                  results.get(e["detail"].get("node", ""))) for e in trace.of("CC_START")]

    return DiagnosticReport(
        trace_id=trace.trace_id,
        workflow=trace.wf_fqdn,
        snapshot_id=trace.snapshot_id,
        outcome=complete[-1]["result_status"] if complete else None,
        ending=ending,
        path=[(e["detail"].get("from_node", ""), e["result_status"], e["detail"].get("to_node"))
              for e in routes],
        contracts=contracts,
        events=[e["detail"].get("ev_fqdn", "") for e in trace.of("EVENT")],
        recorded=[e["detail"].get("step_fqdn", "") for e in trace.of("CT_STEP")
                  if e["detail"].get("purity") == "ct_impure" and not e["detail"].get("replayed")],
        errors=[e["detail"].get("message", "") for e in trace.of("ERROR")],
    )


__all__ = ["analyze", "DiagnosticReport", "ParsedTrace", "TraceParseError"]
