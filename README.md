# protocol_runtime

**Deterministic execution engine for Protocol-Governed Computing (import package: `runtime`).**

The runtime traverses a precompiled execution graph and produces traceable, governed outcomes.  
It does not discover behavior. It does not interpret intent. It does not contain business logic.

Behavior is declared in protocol, executed by runtime, implemented in capabilities, and observed via traces and state.

> **New to PGS?** This is one of the repositories in the Protocol-Governed Systems ecosystem.
> For orientation, architecture overview, and end-to-end execution, start at [pgs_workspace](https://github.com/bachipeachy/pgs_workspace).

---

## What this component is (and is not)

**This is:**
- A generic execution substrate
- A graph traversal engine
- A trace generator
- A host for capability implementations (CT/CS bindings)

**This is not:**
- A workflow authoring system
- A rules engine
- A business logic container
- A framework with pluggable behavior

All behavior is declared and compiled before execution. The runtime only consumes a sealed snapshot.

---

## Inputs → Outputs

**Inputs:**
```
protocol_snapshot/   ← compiled artifacts (from pgs_compiler)
payload              ← external input (JSON)
data_root            ← state storage boundary
workspace            ← execution context
```

**Outputs:**
```
traces/<TRACE_ID>/
├── <TRACE_ID>.jsonl   ← append-only execution log
├── <TRACE_ID>.md      ← human-readable summary
└── <TRACE_ID>.png     ← visual execution path

data/
├── registry/          ← idempotent state
├── events/            ← append-only history
└── ...                ← additional declared side-effects
```

---

## CLI surface

```bash
protocol_runtime run \
  --wf blockchain::WF_REGISTER_ACTOR_UNVERIFIED_V0 \
  --payload payload.json \
  --data-root /abs/path/to/pgs_workspace/data \
  --workspace /abs/path/to/pgs_workspace

protocol_runtime examine ./traces/<TRACE_ID>/<TRACE_ID>.jsonl

# module form (no install): PYTHONPATH=<protocol_runtime> python -m runtime.cli run ...
```

That is the entire public interface. Everything else is governed by the protocol snapshot.

---

## How execution works

You run a workflow. The runtime:

1. Loads the compiled DAG from `protocol_snapshot/`
2. Resolves inputs via JSONPath expressions
3. Executes CT/CS nodes (transforms and side-effects)
4. Routes based on declared outcomes
5. Emits trace + state

No logic exists in the runtime for any specific workflow.

**Example:** `protocol_runtime run --wf blockchain::WF_REGISTER_ACTOR_UNVERIFIED_V0 ...`

The runtime has no knowledge of blockchain. It only traverses the precomputed graph compiled from that workflow's protocol declaration.

---

## What makes this runtime different

**1. Compile-time closure**

All routing, bindings, and side-effect semantics are resolved before execution begins:
- no reflection
- no dynamic imports
- no fallback logic

Execution is a pure traversal of a sealed graph.  
No runtime decision can alter the graph.

**2. Outcome-driven control flow**

Every node produces an explicit outcome:
```
SUCCESS
ALREADY_EXISTS
VIOLATION
...
```

These outcomes map to named edges in the graph. There is no implicit branching, no hidden control flow, no exception-driven logic.

**3. Side-effects are governed, not coded**

Side-effects (CS) declare their own semantics:

| Behavior | Example |
|----------|---------|
| Idempotent | registry insert → `ALREADY_EXISTS` on repeat |
| Append-only | event stream → always records |
| Mutable | state overwrite (explicitly declared) |

The runtime executes these semantics as declared. It does not interpret them.

**4. Execution ≠ effect**

A workflow can succeed even when a side-effect returns `ALREADY_EXISTS`. Execution is structural. Effects are governed independently. This separation is visible in traces and state.

**5. Trace is first-class output**

Every run produces a complete causal execution record keyed by `trace_id`. This enables:
- deterministic replay
- auditability
- debugging without code instrumentation

---

## Where this fits in the system

PGS is intentionally split into layers:

| Layer | Repo | Responsibility |
|-------|------|----------------|
| Governance | `pgs_governance` | Invariants, constitutional rules, structural definitions |
| Compilation | `pgs_compiler` | Produce `protocol_snapshot/` |
| Transport | `pgs_transport` | Ingress/egress adapters for HTTP and CLI |
| **Execution** | **`protocol_runtime` (pkg `runtime`) ← here** | **Traverse compiled graph deterministically** |
| Capabilities | `pgs_capabilities` | Provide CT/CS implementations |
| Domains | `pgs_blockchain`, `pgs_ai_governance` | Real-world workflows |
| Change Mgmt | `pgs_change_mgmt` | Governed SDLC — Change Request to Authoring Mandate (new in v0.5.0) |
| Entry point | `pgs_workspace` | Run and observe |

---

## What you should explore next

| Go here | To... |
|---------|-------|
| `pgs_workspace` | Run end-to-end and observe traces |
| `protocol_snapshot/` | Inspect what the runtime actually reads |
| `pgs_governance` + `pgs_compiler` | Author and compile new workflows |
| `pgs_capabilities` | Add new capability implementations |

---

## Research context

This implementation demonstrates:

> *"Extensibility by declaration, not refactor."*

And more broadly: governance-driven execution, compile-time closure of behavior, separation of execution from semantics.

---

## Final note

The runtime does not decide what to do.  
It only executes what has already been decided.

If you find yourself wanting to add logic to this runtime — stop.  
That logic belongs in the protocol (declaration) or in capability implementations (CT/CS).  
The runtime should remain boringly deterministic.

---

## License

Apache-2.0. See LICENSE and NOTICE for details.
