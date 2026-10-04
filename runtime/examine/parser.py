"""
parser.py — reads a completed trace as `SCHEMA_TRACE_EVENT_V2` describes it.

A trace is a classification header, then events sharing one envelope. This reader refuses any other
shape rather than guessing at it: the examiner once read RI-0's trace format and could not read one
line the PGC runtime writes, and nothing said so until someone ran it.

Imports only json and pathlib.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = "v2"

_HEADER_FIELDS = ("trace_schema_version", "event_type", "classified_by", "snapshot_id")
_ENVELOPE_FIELDS = ("trace_schema_version", "trace_id", "event_type", "domain", "wf_addr",
                    "cc_addr", "step_addr", "step_op", "result_status", "detail", "ts_ns")


class TraceParseError(Exception):
    """The file is not a trace this runtime writes."""


@dataclass
class ParsedTrace:
    trace_id: str
    snapshot_id: str
    domain: str
    wf_fqdn: str
    events: list[dict[str, Any]]

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event_type"] == event_type]


def parse_trace(trace_path: Path) -> ParsedTrace:
    if not trace_path.is_file():
        raise TraceParseError(f"no trace at {trace_path}")
    lines = [l for l in trace_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        raise TraceParseError(f"{trace_path} is empty")
    try:
        records = [json.loads(l) for l in lines]
    except json.JSONDecodeError as exc:
        raise TraceParseError(f"{trace_path}: a line is not JSON — {exc}") from exc

    header, events = records[0], records[1:]
    if header.get("event_type") != "trace_classification":
        raise TraceParseError(f"{trace_path} does not begin with the classification header")
    for name in _HEADER_FIELDS:
        if name not in header:
            raise TraceParseError(f"{trace_path}: the header carries no {name!r}")
    for n, event in enumerate(records, 1):
        if event.get("trace_schema_version") != TRACE_SCHEMA_VERSION:
            raise TraceParseError(
                f"{trace_path}:{n} is trace schema {event.get('trace_schema_version')!r}; this "
                f"examiner reads {TRACE_SCHEMA_VERSION!r} only")
    for n, event in enumerate(events, 2):
        missing = [f for f in _ENVELOPE_FIELDS if f not in event]
        if missing:
            raise TraceParseError(f"{trace_path}:{n} {event.get('event_type')} lacks {missing}")

    start = next((e for e in events if e["event_type"] == "WF_START"), None)
    if start is None:
        raise TraceParseError(f"{trace_path} records no WF_START")
    return ParsedTrace(trace_id=start["trace_id"], snapshot_id=header["snapshot_id"],
                       domain=start["domain"], wf_fqdn=start["detail"].get("wf_fqdn", ""),
                       events=events)
