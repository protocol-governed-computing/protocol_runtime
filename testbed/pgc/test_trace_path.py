"""
The path drawn over a workflow's graph is the path the run took, place by place.

The overlay once matched `CC_COMPLETE` events to nodes by contract code. A node whose key is not its
contract's code dropped out, and two places running one contract could not be told apart: a language
model's submission was drawn up to its first reused contract and no further. Each routing event now
names the node it left and the node or ending it reached, and the path is read from those.
"""
import unittest

from runtime.trace_viz import _extract_execution_path

GRAPH: dict = {}   # the path is read from the trace, not the graph


def routes(*hops):
    """WF_ROUTE events for (from_node, outcome, to_node) hops, among other events."""
    return [{"event_type": "CC_COMPLETE", "result_status": "SUCCESS"}] + [
        {"event_type": "WF_ROUTE", "result_status": o, "detail": {"from_node": f, "to_node": t}}
        for f, o, t in hops]


class TracePath(unittest.TestCase):

    def test_a_run_through_two_places_of_one_contract_is_drawn_whole(self):
        hops = [("IN_V0", "ACK", "CONFIRM_STOPPED"), ("CONFIRM_STOPPED", "SUCCESS", "CONFIRM_FINISHED"),
                ("CONFIRM_FINISHED", "SUCCESS", "RECORD_RESPONDED"),
                ("RECORD_RESPONDED", "SUCCESS", "EXIT_RESPONDED")]
        self.assertEqual(_extract_execution_path(routes(*hops), GRAPH), hops)

    def test_a_refusal_is_drawn_to_its_own_place(self):
        hops = [("IN_V0", "ACK", "CONFIRM_STOPPED"), ("CONFIRM_STOPPED", "VIOLATION", "RECORD_BY_RULE"),
                ("RECORD_BY_RULE", "SUCCESS", "EXIT_REFUSED")]
        self.assertEqual(_extract_execution_path(routes(*hops), GRAPH), hops)

    def test_an_outcome_that_reached_no_ending_stops_the_drawing(self):
        hops = [("IN_V0", "ACK", "CONFIRM_STOPPED"), ("CONFIRM_STOPPED", "BACKEND_ERROR", None)]
        self.assertEqual(_extract_execution_path(routes(*hops), GRAPH), hops[:1])

    def test_a_trace_without_node_keys_is_refused_not_guessed(self):
        old = [{"event_type": "WF_ROUTE", "result_status": "ACK", "detail": {"terminal": False}}]
        with self.assertRaises(ValueError):
            _extract_execution_path(old, GRAPH)


if __name__ == "__main__":
    unittest.main()
