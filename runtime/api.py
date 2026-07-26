"""Programmatic runtime entry — run a workflow against a snapshot and return its result surface.

This is the in-process core behind `runtime run`: it loads the domain snapshot, opens a trace, drives
the workflow topology via `scheduler.run_wf`, and returns the terminal status **and result surface**. The
CLI is a thin wrapper over it, and out-of-process consumers that need the workflow surface programmatically
(e.g. the change-management validation pipeline) call this directly instead of shelling out and parsing
stdout or data files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.boot import boot
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
) -> RunResult:
    """Warm-boot the assembled snapshot and execute a workflow; return `(status, surface, trace)`.

    The snapshot (assembled product) is read-only input, verified via its manifest (root of trust).
    All mutable output is scoped to the instance root `data_root`: CS state and `data_root/traces/`.
    `snapshot_root` defaults to the sibling `../snapshot` when None.
    Raises on load/vocab errors and propagates runtime exceptions (after recording them to the trace).
    """
    data_root = Path(data_root)
    domain = wf_fqdn.split("::")[0]

    booted = boot(snapshot_root)
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
                         wf_addr=wf_addr, wf_fqdn=wf_fqdn)
    try:
        status, surface = run_wf(wf_fqdn=wf_fqdn, payload=payload, pkg=pkg,
                                 writer=writer, data_root=str(data_root))
    except Exception as exc:
        writer.error(str(exc))
        raise
    finally:
        writer.close()

    return RunResult(status=status, surface=surface or {}, trace_id=trace_id, trace_dir=trace_dir)
