"""
conformance.py — CT Conformance Runner

Loads compiled CT_CONFORMANCE artifacts from protocol_snapshot/conformance/
and executes each via CTExecutor, asserting expected outputs against actual outputs.

Called by pgs build — not by runtime run.
Does NOT write snapshot_status.json; that is the caller's responsibility.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.ct_executor import CTExecutor, CTExecutionError


@dataclass
class CaseResult:
    fqdn: str
    passed: bool
    error: str | None = None


@dataclass
class ConformanceResult:
    passed: int = 0
    failed: int = 0
    artifact_count: int = 0
    snapshot_hash: str = ""
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.artifact_count > 0


_ALLOWED_MODES: frozenset[str] = frozenset({"exact", "property", "schema"})
_ALLOWED_TYPES: dict[str, frozenset[str]] = {
    "property": frozenset({"hex_string", "byte_length_range", "non_zero"}),
    "schema": frozenset({"json_schema"}),
}


def _assert_structural(actual: dict[str, Any], assertions: dict[str, Any]) -> str | None:
    """
    Validate structural assertions for non-deterministic fields.

    Assertion spec shape (INVARIANT_CONFORMANCE_ASSERTION_MODE_VALID_V0):
        {field_name: {mode: <mode>, type: <type>, ...params}}

    Mode vocabulary: { exact, property, schema }
    Type vocabulary per mode:
        property → { hex_string, byte_length_range, non_zero }
        schema   → { json_schema }

    Returns an error message string if any assertion fails, else None.
    Raises AssertionError for unknown modes or types (hard failure — never silent).
    """
    for field_name, spec in assertions.items():
        if field_name not in actual:
            return f"assertion field '{field_name}' missing from actual output"
        value = actual[field_name]

        mode = spec.get("mode")
        if mode is None:
            raise AssertionError(
                f"Assertion spec for '{field_name}' missing required 'mode' field. "
                f"Allowed: {sorted(_ALLOWED_MODES)}. "
                f"This indicates invalid TEST_DATA that should have been caught at compile time."
            )
        if mode not in _ALLOWED_MODES:
            raise AssertionError(
                f"Assertion for '{field_name}' has unknown mode '{mode}'. "
                f"Allowed: {sorted(_ALLOWED_MODES)}. "
                f"This indicates invalid TEST_DATA that should have been caught at compile time."
            )

        if mode == "exact":
            # exact mode: field is checked via expected dict, not assertions block
            continue

        assert_type = spec.get("type")
        allowed_types = _ALLOWED_TYPES.get(mode, frozenset())
        if assert_type is None:
            raise AssertionError(
                f"Assertion for '{field_name}' with mode '{mode}' missing required 'type' field. "
                f"Allowed types: {sorted(allowed_types)}."
            )
        if assert_type not in allowed_types:
            raise AssertionError(
                f"Assertion for '{field_name}' has unknown type '{assert_type}' for mode '{mode}'. "
                f"Allowed: {sorted(allowed_types)}. "
                f"This indicates invalid TEST_DATA that should have been caught at compile time."
            )

        if mode == "property":
            if assert_type == "hex_string":
                if not isinstance(value, str):
                    return f"field '{field_name}': expected hex string, got {type(value).__name__}"
                hex_val = value[2:] if value.startswith("0x") else value
                try:
                    raw = bytes.fromhex(hex_val)
                except ValueError:
                    return f"field '{field_name}': not a valid hex string: {value!r}"
                byte_length = spec.get("byte_length")
                if byte_length is not None and len(raw) != byte_length:
                    return (
                        f"field '{field_name}': expected {byte_length} bytes, "
                        f"got {len(raw)} bytes (value: {value!r})"
                    )

            elif assert_type == "byte_length_range":
                min_len = spec["min"]
                max_len = spec["max"]
                hex_val = value[2:] if isinstance(value, str) and value.startswith("0x") else value
                try:
                    raw = bytes.fromhex(hex_val) if isinstance(value, str) else value
                except (ValueError, AttributeError):
                    return f"field '{field_name}': cannot determine byte length: {value!r}"
                if not (min_len <= len(raw) <= max_len):
                    return (
                        f"field '{field_name}': expected {min_len}–{max_len} bytes, "
                        f"got {len(raw)} bytes"
                    )

            elif assert_type == "non_zero":
                if value == 0 or value == "0x0" or value == b"\x00" or value == "" or value is None:
                    return f"field '{field_name}': expected non-zero value, got {value!r}"

        elif mode == "schema":
            raise AssertionError(
                f"Assertion for '{field_name}': schema/json_schema validation is not supported in the conformance runner."
            )

    return None


def _resolve_outputs(ct_ir: dict[str, Any], vars_result: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve ct_ir output mapping from executor result vars.

    ct_ir.outputs maps output key → {"from": "<var_name>"}
    Each output key is looked up inside the named var dict.
    """
    outputs_spec = ct_ir.get("outputs", {})
    if not outputs_spec:
        return vars_result

    actual: dict[str, Any] = {}
    for output_key, output_spec in outputs_spec.items():
        from_var = output_spec.get("from")
        if not from_var or from_var not in vars_result:
            continue
        source = vars_result[from_var]
        if isinstance(source, dict) and output_key in source:
            actual[output_key] = source[output_key]
        else:
            actual[output_key] = source
    return actual


def _snapshot_hash(conformance_dir: Path) -> str:
    """Stable SHA-256 over sorted conformance artifact names + contents."""
    h = hashlib.sha256()
    for path in sorted(conformance_dir.glob("*.json")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def run(snapshot_root: Path) -> ConformanceResult:
    """
    Execute all CT conformance tests in snapshot_root/conformance/.

    Returns ConformanceResult with per-case pass/fail detail.
    Raises FileNotFoundError if conformance dir is missing.
    """
    conformance_dir = snapshot_root / "conformance"
    if not conformance_dir.exists():
        raise FileNotFoundError(f"Conformance directory not found: {conformance_dir}")

    case_files = sorted(conformance_dir.glob("*.json"))
    result = ConformanceResult(
        artifact_count=len(case_files),
        snapshot_hash=_snapshot_hash(conformance_dir),
    )

    executor = CTExecutor()

    for case_file in case_files:
        artifact = json.loads(case_file.read_text())
        fqdn = artifact.get("fqdn", case_file.stem)
        ct_ir = artifact.get("ct_ir", {})
        inputs = ct_ir.get("inputs", {})
        expected = artifact.get("expected", {})
        assertions = artifact.get("assertions", {})

        expected_outcome = artifact.get("expected_outcome", "SUCCESS")

        try:
            vars_result = executor.execute(ct_ir=ct_ir, inputs=inputs)
            actual = _resolve_outputs(ct_ir, vars_result)

            if expected_outcome == "VIOLATION":
                # CT completed without error but a VIOLATION was expected.
                result.failed += 1
                result.cases.append(CaseResult(
                    fqdn=fqdn,
                    passed=False,
                    error="expected CTExecutionError (VIOLATION) but CT completed without raising",
                ))
                continue

            # Structural assertions validate non-deterministic fields by shape/type/size.
            # Fields covered by assertions are excluded from exact-match comparison.
            assertion_error = _assert_structural(actual, assertions) if assertions else None

            # Exact match on fields NOT covered by assertions
            asserted_keys = set(assertions.keys())
            expected_exact = {k: v for k, v in expected.items() if k not in asserted_keys}
            actual_exact = {k: v for k, v in actual.items() if k not in asserted_keys}

            if assertion_error:
                result.failed += 1
                result.cases.append(CaseResult(fqdn=fqdn, passed=False, error=f"assertion failed: {assertion_error}"))
            elif actual_exact != expected_exact:
                result.failed += 1
                result.cases.append(CaseResult(
                    fqdn=fqdn,
                    passed=False,
                    error=f"output mismatch\n    expected: {json.dumps(expected_exact)}\n    actual:   {json.dumps(actual_exact)}",
                ))
            else:
                result.passed += 1
                result.cases.append(CaseResult(fqdn=fqdn, passed=True))

        except CTExecutionError as e:
            if expected_outcome == "VIOLATION":
                # CT raised as expected — PASS.
                result.passed += 1
                result.cases.append(CaseResult(fqdn=fqdn, passed=True))
            else:
                result.failed += 1
                result.cases.append(CaseResult(fqdn=fqdn, passed=False, error=str(e)))

    return result
