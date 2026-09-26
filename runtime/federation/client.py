"""How a node reaches the coordinator. Stdlib HTTP only; the runtime carries no dependencies."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
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


def _call_awaited(url: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """A call whose answer is waited for however long it takes.

    Connecting is bounded, because a coordinator that cannot be connected to never received the
    request. Once connected there is no bound: the coordinator may be blocked on the evidence store,
    and a caller that gave up then could be told a unit failed that is later admitted and executed.
    """
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=10.0)
    try:
        connection.connect()
    except OSError as exc:
        raise CoordinationRefused(f"coordinator {url} is not reachable: {exc}") from exc
    connection.sock.settimeout(None)
    try:
        data = None if body is None else json.dumps(body).encode("utf-8")
        connection.request("GET" if body is None else "POST", parts.path, body=data,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def submit_and_wait(coordinator_url: str, *, wf_fqdn: str, payload: dict[str, Any], snapshot_id: str,
                    poll: float = 0.1) -> dict[str, Any]:
    """Enqueue a unit and return its outcome record once a worker has written one.

    **Block, never misreport.** A failure is raised only while it is still true that nothing was
    admitted: the coordinator could not be connected to, or it refused the unit. Once a unit is
    admitted its outcome is waited for without limit — through a store outage, a coordinator
    restart, or a queue no worker is draining — because every one of those ends with the unit
    executed, and reporting failure first would contradict the evidence it later leaves. A caller
    that will not wait ends the wait itself; that is the caller's timeout, not a determination.

    One case stays ambiguous: the connection lost after the unit was sent and before the coordinator
    answered. The unit may or may not have been admitted, and the error says so.
    """
    try:
        code, body = _call_awaited(f"{coordinator_url}/units",
                                   {"wf_fqdn": wf_fqdn, "payload": payload, "snapshot_id": snapshot_id})
    except (OSError, http.client.HTTPException) as exc:
        raise CoordinationRefused(
            f"connection to coordinator {coordinator_url} lost while submitting; whether the unit was "
            f"admitted is unknown: {exc}"
        ) from exc
    if code != 202:
        raise CoordinationRefused(f"coordinator refused the unit: {code} {body.get('error', body)}")
    unit_id = body["unit_id"]
    while True:
        try:
            code, body = _call_awaited(f"{coordinator_url}/units/{unit_id}")
        except (CoordinationRefused, OSError, http.client.HTTPException):
            # The unit is admitted; the coordinator being briefly unreachable does not change that.
            time.sleep(1.0)
            continue
        if code == 200:
            return body
        if code != 202:
            raise CoordinationRefused(f"unit {unit_id} lost at the coordinator: {code} {body}")
        time.sleep(poll)
