"""
The path drawn over a workflow's graph is the path the run took, place by place.

The overlay once matched `CC_COMPLETE` events to nodes by contract code. A node whose key is not its
contract's code dropped out, and two places running one contract could not be told apart: a language
model's submission was drawn up to its first reused contract and no further. The path is now walked
over the graph's node-keyed edges, one recorded routing outcome at a time.
"""
import unittest

from runtime.trace_viz import _extract_execution_path

# CONFIRM runs at two places, RECORD at two; the graph names places.
GRAPH = {
    "entry": "IN_V0",
    "edges": [
        {"from": "IN_V0", "condition": "ACK", "to": "CONFIRM_STOPPED"},
        {"from": "IN_V0", "condition": "NACK", "to": "EXIT_REJECTED"},
        {"from": "CONFIRM_STOPPED", "condition": "SUCCESS", "to": "CONFIRM_FINISHED"},
        {"from": "CONFIRM_STOPPED", "condition": "VIOLATION", "to": "RECORD_BY_RULE"},
        {"from": "CONFIRM_FINISHED", "condition": "SUCCESS", "to": "RECORD_RESPONDED"},
        {"from": "CONFIRM_FINISHED", "condition": "VIOLATION", "to": "RECORD_BY_RULE"},
        {"from": "RECORD_RESPONDED", "condition": "SUCCESS", "to": "EXIT_RESPONDED"},
        {"from": "RECORD_BY_RULE", "condition": "SUCCESS", "to": "EXIT_REFUSED"},
    ],
}


def routes(*outcomes):
    return [{"event_type": "CC_COMPLETE", "result_status": "SUCCESS"}] + \
        [{"event_type": "WF_ROUTE", "result_status": o} for o in outcomes]


class TracePath(unittest.TestCase):

    def test_a_run_through_two_places_of_one_contract_is_drawn_whole(self):
        path = _extract_execution_path(routes("ACK", "SUCCESS", "SUCCESS", "SUCCESS"), GRAPH)
        self.assertEqual([t for _, _, t in path],
                         ["CONFIRM_STOPPED", "CONFIRM_FINISHED", "RECORD_RESPONDED", "EXIT_RESPONDED"])

    def test_a_refusal_is_drawn_to_its_own_place(self):
        path = _extract_execution_path(routes("ACK", "VIOLATION", "SUCCESS"), GRAPH)
        self.assertEqual([t for _, _, t in path], ["CONFIRM_STOPPED", "RECORD_BY_RULE", "EXIT_REFUSED"])

    def test_an_admission_refused_is_drawn_to_its_ending(self):
        self.assertEqual(_extract_execution_path(routes("NACK"), GRAPH),
                         [("IN_V0", "NACK", "EXIT_REJECTED")])

    def test_an_outcome_the_graph_does_not_route_stops_the_drawing(self):
        path = _extract_execution_path(routes("ACK", "BACKEND_ERROR"), GRAPH)
        self.assertEqual(path, [("IN_V0", "ACK", "CONFIRM_STOPPED")])


if __name__ == "__main__":
    unittest.main()
