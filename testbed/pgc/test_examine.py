"""
The trace examiner reads the traces this runtime writes.

It read RI-0's format — `execution_start`, `node_start`, `sequence` — and could not read one line
PGC writes, and nothing said so until someone ran it. It now reads `SCHEMA_TRACE_EVENT_V2`, refuses
anything else, and tells a completed run, refusals included, from a structural failure.
"""
import json
import tempfile
import unittest
from pathlib import Path

from runtime.examine import TraceParseError, analyze

WORKSPACE = Path(__file__).resolve().parents[3]
HEADER = {"trace_schema_version": "v2", "event_type": "trace_classification",
          "classified_by": "vocabulary::VOCAB_EVIDENCE_CONTENT_CLASSIFICATION_V1",
          "snapshot_id": "abc", "determinative": [], "observational": [], "observational_keys": []}


def ev(event_type, status=None, **detail):
    return {"trace_schema_version": "v2", "trace_id": "T1", "event_type": event_type, "domain": "d",
            "wf_addr": 1, "cc_addr": None, "step_addr": None, "step_op": None,
            "result_status": status, "detail": detail, "ts_ns": 0}


START = ev("WF_START", wf_fqdn="d::WF_SUBMIT_V0", payload_keys=[])


def write(records) -> Path:
    path = Path(tempfile.mkdtemp()) / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


class Examine(unittest.TestCase):

    def test_a_refusal_is_a_completed_run(self):
        report = analyze(write([HEADER, START,
            ev("WF_ROUTE", "ACK", from_node="IN_SUBMIT_V0", to_node="CHECK"),
            ev("CC_START", cc_fqdn="d::CC_CHECK_V0", node="CHECK", inputs={}),
            ev("CC_COMPLETE", "VIOLATION", cc_fqdn="d::CC_CHECK_V0", node="CHECK", output_keys=[]),
            ev("WF_ROUTE", "VIOLATION", from_node="CHECK", to_node="EXIT_REFUSED"),
            ev("EVENT", ev_fqdn="d::EV_REFUSED_V0", payload={}),
            ev("WF_COMPLETE", "VIOLATION", wf_fqdn="d::WF_SUBMIT_V0")]))
        self.assertFalse(report.has_structural_failure)
        self.assertEqual((report.outcome, report.ending), ("VIOLATION", "EXIT_REFUSED"))
        self.assertEqual(report.contracts, [("CHECK", "d::CC_CHECK_V0", "VIOLATION")])
        self.assertEqual(report.events, ["d::EV_REFUSED_V0"])

    def test_an_unrouted_outcome_is_a_structural_failure(self):
        report = analyze(write([HEADER, START,
            ev("ERROR", message="unrouted outcome", node=3, outcome="BACKEND_ERROR"),
            ev("WF_ROUTE", "BACKEND_ERROR", from_node="CHECK", to_node=None),
            ev("WF_COMPLETE", "VIOLATION", wf_fqdn="d::WF_SUBMIT_V0")]))
        self.assertTrue(report.has_structural_failure)
        self.assertEqual(report.path, [("CHECK", "BACKEND_ERROR", None)])

    def test_a_run_that_never_completed_is_a_structural_failure(self):
        report = analyze(write([HEADER, START, ev("ERROR", message="boom")]))
        self.assertTrue(report.has_structural_failure)
        self.assertIsNone(report.outcome)

    def test_a_trace_in_another_format_is_refused_not_guessed(self):
        ri0 = {"event_type": "execution_start", "timestamp": 0, "execution_id": "x", "sequence": 0}
        with self.assertRaises(TraceParseError):
            analyze(write([ri0]))
        with self.assertRaises(TraceParseError):
            analyze(write([START]))

    def test_every_trace_the_runtime_wrote_is_read(self):
        traces = sorted((WORKSPACE / "data").glob("**/traces/**/*.jsonl"))
        if not traces:
            self.skipTest("no traces under data/ — run the regression first")
        for path in traces:
            report = analyze(path)
            self.assertFalse(report.has_structural_failure, path)
            self.assertTrue(report.ending, path)


if __name__ == "__main__":
    unittest.main()
