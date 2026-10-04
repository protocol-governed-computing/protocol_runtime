# trace_examiner

Post-execution evidence analysis and prescriptive diagnostics.

Reads a completed `.jsonl` trace, classifies failures deterministically, resolves artifact paths, and generates prescriptive fix hints.

Public API: `analyze(trace_path) -> DiagnosticReport`

No imports from execution machinery. Pure post-hoc analysis against a completed trace.
