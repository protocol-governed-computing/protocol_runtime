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
    "continue"   — proceed to next step (default if status not listed)
    "exit"       — terminate pipeline and return this result_status

Result status:
    CT steps:  "SUCCESS" on completion, "VIOLATION" on any exception
    CS steps:  raw_result["result_status"] (declared by the CS runtime)
"""

from __future__ import annotations

import importlib
import json
from typing import Any

from runtime.loader import RuntimePackage
from runtime.evidence import TraceWriter
from runtime.ct_execute import execute_ct


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
    writer.cc_start(cc_addr, cc_fqdn, cc_inputs)

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
        step_id:     str        = step.get("step_id") or ""

        # Resolve step inputs from CC inputs and accumulated step results
        resolved_inputs = _resolve_step_inputs(inputs_spec, cc_inputs, step_results)

        # --- Execute step ---
        if op is None:
            # CT step — pure computation, zero side effects
            result_status, raw_result = _execute_ct_step(step_addr, resolved_inputs, pkg)
        else:
            # CS step — controlled side effect via declared handler
            result_status, raw_result = _execute_cs_step(
                step_addr, op, resolved_inputs, rb_addr, pkg, data_root, wf_executor
            )

        # Apply outputs mapping: {cc_field: "$.capability_result.<ct_field>"} → surface fragment
        surface_fragment = _apply_outputs(outputs_spec, raw_result, step_results)

        # Store surface fragment + raw capability_result for cross-step references.
        # $.results.<step_id>.<field>              — addresses the mapped surface
        # $.results.<step_id>.capability_result.<field> — addresses the raw result
        if step_id:
            step_results[step_id] = {**surface_fragment, "capability_result": raw_result}

        # Accumulate into CC surface
        surface.update(surface_fragment)

        # Emit step trace event
        writer.cc_step(
            cc_addr,
            step_addr,
            pkg.vocab.fqdn(step_addr),
            op,
            surface_fragment,
        )

        # Route: "exit" → break pipeline; "continue" (or unlisted) → proceed
        action = on_result.get(result_status, "continue")
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

    writer.cc_complete(cc_addr, cc_fqdn, result_status, surface)
    return result_status, surface


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------

def _execute_ct_step(
    ct_addr: int,
    resolved_inputs: dict[str, Any],
    pkg: RuntimePackage,
) -> tuple[str, dict[str, Any]]:
    """
    Execute a CT (pure transform) step.

    Returns ("SUCCESS", ct_outputs) on completion.
    Returns ("VIOLATION", {}) on any exception — CT failure is a protocol violation.
    """
    ct_entry = pkg.handlers.ct.get(ct_addr)
    if ct_entry is None:
        raise RuntimeError(
            f"CT addr {ct_addr} not found in handlers — snapshot may be stale"
        )
    ct_ir = ct_entry.get("ct_ir", {})

    try:
        raw_result = execute_ct(ct_ir, resolved_inputs)
        return "SUCCESS", (raw_result if isinstance(raw_result, dict) else {})
    except Exception:
        # CT exception → protocol VIOLATION; do not propagate
        return "VIOLATION", {}


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
    policy = _expand_policy(policy_raw, data_root)

    # Inject workflow executor for CS types that need nested WF invocation.
    # Injected unconditionally — CSs that don't use it ignore the key.
    if wf_executor is not None:
        policy = {**policy, "workflow_executor": wf_executor}

    # Instantiate CS runtime
    handler_ref = cs_entry["handler_ref"]
    cs_metadata = cs_entry.get("cs_metadata", {})
    cs_fqdn = pkg.vocab.fqdn(cs_addr)

    mod = importlib.import_module(handler_ref["module"])
    cls = getattr(mod, handler_ref["callable"])
    runtime = cls(config=policy, metadata=cs_metadata, capability_code=cs_fqdn)

    # Translate __store__ (compiler-emitted entity tag) to __pgs_store_entity__ (CS protocol key)
    store_entity = resolved_inputs.get("__store__")
    cs_inputs = {k: v for k, v in resolved_inputs.items() if k != "__store__"}
    if store_entity:
        cs_inputs["__pgs_store_entity__"] = store_entity

    raw_result = runtime.execute(op=op, payload=cs_inputs)
    if not isinstance(raw_result, dict):
        raw_result = {}

    result_status = raw_result.get("result_status", "SUCCESS")
    return result_status, raw_result


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
    """
    resolved: dict[str, Any] = {}
    for key, value in inputs_spec.items():
        resolved[key] = _resolve_value(value, cc_inputs, step_results)
    return resolved


def _resolve_value(
    value: Any,
    cc_inputs: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
) -> Any:
    """Resolve a single binding value — recursively handles nested dicts/lists."""
    if isinstance(value, str):
        if value.startswith("$.inputs."):
            field = value[len("$.inputs."):]
            return _nested_get(cc_inputs, field)

        if value.startswith("$.results."):
            # $.results.<step_id>.<field>[.<nested>...]
            after = value[len("$.results."):]
            dot = after.find(".")
            if dot < 0:
                return None  # malformed path
            step_id = after[:dot]
            field_path = after[dot + 1:]
            step_surface = step_results.get(step_id, {})
            return _nested_get(step_surface, field_path)

        # Literal string value
        return value

    if isinstance(value, dict):
        return {k: _resolve_value(v, cc_inputs, step_results) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_resolve_value(v, cc_inputs, step_results) for v in value]

    return value  # int, float, bool, None — returned as-is


def _nested_get(obj: Any, dotted_key: str) -> Any:
    """Traverse a nested dict by a dot-separated key path. Returns None on miss."""
    parts = dotted_key.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


# ---------------------------------------------------------------------------
# Output mapping
# ---------------------------------------------------------------------------

def _apply_outputs(
    outputs_spec: dict[str, str],
    raw_result: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    Apply the compiler-emitted outputs mapping to the raw step result.

    Supported path prefixes:
        $.capability_result.<field>        — field from this step's raw result
        $.results.<step_id>.<field>        — field from a prior step's surface fragment

    Unmapped fields from raw_result are NOT included — surface is compiler-declared.
    """
    if not outputs_spec:
        return {}

    fragment: dict[str, Any] = {}
    for surface_field, path in outputs_spec.items():
        if not isinstance(path, str):
            fragment[surface_field] = path
        elif path.startswith("$.capability_result."):
            result_field = path[len("$.capability_result."):]
            fragment[surface_field] = _nested_get(raw_result, result_field)
        elif path.startswith("$.results."):
            after = path[len("$.results."):]
            dot = after.find(".")
            if dot < 0:
                fragment[surface_field] = None
            else:
                step_id = after[:dot]
                field_path = after[dot + 1:]
                step_surface = step_results.get(step_id, {})
                fragment[surface_field] = _nested_get(step_surface, field_path)
        elif path.startswith("$."):
            # Bare $.field path — direct reference into the raw step result
            field_path = path[len("$."):]
            fragment[surface_field] = _nested_get(raw_result, field_path)
        else:
            # Literal value — returned as-is
            fragment[surface_field] = path

    return fragment


# ---------------------------------------------------------------------------
# Policy template expansion
# ---------------------------------------------------------------------------

def _expand_policy(policy_raw: dict[str, Any], data_root: str) -> dict[str, Any]:
    """
    Expand {{module_data_root}} templates in the CS policy config.

    Serializes to JSON, replaces the template string, deserializes back.
    data_root must be an absolute path string — never a relative path.
    """
    if not policy_raw:
        return {}

    root = str(data_root).rstrip("/")
    policy_str = json.dumps(policy_raw)
    policy_str = policy_str.replace("{{module_data_root}}", root)
    return json.loads(policy_str)
