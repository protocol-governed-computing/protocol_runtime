## Response Style

Keep responses under ~400 output tokens. Prefer terse bullet summaries over prose narration. When a full document is needed, Write it to a file instead of printing it in chat.

## Document Authoring Rules

- Never include internal review notes, meta-scaffolding, work-order framing, cross-reference stubs, or 'Status:' metadata in deliverable docs or figures.
- Do not add dates to papers or appendices.
- An 'outline' means a clean reader-facing outline of the paper, not an execution plan.

# Protocol Runtime (PGC) — Architectural Rules

**Repo:** `protocol-governed-computing/protocol_runtime` &nbsp;|&nbsp; **Import package:** `runtime` &nbsp;|&nbsp; **Invoke:** `python -m runtime.cli` (or console script `pgc_runtime`).
It began as a fork of RI-0's `pgs_runtime`, as the compiler did, and imports no `pgs_*` package.

## Core Doctrine

**Runtime executes, Protocol declares.**
- Runtime MUST only execute what snapshot declares
- Zero inference. Zero guessing. Zero fallback.
- If not in snapshot → FAIL HARD

## Critical Architectural Invariants

### Layer Isolation (MANDATORY)

**Runtime MUST NOT import:**
- `compiler.*` (the PGC compiler — layer isolation)
- `pgs_*` (RI-0 packages — the fork is severed)
- `structure.*`
- Protocol `.md` files

**Runtime works from JSON snapshot ONLY:**
- All artifacts loaded from `protocol_snapshot/`
- No dynamic protocol loading
- No ProtocolLoader, LayerResolver, or path_registry
- Fail hard if artifact not found in snapshot

**Dependency direction is strictly:**
```
protocol → snapshot → runtime → domains
```

### Runtime Purity

**Runtime is EXECUTOR:**
- Load snapshot artifacts
- Instantiate capabilities
- Execute workflows
- Emit traces

**Runtime MUST NOT:**
- Parse protocol .md files
- Compile artifacts
- Validate protocol semantics
- Infer missing configuration

**Runtime input MUST be:**
- JSON artifacts from protocol_snapshot/
- Pre-validated by compiler
- Complete and self-contained

## Compile-Time Binding (MANDATORY)

**CS and CT execution binding is compile-time sealed — no runtime registries.**

### CS (Capability Side Effects)

CS runtime instantiation is driven by `cs_ir.handler_ref` in compiled CS artifacts:
- `handler_ref.module` + `handler_ref.callable` → `importlib.import_module` → `cls(config, metadata, capability_code)`
- `RuntimeLoader` reads the compiled CS artifact from snapshot, not a static registry
- Adding a new CS = declare `machine.implementation` in CS source → compiler emits `cs_ir`

### CT (Capability Transforms)

CT execution is driven by `handler_ref` in CT-IR atom_stream steps:
- `handler_ref.module` + `handler_ref.callable` → `importlib.import_module` → `execute_fn(inputs=...)`
- `ct_executor.py` reads handler_ref from the compiled CT-IR step
- Adding a new CT atom = declare `machine.implementation` in CT source → compiler emits handler_ref

**Missing implementation MUST cause:**
- Build failure at compile time (validate phase)
- Clear error message identifying the artifact
- No fallback resolution

## Import Rules (MANDATORY)

All imports MUST be absolute and fully-qualified from `runtime.*`:

```python
# ✅ CORRECT
from runtime.loader import load_domain
from runtime.dispatcher import execute_cc
from runtime.scheduler import run_wf

# ❌ FORBIDDEN
from loader import load_domain              # Missing runtime root
from .loader import load_domain             # Relative import
from structure.loading import *             # structure.* import
from compiler.loader import something       # compiler.* import (layer isolation)
```

**Rules:**
- All imports MUST be absolute
- All imports MUST start with `runtime.*`
- No relative imports (`.` or `..`) EVER
- No `structure.*` imports EVER
- No `compiler.*` or `pgs_*` imports EVER
- No short-name imports

## Snapshot Loading Rules

**The runtime consumes the ASSEMBLED snapshot (the assembler product) via its manifest — never a
repo's private compiled layout, never a PGS workspace.** Contract:
`standards/doc/SNAPSHOT_ASSEMBLY_CONTRACT.md`. The `manifest.json` is the **root of trust**.

**Assembled snapshot root layout** (`PGC_SNAPSHOT_ROOT`, default sibling `../snapshot`):
- `manifest.json` — domains, per-domain hashes, content-derived `composite_hash` (identity)
- `tokenized/<domain>/dispatch.json`, `handlers.json`, `metadata.json`
- `trust/<domain>/structure_attestation.json` (hash verification)
- `vocabulary/<domain>/forward.json`, `reverse.json`
- Runtime does NOT parse individual WF/CC/CT/CS JSON artifacts directly

**Warm reboot = load + hash-verify all manifest domains** (there may be no `WF_` to traverse — a
surface-only snapshot like `platform` still boots and verifies).

**Loading Pattern:**
```python
# ✅ CORRECT - warm-boot the assembled snapshot through the manifest (root of trust)
from runtime.boot import boot
booted = boot(snapshot_root)              # verifies composite_hash; anchors each domain
pkg = booted.domains["platform"]          # RuntimePackage — frozen, hash-verified

# ✅ CORRECT - load a single domain, anchored to the manifest's tokenized hash
from runtime.loader import load_domain
pkg = load_domain(snapshot_root, domain, expected_tokenized_hash=<from manifest>)

# ❌ FORBIDDEN - Dynamic protocol loading
from structure.loading.protocol_loader import resolve_artifact
artifact = resolve_artifact(code, search_roots)  # NO!
```

**Runtime output ≠ snapshot.** Traces + CS state (`data/`) go to a separate **instance root**
(`--data-root` / `PGC_DATA_ROOT`); the assembled snapshot is read-only input. Never write results
into the snapshot.

## CS Runtime Implementation Rules

**CS runtimes MUST:**
- Inherit common interface (capability_code, execute method)
- Accept policy dict in __init__
- Implement execute(op, payload) → result dict
- Return result_status in response
- Be stateless (policy-driven configuration only)

**CS runtime template:**
```python
class MyCustomRuntime:
    capability_kind = "CS"

    def __init__(self, policy: dict, capability_code: str):
        self._policy = policy
        self._capability_code = capability_code
        # Parse policy configuration

    @property
    def capability_code(self) -> str:
        return self._capability_code

    @property
    def supported_operation_specs(self) -> set[str]:
        return {"CREATE", "READ", "UPDATE"}

    def execute(self, *, op: str, payload: dict) -> dict:
        if op == "CREATE":
            # Implementation
            return {"result_status": "SUCCESS", "value": result}
        # ...
```

**CS runtimes MUST NOT:**
- Import from structure.*
- Import from compiler.* or pgs_*
- Use importlib or dynamic imports
- Have hidden side effects
- Mutate global state

## CT Implementation Rules

**CT implementations MUST:**
- Be functions of their inputs (inputs → outputs)
- Accept inputs dict parameter
- Return dict with outputs
- Be deterministic, unless the CT declares `ct_purity: ct_impure`
- Have no side effects, whatever their purity

A `ct_impure` atom's every result is recorded as a `CT_STEP` event with its values, and
`runtime replay` substitutes the recorded result rather than calling the atom. Determinism holds
relative to recorded outcomes (`capability_transforms::CONSTITUTION_NONDETERMINISTIC_ATOMS_V0`).

**CT implementation template:**
```python
class CTExecutionError(RuntimeError):
    """The atom's refusal, recognised by this class name (`refusal: raises`)."""


def execute(inputs: dict, context=None) -> dict:
    """Pure transform implementation."""
    # Refuse by raising CTExecutionError; any other exception is a fault that ends the run
    required = ["field1", "field2"]
    for field in required:
        if field not in inputs:
            raise CTExecutionError(f"Missing required input: {field}")

    # Pure computation
    result = compute_pure_function(inputs["field1"], inputs["field2"])

    # Return outputs dict
    return {"value": result}
```

**CT implementations MUST NOT:**
- Perform I/O operations
- Access filesystem
- Make network calls
- Use random without seed, unless the CT declares `ct_impure`
- Mutate global state
- Import from structure.*

## Execution Rules

**Runtime is executor ONLY:**
- Load artifacts from snapshot
- Instantiate capabilities from static registries
- Execute workflow DAG
- Emit execution trace
- Return execution result

**Runtime MUST NOT:**
- Decide what to execute (DAG declares)
- Infer missing configuration (fail hard)
- Interpret protocol intent (execute literally)
- Add logging/telemetry not declared (trace only)
- Override execution policy (policy-driven)

## Error Handling Rules

**Fail fast on:**
- Missing artifact in snapshot
- Capability not in static registry
- Invalid artifact structure
- Contract violation
- Missing required input

**Never:**
- Silent success
- Warning-based continuation
- Retry logic (unless declared)
- Fallback paths
- Guess missing values

## KISS Design Principles

**Single canonical path:**
- One way to load artifacts (snapshot)
- One way to register capabilities (static registry)
- One way to execute workflows (DAG traversal)
- No fallback logic
- No "try this, if not try that"

**No backward compatibility:**
- Use current canonical format ONLY
- No version detection
- No migration shims
- No deprecated code paths

**Fail fast:**
- Invalid input → immediate error
- Missing config → immediate error
- Unknown capability → immediate error
- No silent degradation

## Forbidden Patterns

**NEVER use:**
- `importlib.import_module()` — except in `dispatcher.py` for CS handler instantiation,
  which is compile-time sealed (handler_ref comes from the snapshot, not discovered at runtime)
- `__import__()`
- `getattr()` for module loading outside the dispatcher's sealed handler_ref pattern
- `sys.path` manipulation
- Dynamic imports of any kind outside the sealed CS/CT handler_ref carve-out
- `structure.*` imports
- `compiler.*` or `pgs_*` imports
- Relative imports
- Filesystem-based discovery
- Environment-driven branching (except SMTP config)
- `try/except` for control flow
- Fallback logic
- Default values not in snapshot
- Magic constants

**Deployment configuration is not branching.** The sealed snapshot decides
behaviour: whether execution is federated, how a unit is admitted, executed and evidenced. Where a
coordinator listens, the address a node reaches it at, and how long a node waits to *connect* are
facts about a deployment, which a snapshot sealed once and run on many deployments cannot know. They
may come from the environment or the CLI. `runtime.api` reading `PGC_COORDINATOR_URL` under a sealed
`FEDERATED_NODE` placement is conforming: the placement decides, and the environment supplies only
the address. The only defaults the runtime carries are in `runtime/federation/defaults.py`, each
documented and overridable. A new one goes there or nowhere, and anything that would change what a
run determines belongs in the snapshot instead.

## Testing Requirements

**Runtime tests MUST:**
- Use snapshot artifacts as input
- Test against static registries
- Verify execution determinism
- Test failure modes explicitly
- Mock external I/O (SMTP, etc.)

**Test categories:**
1. Snapshot loading tests
2. CT conformance tests (execute CT-IR)
3. CS binding tests (instantiate from policy)
4. Workflow execution tests (end-to-end)
5. Failure mode tests (missing artifacts, invalid config)
6. Determinism tests (same input → same output)

**Tests MUST NOT:**
- Use compiler APIs
- Parse protocol .md files
- Use structure.* modules
- Depend on compiler or pgs_* code
- Require filesystem discovery

## File Organization
**For latest file org see the current repo**

```
protocol_runtime/         # Repo root
├── runtime/              # Import package (python -m runtime.cli)
│   ├── cli.py            # runtime CLI (run, examine, behavior-logic subcommands)
│   ├── loader.py         # Tokenized snapshot loader (load_domain)
│   ├── dispatcher.py     # CC-level pipeline executor (execute_cc)
│   ├── scheduler.py      # WF-level topology traversal (run_wf)
│   ├── memory.py         # Execution context (payload + CC results)
│   ├── ct_executor.py    # CT-IR evaluation engine
│   ├── ct_execute.py     # CT adapter (dispatcher ↔ executor)
│   ├── ct_errors.py      # StructuredError base class
│   ├── conformance.py    # CT conformance test runner
│   ├── evidence.py       # TraceWriter (append-only JSONL emitter)
│   ├── trace_viz.py      # Evidence projection (execution-path PNG)
│   ├── server.py         # HTTP gateway (POST /api/run)
│   └── examine/          # What a completed run did, read from its trace
│       ├── __init__.py   # analyze(trace_path) -> DiagnosticReport
│       ├── cli.py
│       ├── parser.py     # SCHEMA_TRACE_EVENT_V0 reader; refuses any other format
│       └── reporter.py
├── scripts/
└── testbed/
```
## Workflow Rules (MANDATORY)

**Diagnose before fix — ALWAYS:**
1. State the root cause you've identified
2. List which files need to change and why
3. Identify any architectural layer boundaries being crossed
4. **Wait for approval before implementing anything**

**Multi-file changes — plan first:**
- Present the complete list of files to be changed before starting any edits
- No scattered incremental edits — plan the full pass, then execute it
- One coherent change set, not piecemeal patches

**Do NOT jump to reading files or writing code before fully understanding the request.**

## Environment Rules

- Do NOT run `pip install` — all dependencies are pre-installed
- Do NOT run `pip`, `pip3`, or any package setup commands
- Do NOT manipulate virtual environments
- Dependencies are managed externally; assume they are available

## Agent Enforcement Rule

**Before writing ANY code:**
- If artifact not in snapshot → DO NOT INFER, raise error
- If capability not in registry → DO NOT DISCOVER, raise error
- If import from structure.* → DO NOT PROCEED, flag violation

## Final Invariant

Runtime MUST be:
- Deterministic (same snapshot + payload + recorded outcomes → same result)
- Snapshot-driven (no protocol .md parsing)
- Registry-bound (no dynamic capability discovery)
- Side-effect-explicit (all mutations via declared CS)

**Any deviation → architectural violation.**

## One-Line Summary

Absolute `runtime.*` imports, snapshot-only loading, static registries, zero dynamic behavior—runtime executes what snapshot declares, nothing more.
