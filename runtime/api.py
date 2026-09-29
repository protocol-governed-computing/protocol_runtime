"""Programmatic runtime entry — run a workflow against a snapshot and return its result surface.

This is the in-process core behind `runtime run`: it loads the domain snapshot, opens a trace, drives
the workflow topology via `scheduler.run_wf`, and returns the terminal status **and result surface**. The
CLI is a thin wrapper over it, and out-of-process consumers that need the workflow surface programmatically
(e.g. the change-management validation pipeline) call this directly instead of shelling out and parsing
stdout or data files.
"""
from __future__ import annotations

import functools
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.boot import BootedSnapshot, boot, default_snapshot_root
from runtime.evidence import TraceWriter, make_trace_id
from runtime.scheduler import run_wf


@dataclass(frozen=True)
class RunResult:
    status: str                     # terminal workflow outcome (e.g. "SUCCESS", "ACK", "VIOLATION")
    surface: dict[str, Any]         # workflow result surface (the observable outputs)
    trace_id: str
    trace_dir: Path


def run_workflow(
    *,
    wf_fqdn: str,
    payload: dict[str, Any],
    data_root: str | Path,
    snapshot_root: str | Path | None = None,
    replay_trace: str | Path | None = None,
) -> RunResult:
    """Warm-boot the assembled snapshot and execute a workflow; return `(status, surface, trace)`.

    The snapshot (assembled product) is read-only input, verified via its manifest (root of trust).
    All mutable output is scoped to the instance root `data_root`: CS state and `data_root/traces/`.
    `snapshot_root` defaults to the sibling `../snapshot` when None.
    Raises on load/vocab errors and propagates runtime exceptions (after recording them to the trace).
    """
    data_root = Path(data_root)
    domain = wf_fqdn.split("::")[0]

    booted = resident(snapshot_root)
    pkg = booted.domains.get(domain)
    if pkg is None:
        raise RuntimeError(
            f"Domain {domain!r} is not in the assembled snapshot "
            f"(manifest domains: {sorted(booted.domains)})."
        )

    trace_id = make_trace_id(domain, wf_fqdn, payload)
    wf_code = wf_fqdn.split("::")[-1]
    trace_dir = data_root / "traces" / domain / wf_code / trace_id
    trace_dir.mkdir(parents=True, exist_ok=True)

    wf_addr = pkg.vocab.addr(wf_fqdn)   # KeyError if the WF is not in the snapshot vocab
    writer = TraceWriter(trace_dir=trace_dir, trace_id=trace_id, domain=domain,
                         wf_addr=wf_addr, wf_fqdn=wf_fqdn, snapshot_root=booted.snapshot_root,
                         snapshot_id=booted.snapshot_id)
    if replay_trace is not None:
        # A replay substitutes each recorded outcome of an atom declared not deterministic, so it
        # reproduces the original determination rather than drawing a new one.
        from runtime.replay import recorded_outcomes
        writer.replay_from(recorded_outcomes(Path(replay_trace)))
    try:
        status, surface = run_wf(wf_fqdn=wf_fqdn, payload=payload, pkg=pkg,
                                 writer=writer, data_root=str(data_root))
    except Exception as exc:
        writer.error(str(exc))
        raise
    finally:
        # The JSONL is the evidence. A picture of the path is a view of it, drawn on request
        # (`run --behavior-logic`, or `behavior-logic <trace>`), never as a cost of every run.
        writer.close()

    return RunResult(status=status, surface=surface or {}, trace_id=trace_id, trace_dir=trace_dir)


# Warm boot is load once, verify once. Every call booting afresh re-verified the whole snapshot from
# its bytes on each run — about 60% of a run's time, repeated by every server, worker and suite that
# calls this in a loop. The package a boot produces is frozen, and nothing reads the snapshot again
# after it, so one verified boot serves every run against the same snapshot.
#
# What makes it the same snapshot is the manifest — its identity, and the file itself — together
# with what acceptance was judged against: the trust anchor and the profile root. A rebuild in place
# rewrites the manifest, so it is verified afresh. `boot()` itself is never cached: the CLI's `boot`
# and anything asking for acceptance get the full determination every time.
_RESIDENT: dict[tuple, BootedSnapshot] = {}
_RESIDENT_LOCK = threading.Lock()
_RESIDENT_LIMIT = 4


def _resident_key(root: Path) -> tuple | None:
    manifest = root / "manifest.json"
    try:
        stat = manifest.stat()
    except FileNotFoundError:
        return None
    return (str(root), stat.st_mtime_ns, stat.st_size,
            os.environ.get("PGC_TRUST_ROOT_PUBKEY"), os.environ.get("PGC_SNAPSHOT_PROFILES"))


def resident(snapshot_root: str | Path | None = None) -> BootedSnapshot:
    """The booted snapshot at `snapshot_root`, verified once per process and per manifest."""
    given = Path(snapshot_root) if snapshot_root is not None else default_snapshot_root()
    key = _resident_key(given.resolve())
    if key is None:
        return boot(given)          # no manifest: let boot refuse it, and say why
    with _RESIDENT_LOCK:
        booted = _RESIDENT.get(key)
        if booted is None:
            booted = boot(given)
            if len(_RESIDENT) >= _RESIDENT_LIMIT:
                _RESIDENT.pop(next(iter(_RESIDENT)))
            _RESIDENT[key] = booted
        return booted


@functools.lru_cache(maxsize=8)
def _placement(snapshot_root: Path) -> str | None:
    # The snapshot is immutable, so what it was permitted cannot change under a running process.
    from runtime.coordinator import sealed_placement_mode
    return sealed_placement_mode(snapshot_root)


def invoke_workflow(
    *,
    wf_fqdn: str,
    payload: dict[str, Any],
    data_root: str | Path,
    snapshot_root: str | Path | None = None,
) -> RunResult:
    """Have a workflow executed wherever the sealed composition places execution.

    The entry point for a party that receives work but is not itself where work is placed — the
    interaction boundary. Under `FEDERATED_NODE` the unit goes to the coordinator named by
    `PGC_COORDINATOR_URL` and a worker executes it; under every other placement it runs here through
    `run_workflow`. Either way the result is the same `RunResult`, with `trace_dir` under the shared
    `data_root`.

    Under `FEDERATED_NODE` this waits for the unit's outcome without limit once it is admitted, so
    a failure it raises is always one in which nothing was admitted (see `submit_and_wait`).

    Placement is read from the snapshot, not from configuration: a boundary that chose its own
    arrangement would be granting itself what the composition was or was not permitted. Execution
    itself never consults placement — this decides only where `run_workflow` is called.
    """
    root = Path(snapshot_root) if snapshot_root is not None else default_snapshot_root()
    if _placement(root.resolve()) != "FEDERATED_NODE":
        return run_workflow(wf_fqdn=wf_fqdn, payload=payload, data_root=data_root, snapshot_root=root)

    from runtime.federation.client import submit_and_wait

    coordinator_url = os.environ.get("PGC_COORDINATOR_URL")
    if not coordinator_url:
        raise RuntimeError(
            "the snapshot places execution on FEDERATED_NODE and PGC_COORDINATOR_URL is not set: "
            "this node does not execute, and has nowhere to send the work."
        )
    booted = resident(root)  # a boundary acts on no snapshot it has not authenticated (OB-1)
    outcome = submit_and_wait(coordinator_url.rstrip("/"), wf_fqdn=wf_fqdn, payload=payload,
                              snapshot_id=booted.snapshot_id)
    if outcome.get("state") != "executed":
        raise RuntimeError(f"unit {outcome.get('unit_id')} failed on {outcome.get('worker')}: "
                           f"{outcome.get('detail')}")
    return RunResult(status=outcome["status"], surface=outcome.get("surface") or {},
                     trace_id=outcome["trace_id"], trace_dir=Path(data_root) / outcome["trace_dir"])
