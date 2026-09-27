"""
FEDERATED_NODE placement — a coordinator and workers meeting only at a shared evidence store.

Builds a federated composition from the compiled roots (`STRUCTURE_BUILD_PLATFORM_FEDERATED_CONFIG_V2`
output + collatz + the inspection tool domain), then runs the coordinator and two workers as separate
processes over one store directory, the arrangement the node group realizes across hosts. Skips when
the compiled roots are absent.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from runtime.api import invoke_workflow, run_workflow
from runtime.boot import default_snapshot_root
from runtime.coordinator import CoordinationRefused, sealed_placement_mode
from runtime.federation.coordinator import FederatedCoordinator
from runtime.federation.store import Store
from runtime.federation.worker import FederatedWorker

W = Path(__file__).resolve().parents[3]
SOURCES = [W / "software_governance/snapshot_fed/compiled",
           W / "conformance_workloads/workloads/collatz/snapshot/compiled",
           W / "snapshot_inspector/snapshot/compiled"]
PAYLOAD = json.loads((W / "conformance_workloads/workloads/collatz/test_payloads/01_happy_path.json")
                     .read_text(encoding="utf-8"))
WF = "workload::WF_COLLATZ_CONJECTURE_V0"
_HAVE_SOURCES = all(s.is_dir() for s in SOURCES)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _reap(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.communicate()


def _wait_healthy(url: str, proc: subprocess.Popen) -> None:
    from runtime.federation.client import health
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"coordinator exited: {proc.stderr.read()}")
        try:
            health(url)
            return
        except CoordinationRefused:
            time.sleep(0.1)
    raise AssertionError("coordinator never became healthy")


@unittest.skipUnless(_HAVE_SOURCES, "federated composition sources not compiled")
class FederationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pgc_fed_"))
        cls.snapshot = cls.tmp / "snapshot"
        args = [sys.executable, "-m", "assembler.cli", "assemble", "--out", str(cls.snapshot),
                "--profile", "SIGNED_FEDERATED_MULTINODE_PROFILE_V0"]
        for s in SOURCES:
            args += ["--source", str(s)]
        subprocess.run(args, check=True, capture_output=True)
        assert sealed_placement_mode(cls.snapshot) == "FEDERATED_NODE"

    def _store(self, name: str) -> Path:
        root = self.tmp / name
        root.mkdir()
        return root

    def _start_coordinator(self, data_root: Path) -> tuple[str, subprocess.Popen]:
        port = _free_port()
        proc = subprocess.Popen([sys.executable, "-m", "runtime.cli", "coordinator",
                                 "--snapshot", str(self.snapshot), "--data-root", str(data_root),
                                 "--port", str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(_reap, proc)
        url = f"http://127.0.0.1:{port}"
        _wait_healthy(url, proc)
        return url, proc

    def _start_worker(self, data_root: Path, url: str, name: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-m", "runtime.cli", "worker",
                                 "--snapshot", str(self.snapshot), "--data-root", str(data_root),
                                 "--coordinator", url, "--id", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(_reap, proc)
        return proc

    # ── the path a request takes ────────────────────────────────

    def test_boundary_to_worker_matches_local_execution(self):
        """A determination reached through the node group equals the one reached in place."""
        data = self._store("path")
        url, _ = self._start_coordinator(data)
        self._start_worker(data, url, "w1")
        os.environ["PGC_COORDINATOR_URL"] = url
        self.addCleanup(os.environ.pop, "PGC_COORDINATOR_URL")

        remote = invoke_workflow(wf_fqdn=WF, payload=PAYLOAD, data_root=data, snapshot_root=self.snapshot)
        local = run_workflow(wf_fqdn=WF, payload=PAYLOAD, data_root=self._store("local"),
                             snapshot_root=self.snapshot)
        self.assertEqual((remote.status, remote.surface), (local.status, local.surface))
        self.assertTrue(remote.trace_dir.is_relative_to(data), "the trace is not in the shared store")
        self.assertTrue(any(remote.trace_dir.glob("*.jsonl")), "no trace written to the store")

    def test_each_unit_executes_exactly_once_across_workers(self):
        data = self._store("race")
        url, _ = self._start_coordinator(data)
        coordinator = FederatedCoordinator(self.snapshot, data)
        units = []
        for _ in range(8):
            code, body = coordinator.submit({"wf_fqdn": WF, "payload": PAYLOAD,
                                             "snapshot_id": coordinator.snapshot_id})
            self.assertEqual(code, 202)
            units.append(body["unit_id"])
        workers = [self._start_worker(data, url, f"w{i}") for i in (1, 2)]

        store = Store(data)
        deadline = time.monotonic() + 120
        while any(store.outcome(u) is None for u in units) and time.monotonic() < deadline:
            time.sleep(0.2)
        for w in workers:
            w.kill()
        executed = [line.split()[-1] for w in workers for line in w.communicate()[0].splitlines()
                    if " executed " in line]
        self.assertEqual(sorted(executed), sorted(units), "a unit ran twice or not at all")
        self.assertEqual({store.outcome(u)["state"] for u in units}, {"executed"})

    def test_claimed_units_are_not_redispatched(self):
        data = self._store("redispatch")
        url, _ = self._start_coordinator(data)
        coordinator = FederatedCoordinator(self.snapshot, data)
        _, body = coordinator.submit({"wf_fqdn": WF, "payload": PAYLOAD,
                                      "snapshot_id": coordinator.snapshot_id})
        # A worker that claimed and was lost: the claim stands, no outcome is ever written.
        self.assertTrue(Store(data).claim(body["unit_id"], "lost"))
        worker = FederatedWorker(self.snapshot, data, url, "w1")
        self.assertEqual(worker.run_once(), [])
        self.assertEqual(coordinator.status(body["unit_id"])[1]["state"], "claimed")

    def test_concurrent_claims_have_one_winner(self):
        store = Store(self._store("claims"))
        store.require_writable()
        with ThreadPoolExecutor(max_workers=16) as pool:
            wins = list(pool.map(lambda i: store.claim("u", f"w{i}"), range(16)))
        self.assertEqual(wins.count(True), 1)

    # ── refusals (EO-2, and authority from the sealed composition) ─

    def test_parties_refuse_a_snapshot_not_placed_on_federated_nodes(self):
        local = default_snapshot_root()
        if sealed_placement_mode(local) in (None, "FEDERATED_NODE"):
            self.skipTest("no non-federated snapshot assembled")
        with self.assertRaises(CoordinationRefused):
            FederatedCoordinator(local, self._store("refuse_c"))
        with self.assertRaises(CoordinationRefused):
            FederatedWorker(local, self._store("refuse_w"), "http://127.0.0.1:9")

    def test_worker_refuses_unreachable_store_or_coordinator(self):
        data = self._store("prereq")
        url, _ = self._start_coordinator(data)
        with self.assertRaises(CoordinationRefused):
            FederatedWorker(self.snapshot, self.tmp / "no_such_store", url)
        with self.assertRaises(CoordinationRefused):
            FederatedWorker(self.snapshot, data, f"http://127.0.0.1:{_free_port()}")

    def test_boundary_refuses_without_a_coordinator(self):
        os.environ.pop("PGC_COORDINATOR_URL", None)
        with self.assertRaisesRegex(RuntimeError, "PGC_COORDINATOR_URL"):
            invoke_workflow(wf_fqdn=WF, payload=PAYLOAD, data_root=self._store("nocoord"),
                            snapshot_root=self.snapshot)

    def test_coordinator_refuses_a_unit_from_another_snapshot(self):
        coordinator = FederatedCoordinator(self.snapshot, self._store("foreign"))
        code, _ = coordinator.submit({"wf_fqdn": WF, "payload": PAYLOAD, "snapshot_id": "0" * 64})
        self.assertEqual(code, 409)

    # ── block, never misreport (EO-2) ───────────────────────────

    def test_boundary_waits_for_an_admitted_unit_rather_than_failing(self):
        """No worker drains the queue for a while; the boundary waits and then reports the outcome."""
        data = self._store("stall")
        url, _ = self._start_coordinator(data)
        os.environ["PGC_COORDINATOR_URL"] = url
        self.addCleanup(os.environ.pop, "PGC_COORDINATOR_URL")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(invoke_workflow, wf_fqdn=WF, payload=PAYLOAD, data_root=data,
                                  snapshot_root=self.snapshot)
            time.sleep(3)
            self.assertFalse(pending.done(), "the boundary answered before any worker ran the unit")
            self._start_worker(data, url, "late")
            self.assertEqual(pending.result(timeout=60).status, "SUCCESS")

    def test_boundary_waits_on_a_coordinator_slower_than_any_fixed_limit(self):
        """A coordinator blocked on the store answers late; the answer is still the one reported."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from runtime.federation.client import submit_and_wait

        class Stalled(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(12)          # longer than the 10 s the boundary once allowed
                self._send(202, {"unit_id": "late"})

            def do_GET(self):
                self._send(200, {"unit_id": "late", "state": "executed", "status": "SUCCESS"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stalled)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)     # cleanups run last-in first-out: shut down, then close
        self.addCleanup(server.shutdown)
        outcome = submit_and_wait(f"http://127.0.0.1:{server.server_port}", wf_fqdn=WF,
                                  payload=PAYLOAD, snapshot_id="x")
        self.assertEqual(outcome["status"], "SUCCESS")

    def test_boundary_fails_only_when_nothing_was_admitted(self):
        data = self._store("unadmitted")
        Store(data).require_writable()
        os.environ["PGC_COORDINATOR_URL"] = f"http://127.0.0.1:{_free_port()}"
        self.addCleanup(os.environ.pop, "PGC_COORDINATOR_URL")
        with self.assertRaisesRegex(CoordinationRefused, "not reachable"):
            invoke_workflow(wf_fqdn=WF, payload=PAYLOAD, data_root=data, snapshot_root=self.snapshot)
        self.assertEqual(Store(data).queued(), [], "a unit was admitted although failure was reported")


if __name__ == "__main__":
    unittest.main()
