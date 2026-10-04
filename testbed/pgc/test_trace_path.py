"""
The path drawn over a workflow's graph is the path the run took, place by place — and the drawing
says why, as far as the inspector's explanation does.

The overlay once matched `CC_COMPLETE` events to nodes by contract code. A node whose key is not its
contract's code dropped out, and two places running one contract could not be told apart. Each
routing event now names the node it left and the node or ending it reached. The renderer no longer
reads the trace at all: it draws `si.execution.explain`, whose visits carry those routes.
"""
import unittest

from runtime.trace_viz import ExplanationRefused, _generate_dot, _taken


def explanation(*hops, **extra):
    """An explanation whose visits follow (from_node, outcome, to_node) hops."""
    visits = []
    for f, o, t in hops:
        visits.append({"node": f, "route": {"to": t, "outcome": o, "terminal": False},
                       "determination": None, "errors": [], "atoms": []})
    if hops and hops[-1][2] is not None:
        visits.append({"node": hops[-1][2], "route": None, "determination": None,
                       "errors": [], "atoms": []})
    return {"wf": "d::WF_V0", "status": "SUCCESS", "visits": visits,
            "tie": {"snapshot_id": "abc"}, "ending": {}, **extra}


GRAPH = {"wf_id": "WF_V0",
         "nodes": [{"id": "IN_V0", "type": "IN"},
                   {"id": "CHECK", "type": "CC", "capability": "d::CT_CHECK_V0"},
                   {"id": "EXIT_OK", "type": "EXIT"}, {"id": "EXIT_REJECTED", "type": "EXIT"}],
         "edges": [{"from": "IN_V0", "to": "CHECK", "condition": "ACK"},
                   {"from": "IN_V0", "to": "EXIT_REJECTED", "condition": "NACK"},
                   {"from": "CHECK", "to": "EXIT_OK", "condition": "SUCCESS"}]}


class TracePath(unittest.TestCase):

    def test_a_run_through_two_places_of_one_contract_is_drawn_whole(self):
        hops = [("IN_V0", "ACK", "CONFIRM_STOPPED"), ("CONFIRM_STOPPED", "SUCCESS", "CONFIRM_FINISHED"),
                ("CONFIRM_FINISHED", "SUCCESS", "RECORD_RESPONDED"),
                ("RECORD_RESPONDED", "SUCCESS", "EXIT_RESPONDED")]
        self.assertEqual(_taken(explanation(*hops)), hops)

    def test_a_refusal_is_drawn_to_its_own_place(self):
        hops = [("IN_V0", "ACK", "CONFIRM_STOPPED"), ("CONFIRM_STOPPED", "VIOLATION", "RECORD_BY_RULE"),
                ("RECORD_BY_RULE", "SUCCESS", "EXIT_REFUSED")]
        self.assertEqual(_taken(explanation(*hops)), hops)

    def test_an_outcome_that_reached_no_ending_stops_the_drawing(self):
        hops = [("IN_V0", "ACK", "CONFIRM_STOPPED"), ("CONFIRM_STOPPED", "BACKEND_ERROR", None)]
        self.assertEqual(_taken(explanation(*hops)), hops[:1])

    def test_a_trace_without_node_keys_is_refused_not_guessed(self):
        old = {"visits": [{"node": None, "route": {"to": "X", "outcome": "ACK"}}]}
        with self.assertRaises(ExplanationRefused):
            _taken(old)

    def test_a_failed_admission_check_is_drawn_at_the_deciding_gate(self):
        e = explanation(("IN_V0", "NACK", "EXIT_REJECTED"),
                        ending={"kind": "declared_ending", "decided_at": "IN_V0"})
        e["visits"][0]["determination"] = {
            "basis": "recorded admission checks",
            "checks": [{"field": "numbers", "rule": "type", "expected": "array", "held": False}],
            "failed": [{"field": "numbers", "rule": "type", "expected": "array", "held": False}]}
        dot = _generate_dot(GRAPH, e)
        self.assertIn("✗ numbers: type array", dot)
        self.assertIn('"IN_V0" -> "EXIT_REJECTED" [label="NACK", color=red', dot)
        self.assertIn("peripheries=2", dot)

    def test_joined_capability_is_drawn_apart_from_recorded_outcome(self):
        e = explanation(("IN_V0", "ACK", "CHECK"), ("CHECK", "SUCCESS", "EXIT_OK"))
        e["visits"][1]["determination"] = {"basis": "capability outcome", "outcome": "SUCCESS"}
        dot = _generate_dot(GRAPH, e)
        self.assertIn('COLOR="gray35"><I>CT_CHECK_V0</I>', dot)
        self.assertIn('COLOR="red3">→ SUCCESS<', dot)

    def test_an_undeclared_route_is_drawn_dashed(self):
        e = explanation(("IN_V0", "ACK", "EXIT_OK"))
        dot = _generate_dot(GRAPH, e)
        self.assertIn('"IN_V0" -> "EXIT_OK" [label="ACK (undeclared)", color=red, style=dashed', dot)


if __name__ == "__main__":
    unittest.main()
