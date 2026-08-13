"""
scheduler.py — WF-level topology driver for the token-native runtime.

Traverses the compiled execution topology for a single workflow invocation.
Drives CC execution in declared order, resolves WF-level input bindings from
the ExecutionContext, routes between nodes on result status, and emits
WF-level trace events.

The scheduler is a blind executor:
    - All routing is read from dispatch.routing (compiled by S2/S3)
    - All CC input bindings are read from dispatch.bindings (compiled by S6)
    - Condition resolution uses the vocab (transition:: / outcome:: addresses)
    - No domain logic, no semantic inference, no path construction

Topology traversal rules:
    - Entry point is dispatch.entry[wf_addr]["start"]
    - Each CC produces a result_status; that status resolves to a condition address
    - The condition address is looked up in dispatch.routing[wf_addr][cc_addr] → next node
    - Traversal ends when no routing entry exists for the current (cc_addr, condition)

Boundary nodes (IN_, EXIT_):
    Nodes without a pipeline entry (not in dispatch.pipeline) are boundary nodes.
    IN_ nodes perform admission gating; prior to admission_snapshot integration,
    they pass through as ACK. The routing table routes ACK forward.
    EXIT_ nodes (no routing) terminate the loop naturally.

Bindings path grammar (WF-level, compiler-emitted):
    $.payload.<field>          — from the original payload
    $.inputs.<field>           — alias for $.payload.<field>
    $.results.<cc_addr>.<field>— from a prior CC's result surface (int cc_addr)
    <literal>                  — returned as-is

Result:
    (result_status, surface) from the last CC executed.
    result_status: the WF terminal outcome string (e.g. "SUCCESS", "VIOLATION")
    surface: the last CC's output dict (for transport/egress use)
"""

from __future__ import annotations

from typing import Any

from runtime.dispatcher import execute_cc
from runtime.evidence import TraceWriter
from runtime.loader import RuntimePackage
from runtime.memory import ExecutionContext

# Guard against pathological graphs (cycles, runaway traversal)
_MAX_HOPS = 64


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_wf(
    wf_fqdn:   str,
    payload:   dict[str, Any],
    pkg:       RuntimePackage,
    writer:    TraceWriter,
    data_root: str,
) -> tuple[str, dict[str, Any]]:
    """
    Execute a workflow topology and return (result_status, surface).

    Args:
        wf_fqdn:   Fully-qualified name of the workflow (e.g. "blockchain::WF_...").
        payload:   Inbound payload dict (already normalized by transport layer).
        pkg:       Frozen RuntimePackage (loader output for this domain).
        writer:    TraceWriter for this execution trace.
        data_root: Absolute data directory root for CS path expansion.

    Returns:
        (result_status, surface) where:
            result_status — terminal WF outcome (e.g. "SUCCESS", "VIOLATION")
            surface       — last CC output dict (passed to transport egress)

    Raises:
        KeyError:    WF FQDN not in vocab or entry table.
        RuntimeError: Hop limit exceeded (indicates a compiler-emitted cycle).
    """
    wf_addr = pkg.vocab.addr(wf_fqdn)

    entry = pkg.dispatch.entry.get(wf_addr)
    if entry is None:
        raise RuntimeError(
            f"No entry point for WF {wf_fqdn!r} (addr {wf_addr}) — "
            f"snapshot may be stale or domain mismatch"
        )

    rb_addr = entry.get("rb", -1)  # -1 = no runtime binding (CT-only workflow, no CS to govern)
    current_addr: int | None = entry["start"]
    current_node_key: str = entry.get("start_key", "")
    actor_context = entry.get("actor")  # Authority: actor FQDN bound to this WF

    # Bind the actor into the execution context — genuinely propagated through the run (not merely
    # logged), then attributed in the trace. No authorization is enforced (authority model TBD).
    ctx = ExecutionContext(payload, actor=actor_context)
    writer.wf_start(payload, actor=ctx.actor)

    result_status = "SUCCESS"
    surface: dict[str, Any] = {}
    hops = 0

    while current_addr is not None:
        if hops >= _MAX_HOPS:
            raise RuntimeError(
                f"WF {wf_fqdn!r} exceeded {_MAX_HOPS} topology hops — "
                f"possible cycle in compiled routing"
            )
        hops += 1

        if current_addr in pkg.dispatch.pipeline:
            # CC node — resolve WF-level bindings and execute.
            # Bindings are keyed by node_key (not CC addr) so that distinct WF
            # usages of the same CC (e.g. four denial audit nodes) each carry
            # their own literal inputs (e.g. different denial_reason values).
            wf_bindings = (
                pkg.dispatch.bindings
                .get(wf_addr, {})
                .get(current_node_key, {})
            )
            cc_inputs = ctx.resolve_inputs(wf_bindings)

            result_status, surface = execute_cc(
                current_addr, rb_addr, cc_inputs, pkg, writer, data_root, wf_addr
            )
            ctx.record_result(current_addr, surface)

            # Observation: a CC outcome that routes to an emitting exit fires a domain event.
            ev_fqdn = pkg.dispatch.emits.get(wf_addr, {}).get(current_addr, {}).get(result_status)
            if ev_fqdn:
                writer.event(ev_fqdn, surface)

        else:
            # Boundary node (IN_, EXIT_) — no pipeline
            # admission_snapshot not yet integrated; IN_ nodes pass as ACK
            result_status = "ACK"

        # Resolve result_status → condition address and route to next node.
        # Routing values are {"addr": int, "key": str} — addr is the next CC address,
        # key is the next node_key for bindings disambiguation.
        condition_addr = _condition_addr(result_status, pkg)
        routing = pkg.dispatch.routing.get(wf_addr, {}).get(current_addr, {})
        next_entry = routing.get(condition_addr)  # None → terminal
        if isinstance(next_entry, dict):
            current_addr = next_entry.get("addr")
            current_node_key = next_entry.get("key", "")
        else:
            current_addr = next_entry  # bare int (legacy) or None
            current_node_key = ""

    writer.wf_complete(result_status)
    return result_status, surface


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _condition_addr(result_status: str, pkg: RuntimePackage) -> int:
    """
    Resolve a result_status string to its transition address integer.

    Lookup order:
        1. transition::<result_status>  (primary — WF routing namespace)
        2. outcome::<result_status>     (fallback — CC outcome namespace)

    Returns -1 if the status has no registered address (no routing will match).
    """
    try:
        return pkg.vocab.addr(f"transition::{result_status}")
    except KeyError:
        pass
    try:
        return pkg.vocab.addr(f"outcome::{result_status}")
    except KeyError:
        pass
    return -1
