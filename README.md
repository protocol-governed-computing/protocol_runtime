# protocol_runtime

**Deterministic execution engine for Protocol-Governed Computing** (import package: `runtime`).

The runtime traverses a precompiled execution graph and produces traceable, governed outcomes. It
does not discover behavior, interpret intent, or contain business logic. Everything it will do was
decided at compile time; execution is a traversal of what the snapshot already says.

## Install

```bash
pip install pgc-runtime
```

Once installed:

```bash
protocol_runtime --help
```

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

A run writes an append-only trace beside the state its declared side effects produce, both under
the data root:

```
<data-root>/traces/<domain>/<WF>/<TRACE_ID>/
    <TRACE_ID>.jsonl    append-only execution log, one SCHEMA_TRACE_EVENT_V1 event per line
    <TRACE_ID>.png      the path the run took, drawn only on request (`run --behavior-logic`,
                        or `behavior-logic <trace>`)

<data-root>/<domain>/<subdomain>/
    the stores the domain's runtime binding declares
```

## Running

```bash
./run.sh                                       # warm-boot the sibling assembled snapshot
./run.sh boot --snapshot /abs/snapshot         # explicit boot
./run.sh run --wf <domain>::WF_… --data-root /abs/instance
./run.sh examine /abs/trace.jsonl
```

`run.sh` wraps the CLI, also installed as the `protocol_runtime` console script:

| command | what it does |
|---|---|
| `run` | execute a workflow against a data root |
| `replay` | re-execute a run from its recorded outcomes and compare it with the original |
| `conformance` | run a compiled domain's test vectors; report each transform proven, unproven or refused |
| `boot` | warm-boot the assembled snapshot — load and hash-verify every manifest domain |
| `coordinator` | serve as the coordinating node of a federated node group |
| `worker` | serve as a worker node of a federated node group |
| `examine` | read a completed trace: its path by node, contracts, results, events and errors |
| `behavior-logic` | render the path a completed trace took as a PNG |

`PGC_SNAPSHOT_ROOT` overrides the snapshot location; `PGC_IMPL_ROOTS` is the colon-separated set of
roots on `PYTHONPATH` for domain capability implementations.

**Warm reboot is its own proof.** Bringing every manifest domain resident and hash-verified
establishes that the snapshot is intact and executable before any workflow runs. A surface-only
snapshot has no workflow to traverse, and warm reboot is exactly what proves it sound anyway.

**A process verifies a snapshot once.** `runtime.api` keeps a booted snapshot resident and runs
against it as often as it is asked. A rewritten manifest, or a different trust anchor or profile
root, is verified afresh, and a snapshot that no longer verifies is refused. `boot` itself always
performs the full determination. A governed run then costs a few milliseconds, not a third of a
second.

**A data root is an instance, not an interface.** Two data roots against the same snapshot are two
independent instances of the same governed behavior.

## How execution works

The runtime loads the compiled graph, admits the request against the intent that declares it, and
walks the workflow node by node. At each node it executes the capability contract's steps —
invoking transforms, applying side effects — and routes on the declared outcome. It resolves
nothing by name at execution time: the compiler assigned integer addresses, and traversal operates
on those.

**A workflow may run one contract at several places.** Each place is a node key, and the contract
it runs is named separately. The compiler seals each place's routing on its own, and refuses a sealed
dispatch that does not realize every transition declared at that node. Routing is by node, and the
trace names the place that ran.

**Not every step is determined by its inputs.** A transform is a deterministic atom, a
non-deterministic atom, or a molecule, and a different constitution governs each. A molecule has no
implementation: the runtime runs its declared steps. A non-deterministic atom's result is recorded
where it is produced, and `replay` substitutes the recorded result, so a run is reproducible even
when a step is not.

**One snapshot can run across nodes.** Under a federated placement, a coordinator schedules units of
work to workers over a shared store. Under the signed federated profile, every node verifies the
snapshot's signature before it runs anything.

Every step emits evidence. The trace is not a log the runtime chose to write; it is the record of
the path actually taken through a graph that was fixed before the run began, which is what makes a
run reproducible and reviewable after the fact.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.

---

## The package family

| Package | Repository | Role |
|---|---|---|
| `pgc-compiler` | `protocol_compiler` | declarations → compiled projections |
| `pgc-assembler` | `snapshot_assembler` | projections → sealed snapshot |
| `pgc-runtime` | `protocol_runtime` | snapshot → governed execution |
| `pgc-inspector` | `snapshot_inspector` | snapshot → read-only inspection |
| `pgc-transformation` | `transformation` | change request → protocol artifacts |
| `pgc-governance` | `software_governance` | the governance surface and its capability implementations |
| `pgc-workloads` | `conformance_workloads` | the workloads that make conformance observable |
| `pgc-domains` | `business_domains` | the business domain implementations the composed snapshot binds |

`pip install protocol-governed-computing` brings in the whole family.

**Installing the toolchain is one of two steps.** The compiler resolves the governance surface from
`PGC_PLATFORM_ROOT` — fail-hard, cwd-independent, zero inference — so the *declarations* come from a
repository you point at, never from a wheel. A registry inside a package would be a second governance
surface competing with the repository's, and a build could then be governed by a stale copy.

```bash
git clone https://github.com/protocol-governed-computing/software_governance
export PGC_PLATFORM_ROOT=$PWD/software_governance
pgc            # reports what is installed and whether the anchor resolves
```

`PGC_DOMAIN_ROOTS` names an additional domain contributing its own `registry/structures` — the
directory that *directly contains* it, not the repository above it; pointing one level too high is a
silent no-op. `PGC_SNAPSHOT_PROFILES` is the directory holding snapshot profiles, required by the
assembler and the runtime alike.

**Where a build writes is declared, not supplied.** Each build configuration names its root in
`output_configuration.root`, and every layer's output consolidates there. The compiler does not read
`PGC_SNAPSHOT_ROOT`; the runtime does, with its own meaning — the assembled snapshot to execute.

`PGC_BUILD_ROOT` is accepted and reported and **nothing reads it**.

The full sequence, with the repositories it needs, is in
[`pgc_install`](https://github.com/protocol-governed-computing/pgc_install).

**Versioning.** Two schemes, and the published version follows the second.

- **Internal** — each repository's `VERSION` file, a monotonic composition ordinal. PGC versions the
  composition rather than each repo: they release together and the governance closure forces lockstep,
  so the ordinal names which composition a repo belongs to. Development happens on `dev/<N>` and each
  cycle is tagged `release-<N>`. This is not published.
- **Public** — `PUBLIC_VERSION`, tagged on every component repository. The platform is at **`v3`**.

**The published version is the public one: `v4` is `4.0.0`.** The standard the packages implement is a
separate artifact on its own track and is not this number.

The standard these packages implement is published separately: https://doi.org/10.5281/zenodo.22150616
