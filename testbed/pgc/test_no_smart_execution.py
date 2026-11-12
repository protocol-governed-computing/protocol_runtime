"""
Execution passes values to transforms and back unchanged (`execution::INVARIANT_NO_SMART_EXECUTION_V0`).

The runtime reads no type declaration as an instruction: it converts no input before a transform
receives it and no output after, and caches no type metadata to decide either. A transform owns its
own canonicalization. The input types a step declares are checked, never used to convert.

The scan covers every runtime module except `conformance.py`, which checks a vector's expected values
against an assertion's form. Reading a hex string there judges a result; it hands nothing to a
transform.

Run:  python testbed/pgc/test_no_smart_execution.py
"""
import re
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
JUDGES_VECTORS = {RUNTIME / "conformance.py"}

PATTERNS = [
    (re.compile(r"load_contract\("), "type metadata loading"),
    (re.compile(r"_load_atom_(?:input|output)_types\("), "type metadata loading"),
    (re.compile(r"""if\s+.*type.*==\s*["']hex_string["']"""), "type-based conditional"),
    (re.compile(r"\.hex\(\)"), "output conversion (.hex())"),
    (re.compile(r"bytes\.fromhex\("), "input conversion (bytes.fromhex)"),
    (re.compile(r"_(?:INPUT|OUTPUT)_TYPES_CACHE"), "type caching"),
]


def scanned() -> list[Path]:
    return sorted(p for p in RUNTIME.rglob("*.py") if p not in JUDGES_VECTORS)


def findings(text: str, name: str) -> list[str]:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern, what in PATTERNS:
            if pattern.search(line):
                out.append(f"{name}:{number} {what}: {line.strip()}")
    return out


def test_the_executors_are_scanned():
    names = {p.name for p in scanned()}
    assert {"dispatcher.py", "ct_executor.py", "scheduler.py"} <= names, sorted(names)


def test_no_runtime_module_converts_by_type():
    found = [f for p in scanned() for f in findings(p.read_text(encoding="utf-8"), p.name)]
    assert not found, "\n".join(found)


def test_a_planted_conversion_is_found():
    planted = 'if input_types[key] == "hex_string":\n    inputs[key] = bytes.fromhex(value[2:])\n'
    assert len(findings(planted, "planted.py")) == 2, findings(planted, "planted.py")


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
