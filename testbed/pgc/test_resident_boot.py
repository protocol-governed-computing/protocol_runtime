"""
A process verifies a snapshot once, and runs against it as often as it is asked.

Every call to `run_workflow` used to boot afresh: the whole snapshot re-verified from its bytes, and
the run's path drawn as a picture, on every run. Together they were over 95% of a run's time. A
booted snapshot is now resident, keyed by its manifest and by what acceptance was judged against; a
changed manifest is verified afresh, and a snapshot that no longer verifies is still refused. A run
writes its evidence and nothing else: the picture is drawn on request.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import runtime.api as api
from runtime.boot import default_snapshot_root

SNAPSHOT_ROOT = default_snapshot_root()
_HAVE_SNAPSHOT = (SNAPSHOT_ROOT / "manifest.json").exists()

# TEST-ONLY: provision the impl roots so the runtime can import the collatz atoms.
_WS = Path(__file__).resolve().parents[3]
for root in (_WS / "conformance_workloads", _WS / "software_governance"):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

WF = "workload::WF_COLLATZ_CONJECTURE_V0"
PAYLOAD = {"numbers": [27]}


@unittest.skipUnless(_HAVE_SNAPSHOT, f"no assembled snapshot at {SNAPSHOT_ROOT} — run assemble.sh")
class ResidentBootTest(unittest.TestCase):

    def setUp(self):
        api._RESIDENT.clear()
        self.boots = 0
        self._boot = api.boot

        def counting(root=None):
            self.boots += 1
            return self._boot(root)

        api.boot = counting
        self.work = Path(tempfile.mkdtemp())

    def tearDown(self):
        api.boot = self._boot
        api._RESIDENT.clear()
        shutil.rmtree(self.work)

    def run_once(self, snapshot_root):
        return api.run_workflow(wf_fqdn=WF, payload=PAYLOAD, snapshot_root=snapshot_root,
                                data_root=self.work / "data")

    def copy(self) -> Path:
        snap = self.work / "snap"
        shutil.copytree(SNAPSHOT_ROOT, snap)
        return snap

    def test_a_second_run_does_not_verify_again(self):
        first = self.run_once(SNAPSHOT_ROOT)
        second = self.run_once(SNAPSHOT_ROOT)
        self.assertEqual(self.boots, 1)
        self.assertEqual(first.status, second.status)

    def test_a_rewritten_manifest_is_verified_afresh(self):
        snap = self.copy()
        self.run_once(snap)
        manifest = snap / "manifest.json"
        manifest.write_text(manifest.read_text() + "\n")
        self.run_once(snap)
        self.assertEqual(self.boots, 2)

    def test_a_snapshot_that_no_longer_verifies_is_refused(self):
        snap = self.copy()
        self.run_once(snap)
        man = json.loads((snap / "manifest.json").read_text())
        meta_path = snap / "tokenized" / man["domains"][0]["domain"] / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["projection_hash"] = "0" * 64
        meta_path.write_text(json.dumps(meta))
        (snap / "manifest.json").write_text((snap / "manifest.json").read_text() + "\n")
        with self.assertRaises(RuntimeError):
            self.run_once(snap)

    def test_a_run_writes_its_evidence_and_no_picture(self):
        run = self.run_once(SNAPSHOT_ROOT)
        written = sorted(p.suffix for p in run.trace_dir.iterdir())
        self.assertEqual(written, [".jsonl"])


if __name__ == "__main__":
    unittest.main()
