"""
conformance.py — transform conformance, run in the build that supplies the transforms.

A build's vectors (TEST_DATA) are compiled into runnable cases, each bound to its transform exactly as
the composition sealed it. This runs every case of one build and reports every transform the build
supplies as **proven** (every case ran and passed), **unproven** (no vector tests it) or **refused** (a
case failed). It never reports success over nothing: a build with no vector is reported, by name, as
unproven. The build is a domain's, or the platform's for the transforms the platform supplies.
Governed by `conformance::CONSTITUTION_TEST_DATA_V2`.

What a build supplies and what it only carries is read from the capabilities its attestation records
as imported, never from a transform's name: a carried copy is byte-identical to its supplier's, and the
platform's transforms are not named for the platform. A build with any failed case is refused, whoever
supplies the transform the case tests.

A case for a molecule supplies the recorded result of each non-deterministic step. The executor
substitutes it and never runs the step; this confirms, through the step observer, that every supplied
result was used and that no non-deterministic step ran. Such a case is therefore also a proof of replay.

Reads only the domain's compiled JSON — never a protocol document. Runs after a successful compile and
before assembly, on every build: the composition seals a transform's declaration and not its code, so
only running the cases shows the code still does what the declaration says.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.ct_executor import CTArtifactNotFound, CTExecutor, CTExecutionError

NONDETERMINISTIC_PURITY = "ct_impure"
# Where a build's result is written, beside its compiled projections, so the assembler carries it into
# the composition as evidence of its own — apart from composition conformance.
RESULT_DIR = "transform_conformance"
RESULT_FILE = "result.json"


@dataclass
class CaseResult:
    fqdn: str
    passed: bool
    error: str | None = None


@dataclass
class DomainResult:
    """What one domain's build proved about its transforms."""
    domain: str
    proven: list[str] = field(default_factory=list)
    unproven: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def admitted(self) -> bool:
        """A build is admitted when nothing was refused and no case failed, whoever supplies the
        transform a case tests. Unproven is reported, never refused."""
        return not self.refused and all(c.passed for c in self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "counts": {"proven": len(self.proven), "unproven": len(self.unproven),
                       "refused": len(self.refused), "cases": len(self.cases),
                       "cases_failed": sum(1 for c in self.cases if not c.passed)},
            "proven": self.proven,
            "unproven": self.unproven,
            "refused": self.refused,
            # Transforms this build carries from another's surface. Their proof belongs to the build
            # that supplies them, so they are named here rather than counted either way.
            "carried": self.carried,
            "cases": [{"case": c.fqdn, "passed": c.passed, **({"error": c.error} if c.error else {})}
                      for c in self.cases],
        }


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


def _canonical(compiled: Path, kind_dir: str) -> list[dict[str, Any]]:
    folder = compiled / "canonical" / kind_dir
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def _build_manifest(compiled: Path, structure: str | None = None) -> dict[str, Any]:
    """The build manifest this build was compiled from.

    A domain compiles one. The platform compiles every build declaration its surface holds, so its
    build names the one it was compiled from; a build compiling several and naming none is refused.
    """
    manifests = [a for a in _canonical(compiled, "structures")
                 if "::STRUCTURE_BUILD_" in a.get("fqdn_id", "") and a.get("fqdn_id", "").endswith(
                     tuple(f"_CONFIG_V{n}" for n in range(10)))]
    if structure:
        manifests = [a for a in manifests if a["fqdn_id"].split("::")[1] == structure]
    if len(manifests) != 1:
        raise FileNotFoundError(
            f"expected one compiled build manifest{f' {structure}' if structure else ''} under "
            f"{compiled / 'canonical' / 'structures'}, found {len(manifests)}; a build compiling several "
            f"names the one it was compiled from")
    return manifests[0]


def _run_case(executor: CTExecutor, case: dict[str, Any]) -> CaseResult:
    fqdn = case["fqdn"]
    ct_ir = case["ct_ir"]
    expected = case.get("expected", {})
    assertions = case.get("assertions", {})
    supplied = case.get("recorded") or {}
    used: set[str] = set()
    missing: list[str] = []
    records: list[dict[str, Any]] = []

    def recorded(path: str) -> dict[str, Any]:
        if path not in supplied:
            missing.append(path)
            raise CTExecutionError(f"no recorded result supplied for the non-deterministic step at {path}")
        used.add(path)
        return supplied[path]

    try:
        vars_result = executor.execute(ct_ir=ct_ir, inputs=ct_ir.get("inputs", {}),
                                       observer=records.append,
                                       recorded=recorded if supplied else None)
    except CTArtifactNotFound as e:
        # Nothing ran, so nothing was judged: the case fails whatever it expected, never passing a
        # VIOLATION case by accident. A transform whose implementation is absent is refused and named,
        # which is a result; escaping as a crash names nothing and stops the other cases.
        return CaseResult(fqdn, False, f"implementation not present: {e}")
    except CTExecutionError as e:
        # A refusal the case expected passes — unless it was the runner refusing a missing record,
        # which is a defect in the case, not the transform's judgement.
        if case["expected_outcome"] == "VIOLATION" and not missing:
            return CaseResult(fqdn, True)
        return CaseResult(fqdn, False, str(e))

    if case["expected_outcome"] == "VIOLATION":
        return CaseResult(fqdn, False, "expected the transform to refuse, and it completed")
    if supplied:
        ran = [r["path"] for r in records if r.get("purity") == NONDETERMINISTIC_PURITY and not r.get("replayed")]
        if ran:
            return CaseResult(fqdn, False, f"non-deterministic steps ran instead of being replayed: {ran}")
        unused = sorted(set(supplied) - used)
        if unused:
            return CaseResult(fqdn, False, f"recorded results no step used: {unused}")

    actual = _resolve_outputs(ct_ir, vars_result)
    assertion_error = _assert_structural(actual, assertions) if assertions else None
    if assertion_error:
        return CaseResult(fqdn, False, f"assertion failed: {assertion_error}")
    asserted = set(assertions)
    expected_exact = {k: v for k, v in expected.items() if k not in asserted}
    actual_exact = {k: v for k, v in actual.items() if k in expected_exact}
    if actual_exact != expected_exact:
        return CaseResult(fqdn, False, f"output mismatch — expected {json.dumps(expected_exact)}, "
                                       f"got {json.dumps(actual_exact)}")
    return CaseResult(fqdn, True)


def _carried(compiled: Path, domain: str) -> set[str]:
    """The capabilities this build carried in, as its attestation records them. Absent means none."""
    attestation = compiled / "trust" / domain / "structure_attestation.json"
    if not attestation.is_file():
        raise FileNotFoundError(f"no attestation at {attestation}; conformance runs after a compile")
    return set(json.loads(attestation.read_text()).get("imported_capabilities", []))


def run_domain(domain_root: Path, snapshot_root: Path | None = None,
               structure: str | None = None) -> DomainResult:
    """Run every case of one compiled build, and say what it proved about each transform it supplies.

    `snapshot_root` is where the build wrote, when not `<domain_root>/snapshot` — a placement build of
    the platform writes to a root of its own. `structure` names the build manifest the build was
    compiled from, when it compiled more than one.
    """
    snapshot = snapshot_root or domain_root / "snapshot"
    compiled = snapshot / "compiled"
    if not compiled.is_dir():
        raise FileNotFoundError(f"no compiled build at {compiled}; conformance runs after a compile")
    manifest = _build_manifest(compiled, structure)
    domain = manifest.get("frontmatter", {}).get("structure_scope") or manifest.get("namespace", "")
    result = DomainResult(domain=domain)

    transforms = [a for a in _canonical(compiled, "capability_transforms") if a.get("artifact_type") == "CT"]
    carried = _carried(compiled, domain)
    own = sorted(a["fqdn_id"] for a in transforms if a["fqdn_id"] not in carried)
    result.carried = sorted(a["fqdn_id"] for a in transforms if a["fqdn_id"] in carried)

    vectors = [a for a in _canonical(compiled, "test_data")]
    targets = {a.get("frontmatter", {}).get("target") for a in vectors}

    declared = (manifest.get("frontmatter", {}).get("output_configuration", {}) or {}).get("conformance")
    cases: list[dict[str, Any]] = []
    if declared:
        case_dir = snapshot / declared["subpath"]
        cases = [json.loads(p.read_text()) for p in sorted(case_dir.glob("*.json"))]
        cases = [c for c in cases if c.get("artifact_type") == "CT_CONFORMANCE"]
    elif vectors:
        raise FileNotFoundError(f"{domain} declares vectors and no place for their cases")

    executor = CTExecutor()
    by_target: dict[str, list[CaseResult]] = {}
    for case in cases:
        outcome = _run_case(executor, case)
        result.cases.append(outcome)
        by_target.setdefault(case["ct_fqdn"], []).append(outcome)

    for fqdn in own:
        runs = by_target.get(fqdn, [])
        if fqdn in targets and not runs:
            # A vector nothing was compiled from is a build out of step with itself, not an absence.
            result.refused.append(fqdn)
            result.cases.append(CaseResult(fqdn, False, "declares a vector and no case of it ran"))
        elif not runs:
            result.unproven.append(fqdn)
        elif all(r.passed for r in runs):
            result.proven.append(fqdn)
        else:
            result.refused.append(fqdn)
    return result


def write_result(domain_root: Path, result: DomainResult, snapshot_root: Path | None = None) -> Path:
    """Write the result beside the build's compiled projections, where the assembler carries it."""
    out_dir = (snapshot_root or domain_root / "snapshot") / "compiled" / RESULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / RESULT_FILE
    out.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
