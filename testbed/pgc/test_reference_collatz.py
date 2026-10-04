"""
PGC reference-workload regression — execute workload::collatz against the assembled universe and
assert observable behavior. This is the end-to-end proof that PGC composes an independently-authored
domain onto the governance surface and executes it deterministically.

Per design, we assert the OUTPUT SURFACE (Collatz sequences + terminal result), NOT the trace_id —
the trace_id carries a wall-clock prefix and is not a content-derived contract.

Requires an assembled snapshot that includes the `workload` domain (governance + workload). If
absent, the tests skip. The workload's CT implementations live under `conformance_workloads/`
(module `workloads.collatz.implementation.*`); we put that root on sys.path here as TEST-ONLY
environment provisioning (the runtime itself never manipulates sys.path).
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.boot import default_snapshot_root

SNAPSHOT_ROOT = default_snapshot_root()
_MANIFEST = SNAPSHOT_ROOT / "manifest.json"

# TEST-ONLY: provision the impl roots (both source repos) so the runtime can import
# capability_side_effects.* and workloads.collatz.implementation.* — this mirrors the PYTHONPATH a
# real run supplies.
_IMPL_ROOTS = (
    SNAPSHOT_ROOT.parent / "software_governance",      # capability_side_effects.*
    SNAPSHOT_ROOT.parent / "conformance_workloads",    # workloads.collatz.implementation.*
)
for _root in _IMPL_ROOTS:
    if _root.is_dir() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def _has_workload_domain() -> bool:
    if not _MANIFEST.exists():
        return False
    manifest = json.loads(_MANIFEST.read_text())
    return "workload" in {d["domain"] for d in manifest.get("domains", [])}


WF = "workload::WF_COLLATZ_CONJECTURE_V0"


@unittest.skipUnless(
    _has_workload_domain(),
    f"assembled snapshot at {SNAPSHOT_ROOT} lacks the 'workload' domain — assemble platform+workload",
)
class ReferenceCollatzTest(unittest.TestCase):

    def setUp(self):
        self.instance = Path(tempfile.mkdtemp(prefix="collatz_instance_"))

    def tearDown(self):
        shutil.rmtree(self.instance, ignore_errors=True)

    def _run(self, numbers):
        from runtime.api import run_workflow  # imported here so import errors surface per-test
        return run_workflow(
            wf_fqdn=WF,
            payload={"numbers": numbers},
            data_root=str(self.instance),
            snapshot_root=SNAPSHOT_ROOT,
        )

    def test_conjecture_holds_for_known_inputs(self):
        r = self._run([6, 7])
        self.assertEqual(r.status, "SUCCESS")
        self.assertTrue(r.surface["all_terminate"])
        self.assertEqual(r.surface["non_terminating"], [])
        # exact sequences — deterministic pure computation
        self.assertEqual(r.surface["sequences"]["6"], [6, 3, 10, 5, 16, 8, 4, 2, 1])
        self.assertEqual(
            r.surface["sequences"]["7"],
            [7, 22, 11, 34, 17, 52, 26, 13, 40, 20, 10, 5, 16, 8, 4, 2, 1],
        )

    def test_every_sequence_terminates_at_one(self):
        r = self._run([1, 2, 3, 8, 27])
        self.assertEqual(r.status, "SUCCESS")
        self.assertTrue(r.surface["all_terminate"])
        for seq in r.surface["sequences"].values():
            self.assertEqual(seq[-1], 1, "Collatz sequence did not terminate at 1")

    def test_deterministic_surface(self):
        # identical input → identical observable behavior (trace_id is intentionally NOT asserted)
        a = self._run([6, 7]).surface
        b = self._run([6, 7]).surface
        self.assertEqual(a, b)

    def test_store_persists_results(self):
        # CS (Capability Side Effect) concern: the store step persists via the platform's imported
        # CS_MUTABLE_JSON to the STRUCTURE-declared path under the instance root (not the snapshot).
        r = self._run([27])
        self.assertEqual(r.status, "SUCCESS")
        store = self.instance / "workload" / "collatz" / "collatz_results.json"
        self.assertTrue(store.is_file(), f"CS store not persisted at {store}")
        data = json.loads(store.read_text())["collatz_results"]
        self.assertTrue(data["all_terminate"])
        self.assertEqual(data["sequences"]["27"][-1], 1)

    def test_trace_attributes_actor_and_emits_event(self):
        # AC (Authority) + EV (Observation) concerns, live in the trace.
        r = self._run([6, 7])
        events = [
            json.loads(line)
            for line in (r.trace_dir / f"{r.trace_id}.jsonl").read_text().splitlines()
        ]
        # AC: the workflow is attributed to its bound actor context
        wf_start = next(e for e in events if e.get("event_type") == "WF_START")
        self.assertEqual(wf_start["detail"].get("actor"), "workload::AC_REFERENCE_ACTOR_V0")
        # EV: the moments this act announced, in the order it announced them.
        #
        # Asserted as a whole sequence rather than by taking the first. An act may announce several
        # at one ending, and a reader that takes the first accepts extras without noticing — which is
        # exactly the failure the plural announcement could introduce. Taking the first was the one
        # place in the composition where several would have arrived silently.
        announced = [e["detail"]["ev_fqdn"] for e in events if e.get("event_type") == "EVENT"]
        self.assertEqual(
            announced, ["workload::EV_CONJECTURE_EVALUATED_V0"],
            "this act announces exactly one moment; anything else is a moment nobody designed",
        )
        payload = next(e["detail"]["payload"] for e in events if e.get("event_type") == "EVENT")
        self.assertTrue(payload["all_terminate"])


if __name__ == "__main__":
    unittest.main()
