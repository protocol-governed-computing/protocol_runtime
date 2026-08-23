"""
evidence.py — Structured trace event emitter for the token-native runtime.

Writes append-only JSONL trace events to the trace output directory.
Each execution event is a self-contained JSON line with:
    - trace_id      — deterministic per-invocation ID
    - event_type    — WF_START, CC_START, CC_STEP, CC_COMPLETE, WF_COMPLETE, ERROR
    - domain        — domain/structure identifier (e.g. "blockchain")
    - wf_addr       — integer WF address
    - cc_addr       — integer CC address (None for WF-level events)
    - step_addr     — integer step (CT/CS) address (None for CC-level events)
    - step_op       — CS operation name or None for CT steps
    - result_status — outcome string (e.g. "SUCCESS") or None
    - detail        — arbitrary dict (serializable)
    - ts_ns         — monotonic nanosecond timestamp

The trace file is written to:
    <traces_root>/<domain>/<wf_code>/<trace_id>/<trace_id>.jsonl

subdomain is not known to this module — caller passes the full trace dir path.

This module produces the raw JSONL evidence only. Evidence projection
(execution-path PNG overlay) is handled separately in trace_viz.py.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


CLASSIFICATION_FQDN = "vocabulary::VOCAB_EVIDENCE_CONTENT_CLASSIFICATION_V0"


def _content_classification(snapshot_root: Path) -> dict[str, list[str]]:
    """Which trace content is determinative and which observational, from the sealed composition.

    `3e` EV-5 requires the distinction be declared rather than inferred, and the declaration is a
    governed artifact rather than a constant here — adding a field is an authoring act, sealed and
    attested. No fallback: evidence written under an unknown classification is evidence a checker
    cannot compare, which is the state EV-5 exists to end.
    """
    for path in (snapshot_root / "canonical").rglob("*.json"):
        if path.name == "metadata.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("fqdn_id") != CLASSIFICATION_FQDN:
            continue
        fm = data.get("frontmatter") or {}
        return {
            "determinative": list(fm["determinative_fields"]["entries"]),
            "observational": list(fm["observational_fields"]["entries"]),
        }
    raise RuntimeError(
        f"{CLASSIFICATION_FQDN} is not in the composition at {snapshot_root} — the "
        f"determinative/observational classification this trace would be written under is not in "
        f"force (3e EV-5)."
    )


class TraceWriter:
    """
    Append-only trace writer for a single workflow execution.

    Usage:
        writer = TraceWriter(trace_dir, trace_id, domain, wf_addr, wf_fqdn)
        writer.wf_start(payload)
        writer.cc_start(cc_addr, cc_fqdn, cc_inputs)
        writer.cc_step(cc_addr, step_addr, step_fqdn, op, result)
        writer.cc_complete(cc_addr, result_status, outputs)
        writer.wf_complete(result_status)
        writer.close()
    """

    def __init__(
        self,
        trace_dir: Path,
        trace_id: str,
        domain: str,
        wf_addr: int,
        wf_fqdn: str,
        snapshot_root: Path,
        snapshot_id: str,
    ) -> None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        self._path = trace_dir / f"{trace_id}.jsonl"
        self._fh = self._path.open("a", encoding="utf-8")
        self._trace_id = trace_id
        self._domain = domain
        self._wf_addr = wf_addr
        self._wf_fqdn = wf_fqdn

        # The trace states which of its content is determinative, as its first record. Evidence that
        # carried the values and not the classification would be evidence a checker had to guess at,
        # and `3e` §5.2 says a checker that guesses is deciding for itself what governance meant.
        # Carried in the trace rather than looked up later so the record is checkable by a party with
        # no access to the producing system (EV-16, AI-16).
        classification = _content_classification(snapshot_root)
        self._fh.write(json.dumps({
            "trace_schema_version": "v0",
            "event_type": "trace_classification",
            "classified_by": CLASSIFICATION_FQDN,
            # Which closure applied — `3e` §3.1 point 1. At execution the sealed snapshot IS the
            # closure: SN-10 makes it the sole source of governed behaviour, so naming it names
            # every governing element that applied and every rule the closure supplied. A checker
            # holding this id and the snapshot can resolve every address below to what governed it.
            "snapshot_id": snapshot_id,
            **classification,
        }, separators=(",", ":")) + "\n")
        self._fh.flush()

    # --- Public event methods ---

    def wf_start(self, payload: dict[str, Any], actor: str | None = None) -> None:
        # Authority: attribute the actor context this WF executes under (declaration/binding only —
        # not an authorization decision). Absent when the WF binds no actor.
        detail = {"wf_fqdn": self._wf_fqdn, "payload_keys": list(payload.keys())}
        if actor:
            detail["actor"] = actor
        self._emit("WF_START", detail=detail)

    def event(self, ev_fqdn: str, payload: dict[str, Any]) -> None:
        # Observation: a governed domain event (EV_) emitted during execution, recorded in the trace.
        self._emit("EVENT", detail={"ev_fqdn": ev_fqdn, "payload": payload})

    def cc_start(self, cc_addr: int, cc_fqdn: str, cc_inputs: dict[str, Any]) -> None:
        self._emit("CC_START", cc_addr=cc_addr, detail={"cc_fqdn": cc_fqdn, "inputs": cc_inputs})

    def cc_step(
        self,
        cc_addr: int,
        step_addr: int,
        step_fqdn: str,
        op: str | None,
        result: dict[str, Any],
    ) -> None:
        self._emit(
            "CC_STEP",
            cc_addr=cc_addr,
            step_addr=step_addr,
            step_op=op,
            detail={"step_fqdn": step_fqdn, "result_keys": list(result.keys())},
        )

    def cc_complete(
        self,
        cc_addr: int,
        cc_fqdn: str,
        result_status: str,
        outputs: dict[str, Any],
    ) -> None:
        self._emit(
            "CC_COMPLETE",
            cc_addr=cc_addr,
            result_status=result_status,
            detail={"cc_fqdn": cc_fqdn, "output_keys": list(outputs.keys())},
        )

    def wf_complete(self, result_status: str) -> None:
        self._emit("WF_COMPLETE", result_status=result_status, detail={"wf_fqdn": self._wf_fqdn})

    def route(self, from_addr: int | None, condition: str, to_addr: int | None) -> None:
        """The routing determination — `3e` §3.1 point 4, the dominant consequence.

        The trace recorded the sequence of nodes and not the decision that produced it. A reader
        could see that one contract followed another and not that the transition was the one the
        sealed routing table declared for that outcome. Recording the decision makes the path
        checkable against the representation rather than merely consistent with it (EX-15).

        `to_addr` None is terminal: the outcome routed nowhere, which is the traversal ending.
        """
        self._emit("WF_ROUTE", cc_addr=from_addr, step_addr=to_addr, result_status=condition,
                   detail={"terminal": to_addr is None})

    def error(self, message: str, **extra: Any) -> None:
        self._emit("ERROR", detail={"message": message, **extra})

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    # --- Internal ---

    def _emit(
        self,
        event_type: str,
        cc_addr: int | None = None,
        step_addr: int | None = None,
        step_op: str | None = None,
        result_status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "trace_schema_version": "v0",
            "trace_id":      self._trace_id,
            "event_type":    event_type,
            "domain":        self._domain,
            "wf_addr":       self._wf_addr,
            "cc_addr":       cc_addr,
            "step_addr":     step_addr,
            "step_op":       step_op,
            "result_status": result_status,
            "detail":        detail or {},
            "ts_ns":         time.monotonic_ns(),
        }
        self._fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        self._fh.flush()


# ---------------------------------------------------------------------------
# Trace ID generation
# ---------------------------------------------------------------------------

def make_trace_id(domain: str, wf_fqdn: str, payload: dict[str, Any]) -> str:
    """
    Generate a human-sortable trace ID from (domain, wf_fqdn, payload).

    Format: YYYYMMDDTHHMMSSmmmZ__WF_CODE__XXXX
        - Timestamp prefix (UTC, millisecond resolution) — chronological sort
        - WF code extracted from wf_fqdn — self-describing
        - 4-char uppercase hex suffix from sha256(wf_fqdn + payload) — weak idempotency signal

    Example: 20260524T151422183Z__WF_CREATE_WALLET_V0__A7K2

    Not purely deterministic (timestamp advances each call). The hash suffix
    signals identical-input executions without enforcing uniqueness.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    wf_code = wf_fqdn.split("::")[-1]  # e.g. "WF_CREATE_WALLET_V0"

    canonical = json.dumps(
        {"domain": domain, "wf": wf_fqdn, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    suffix = hashlib.sha256(canonical.encode()).hexdigest()[:4].upper()

    return f"{ts}__{wf_code}__{suffix}"
