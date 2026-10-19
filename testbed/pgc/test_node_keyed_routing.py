"""
One contract run at several places in a workflow, each place with its own continuation.

Routing, endings and announcements were keyed by the contract a node runs, so the continuation of the
last such place sealed stood for all of them. A causal language model's submission records its
outcome at ten places — one responds, the others refuse — and every one of them ended where the last
did, announcing nothing. Two confirmations of one contract in sequence routed the first's refusal to
the second's refusal place, recording the wrong reason.

The runtime now routes, ends and announces by the node it has just run.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from runtime import scheduler
from runtime.evidence import TraceWriter
from runtime.loader import _build_dispatch

# The trace is written under the composition's evidence classification, so it needs a snapshot.
SNAPSHOT = Path(__file__).resolve().parents[3] / "snapshot"
WF, CONFIRM, RECORD = 90, 10, 20
SUCCESS, VIOLATION = 601, 602
WF_FQDN = "probe::WF_V0"
VOCAB = {WF_FQDN: WF, "transition::SUCCESS": SUCCESS, "transition::VIOLATION": VIOLATION}


class Vocab:
    def addr(self, fqdn):
        return VOCAB[fqdn]

    def fqdn(self, addr):
        return {v: k for k, v in VOCAB.items()}.get(addr, f"probe::{addr}")


def dispatch():
    """CONFIRM runs at two places, RECORD at two; each place routes differently."""
    step = lambda addr, key: {"addr": addr, "key": key}
    return _build_dispatch({
        "entry": {str(WF): {"start": CONFIRM, "start_key": "CONFIRM_STOPPED"}},
        "routing": {str(WF): {
            "CONFIRM_STOPPED": {str(SUCCESS): step(CONFIRM, "CONFIRM_FINISHED"),
                                str(VIOLATION): step(RECORD, "RECORD_BY_RULE")},
            "CONFIRM_FINISHED": {str(SUCCESS): step(RECORD, "RECORD_RESPONDED"),
                                 str(VIOLATION): step(RECORD, "RECORD_UNFINISHED")},
        }},
        "terminal": {str(WF): {
            "RECORD_RESPONDED": {str(SUCCESS): {"exit": "EXIT_RESPONDED", "type": "EXIT"}},
            "RECORD_BY_RULE": {str(SUCCESS): {"exit": "EXIT_REFUSED", "type": "EXIT"}},
            "RECORD_UNFINISHED": {str(SUCCESS): {"exit": "EXIT_REFUSED", "type": "EXIT"}},
        }},
        "emits": {str(WF): {
            "RECORD_RESPONDED": {"SUCCESS": ["probe::EV_RESPONDED_V0"]},
            "RECORD_BY_RULE": {"SUCCESS": ["probe::EV_REFUSED_V0"]},
            "RECORD_UNFINISHED": {"SUCCESS": ["probe::EV_REFUSED_V0"]},
        }},
        "pipeline": {str(CONFIRM): [], str(RECORD): []},
        "bindings": {str(WF): {
            "RECORD_RESPONDED": {"reason": "none"},
            "RECORD_BY_RULE": {"reason": "stopped_by_rule"},
            "RECORD_UNFINISHED": {"reason": "longest_response_reached"},
        }},
    })


class NodeKeyedRouting(unittest.TestCase):

    def run_with(self, confirmations):
        """Run the workflow; the n-th confirmation returns confirmations[n]. Returns (reasons, events)."""
        pkg = SimpleNamespace(dispatch=dispatch(), vocab=Vocab())
        outcomes, reasons = iter(confirmations), []

        def execute_cc(cc_addr, rb_addr, inputs, pkg, writer, data_root, wf_addr, node_key=""):
            if cc_addr == CONFIRM:
                return next(outcomes), {}
            reasons.append(inputs.get("reason"))
            return "SUCCESS", {"reason": inputs.get("reason")}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(scheduler, "execute_cc", execute_cc):
            writer = TraceWriter(trace_dir=Path(tmp), trace_id="t", domain="probe",
                                 wf_addr=WF, wf_fqdn=WF_FQDN, snapshot_root=SNAPSHOT, snapshot_id="probe")
            status, _ = scheduler.run_wf(wf_fqdn=WF_FQDN, payload={}, pkg=pkg,
                                         writer=writer, data_root=tmp)
            writer.close()
            events = [e for f in Path(tmp).glob("*.jsonl") for e in map(json.loads, f.read_text().splitlines())]
        self.routes = [(e["detail"]["from_node"], e["result_status"], e["detail"]["to_node"])
                       for e in events if e.get("event_type") == "WF_ROUTE"]
        announced = [e["detail"]["ev_fqdn"] for e in events if e.get("event_type") == "EVENT"]
        return status, reasons, announced

    def test_the_first_place_refuses_to_its_own_refusal(self):
        status, reasons, _ = self.run_with(["VIOLATION"])
        self.assertEqual((status, reasons), ("SUCCESS", ["stopped_by_rule"]))

    def test_the_second_place_refuses_to_its_own_refusal(self):
        status, reasons, _ = self.run_with(["SUCCESS", "VIOLATION"])
        self.assertEqual(reasons, ["longest_response_reached"])

    def test_each_place_running_one_contract_announces_its_own_moment(self):
        _, reasons, responded = self.run_with(["SUCCESS", "SUCCESS"])
        _, _, refused = self.run_with(["VIOLATION"])
        self.assertEqual(reasons, ["none"])
        self.assertEqual(responded, ["probe::EV_RESPONDED_V0"])
        self.assertEqual(refused, ["probe::EV_REFUSED_V0"])

    def test_the_trace_names_each_place_the_run_passed_through(self):
        self.run_with(["SUCCESS", "SUCCESS"])
        self.assertEqual(self.routes, [
            ("CONFIRM_STOPPED", "SUCCESS", "CONFIRM_FINISHED"),
            ("CONFIRM_FINISHED", "SUCCESS", "RECORD_RESPONDED"),
            ("RECORD_RESPONDED", "SUCCESS", "EXIT_RESPONDED"),
        ])


if __name__ == "__main__":
    unittest.main()
