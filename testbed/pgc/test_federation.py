"""
FEDERATED_NODE placement — a coordinator and workers meeting only at a shared evidence store.

Builds a federated composition from the compiled roots (`STRUCTURE_BUILD_PLATFORM_FEDERATED_CONFIG_V1`
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


if __name__ == "__main__":
    unittest.main()
