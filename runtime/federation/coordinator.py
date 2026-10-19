"""The coordinating node: admits units into the queue and reports their outcomes. It never executes.

Served over HTTP because it is addressable by definition; that is what separates it from the process
pool `LOCAL_MULTI_WORKER` uses. The endpoint is internal to the node group:

    GET  /health          role, placement, snapshot_id
    POST /units           {"wf_fqdn", "payload", "snapshot_id"} -> 202 {"unit_id"}
    GET  /units/<id>      200 outcome | 202 {"state": "queued" | "claimed"} | 404

A unit is refused when its submitter holds a different snapshot. One authority means one sealed
composition, and a node group running two would be two authorities sharing an address.
"""

from __future__ import annotations

import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from runtime.boot import boot
from runtime.federation.store import AUTHORIZING_MODE, Store, require_federated


class FederatedCoordinator:
    def __init__(self, snapshot_root: str | Path, data_root: str | Path):
        self.snapshot_root = Path(snapshot_root)
        require_federated(self.snapshot_root)
        booted = boot(self.snapshot_root)
        self.snapshot_id = booted.snapshot_id
        self.domains = set(booted.domains)
        self.store = Store(data_root)
        self.store.require_writable()

    def health(self) -> dict[str, Any]:
        return {"role": "coordinator", "placement": AUTHORIZING_MODE, "snapshot_id": self.snapshot_id}

    def submit(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        wf_fqdn, payload = body.get("wf_fqdn"), body.get("payload")
        if not isinstance(wf_fqdn, str) or not isinstance(payload, dict):
            return 400, {"error": "a unit is a wf_fqdn string and a payload object"}
        if body.get("snapshot_id") != self.snapshot_id:
            return 409, {"error": f"submitter holds snapshot {body.get('snapshot_id')!r}; "
                                  f"this node group executes {self.snapshot_id!r}"}
        domain = wf_fqdn.split("::")[0]
        if domain not in self.domains:
            return 404, {"error": f"domain {domain!r} is not in the sealed snapshot"}
        unit_id = uuid.uuid4().hex
        self.store.enqueue({"unit_id": unit_id, "wf_fqdn": wf_fqdn, "payload": payload,
                            "snapshot_id": self.snapshot_id})
        return 202, {"unit_id": unit_id}

    def status(self, unit_id: str) -> tuple[int, dict[str, Any]]:
        outcome = self.store.outcome(unit_id)
        if outcome is not None:
            return 200, outcome
        if self.store.is_claimed(unit_id):
            return 202, {"unit_id": unit_id, "state": "claimed"}
        if (self.store.queue / f"{unit_id}.unit.json").exists():
            return 202, {"unit_id": unit_id, "state": "queued"}
        return 404, {"error": f"no unit {unit_id!r}"}


def serve(coordinator: FederatedCoordinator, bind: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PGCCoordinator/0"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[coordinator] " + (fmt % args) + "\n")

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, coordinator.health())
            elif self.path.startswith("/units/"):
                self._send(*coordinator.status(self.path[len("/units/"):]))
            else:
                self._send(404, {"error": f"no route {self.path!r}"})

        def do_POST(self) -> None:
            if self.path != "/units":
                self._send(404, {"error": f"no route {self.path!r}"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "body is not JSON"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "body is not a JSON object"})
                return
            self._send(*coordinator.submit(body))

    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"[coordinator] serving on http://{bind}:{port}  snapshot {coordinator.snapshot_id}  "
          f"store {coordinator.store.data_root}", flush=True)
    httpd.serve_forever()
