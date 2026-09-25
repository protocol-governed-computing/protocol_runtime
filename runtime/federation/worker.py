"""A worker node: takes queued units by claiming them, and executes each exactly as a lone runtime would.

**Claim before execute**, as in `runtime.coordinator`: a unit is claimed by exclusive creation, and
a claimed unit is never taken again. A worker lost after claiming leaves its unit begun and
un-retried. Work nobody claimed stays in the queue for any other worker, which is how a lost node's
un-started work moves (EO-5) without anyone deciding to move it.

A worker executes only units sealed against the snapshot it booted. A unit carrying another
snapshot's identity is left unclaimed rather than refused on someone else's behalf.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

from runtime.api import run_workflow
from runtime.boot import boot
from runtime.federation.client import require_same_snapshot
from runtime.federation.store import Store, require_federated


class FederatedWorker:
    def __init__(self, snapshot_root: str | Path, data_root: str | Path, coordinator_url: str,
                 worker_id: str | None = None):
        self.snapshot_root = Path(snapshot_root)
        require_federated(self.snapshot_root)
        self.snapshot_id = boot(self.snapshot_root).snapshot_id
        self.store = Store(data_root)
        self.store.require_writable()
        require_same_snapshot(coordinator_url, self.snapshot_id)
        self.worker_id = worker_id or socket.gethostname()

    def run_once(self) -> list[str]:
        """Take and execute every unit available now. Returns the ids this worker executed."""
        taken: list[str] = []
        for path in self.store.queued():
            unit = json.loads(path.read_text(encoding="utf-8"))
            unit_id = unit["unit_id"]
            if unit.get("snapshot_id") != self.snapshot_id or self.store.is_claimed(unit_id):
                continue
            if not self.store.claim(unit_id, self.worker_id):
                continue
            self.store.record_outcome(self._execute(unit))
            taken.append(unit_id)
        return taken

    def run_forever(self, poll: float = 0.2) -> None:
        print(f"[worker {self.worker_id}] snapshot {self.snapshot_id}  store {self.store.data_root}",
              flush=True)
        while True:
            for unit_id in self.run_once():
                print(f"[worker {self.worker_id}] executed {unit_id}", flush=True)
            time.sleep(poll)

    def _execute(self, unit: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {"unit_id": unit["unit_id"], "worker": self.worker_id,
                                  "snapshot_id": self.snapshot_id}
        try:
            run = run_workflow(wf_fqdn=unit["wf_fqdn"], payload=unit["payload"],
                               data_root=self.store.data_root, snapshot_root=self.snapshot_root)
            record.update(state="executed", status=run.status, surface=run.surface, trace_id=run.trace_id,
                          trace_dir=run.trace_dir.relative_to(self.store.data_root).as_posix())
            json.dumps(record)
        except Exception as exc:
            # The claim stands. A failed unit is begun, and a begun unit is not re-dispatched.
            record = {"unit_id": unit["unit_id"], "worker": self.worker_id, "snapshot_id": self.snapshot_id,
                      "state": "failed", "detail": f"{type(exc).__name__}: {exc}"}
        return record
