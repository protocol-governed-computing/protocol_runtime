"""
Test Suite 4: Workflow Execution Tests

Integration tests for end-to-end workflow execution against the PGC assembled snapshot.
They load the `workload` domain (the Collatz reference workload) and run its workflow.

The snapshot root is PGC_SNAPSHOT_ROOT if set, else the workspace's sibling `snapshot/`
(the assembler's default output). Tests are skipped — not failed — when that snapshot has
not been built yet, so a clean checkout without a compiled snapshot is green.

Execution path:
    load_domain(snapshot_root, domain) → RuntimePackage
    make_trace_id(domain, wf_fqdn, payload) → trace_id
    TraceWriter(trace_dir, trace_id, domain, wf_addr, wf_fqdn)
    run_wf(wf_fqdn, payload, pkg, writer, data_root) → (status, surface)
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.evidence import TraceWriter, make_trace_id
from runtime.loader import load_domain
from runtime.scheduler import run_wf

_DOMAIN = "workload"


def _get_snapshot_root() -> Path | None:
    """The PGC assembled snapshot root, if it exists and carries the workload domain.

    PGC_SNAPSHOT_ROOT overrides; default is the workspace-root `snapshot/` (assembler default).
    Returns None (→ skip) when the snapshot has not been assembled.
    """
    env = os.environ.get("PGC_SNAPSHOT_ROOT")
    root = Path(env) if env else Path(__file__).resolve().parents[4] / "snapshot"
    if (root / "tokenized" / _DOMAIN / "metadata.json").is_file():
        return root
    return None


_SNAPSHOT_ROOT = _get_snapshot_root()

_SKIP_REASON = (
    "Integration test requires an assembled PGC snapshot with the 'workload' domain "
    "(run compile → compile_domain → assemble, or set PGC_SNAPSHOT_ROOT)."
)


@unittest.skipUnless(_SNAPSHOT_ROOT, _SKIP_REASON)
class TestWorkflowExecution(unittest.TestCase):
    """Integration tests for end-to-end workflow execution."""

    _WF_FQDN = "workload::WF_COLLATZ_CONJECTURE_V0"
    _DOMAIN = _DOMAIN

    def setUp(self):
        self.snapshot_root = _SNAPSHOT_ROOT
        self.data_root = os.environ.get("PGC_DATA_ROOT") or tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data_root, True)
        self.pkg = load_domain(self.snapshot_root, self._DOMAIN)

    def _run_wf(self, wf_fqdn: str, payload: dict) -> tuple[str, dict, Path]:
        """Execute workflow; return (status, surface, trace_file)."""
        tmp_traces = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp_traces), True)

        trace_id = make_trace_id(self._DOMAIN, wf_fqdn, payload)
        trace_dir = tmp_traces / trace_id
        trace_dir.mkdir(parents=True)

        wf_addr = self.pkg.vocab.addr(wf_fqdn)
        writer = TraceWriter(
            trace_dir=trace_dir,
            trace_id=trace_id,
            domain=self._DOMAIN,
            wf_addr=wf_addr,
            wf_fqdn=wf_fqdn,
        )

        try:
            status, surface = run_wf(
                wf_fqdn=wf_fqdn,
                payload=payload,
                pkg=self.pkg,
                writer=writer,
                data_root=self.data_root,
            )
        finally:
            writer.close()

        trace_file = trace_dir / f"{trace_id}.jsonl"
        return status, surface, trace_file

    def test_load_domain_succeeds(self):
        """load_domain MUST succeed with real workspace."""
        self.assertEqual(self.pkg.domain, self._DOMAIN)
        self.assertIsNotNone(self.pkg.dispatch)
        self.assertIsNotNone(self.pkg.handlers)
        self.assertIsNotNone(self.pkg.vocab)

    def test_wf_in_vocab(self):
        """Target WF FQDN MUST be present in the vocab index."""
        addr = self.pkg.vocab.addr(self._WF_FQDN)
        self.assertIsInstance(addr, int)
        self.assertGreater(addr, 0)

    def test_make_trace_id_format(self):
        """make_trace_id MUST return a non-empty string embedding the WF code."""
        payload = {"numbers": [3, 6, 7]}
        trace_id = make_trace_id(self._DOMAIN, self._WF_FQDN, payload)
        self.assertIsInstance(trace_id, str)
        self.assertGreater(len(trace_id), 0)
        wf_code = self._WF_FQDN.split("::")[-1]
        self.assertIn(wf_code, trace_id)

    def test_workflow_execution_returns_status(self):
        """run_wf MUST return a non-empty status string."""
        payload = {"numbers": [3, 6, 7, 11, 17]}
        status, surface, _ = self._run_wf(self._WF_FQDN, payload)
        self.assertIsInstance(status, str)
        self.assertIn(status, {"SUCCESS", "VIOLATION", "NACK"})

    def test_trace_file_written(self):
        """run_wf MUST write a non-empty JSONL trace file."""
        import json
        payload = {"numbers": [42, 63, 97, 99]}
        status, _, trace_file = self._run_wf(self._WF_FQDN, payload)
        self.assertTrue(trace_file.exists(), f"Trace file not written: {trace_file}")
        lines = [json.loads(line) for line in trace_file.read_text().splitlines() if line.strip()]
        self.assertGreater(len(lines), 0)

    def test_same_payload_same_hash_suffix(self):
        """Identical payload MUST produce the same hash suffix in the trace ID."""
        payload = {"numbers": [3, 6, 7, 11]}
        id1 = make_trace_id(self._DOMAIN, self._WF_FQDN, payload)
        id2 = make_trace_id(self._DOMAIN, self._WF_FQDN, payload)
        suffix1 = id1.rsplit("__", 1)[-1]
        suffix2 = id2.rsplit("__", 1)[-1]
        self.assertEqual(suffix1, suffix2)

    def test_different_payload_different_hash_suffix(self):
        """Different payloads MUST produce different hash suffixes in the trace ID."""
        id1 = make_trace_id(self._DOMAIN, self._WF_FQDN, {"numbers": [3]})
        id2 = make_trace_id(self._DOMAIN, self._WF_FQDN, {"numbers": [6]})
        suffix1 = id1.rsplit("__", 1)[-1]
        suffix2 = id2.rsplit("__", 1)[-1]
        self.assertNotEqual(suffix1, suffix2)


if __name__ == '__main__':
    unittest.main()
