# Architecture — `protocol_runtime`

This document describes what this repository is, what it owns, and what it must never do. It is
written to be read before any code, and assumes no prior familiarity with Protocol-Governed
Computing.

For the big picture — what PGC is and how the repositories compose — see
**https://github.com/protocol-governed-computing**.

---

## 1. What this repo is

This is the **runtime**. It reads a sealed snapshot and executes the workflows in it.

The unusual thing about it is what it does *not* contain:

> The runtime knows nothing about any business. It contains no rule, no policy, no branch that
> depends on what a workflow means. **It decides nothing.**

It is closer to a train than to a driver. The track was laid at compile time; the runtime travels
it. Where the track goes is not the runtime's business, and there is no manoeuvre by which it could
leave the track — because there is no track to leave it onto.

**What this repo is not.** It is not an application server, an orchestration engine that interprets
rules, or a framework a domain plugs behaviour into. Behaviour is not extended by writing runtime
code. It is extended by declaring more protocol, compiling it, and sealing a new snapshot.

## 2. Where it sits

```
   sealed snapshot  (read-only, never modified)
          │
          │  warm reboot: load once, verify against the manifest
          ▼
   ┌──────────────────────────────────────────────┐
   │  protocol_runtime      ← YOU ARE HERE        │
   │                                              │
   │   scheduler   walks the workflow             │
   │   dispatcher  runs one contract's steps      │
   │   loader      resolves what to run           │
   │   evidence    records what happened          │
   └───────────────┬──────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   governed state         trace / evidence
   (through declared      (an immutable record
    capabilities only)     of the traversal)
```

Callers reach the runtime either directly, or across the governed boundary in `protocol_transport`.
Either way they invoke a workflow the snapshot already contains; **neither can introduce one**.

## 3. The central idea: traversal, not interpretation

The distinction that explains every design choice in this repository:

```
   AN INTERPRETER                       THIS RUNTIME

   reads instructions                   reads a graph that was already
   and decides what they mean           built and already checked
        │                                    │
        │  meaning lives here                │  meaning lived at compile time
        ▼                                    ▼
   behaviour is produced                behaviour is traversed
   at run time                          — every path pre-existing
```

The scheduler asks one question at each step: *the last step produced this outcome; where does the
graph say that outcome goes?* It looks the answer up. It does not compute it, infer it, or fall back
when the answer is missing — a missing answer is a failure, because the alternative is a runtime that
invents a path nobody governed.

**This is what "the runtime is simple" means.** Not that it is small, but that its simplicity is
*load-bearing*: every judgement it declines to make is a judgement that was made, checked and sealed
earlier, where it could be reviewed.

## 4. What it owns, and what it must never do

**It owns:**

- **warm reboot** — loading a snapshot once and verifying it against its manifest before anything runs;
- **traversal** — walking a workflow's nodes, following declared routing on declared outcomes;
- **invocation** — calling the implementations the snapshot binds, through the declared capabilities;
- **evidence** — emitting a structured trace of the traversal as it happens.

**It must never:**

- **decide anything about a domain.** No branch in this repository may depend on what a workflow
  means. The test is mechanical: search the runtime for any business noun and find nothing.
- **add a path.** If the graph does not contain a route, execution ends. It is never synthesised.
- **write outside a declared capability.** Every mutation goes through a capability the snapshot
  declares, or it does not happen.
- **reload or mutate the snapshot.** It is read once and treated as immutable for the life of the
  process.

## 5. How one execution proceeds

```
   invoke workflow ─────────────────────────────────────┐
                                                        │
   ┌────────────────────────────────────────────────────▼─────┐
   │  boot        snapshot resident, verified against manifest │
   ├──────────────────────────────────────────────────────────┤
   │  scheduler   at the entry node                            │
   │      │                                                    │
   │      ├─▶ dispatcher   run this contract's steps in order  │
   │      │       │            each step: a transform, or a    │
   │      │       │            side effect through a capability│
   │      │       ▼                                            │
   │      │   step outcome                                     │
   │      │                                                    │
   │      ◀── look up where that outcome routes                │
   │      │        found → continue at the next node           │
   │      │        none  → the traversal ends                  │
   └──────┼──────────────────────────────────────────────────  ┘
          ▼
   result + trace
```

Every arrow above was constructed at compile time. The runtime supplies the walking, never the map.

## 6. Evidence

Execution produces a **trace**: an ordered record of the traversal, written as it happens rather than
reconstructed afterwards.

| event | meaning |
|---|---|
| `WF_START` / `WF_COMPLETE` | a workflow traversal began / ended |
| `CC_START` / `CC_COMPLETE` | a capability contract began / ended |
| `CC_STEP` | one step within a contract ran |
| `EVENT` | a declared domain moment was announced |

The trace exists to make a claim checkable. *"This ran and conformed"* is an assertion; the trace is
what turns it into something a second party can verify without trusting the first. It records the
path actually taken — which, because paths are pre-existing, can be compared against the compiled
graph.

## 7. Layout

```
run.sh              run a workflow from the command line

runtime/
    api.py          the entry point: run a workflow, get a result and a trace
    cli.py          the `protocol_runtime` console script — a client of the API
    boot.py         warm reboot — snapshot resident, hash-verified
    loader.py       resolves what the snapshot says to run
    scheduler.py    workflow-level traversal
    dispatcher.py   contract-level step execution
    ct_execute.py   transform invocation
    memory.py       one run's execution context
    evidence.py     trace emission
    trace_viz.py    rendering a trace for reading
    conformance.py  runtime conformance checks
    examine/        inspection of a completed run

testbed/            worked runs against known snapshots
```

## 8. Rules this repo enforces

1. **No domain knowledge.** Searching this repository for a business noun returns nothing.
2. **Execution adds no path.** Routing comes only from the compiled graph.
3. **No fallback.** A missing binding, capability or route is a failure, never a default.
4. **All mutation is through declared capabilities.** There is no other write path.
5. **The snapshot is read once and never modified.**
6. **Every run emits a trace**, produced by the same traversal that produced the effects.

## 9. How to know it works

```bash
./run.sh run --wf <domain>::<WORKFLOW> --payload <file> --data-root <dir>
```

A run reports its terminal status and writes a trace under the data root. Correct behaviour is
checkable two ways: the result is what the workflow's declarations require, and **the path in the
trace is a path present in the compiled graph** — the second is the one that matters, because it
tests the guarantee rather than the outcome.

## 10. Where the architecture is explained

This document describes *this repository*. The architecture it realizes is developed in the papers
indexed at **https://github.com/protocol-governed-computing**:

- **An Architecture for Deterministic Declarative Execution** — the closest companion to this
  repository: the execution partition, the runtime as interpreter of a protocol rather than
  originator of behaviour, determinism, replay, and the trace.
- **Compiler Conceptual Model** — what builds the graph this runtime walks, and why that leaves the
  runtime with nothing to decide.
- **A Conceptual Model** — the snapshot as the immutable admissibility boundary, and the evidence
  model.
