# protocol_runtime

**Deterministic execution engine for Protocol-Governed Computing** (import package: `runtime`).

The runtime traverses a precompiled execution graph and produces traceable, governed outcomes. It
does not discover behavior, interpret intent, or contain business logic. Everything it will do was
decided at compile time; execution is a traversal of what the snapshot already says.

## Where it fits

```
software_governance    the normative surface every composition rests on
conformance_workloads  workloads that prove conformance
business_domains       domains built on the surface

protocol_compiler      source      → compiled projections
snapshot_assembler     projections → assembled snapshot
protocol_runtime       snapshot    → execution            (this repo)
snapshot_inspector     snapshot    → inspection
```

`protocol_transport` governs the boundary at either end of execution — ingress and egress as
first-class contracts. The runtime consumes only the **assembled** snapshot, never an individual
repo's compiled layout.

## What it is, and is not

**It is** a deterministic graph traverser, a trace generator, and a host for the capability
implementations a snapshot names.

**It is not** a workflow authoring system, a rules engine, a business-logic container, or a
framework with pluggable behavior. There is no extension point, because an extension point is a
place where ungoverned behavior could enter.

## Inputs and outputs

```
snapshot root   the assembled snapshot — the sole source of behavior
payload         external input (JSON)
data-root       the state storage boundary; one data root is one instance
```

A run writes an append-only trace alongside the state its declared side effects produce:

```
traces/<TRACE_ID>/
    <TRACE_ID>.jsonl    append-only execution log
    <TRACE_ID>.md       human-readable summary
    <TRACE_ID>.png      the execution path, rendered

data/
    registry/           idempotent state
    events/             append-only history
```

## Running

```bash
./run.sh                                       # warm-boot the sibling assembled snapshot
./run.sh boot --snapshot /abs/snapshot         # explicit boot
./run.sh run --wf <domain>::WF_… --data-root /abs/instance
./run.sh examine /abs/trace.jsonl
```

`run.sh` wraps the CLI, also installed as the `protocol_runtime` console script, with four
subcommands:

| command | what it does |
|---|---|
| `run` | execute a workflow against a data root |
| `boot` | warm-boot the assembled snapshot — load and hash-verify every manifest domain |
| `examine` | analyze a completed trace file |
| `behavior-logic` | render the execution path from a completed trace as a PNG |

`PGC_SNAPSHOT_ROOT` overrides the snapshot location; `PGC_IMPL_ROOTS` is the colon-separated set of
roots on `PYTHONPATH` for domain capability implementations.

**Warm reboot is its own proof.** Bringing every manifest domain resident and hash-verified
establishes that the snapshot is intact and executable before any workflow runs. A surface-only
snapshot has no workflow to traverse, and warm reboot is exactly what proves it sound anyway.

**A data root is an instance, not an interface.** Two data roots against the same snapshot are two
independent instances of the same governed behavior.

## How execution works

The runtime loads the compiled graph, admits the request against the intent that declares it, and
walks the workflow node by node. At each node it executes the capability contract's steps —
invoking transforms, applying side effects — and routes on the declared outcome. It resolves
nothing by name at execution time: the compiler assigned integer addresses, and traversal operates
on those.

Every step emits evidence. The trace is not a log the runtime chose to write; it is the record of
the path actually taken through a graph that was fixed before the run began, which is what makes a
run reproducible and reviewable after the fact.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
