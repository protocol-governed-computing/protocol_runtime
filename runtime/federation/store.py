"""The evidence store as the federation sees it: a queue, claims, and outcomes under `data_root`.

Every write here is either an exclusive create or a rename into place, so a reader on another node
sees a record whole or not at all. Both are atomic on a local filesystem and on NFSv4, which is what
the store is when the nodes are hosts.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from runtime.coordinator import CoordinationRefused, sealed_placement_mode

AUTHORIZING_MODE = "FEDERATED_NODE"


class Store:
    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root)
        self.queue = self.data_root / "queue"
        self.claims = self.data_root / "claims"
        self.outcomes = self.data_root / "outcomes"

    def require_writable(self) -> None:
        """EO-2: a party does not start against a store it cannot write."""
        if not self.data_root.is_dir():
            raise CoordinationRefused(f"evidence store {self.data_root} is not reachable")
        for d in (self.queue, self.claims, self.outcomes):
            d.mkdir(parents=True, exist_ok=True)
        probe = self.data_root / f".probe-{uuid.uuid4().hex}"
        try:
            _create_exclusive(probe, {"probe": True})
            probe.unlink()
        except OSError as exc:
            raise CoordinationRefused(f"evidence store {self.data_root} is not writable: {exc}") from exc

    def enqueue(self, unit: dict[str, Any]) -> None:
        _create_exclusive(self.queue / f"{unit['unit_id']}.unit.json", unit)

    def queued(self) -> list[Path]:
        return sorted(self.queue.glob("*.unit.json"))

    def is_claimed(self, unit_id: str) -> bool:
        return (self.claims / f"{unit_id}.claim.json").exists()

    def claim(self, unit_id: str, worker: str) -> bool:
        """Take a unit, or learn it was already taken. The filesystem decides, once."""
        try:
            _create_exclusive(self.claims / f"{unit_id}.claim.json",
                              {"unit_id": unit_id, "worker": worker, "pid": os.getpid()})
        except FileExistsError:
            return False
        return True

    def record_outcome(self, outcome: dict[str, Any]) -> None:
        path = self.outcomes / f"{outcome['unit_id']}.outcome.json"
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def outcome(self, unit_id: str) -> dict[str, Any] | None:
        path = self.outcomes / f"{unit_id}.outcome.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def require_federated(snapshot_root: str | Path) -> None:
    """A party takes its authority from the sealed composition, never from its own arguments."""
    mode = sealed_placement_mode(Path(snapshot_root))
    if mode != AUTHORIZING_MODE:
        raise CoordinationRefused(
            f"the snapshot records placement {mode!r} and federation requires {AUTHORIZING_MODE!r}. "
            "A node cannot grant itself an arrangement the composition does not carry."
        )


def _create_exclusive(path: Path, record: dict[str, Any]) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
