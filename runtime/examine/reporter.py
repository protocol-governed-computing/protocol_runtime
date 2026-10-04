"""
reporter.py — what a completed run did, read from its trace.

A run either **completed** — it reached a declared ending, whatever the outcome, refusals included —
or it **failed structurally**: the runtime could not carry out what the snapshot declares (an
outcome with no routing and no ending, an admission gate with no contract, an exception). The first
is the business answering; the second is a defect in the composition or the runtime, and exits 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DiagnosticReport:
    trace_id: str
    workflow: str
    snapshot_id: str
    outcome: str | None             # WF_COMPLETE's result, or None when the run never completed
    ending: str | None              # the declared ending reached, when there is one
    path: list[tuple[str, str, str | None]] = field(default_factory=list)   # (from, outcome, to)
    contracts: list[tuple[str, str, str | None]] = field(default_factory=list)  # (node, contract, result)
    events: list[str] = field(default_factory=list)
    recorded: list[str] = field(default_factory=list)   # non-deterministic results recorded
    errors: list[str] = field(default_factory=list)

    @property
    def has_structural_failure(self) -> bool:
        return bool(self.errors) or self.outcome is None

    def format(self) -> str:
        sep = "=" * 60
        head = "STRUCTURAL FAILURE" if self.has_structural_failure else "COMPLETED"
        lines = [sep, f"[trace-examiner] {head}", sep,
                 f"Trace:      {self.trace_id}",
                 f"Workflow:   {self.workflow}",
                 f"Snapshot:   {self.snapshot_id}",
                 f"Outcome:    {self.outcome or '(never completed)'}",
                 f"Ending:     {self.ending or '(none reached)'}"]
        if self.path:
            lines.append("Path:")
            lines += [f"  {f} --{o}--> {t or '(nowhere)'}" for f, o, t in self.path]
        if self.contracts:
            lines.append("Contracts:")
            lines += [f"  {n or '?'}  {c}  {r or '(no result)'}" for n, c, r in self.contracts]
        if self.events:
            lines.append("Events:")
            lines += [f"  {e}" for e in self.events]
        if self.recorded:
            lines.append("Recorded non-deterministic results:")
            lines += [f"  {r}" for r in self.recorded]
        if self.errors:
            lines.append("Errors:")
            lines += [f"  {e}" for e in self.errors]
        lines.append(sep)
        return "\n".join(lines)
