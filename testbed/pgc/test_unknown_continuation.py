"""
Routing is a lookup with two answers: a step's outcome continues or exits, and nothing else.

The dispatcher ended a contract on `exit` and read every other answer as going on. A contract whose
step routed SUCCESS to an evaluation target — a condition nothing ran — ended with its last step's
outcome whatever the condition said, and the Collatz gate could not fail. The build now refuses such a
contract (`execution_topology::INVARIANT_TOPOLOGY_CONTRACT_CLOSED_V1`). Execution refuses too, so an
answer the build missed is never read as going on.

Run:  python testbed/pgc/test_unknown_continuation.py
"""
import sys
from types import SimpleNamespace
from unittest import mock

from runtime import dispatcher
from runtime.dispatcher import UnknownContinuationError, UnlistedStepOutcomeError, execute_cc


class _Writer:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))


def _pkg(on_result: dict, steps: int = 1):
    pipeline = [{"addr": 10 + i, "op": None, "inputs": {}, "outputs": {}, "on_result": on_result,
                 "step": f"s{i}"} for i in range(steps)]
    return SimpleNamespace(dispatch=SimpleNamespace(pipeline={1: pipeline}),
                           vocab=SimpleNamespace(fqdn=lambda addr: f"probe::X_{addr}"))


def _run(on_result: dict, steps: int = 1):
    writer = _Writer()
    with mock.patch.object(dispatcher, "_execute_ct_step", return_value=("SUCCESS", {})), \
            mock.patch.object(dispatcher, "_make_workflow_executor", return_value=None):
        return execute_cc(1, -1, {}, _pkg(on_result, steps), writer, "/nowhere"), writer


def test_continue_and_exit_are_the_two_answers():
    (status, _), _ = _run({"SUCCESS": "continue"}, steps=2)
    assert status == "SUCCESS"
    (status, _), _ = _run({"SUCCESS": "exit"})
    assert status == "SUCCESS"


def test_an_evaluation_target_refuses_the_run_and_is_recorded():
    writer = _Writer()
    try:
        with mock.patch.object(dispatcher, "_execute_ct_step", return_value=("SUCCESS", {})), \
                mock.patch.object(dispatcher, "_make_workflow_executor", return_value=None):
            execute_cc(1, -1, {}, _pkg({"SUCCESS": "evaluate_cap"}, steps=2), writer, "/nowhere")
    except UnknownContinuationError as exc:
        assert "evaluate_cap" in str(exc), exc
    else:
        raise AssertionError("a routing answer nothing performs was read as going on")
    errors = [c for c in writer.calls if c[0] == "error"]
    assert errors and errors[0][2]["continuation"] == "evaluate_cap", writer.calls
    assert not any(c[0] == "cc_complete" for c in writer.calls), "the contract completed"


def test_the_refusal_is_one_the_scheduler_already_ends_a_workflow_on():
    assert issubclass(UnknownContinuationError, UnlistedStepOutcomeError)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # report every test, then fail the run
            failed += 1
            print(f"  FAIL  {name}: {exc!r}"[:1500])
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
