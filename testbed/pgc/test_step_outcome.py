"""
A composed step's outcome selects a declared continuation, and the trace records both.

The dispatcher read a step's continuation with `on_result.get(outcome, "continue")`, so an outcome
the contract declared nothing for proceeded to the next step. A failed lookup carried on that way,
and a person was accepted (SoSyM study, case O3). It now refuses there (`3a` EX-18). The step record
carried only the names of the step's results, so nothing in the trace showed the decision; it now
records the outcome and the continuation it selected (`3e` EV-19).

A fault is not an outcome either. A transform that would not load, an atom that broke, a side
effect that raised or stated no outcome were all turned into VIOLATION and routed as a business
answer: a reclaim whose check could not load ended as "still active". Each now refuses the run
(`3a` §4.1, `3c` §7), and an atom's own refusal still routes as VIOLATION.

Runs the conformance workload, Collatz, against the assembled snapshot: its workflow admits a
request, computes, verifies through a transform that refuses, and writes through a side effect. The
platform's tests run on the workload, so a business domain's change never reaches them. Each refusal
case replaces one thing for that run only: the store's answer, or the termination check's module.
"""
import json
import shutil
import sys
import tempfile
from unittest import mock
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
SNAPSHOT = WORKSPACE / "snapshot"

# Test-only environment provisioning, as `run.sh` provides on PYTHONPATH: the platform's side effects
# and the workload's transforms. The runtime itself never manipulates sys.path.
for _root in (WORKSPACE / "software_governance", WORKSPACE / "conformance_workloads"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from capability_side_effects.implementation.CS_MUTABLE_JSON_V0.impl.executor import MutableJsonEngine  # noqa: E402
from runtime import api  # noqa: E402
from runtime import ct_executor  # noqa: E402
from runtime.dispatcher import CapabilityFaultError, UnlistedStepOutcomeError  # noqa: E402

WF = "WF_COLLATZ_CONJECTURE_V0"
NUMBERS = {"numbers": [3, 6, 27]}
TERMINATION_MODULE = "ct_pure_termination_check_v0"


def _run(root: Path):
    return api.run_workflow(wf_fqdn=f"workload::{WF}", payload=NUMBERS,
                            snapshot_root=str(SNAPSHOT), data_root=str(root))


def events(root: Path) -> list[dict]:
    traces = sorted((root / "traces" / "workload" / WF).rglob("*.jsonl"))
    return [json.loads(line) for line in traces[-1].read_text().splitlines() if line.strip()]


def fresh() -> Path:
    return Path(tempfile.mkdtemp(prefix="pgc_step_outcome_"))


def test_every_step_record_carries_its_outcome_and_continuation():
    root = fresh()
    try:
        _run(root)
        steps = [e["detail"] for e in events(root)
                 if e["event_type"] == "CC_STEP" and e.get("step_op") != "ADMIT"]
        assert steps and all(d.get("outcome") and d.get("continuation") in ("continue", "exit")
                             for d in steps), steps
        write = [d for d in steps if d["step_fqdn"].endswith("CS_MUTABLE_JSON_V0")][-1:]
        assert write and write[0]["outcome"] == "SUCCESS" and write[0]["continuation"] == "exit", write
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_admission_record_names_the_route_as_its_continuation():
    root = fresh()
    try:
        _run(root)
        admits = [e["detail"] for e in events(root)
                  if e["event_type"] == "CC_STEP" and e.get("step_op") == "ADMIT"]
        assert admits and all(d["outcome"] == "ACK" and d["continuation"] == "route"
                              for d in admits), admits
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_outcome_with_no_declared_continuation_refuses_and_runs_nothing_after_it():
    root = fresh()
    real = MutableJsonEngine.write
    MutableJsonEngine.write = lambda self, payload: {"result_status": "ALREADY_EXISTS"}
    try:
        try:
            _run(root)
        except UnlistedStepOutcomeError:
            pass
        else:
            raise AssertionError("an unlisted step outcome did not refuse")
        ev = events(root)
        write = [e["detail"] for e in ev if e["event_type"] == "CC_STEP"
                 and e["detail"]["step_fqdn"].endswith("CS_MUTABLE_JSON_V0")][-1:]
        assert write == [dict(write[0], outcome="ALREADY_EXISTS", continuation=None)], write
        errors = [e for e in ev if e["event_type"] == "ERROR"]
        assert len(errors) == 1 and errors[0]["detail"].get("outcome") == "ALREADY_EXISTS", \
            f"one refusal, one record: {[e['detail'] for e in errors]}"
        assert not any(e["event_type"] == "EVENT" for e in ev), "an announcement followed the refusal"
        # compute, verify, store: the write is the last step, and nothing after it runs
        assert sum(e["event_type"] == "CC_START" for e in ev) == 3, "a contract ran after the refusal"
        assert [e for e in ev if e["event_type"] == "WF_COMPLETE"][-1]["result_status"] == "VIOLATION"
    finally:
        MutableJsonEngine.write = real
        shutil.rmtree(root, ignore_errors=True)


def refused_by_fault(replace) -> list[dict]:
    """Run the workflow with one thing replaced; assert it refused as a fault, and return the trace."""
    root = fresh()
    try:
        with replace:
            try:
                _run(root)
            except CapabilityFaultError:
                pass
            else:
                raise AssertionError("a fault did not refuse the run")
        ev = events(root)
        errors = [e for e in ev if e["event_type"] == "ERROR"]
        assert len(errors) == 1 and errors[0]["detail"].get("message") == "capability fault", \
            f"one fault, one record: {[e['detail'].get('message') for e in errors]}"
        assert not any(e["event_type"] == "WF_ROUTE" and e["detail"].get("terminal") for e in ev), \
            "the fault was routed to an ending"
        assert not any(e["event_type"] == "EVENT" for e in ev), "an announcement followed the fault"
        assert [e for e in ev if e["event_type"] == "WF_COMPLETE"][-1]["result_status"] == "VIOLATION"
        return ev
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _import_replacing(module_name, replacement):
    real = ct_executor.importlib.import_module

    def imp(module, *a, **k):
        if module.endswith(module_name):
            return replacement(module)
        return real(module, *a, **k)
    return mock.patch.object(ct_executor.importlib, "import_module", imp)


def _missing(module):
    raise ModuleNotFoundError(f"No module named {module!r}", name=module)


class _Broken:
    def __getattr__(self, name):
        def execute(inputs, context=None):
            raise KeyError("sequences")
        return execute


class _NeverTerminates:
    """A termination check that answers, truthfully for its inputs, that a sequence never ended."""
    @staticmethod
    def execute(inputs, context=None):
        return {"all_terminate": False, "non_terminating": [27]}


def test_a_transform_that_will_not_load_refuses_and_is_not_routed_as_a_verdict():
    ev = refused_by_fault(_import_replacing(TERMINATION_MODULE, _missing))
    faults = [e["detail"] for e in ev if e["event_type"] == "ERROR"
              and e["detail"].get("message") == "capability fault"]
    assert faults[0]["refusal"] == "CT_ARTIFACT_NOT_FOUND", faults


def test_an_atom_that_breaks_rather_than_refuses_refuses_the_run():
    refused_by_fault(_import_replacing(TERMINATION_MODULE, lambda module: _Broken()))


def test_a_side_effect_that_raises_refuses_the_run():
    def crash(self, payload):
        raise OSError("disk unavailable")
    refused_by_fault(mock.patch.object(MutableJsonEngine, "write", crash))


def test_a_side_effect_that_states_no_outcome_refuses_the_run():
    refused_by_fault(mock.patch.object(MutableJsonEngine, "write", lambda self, payload: {}))


def test_an_atoms_own_refusal_still_routes_as_violation():
    # The platform's own atom, CT_PURE_REQUIRE_TRUE_V0, refuses a sequence that did not terminate.
    root = fresh()
    try:
        with _import_replacing(TERMINATION_MODULE, lambda module: _NeverTerminates()):
            _run(root)
        ev = events(root)
        check = [e["detail"] for e in ev if e["event_type"] == "CC_STEP"
                 and e["detail"]["step_fqdn"].endswith("CT_PURE_REQUIRE_TRUE_V0")]
        routes = [e["detail"].get("to_node") for e in ev if e["event_type"] == "WF_ROUTE"]
        assert check and check[0]["outcome"] == "VIOLATION" and check[0]["continuation"] == "exit", check
        assert routes[-1] == "EXIT_CONJECTURE_VIOLATED", routes
        assert not any(e["event_type"] == "ERROR" for e in ev), "a refusal was recorded as a fault"
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if not (SNAPSHOT / "manifest.json").is_file():
        print("SKIP  no assembled snapshot")
        sys.exit(0)
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
