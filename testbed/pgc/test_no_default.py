"""
A binding that reaches nothing is never given a value (`3c` RT-6).

Every resolver answered a path it could not follow with None, and the consumer could not tell that
from a value present as null. A survey of the regression counted 168 such lookups: 66 step outputs
read from a result that was a refusal, 95 optional step inputs, 4 workflow inputs, and one contract
whose mapping named a field its transform never returned. None was a default the declarations never
gave.

Now:
    R0  a transform's path that reaches nothing refuses as a fault;
    R1  a result is mapped only on SUCCESS, and every field mapped from it must be there;
    R2  an absent source at step or workflow level is left out, never set to None;
    R3  a malformed path refuses.
A value present as null passes everywhere.

Run:  python testbed/pgc/test_no_default.py
"""
import sys

from runtime.ct_executor import CTFault, _CTContext, _LoopContext
from runtime.dispatcher import ABSENT, BindingFault, _apply_outputs, _resolve_step_inputs
from runtime.memory import ExecutionContext, MalformedBindingError


def _raises(exc_type, fn, *args, text: str = ""):
    try:
        fn(*args)
    except exc_type as exc:
        assert text in str(exc), exc
        return
    raise AssertionError(f"{fn.__name__}{args!r} did not refuse")


# --- R0: a transform's path ------------------------------------------------------------------

def test_a_transform_path_that_reaches_nothing_refuses():
    ctx = _CTContext(inputs={"a": {"b": 1}})
    ctx.set_value("x", {"y": 2})
    assert ctx.resolve("$.inputs.a.b") == 1 and ctx.resolve("$.results.x.y") == 2
    _raises(CTFault, ctx.resolve, "$.inputs.missing", text="reaches nothing")
    _raises(CTFault, ctx.resolve, "$.inputs.a.b.c", text="reaches nothing")
    _raises(CTFault, ctx.resolve, "$.results.never.y", text="no step has produced")
    _raises(CTFault, ctx.resolve, "$.elsewhere.y", text="no root")
    _raises(CTFault, ctx.resolve, "inputs.a", text="not a path")


def test_a_transform_path_to_a_null_passes():
    assert _CTContext(inputs={"a": None}).resolve("$.inputs.a") is None


def test_a_loop_path_that_reaches_nothing_refuses():
    parent = _CTContext(inputs={"a": 1})
    loop = _LoopContext(parent, {"n": 0}, "item", {"k": None})
    assert loop.resolve("$.accumulator.n") == 0 and loop.resolve("$.iterator.k") is None
    _raises(CTFault, loop.resolve, "$.accumulator.m", text="reaches nothing")
    _raises(CTFault, loop.resolve, "$.inputs.b", text="reaches nothing")


# --- R2 and R3 at a step ---------------------------------------------------------------------

def test_an_absent_step_input_is_left_out_and_a_null_passes():
    spec = {"a": "$.inputs.a", "gone": "$.inputs.gone", "n": "$.inputs.n",
            "nested": {"keep": "$.results.s.f", "drop": "$.results.s.nothing"}, "lit": "x"}
    got = _resolve_step_inputs(spec, {"a": 1, "n": None}, {"s": {"f": 2}})
    assert got == {"a": 1, "n": None, "nested": {"keep": 2}, "lit": "x"}, got


def test_a_list_naming_an_absent_source_refuses():
    _raises(BindingFault, _resolve_step_inputs, {"l": ["$.inputs.a", "$.inputs.gone"]},
            {"a": 1}, {}, text="absent")


def test_a_malformed_or_unreached_step_path_refuses():
    _raises(BindingFault, _resolve_step_inputs, {"a": "$.results.s"}, {}, {"s": {}},
            text="not a path")
    _raises(BindingFault, _resolve_step_inputs, {"a": "$.results.later.f"}, {}, {},
            text="has not run")


# --- R1: a step's outputs --------------------------------------------------------------------

def test_a_successful_result_must_carry_every_field_mapped_from_it():
    spec = {"v": "$.capability_result.v", "w": "$.w", "status": "$.result_status", "lit": 3}
    got = _apply_outputs(spec, {"v": None, "w": 1}, {}, "SUCCESS")
    assert got == {"v": None, "w": 1, "status": "SUCCESS", "lit": 3}, got
    _raises(BindingFault, _apply_outputs, {"v": "$.capability_result.v"}, {}, {}, "SUCCESS",
            text="carries no")


def test_any_other_outcome_maps_nothing_from_the_result():
    spec = {"v": "$.capability_result.v", "status": "$.result_status", "lit": "x"}
    refusal = {"result_status": "VIOLATION", "refusal": "X", "v": "stale"}
    assert _apply_outputs(spec, refusal, {}, "VIOLATION") == {"status": "VIOLATION", "lit": "x"}


def test_a_prior_steps_absent_field_is_left_out():
    assert _apply_outputs({"p": "$.results.s.gone"}, {}, {"s": {}}, "SUCCESS") == {}


def test_the_absence_marker_is_never_a_value():
    assert ABSENT is not None and repr(ABSENT) == "ABSENT"


# --- R2 and R3 in a workflow -----------------------------------------------------------------

def test_an_absent_workflow_binding_is_left_out_and_a_null_passes():
    ctx = ExecutionContext({"a": 1, "n": None})
    ctx.record_result(7, {"f": 2})
    got = ctx.resolve_inputs({"a": "$.payload.a", "n": "$.inputs.n", "gone": "$.payload.gone",
                              "f": "$.results.7.f", "later": "$.results.9.f",
                              "nested": {"x": "$.payload.gone", "y": "$.payload.a"}})
    assert got == {"a": 1, "n": None, "f": 2, "nested": {"y": 1}}, got


def test_a_malformed_workflow_path_refuses():
    ctx = ExecutionContext({})
    for path in ("$.results.7", "$.results.CC_X.f"):
        _raises(MalformedBindingError, ctx.resolve_inputs, {"a": path}, text="not a path")
    _raises(MalformedBindingError, ctx.resolve_inputs, {"l": ["$.payload.gone"]}, text="absent")


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
