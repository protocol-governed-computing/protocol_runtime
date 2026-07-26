"""
verify_release.py — Pre-release gate for protocol_runtime (PGC v1.0.0).

Verifies the runtime package is releasable:
  - static:    clean import, no forbidden dependencies (pgs_*, omnibachi, structure),
               no relative imports / sys.path hacks / .md loading / fallback markers,
               importlib confined to the approved handler_ref carve-out
  - packaging: pyproject present
  - execution: executes a reference workload against the assembled PGC snapshot,
               deterministically, producing a schema-versioned trace

Fixture: the sibling assembled snapshot (../snapshot) + the Collatz reference workload
(workload::WF_COLLATZ_CONJECTURE_V0). The runtime *consumes* an assembled snapshot; it does
not build one — so this gate uses the workspace's assembled snapshot as its input, and the
platform repo as the impl root for handler_ref execution.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = RUNTIME_ROOT / "runtime"
WORKSPACE = RUNTIME_ROOT.parent

SNAPSHOT_ROOT = WORKSPACE / "snapshot"      # assembled PGC snapshot (runtime input, read-only)
IMPL_ROOTS = WORKSPACE / "platform"         # domain CT/CS impls loaded by handler_ref at execution

_WF = "workload::WF_COLLATZ_CONJECTURE_V0"
_PAYLOAD = {"numbers": [27]}

_IMPORTLIB_APPROVED_FILES = {
    "runtime_loader.py",
    "ct_executor.py",
    "execute_ct.py",
    "loader.py",
    "dispatcher.py",  # CS handler instantiation via handler_ref["module"]
}

_PASS = []
_FAIL = []

# ── Helpers ─────────────────────────────────────────────────────────────

def _run(cmd: str, env: dict | None = None) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd}\n{result.stdout}\n{result.stderr}".strip())
    return result.stdout


def _grep(pattern: str, flags: str = ""):
    result = subprocess.run(
        f'grep -rn {flags} --include="*.py" "{pattern}" "{PKG_DIR}"',
        shell=True,
        capture_output=True,
        text=True,
    )
    return [l for l in result.stdout.splitlines() if "__pycache__" not in l]


def _assert_no_hits(pattern: str, flags: str = ""):
    hits = _grep(pattern, flags)
    if hits:
        raise AssertionError("\n".join(hits[:10]))


def _check(name: str, fn):
    try:
        fn()
        _PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        _FAIL.append((name, str(e)))
        print(f"  [FAIL] {name}")
        for line in str(e).splitlines()[:6]:
            print(f"         {line}")


# ── Static Checks ────────────────────────────────────────────────────────

def check_import():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RUNTIME_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    _run(f'{sys.executable} -c "import runtime"', env=env)


def check_no_omnibachi_import():
    _assert_no_hits(r"(from|import) omnibachi", "-E")


def check_no_pgs_imports():
    _assert_no_hits(r"(from|import) pgs_", "-E")


def check_no_structure_import():
    _assert_no_hits(r"(from|import) structure[\. ]", "-E")


def check_no_relative_imports():
    _assert_no_hits(r"^from \.", "-E")


def check_no_sys_path_manipulation():
    _assert_no_hits(r"sys\.path", "-E")


def check_no_md_file_loading():
    _assert_no_hits(r'\.md["\']', "-E")


def check_no_fallback_markers():
    hits = _grep(
        r"#.*(fallback (to|for)|with .* fallback|pass.through|treat as)",
        "-iE",
    )
    if hits:
        raise AssertionError("\n".join(hits[:10]))


def check_importlib_confined():
    hits = _grep(r"importlib\.import_module")
    violations = [h for h in hits if not any(f in h for f in _IMPORTLIB_APPROVED_FILES)]
    if violations:
        raise AssertionError("\n".join(violations[:10]))


def check_pyproject_toml():
    if not (RUNTIME_ROOT / "pyproject.toml").exists():
        raise AssertionError("pyproject.toml not found")


# ── Execution Setup ─────────────────────────────────────────────────────

def _exec_env() -> dict:
    env = os.environ.copy()
    pythonpath = f"{RUNTIME_ROOT}{os.pathsep}{IMPL_ROOTS}"
    env["PYTHONPATH"] = pythonpath + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PGC_SNAPSHOT_ROOT"] = str(SNAPSHOT_ROOT)
    return env


def _require_snapshot():
    if not (SNAPSHOT_ROOT / "manifest.json").exists():
        raise AssertionError(
            f"assembled snapshot not found at {SNAPSHOT_ROOT} — compile + assemble first"
        )


def _status(out: str) -> str | None:
    for line in out.splitlines():
        if line.strip().startswith("Status:"):
            return line.split(":", 1)[1].strip()
    return None


def _run_workflow(data_root: str) -> str:
    payload = Path(data_root) / "payload.json"
    payload.write_text(json.dumps(_PAYLOAD), encoding="utf-8")
    return _run(
        f"{sys.executable} -m runtime.cli run --wf {_WF}"
        f" --payload {payload}"
        f" --data-root {data_root}"
        f" --snapshot {SNAPSHOT_ROOT}",
        env=_exec_env(),
    )


# ── Execution Checks ────────────────────────────────────────────────────

def check_execute_workflow():
    _require_snapshot()
    with tempfile.TemporaryDirectory() as dr:
        out = _run_workflow(dr)
    status = _status(out)
    if status != "SUCCESS":
        raise AssertionError(f"expected SUCCESS, got {status!r}")


def check_determinism():
    _require_snapshot()
    results = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as dr:
            results.append(_status(_run_workflow(dr)))
    if results[0] != results[1] or results[0] != "SUCCESS":
        raise AssertionError(f"non-deterministic protocol result: {results}")


def check_trace_schema():
    _require_snapshot()
    with tempfile.TemporaryDirectory() as dr:
        _run_workflow(dr)
        trace_files = list(Path(dr).rglob("*.jsonl"))
        if not trace_files:
            raise AssertionError("no trace files produced")
        with open(trace_files[0], encoding="utf-8") as f:
            first = json.loads(f.readline())
        if "trace_schema_version" not in first:
            raise AssertionError("trace_schema_version missing")


# ── Main ───────────────────────────────────────────────────────────────

def main():
    print("\n=== PGC protocol_runtime Release Verification (v1.0.0) ===")
    print(f"  runtime root : {RUNTIME_ROOT}")
    print(f"  snapshot     : {SNAPSHOT_ROOT}")

    print("\n-- Static Checks --")
    _check("Package imports cleanly", check_import)
    _check("No omnibachi.* imports", check_no_omnibachi_import)
    _check("No pgs_* imports", check_no_pgs_imports)
    _check("No structure.* imports", check_no_structure_import)
    _check("No relative imports", check_no_relative_imports)
    _check("No sys.path manipulation", check_no_sys_path_manipulation)
    _check("No .md file loading", check_no_md_file_loading)
    _check("No fallback pattern markers", check_no_fallback_markers)
    _check("importlib confined to approved", check_importlib_confined)

    print("\n-- Packaging Checks --")
    _check("pyproject.toml valid", check_pyproject_toml)

    print("\n-- Execution Checks --")
    _check("Workflow executes to SUCCESS", check_execute_workflow)
    _check("Execution is deterministic", check_determinism)
    _check("Trace carries trace_schema_version", check_trace_schema)

    total = len(_PASS) + len(_FAIL)
    print(f"\n{'─' * 42}")
    print(f"  Passed : {len(_PASS)} / {total}")
    print(f"  Failed : {len(_FAIL)} / {total}")

    if _FAIL:
        print("\n  Failed checks:")
        for name, _ in _FAIL:
            print(f"    ✗ {name}")
        sys.exit(1)
    else:
        print("\n  ALL CHECKS PASSED — READY FOR RELEASE\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
