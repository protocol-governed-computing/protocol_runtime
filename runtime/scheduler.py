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

def _admit(payload: dict, contract: dict) -> str:
    """Determine admission against the gate's declared input contract — ACK or NACK.

    Determined from what the IN declares and nothing else: a required field absent, or a declared
    type unsatisfied, is NACK. The workflow routes on that outcome exactly as it routes on any other,
    so a refusal here is carried by the topology rather than raised past it.

    The IN also carries prose `extensions.admission_rules` ("each element must be a positive
    integer"). Prose determines nothing (MB-1) and is not consulted. Where a gate must enforce more
    than its declared contract, the contract is what needs to say so.
    """
    _TYPES = {"array": list, "string": str, "integer": int, "number": (int, float),
              "boolean": bool, "object": dict}
    for field, spec in contract.items():
        present = field in payload
        if spec.get("required") and not present:
            return "NACK"
        if not present:
            continue
        expected = _TYPES.get(spec.get("type"))
        if expected is not None and not isinstance(payload[field], expected):
            return "NACK"
    return "ACK"


class UnroutedOutcomeError(RuntimeError):
    """An outcome with neither declared routing nor a declared ending (EX-5, RT-9)."""


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

            # Observation: a CC outcome that routes to an announcing exit states the moments that
            # act completed, in the order the composition sealed. The order is normative — it is what
            # a reader of the account sees — so the sequence is announced as sealed and never
            # reordered here. A sequence of one is the ordinary case.
            announced = pkg.dispatch.emits.get(wf_addr, {}).get(current_addr, {}).get(result_status)
            if announced:
                # A single moment was sealed as a string before announcements could be plural. Read
                # here rather than refused, so a snapshot built by an older compiler still runs.
                if isinstance(announced, str):
                    announced = [announced]
                for ev_fqdn in announced:
                    if not ev_fqdn:
                        # A moment declared for this transition and absent from what was sealed. The
                        # act says so rather than announcing nothing: silence is indistinguishable
                        # from a moment nobody declared, which is the failure this exists to end.
                        writer.event("", {"unannounceable": True, "transition": result_status})
                        continue
                    writer.event(ev_fqdn, surface)

        else:
            # Boundary node — an IN admission gate. EXIT nodes carry no address and are reached as
            # a declared ending rather than traversed, so this branch is IN only.
            #
            # This returned an unconditional "ACK". A declared admission point that determines
            # nothing produces the same outcome as one that permits, which is `1c` AI-6 — the
            # invariant whose breach is least visible, because the system behaves like a governed
            # one until the case arrives that governance would have refused.
            contract = pkg.dispatch.admission.get(current_addr)
            if contract is None:
                writer.error("no admission contract", node=current_addr)
                writer.wf_complete("VIOLATION")
                raise UnroutedOutcomeError(
                    f"admission gate {current_addr} declares no input contract — there is nothing "
                    f"to determine admission against, and absence is not permission (1c AI-6)."
                )
            result_status = _admit(payload, contract)
            writer.cc_step(current_addr, current_addr, pkg.vocab.fqdn(current_addr),
                           "ADMIT", {"outcome": result_status})

        # Resolve result_status → condition address and route to next node.
        # Routing values are {"addr": int, "key": str} — addr is the next CC address,
        # key is the next node_key for bindings disambiguation.
        previous_addr = current_addr
        condition_addr = _condition_addr(result_status, pkg)
        routing = pkg.dispatch.routing.get(wf_addr, {}).get(current_addr, {})
        next_entry = routing.get(condition_addr)

        if isinstance(next_entry, dict):
            current_addr = next_entry.get("addr")
            current_node_key = next_entry.get("key", "")
        elif next_entry is not None:
            current_addr = next_entry  # bare int (legacy)
            current_node_key = ""
        else:
            # No continuation. Two cases that were one, and reporting success for both is what
            # `3a` EX-5 and `3c` RT-9 forbid: an outcome the declarations do not answer for MUST
            # refuse, and ending the traversal instead made a dead end indistinguishable from a
            # declared ending. Termination is now declared (`dispatch.terminal`); its absence is
            # the dead end.
            ending = pkg.dispatch.terminal.get(wf_addr, {}).get(current_addr, {}).get(condition_addr)
            if ending is None:
                writer.error(
                    "unrouted outcome",
                    node=current_addr, outcome=result_status, condition_addr=condition_addr,
                )
                writer.route(from_addr=current_addr, condition=result_status, to_addr=None)
                writer.wf_complete("VIOLATION")
                raise UnroutedOutcomeError(
                    f"outcome {result_status!r} from node {current_addr} has neither declared "
                    f"routing nor a declared ending in this workflow — the declarations do not "
                    f"answer for it (3a EX-5, 3c RT-9)."
                )
            current_addr = None
            current_node_key = ""
            declared_ending = ending.get("exit", "")

        # Evidence the routing determination, not only its effect (`3e` §3.1 point 4).
        writer.route(from_addr=previous_addr, condition=result_status, to_addr=current_addr)

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
