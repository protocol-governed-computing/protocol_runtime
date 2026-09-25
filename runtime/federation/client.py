"""How a node reaches the coordinator. Stdlib HTTP only; the runtime carries no dependencies."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from runtime.coordinator import CoordinationRefused


def _call(url: str, body: dict[str, Any] | None = None, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (urllib.error.URLError, OSError) as exc:
        raise CoordinationRefused(f"coordinator {url} is not reachable: {exc}") from exc


def health(coordinator_url: str) -> dict[str, Any]:
    code, body = _call(f"{coordinator_url}/health")
    if code != 200:
        raise CoordinationRefused(f"coordinator {coordinator_url} is unhealthy: {code} {body}")
    return body


def require_same_snapshot(coordinator_url: str, snapshot_id: str) -> None:
    """EO-2: a node starts only when its coordinator is reachable and executes the same snapshot."""
    theirs = health(coordinator_url).get("snapshot_id")
    if theirs != snapshot_id:
        raise CoordinationRefused(
            f"coordinator {coordinator_url} executes snapshot {theirs!r}; this node holds {snapshot_id!r}"
        )


def submit_and_wait(coordinator_url: str, *, wf_fqdn: str, payload: dict[str, Any], snapshot_id: str,
                    timeout: float, poll: float = 0.1) -> dict[str, Any]:
    """Enqueue a unit and return its outcome record once a worker has written one."""
    code, body = _call(f"{coordinator_url}/units",
                       {"wf_fqdn": wf_fqdn, "payload": payload, "snapshot_id": snapshot_id})
    if code != 202:
        raise CoordinationRefused(f"coordinator refused the unit: {code} {body.get('error', body)}")
    unit_id = body["unit_id"]
    deadline = time.monotonic() + timeout
    while True:
        code, body = _call(f"{coordinator_url}/units/{unit_id}")
        if code == 200:
            return body
        if code != 202:
            raise CoordinationRefused(f"unit {unit_id} lost at the coordinator: {code} {body}")
        if time.monotonic() > deadline:
            raise CoordinationRefused(
                f"unit {unit_id} has no outcome after {timeout}s (state {body.get('state')!r}). "
                "It is not re-submitted: a claimed unit is begun, and a begun unit is not re-dispatched."
            )
        time.sleep(poll)
