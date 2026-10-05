"""
dispatcher.py — CC-level pipeline executor for the token-native runtime.

Executes a single Capability Contract by iterating its compiled pipeline steps.
Each step is a named-field execution instruction record materialized by the
compiler — the dispatcher consumes them blindly with no semantic reconstruction.

Consumed from RuntimePackage:
    pkg.dispatch.pipeline[cc_addr]             — ordered list of step dicts
    pkg.handlers.ct[ct_addr]["ct_ir"]          — CT-IR for pure transforms
    pkg.handlers.cs[cs_addr]                   — handler_ref + cs_metadata for side effects
    pkg.handlers.rb_policy[rb_addr][cs_addr]   — per-binding config (path, etc.)
    pkg.vocab.fqdn(addr)                       — address → FQDN for trace labels

Pipeline step format (named-field execution instruction record):
    {
        "addr":      int,        # CT or CS integer address
        "op":        str|None,   # None for CT, operation name for CS
        "inputs":    dict|None,  # resolved input bindings
        "outputs":   dict|None,  # surface mapping: {cc_field: "$.capability_result.ct_field"}
        "on_result": dict|None,  # continuation: {"SUCCESS": "continue", "VIOLATION": "exit"}
        "step_id":   str,        # symbolic name for cross-step $.results.<step_id>.X refs
    }

Input path grammar (step-level, compiler-emitted):
    $.inputs.<field>               — CC-level input (from cc_inputs)
    $.results.<step_id>.<field>    — previous step output (from step_results)
    <anything else>                — literal value (returned as-is)

Output path grammar:
    $.capability_result.<field>    — extract named field from raw step result

on_result actions:
    "continue"   — proceed to next step
    "exit"       — terminate pipeline and return this result_status

Result status:
    CT steps:  "SUCCESS" on completion, "VIOLATION" when the atom refuses; a fault refuses the run
    CS steps:  raw_result["result_status"] (declared by the CS runtime)
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from runtime.loader import RuntimePackage
from runtime.evidence import RecordedRefusal, TraceWriter
from runtime.ct_execute import execute_ct
from runtime.ct_errors import StructuredError
from runtime.ct_executor import CTExecutionError, CTFault


def _violation_payload(exc: StructuredError) -> dict[str, Any]:
    """A structured refusal, rendered into the shape a step result carries.

    Refusals reach the workflow as a VIOLATION result and route on it — the same channel the reach
    refusal uses. An exception escaping the dispatcher would bypass routing and the trace both.
    """
    return {
        "result_status": "VIOLATION",
        "refusal": exc.error_code,
        "node_category": exc.node_category,
        "message": str(exc),
    }


class CapabilityFaultError(RecordedRefusal):
    """A capability failed in a way its declaration does not answer for (`3a` §4.1, `3c` §7).

    A transform's refusal and a side effect's returned status are outcomes, and the contract routes
    on them. A module that will not load, a malformed seal, a capability that raises, or a result
    carrying no status are not outcomes. Turning them into VIOLATION routed a fault as a business
    answer: a reclaim whose check could not load ended as "still active". Execution refuses there.
    """


class UnlistedStepOutcomeError(RecordedRefusal):
    """A composed step ended with an outcome its contract declares no continuation for (`3a` EX-18).

    The step-level counterpart of the scheduler's `UnroutedOutcomeError`. A missing continuation is
    not a default: execution refuses there, and never proceeds past it.
    """


class BindingFault(StructuredError):
    """A binding reached nothing it may stand in for (`3c` RT-6).

    A malformed path, a step that has not run, or a field a successful result does not carry. None
    is not supplied in its place: a value the declarations never gave is a default.
    """

    def __init__(self, message: str):
        super().__init__(error_code="CT_EXECUTION_FAILED", node_category="CC", message=message)


def _refuse_binding(writer: TraceWriter, cc_addr: int, cc_fqdn: str, step_id: str,
                    exc: BindingFault) -> None:
    writer.error("capability fault", node=cc_addr, step=step_id, refusal=exc.error_code,
                 reason=str(exc))
    raise CapabilityFaultError(
        f"step {step_id!r} of {cc_fqdn}: {exc} — execution refuses rather than supply a value "
        f"the declarations never gave (3c RT-6)."
    ) from exc


class CSExecutionError(StructuredError):
    """A CS step could not be executed.

    `CS_EXECUTION_FAILED` is the only CS code the trace schema admits, so the distinction between
    an absent handler, an absent callable, a missing optional dependency and a failing capability
    is carried in the message rather than in a code this snapshot does not declare.

    Distinct from a VIOLATION return: a violation is governance deciding no, and it routes through
    the workflow. This is the environment being unable to run the step at all.
    """

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(
            error_code="CS_EXECUTION_FAILED",
            node_category="CS",
            message=message,
            cause=cause,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_cc(
    cc_addr:   int,
    rb_addr:   int,
    cc_inputs: dict[str, Any],
    pkg:       RuntimePackage,
    writer:    TraceWriter,
    data_root: str,
    wf_addr:   int = -1,
    node_key:  str = "",
) -> tuple[str, dict[str, Any]]:
    """
    Execute a CC pipeline and return (result_status, surface).

    Args:
        cc_addr:   Integer address of the Capability Contract to execute.
        rb_addr:   Integer address of the Runtime Binding governing this CC.
        cc_inputs: Resolved inputs for this CC (already bound by the scheduler).
        pkg:       Frozen RuntimePackage (loader output).
        writer:    TraceWriter for this execution trace.
        data_root: Absolute data directory root (for {{module_data_root}} expansion).

    Returns:
        (result_status, surface) where:
            result_status — final outcome string (e.g. "SUCCESS", "VIOLATION")
            surface       — dict of CC-level named outputs for downstream binding
    """
    cc_fqdn = pkg.vocab.fqdn(cc_addr)
    writer.cc_start(cc_addr, cc_fqdn, cc_inputs, node=node_key)

    steps = pkg.dispatch.pipeline.get(cc_addr, [])
    step_results: dict[str, dict[str, Any]] = {}  # step_id → surface fragment
    surface: dict[str, Any] = {}
    result_status = "SUCCESS"

    # Build a workflow executor closure for CS types that need nested WF invocation.
    # Injected unconditionally — CSs that don't need it ignore it.
    wf_executor = _make_workflow_executor(pkg, writer, data_root)

    for step in steps:
        step_addr:   int        = step["addr"]
        op:          str | None = step.get("op")
        inputs_spec: dict       = step.get("inputs") or {}
        outputs_spec: dict      = step.get("outputs") or {}
        on_result:   dict       = step.get("on_result") or {}
        # The compiler emits a step's name under `step`; `step_id` was the older spelling and is
        # still honoured. Reading only the latter left `step_results` unkeyed, so every
        # `$.results.<step>.<field>` resolved to None — a binding the grammar accepts, the compiler
        # renders and the runtime silently dropped.
        step_id:     str        = step.get("step_id") or step.get("step") or ""

        # Resolve step inputs from CC inputs and accumulated step results
        try:
            resolved_inputs = _resolve_step_inputs(inputs_spec, cc_inputs, step_results)
        except BindingFault as exc:
            _refuse_binding(writer, cc_addr, cc_fqdn, step_id, exc)

        # --- Execute step ---
        # A fault is recorded and refused here, at the step, rather than routed: it is not an
        # outcome the contract declares, and the trace must still say where execution stopped.
        try:
            if op is None:
                # CT step — pure computation, zero side effects
                result_status, raw_result = _execute_ct_step(step_addr, resolved_inputs, pkg, writer, cc_addr)
            else:
                # CS step — controlled side effect via declared handler
                result_status, raw_result = _execute_cs_step(
                    step_addr, op, resolved_inputs, rb_addr, pkg, data_root, wf_executor, wf_addr
                )
        except StructuredError as exc:
            writer.error(
                "capability fault",
                node=cc_addr, step=step_id, refusal=exc.error_code, reason=str(exc),
            )
            raise CapabilityFaultError(
                f"step {step_id!r} of {cc_fqdn} failed with {exc.error_code}: {exc} — a fault is not "
                f"a declared outcome, and execution refuses rather than route on it (3a §4.1, 3c §7)."
            ) from exc

        # Apply outputs mapping: {cc_field: "$.capability_result.<ct_field>"} → surface fragment
        try:
            surface_fragment = _apply_outputs(outputs_spec, raw_result, step_results, result_status)
        except BindingFault as exc:
            _refuse_binding(writer, cc_addr, cc_fqdn, step_id, exc)

        # Store surface fragment + raw capability_result for cross-step references.
        # $.results.<step_id>.<field>              — addresses the mapped surface
        # $.results.<step_id>.capability_result.<field> — addresses the raw result
        if step_id:
            step_results[step_id] = {**surface_fragment, "capability_result": raw_result}

        # Accumulate into CC surface
        surface.update(surface_fragment)

        # The continuation the contract declares for this outcome. None is the absence of one, which
        # is not a default: `3a` EX-18 requires refusal there. Proceeding past an unlisted outcome is
        # how a failed lookup once let a person be accepted (SoSyM study, case O3).
        action = on_result.get(result_status)

        # Emit step trace event — the outcome and the continuation it selected (`3e` EV-19)
        writer.cc_step(
            cc_addr,
            step_addr,
            pkg.vocab.fqdn(step_addr),
            op,
            surface_fragment,
            outcome=result_status,
            continuation=action,
        )

        if action is None:
            writer.error(
                "unlisted step outcome",
                node=cc_addr, step=step_id, outcome=result_status,
            )
            raise UnlistedStepOutcomeError(
                f"step {step_id!r} of {cc_fqdn} ended with {result_status!r}, for which the "
                f"contract declares no continuation — execution refuses rather than proceed (3a EX-18)."
            )
        if action == "exit":
            break

    # Merge cc_inputs fields not covered by pipeline outputs into the surface.
    # The pipeline outputs_spec is compiler-declared and may not map every input
    # field (e.g. CC_STORE_RESULTS receives sequences/all_terminate/non_terminating
    # as cc_inputs but only declares result_status in its outputs). Merging here
    # restores the expected surface without changing the pipeline contract.
    _INTERNAL_KEYS = {"__store__", "__pgs_store_entity__"}
    for key, value in cc_inputs.items():
        if key not in surface and key not in _INTERNAL_KEYS:
            surface[key] = value

    writer.cc_complete(cc_addr, cc_fqdn, result_status, surface, node=node_key)
    return result_status, surface


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------

def _execute_ct_step(
    ct_addr: int,
    resolved_inputs: dict[str, Any],
    pkg: RuntimePackage,
    writer: TraceWriter | None = None,
    cc_addr: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Execute a CT (pure transform) step.

    Returns ("SUCCESS", ct_outputs) on completion, and ("VIOLATION", refusal) when the atom refuses
    by raising `CTExecutionError` — its declared outcome. Raises `CTFault` for anything else: a
    failure the declaration does not answer for is not routed on.
    """
    ct_entry = pkg.handlers.ct.get(ct_addr)
    if ct_entry is None:
        raise RuntimeError(
            f"CT addr {ct_addr} not found in handlers — snapshot may be stale"
        )
    ct_ir = ct_entry.get("ct_ir", {})

    try:
        # Every atom the transform runs, at any depth of a molecule, leaves its own record; a replay
        # substitutes the recorded result of each atom declared not deterministic.
        observer = (lambda record: writer.ct_step(cc_addr, ct_addr, record)) if writer else None
        recorded = ((lambda path: writer.recorded_outcome(cc_addr, ct_addr, path))
                    if writer is not None and writer.replaying else None)
        raw_result = execute_ct(ct_ir, resolved_inputs, observer=observer, recorded=recorded)
        return "SUCCESS", (raw_result if isinstance(raw_result, dict) else {})
    except CTExecutionError as exc:
        # The atom's refusal → protocol VIOLATION, carrying what was refused. Returning a bare {}
        # here discarded the only account of why the step failed.
        return "VIOLATION", _violation_payload(exc)
    except StructuredError:
        raise
    except Exception as exc:
        # Unstructured, from the executor itself rather than the atom: a fault, named.
        raise CTFault(f"{type(exc).__name__}: {exc}", cause=exc) from exc


def _make_workflow_executor(
    pkg: RuntimePackage,
    writer: TraceWriter,
    data_root: str,
):
    """
    Build a workflow executor callable for injection into CS config.

    The returned callable lets CS implementations invoke sub-workflows without
    importing the scheduler directly (avoids circular imports at module load time).

    Interface: executor(wf_fqdn: str, payload: dict) -> (result_status: str, surface: dict)
    """
    def executor(wf_fqdn_or_addr, payload: dict) -> tuple[str, dict]:
        from runtime.scheduler import run_wf  # lazy import — avoids circular dependency
        # Compiler tokenizes nested dict string values to int addresses.
        # CS_WORKFLOW_LOOP_V0 receives int addrs from the compiled mapping; resolve to FQDN here.
        if isinstance(wf_fqdn_or_addr, int):
            wf_fqdn_or_addr = pkg.vocab.fqdn(wf_fqdn_or_addr)
        return run_wf(wf_fqdn_or_addr, payload, pkg, writer, data_root)
    return executor


def _execute_cs_step(
    cs_addr: int,
    op: str,
    resolved_inputs: dict[str, Any],
    rb_addr: int,
    pkg: RuntimePackage,
    data_root: str,
    wf_executor=None,
    wf_addr: int = -1,
) -> tuple[str, dict[str, Any]]:
    """
    Execute a CS (side effect) step.

    Looks up the CS handler and per-binding policy, expands path templates,
    instantiates the CS runtime, and calls execute(op, payload).

    Returns (result_status, raw_result) where result_status is declared by the CS.
    """
    cs_entry = pkg.handlers.cs.get(cs_addr)
    if cs_entry is None:
        raise RuntimeError(
            f"CS addr {cs_addr} not found in handlers — snapshot may be stale"
        )

    # Resolve per-RB policy config for this CS
    rb_cs_map = pkg.handlers.rb_policy.get(rb_addr, {})
    policy_entry = rb_cs_map.get(cs_addr, {})
    policy_raw = policy_entry.get("policy") or {}

    # An act that declared a reach resolves against the composed description the compiler sealed
    # for it — its own entities and the consulted ones, each marked. Handed over, not resolved
    # here: the runtime reads what the composition gives it.
    composed = pkg.handlers.wf_storage.get(wf_addr)
    if composed is not None and policy_raw.get("storage_structure_artifact") is not None:
        policy_raw = {**policy_raw, "storage_structure_artifact": composed}

    policy = _expand_policy(policy_raw, data_root, pkg.snapshot_root)

    # A reach reads and never writes. The operation declares whether it writes and the composed
    # description declares whether the entity is consulted, so the refusal rests on two declared
    # facts and infers nothing. Refused before the capability runs, because a write that has
    # happened cannot be unhappened.
    store_entity = resolved_inputs.get("__store__")
    if composed is not None and store_entity:
        entity = (composed.get("frontmatter", {}).get("core", {})
                  .get("entity_stores", {}).get(store_entity, {}))
        effect = ((cs_entry.get("cs_metadata", {}).get("operations", {})
                   .get("operations", {}).get(op, {})).get("effect"))
        if entity.get("reach") == "consulted" and effect == "write":
            # VIOLATION rather than an exception: a step states its outcome and the act routes on
            # it, which is how every other refusal reaches the workflow. Returned before the
            # capability runs, because a write that has happened cannot be unhappened.
            return "VIOLATION", {
                "result_status": "VIOLATION",
                "refusal": "REACH_IS_READ_ONLY",
                "store": store_entity,
                "described_by": entity.get("described_by"),
                "message": (
                    f"act may not write to '{store_entity}': it is described by "
                    f"{entity.get('described_by')}, which this act consults and does not own. "
                    f"The subdomain that owns a record is the only writer of it"
                ),
            }

    # Inject workflow executor for CS types that need nested WF invocation.
    # Injected unconditionally — CSs that don't use it ignore the key.
    if wf_executor is not None:
        policy = {**policy, "workflow_executor": wf_executor}

    # Instantiate CS runtime
    handler_ref = cs_entry["handler_ref"]
    cs_metadata = cs_entry.get("cs_metadata", {})
    cs_fqdn = pkg.vocab.fqdn(cs_addr)

    # Loading a sealed handler_ref runs code this package does not own. Every step of that —
    # importing the module, finding the callable, constructing it — may raise, and none of it may
    # escape as a bare ModuleNotFoundError or AttributeError. An ungoverned crash is not a declared
    # outcome, and the trace cannot record what it never sees.
    module_path = handler_ref.get("module")
    callable_name = handler_ref.get("callable")
    if not module_path or not callable_name:
        raise CSExecutionError(
            f"incomplete handler_ref for CS {cs_fqdn}: module={module_path!r} "
            f"callable={callable_name!r}"
        )

    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == module_path or module_path.startswith(missing + "."):
            raise CSExecutionError(
                f"handler_ref names module {module_path!r} for CS {cs_fqdn} and it is not "
                f"importable — the snapshot names an implementation this environment does not carry",
                cause=exc,
            ) from exc
        raise CSExecutionError(
            f"handler_ref module {module_path!r} for CS {cs_fqdn} requires {missing!r}, which is "
            f"not installed — the domain's optional dependency is missing, not the capability",
            cause=exc,
        ) from exc
    except ImportError as exc:
        raise CSExecutionError(
            f"handler_ref module {module_path!r} for CS {cs_fqdn} failed to import: {exc}",
            cause=exc,
        ) from exc

    try:
        cls = getattr(mod, callable_name)
    except AttributeError as exc:
        raise CSExecutionError(
            f"handler_ref names callable {callable_name!r} in {module_path!r} for CS {cs_fqdn} "
            f"and the module does not define it",
            cause=exc,
        ) from exc

    try:
        runtime = cls(config=policy, metadata=cs_metadata, capability_code=cs_fqdn)
    except StructuredError:
        raise
    except Exception as exc:
        raise CSExecutionError(
            f"CS {cs_fqdn} ({module_path}.{callable_name}) raised while being constructed: {exc}",
            cause=exc,
        ) from exc

    # Translate __store__ (compiler-emitted entity tag) to __pgs_store_entity__ (CS protocol key)
    store_entity = resolved_inputs.get("__store__")
    cs_inputs = {k: v for k, v in resolved_inputs.items() if k != "__store__"}
    if store_entity:
        cs_inputs["__pgs_store_entity__"] = store_entity

    # The capability itself is external code. The CT path already wraps its atom call; this is the
    # same boundary, and it was the one place a domain exception could still reach the caller
    # unstructured.
    try:
        raw_result = runtime.execute(op=op, payload=cs_inputs)
    except StructuredError:
        raise
    except Exception as exc:
        raise CSExecutionError(
            f"CS {cs_fqdn} raised during op {op!r}: {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc
    # A capability states its outcome. One that states none has not answered, and supplying SUCCESS
    # for it was a default the declarations never gave (`3c` RT-6).
    if not isinstance(raw_result, dict) or not raw_result.get("result_status"):
        raise CSExecutionError(
            f"CS {cs_fqdn} op {op!r} returned no result_status — a capability that states no "
            f"outcome has not answered, and none is supplied for it"
        )
    return raw_result["result_status"], raw_result


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------

def _resolve_step_inputs(
    inputs_spec: dict[str, Any],
    cc_inputs: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    Resolve step input bindings to concrete values.

    Path grammar:
        $.inputs.<field>               → cc_inputs[field]
        $.results.<step_id>.<field>    → step_results[step_id][field]
        <other>                        → literal (returned as-is)

    A source that is absent is left out, never set to None; a value present as null is passed. The
    capability then sees exactly what was given, and refuses on its own declaration if it needs it.
    """
    resolved = _resolve_value(inputs_spec, cc_inputs, step_results)
    return resolved


def _resolve_value(
    value: Any,
    cc_inputs: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
) -> Any:
    """Resolve a single binding value — recursively handles nested dicts/lists.

    Returns `ABSENT` for a path whose source is absent. A mapping leaves such a key out; a list
    cannot, because leaving a member out moves every member after it, so it refuses.
    """
    if isinstance(value, str):
        if value.startswith("$.inputs."):
            return _nested_get(cc_inputs, value[len("$.inputs."):])

        if value.startswith("$.results."):
            step_id, field_path = _split_results_path(value)
            if step_id not in step_results:
                raise BindingFault(f"{value!r} names step {step_id!r}, which has not run")
            return _nested_get(step_results[step_id], field_path)

        # Literal string value
        return value

    if isinstance(value, dict):
        resolved = {k: _resolve_value(v, cc_inputs, step_results) for k, v in value.items()}
        return {k: v for k, v in resolved.items() if v is not ABSENT}

    if isinstance(value, (list, tuple)):
        resolved = [_resolve_value(v, cc_inputs, step_results) for v in value]
        if any(v is ABSENT for v in resolved):
            raise BindingFault(f"a list binding {list(value)!r} names a source that is absent")
        return resolved

    return value  # int, float, bool, None — returned as-is


class _Absent:
    """A source that is not there. Distinct from None, which is a value present as null."""

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Any = _Absent()


def _split_results_path(path: str) -> tuple[str, str]:
    """`$.results.<step_id>.<field>[.<nested>...]` → (step_id, field path); malformed refuses."""
    step_id, dot, field_path = path[len("$.results."):].partition(".")
    if not step_id or not dot or not field_path:
        raise BindingFault(f"{path!r} is not a path of the form $.results.<step>.<field>")
    return step_id, field_path


def _nested_get(obj: Any, dotted_key: str) -> Any:
    """Traverse a nested dict by a dot-separated key path. Returns `ABSENT` on miss."""
    current = obj
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return ABSENT
        current = current[part]
    return current


# ---------------------------------------------------------------------------
# Output mapping
# ---------------------------------------------------------------------------

def _apply_outputs(
    outputs_spec: dict[str, str],
    raw_result: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
    result_status: str,
) -> dict[str, Any]:
    """
    Apply the compiler-emitted outputs mapping to the raw step result.

    Supported path prefixes:
        $.result_status                    — the outcome this step ended with
        $.capability_result.<field>        — field from this step's raw result
        $.<field>                          — the same, written bare
        $.results.<step_id>.<field>        — field from a prior step's surface fragment

    A result is mapped only when the step succeeded, and then every field mapped from it must be
    there: a successful result missing a field it declares is a fault, not a null. Any other outcome
    maps nothing from the result — what it carries is a refusal, not the fields a success declares —
    and the raw result stays addressable under `capability_result`. A prior step's field that is
    absent is left out, as an input is.

    Unmapped fields from raw_result are NOT included — surface is compiler-declared.
    """
    fragment: dict[str, Any] = {}
    for surface_field, path in (outputs_spec or {}).items():
        if not isinstance(path, str) or not path.startswith("$."):
            fragment[surface_field] = path  # literal
        elif path == "$.result_status":
            fragment[surface_field] = result_status
        elif path.startswith("$.results."):
            step_id, field_path = _split_results_path(path)
            if step_id not in step_results:
                raise BindingFault(f"{path!r} names step {step_id!r}, which has not run")
            found = _nested_get(step_results[step_id], field_path)
            if found is not ABSENT:
                fragment[surface_field] = found
        elif result_status == "SUCCESS":
            field = path[len("$.capability_result."):] if path.startswith(
                "$.capability_result.") else path[len("$."):]
            found = _nested_get(raw_result, field)
            if found is ABSENT:
                raise BindingFault(f"the result maps {path!r} to {surface_field!r} and carries no "
                                   f"{field!r}")
            fragment[surface_field] = found

    return fragment


# ---------------------------------------------------------------------------
# Policy template expansion
# ---------------------------------------------------------------------------

def _expand_policy(
    policy_raw: dict[str, Any], data_root: str, snapshot_root: str = ""
) -> dict[str, Any]:
    """
    Expand path templates in the CS policy config.

        {{module_data_root}}   where a capability keeps its state
        {{snapshot_root}}      the composition this workflow is executing from

    The second exists for capabilities that *observe* the composition rather than store state in
    it. Such a capability must be bound to the snapshot it is running inside — if the root came
    from a caller, a workflow could be pointed at a different composition and would report
    confidently about the wrong one.

    Serializes to JSON, replaces the template strings, deserializes back. Both roots must be
    absolute path strings — never relative.
    """
    if not policy_raw:
        return {}

    policy_str = json.dumps(policy_raw)
    policy_str = policy_str.replace("{{module_data_root}}", str(data_root).rstrip("/"))
    policy_str = policy_str.replace("{{snapshot_root}}", str(snapshot_root).rstrip("/"))
    return json.loads(policy_str)
