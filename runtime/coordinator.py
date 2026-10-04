"""A coordinating party that runs several runtimes.

`CONSTITUTION_EXECUTION_PLACEMENT_V1` authorizes `LOCAL_MULTI_WORKER`: more than one worker process
on one host, each drawing work from a coordinating party and executing a governed topology in its
own process. This is that party.

**It is not part of the runtime, and the runtime knows nothing of it.** Placement §4 obliges a
runtime to execute the governed topology *without consulting placement mode for branching* — a
runtime that behaved differently for being one of several would make one snapshot mean two things
depending on where it ran. So nothing here changes how a workflow executes. Each worker calls
`run_workflow` exactly as a lone runtime would, against the same sealed snapshot, under the same
closure, reaching the determination it would have reached alone.

What this adds is *which* runtime receives *which* unit, and that is a placement question.

**Claim before execute.** A unit is claimed by creating its claim record exclusively; the claim
either succeeds or the unit was already taken. Only unclaimed units are ever dispatched, so a unit
whose worker was lost is not re-dispatched.

That over-refuses, deliberately. A worker that claimed and died before its first effect leaves work
nobody retries, even though retrying would have been safe. The alternative — inferring *begun* from
effects — means reading state a worker may be mid-write on, which is the condition it is trying to
detect. SM-7a obliges a realization that can apply a transition partly to determine what state
results; declining to resume is how this platform declines to be in that position, and the profile
says so rather than discovering it here.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.api import run_workflow

AUTHORIZING_MODE = "LOCAL_MULTI_WORKER"


class CoordinationRefused(RuntimeError):
    """The snapshot does not authorize coordination, or the store cannot hold a claim."""


@dataclass
class Unit:
    """One workflow run: an identity, a workflow, and its payload."""
    unit_id: str
    wf_fqdn: str
    payload: dict[str, Any]


@dataclass
class Outcome:
    unit_id: str
    state: str                      # executed | claimed_elsewhere | failed
    status: Any = None
    detail: str = ""
    worker: int | None = None


@dataclass
class Dispatch:
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def executed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.state == "executed"]

    @property
    def unclaimed(self) -> list[str]:
        """Units still available to a later dispatch — never those already claimed."""
        return [o.unit_id for o in self.outcomes if o.state == "failed"]


def sealed_placement_mode(snapshot_root: Path) -> str | None:
    """The placement mode the snapshot records.

    Read from the composition rather than from configuration: what a snapshot was permitted was
    settled when it was sealed, and a coordinator that took its authority from its own arguments
    would be granting itself the permission it is meant to be checking.
    """
    for path in (Path(snapshot_root) / "canonical").rglob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        frontmatter = record.get("frontmatter") or {}
        fqdn = str(record.get("fqdn") or record.get("fqdn_id") or "")
        if frontmatter.get("artifact_kind") == "STRUCTURE" and "EXECUTION_PLACEMENT" in fqdn:
            mode = frontmatter.get("placement_mode")
            if isinstance(mode, str):
                return mode
    return None


def _claim(claims_root: Path, unit_id: str, worker: int) -> bool:
    """Take a unit, or report that it was already taken.

    Exclusive creation is the whole mechanism: the filesystem decides, once, and the loser of a race
    learns it lost rather than discovering it later in the evidence. A claim that checked for
    existence and then wrote would leave a window in which two workers both believed they had it.
    """
    claims_root.mkdir(parents=True, exist_ok=True)
    path = claims_root / f"{unit_id}.claim.json"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as exc:
        raise CoordinationRefused(f"claim could not be recorded for {unit_id}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"unit_id": unit_id, "worker": worker, "pid": os.getpid()}, handle, sort_keys=True)
    return True


def _execute(unit: Unit, snapshot_root: str, data_root: str, claims_root: str, worker: int) -> Outcome:
    """Claim, then execute. Runs in a worker process."""
    if not _claim(Path(claims_root), unit.unit_id, worker):
        return Outcome(unit.unit_id, "claimed_elsewhere", worker=worker)
    try:
        result = run_workflow(
            wf_fqdn=unit.wf_fqdn,
            payload=unit.payload,
            data_root=data_root,
            snapshot_root=snapshot_root,
        )
    except Exception as exc:
        # A lost or failed run leaves its claim standing. The unit is begun, and a begun unit is
        # not re-dispatched — that is the point, not an oversight.
        return Outcome(unit.unit_id, "failed", detail=f"{type(exc).__name__}: {exc}", worker=worker)
    status = result[0] if isinstance(result, tuple) else result
    return Outcome(unit.unit_id, "executed", status=status, worker=worker)


class Coordinator:
    """Dispatches units to worker processes against one sealed snapshot."""

    def __init__(self, snapshot_root: str | Path, data_root: str | Path, workers: int = 2):
        self.snapshot_root = Path(snapshot_root)
        self.data_root = Path(data_root)
        self.workers = int(workers)

        if self.workers < 2:
            raise CoordinationRefused(
                f"a coordinating party runs several runtimes; {self.workers} is not several. "
                "One worker is LOCAL_SINGLE_NODE, which needs no coordinator."
            )

        mode = sealed_placement_mode(self.snapshot_root)
        if mode != AUTHORIZING_MODE:
            raise CoordinationRefused(
                f"the snapshot records placement {mode!r} and coordination requires "
                f"{AUTHORIZING_MODE!r}. Placement is what a composition was permitted when it was "
                f"sealed; a coordinator cannot grant itself an arrangement the composition does not "
                f"carry."
            )

        # Claims live beside the evidence and outlive any one worker: a claim held in a worker's
        # memory would be released by the loss it exists to survive.
        self.claims_root = self.data_root / "claims"

    def dispatch(self, units: list[Unit]) -> Dispatch:
        """Run every unclaimed unit across the workers."""
        dispatch = Dispatch()
        if not units:
            return dispatch

        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(
                    _execute, unit, str(self.snapshot_root), str(self.data_root),
                    str(self.claims_root), i % self.workers,
                ): unit
                for i, unit in enumerate(units)
            }
            for future in as_completed(futures):
                dispatch.outcomes.append(future.result())

        dispatch.outcomes.sort(key=lambda o: o.unit_id)
        return dispatch
