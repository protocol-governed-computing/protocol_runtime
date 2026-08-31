"""
reporter.py — Diagnostic report formatting.

Governed by: Trace Examiner spec §8

Formats DiagnosticReport to terminal output.
Deterministic, stable format. Pure function.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.examine.classifier import FailureClass


_SE_SUCCESS_STATUSES = frozenset({"SUCCESS", "ACK", "completed"})


@dataclass
class SideEffectOutcome:
    """Outcome of a side-effect capability node."""

    cc_code: str
    result_status: str

    @property
    def succeeded(self) -> bool:
        return self.result_status in _SE_SUCCESS_STATUSES


@dataclass
class DiagnosticReport:
    """
    Complete diagnostic report for a trace examination.

    Per spec §6 data structure.
    """

    execution_id: str
    workflow_code: str
    has_structural_failure: bool
    failure_class: FailureClass | None
    failing_node: str | None
    reason: str
    artifact_path: str | None
    fix_hint: str
    side_effect_outcomes: list[SideEffectOutcome]

    def format(self) -> str:
        """Format report for terminal output per spec §8."""
        if not self.has_structural_failure:
            return self._format_success()
        return self._format_failure()

    def _format_failure(self) -> str:
        sep = "=" * 60
        lines = [
            sep,
            "[trace-examiner] STRUCTURAL FAILURE DETECTED",
            sep,
            f"Trace ID:      {self.execution_id}",
            f"Workflow:       {self.workflow_code}",
            f"Failing Node:   {self.failing_node or '(workflow-level)'}",
            f"Failure Class:  {self.failure_class.value if self.failure_class else 'UNKNOWN'}",
            f"Reason:         {self.reason}",
            f"Artifact:       {self.artifact_path or '(unresolved)'}",
            f"Fix:            {self.fix_hint}",
            sep,
        ]
        return "\n".join(lines)

    def _format_success(self) -> str:
        parts: list[str] = []

        if self.failure_class == FailureClass.BUSINESS_VIOLATION:
            sep = "-" * 60
            parts.extend([
                sep,
                "[trace-examiner] BUSINESS VIOLATION (not escalated)",
                sep,
                f"Trace ID:      {self.execution_id}",
                f"Workflow:       {self.workflow_code}",
                f"Node:           {self.failing_node or '(unknown)'}",
                f"Reason:         {self.reason}",
                sep,
            ])
        else:
            parts.append(
                f"[trace-examiner] {self.workflow_code} — "
                f"{self.execution_id} — no structural failures"
            )

        if self.side_effect_outcomes:
            parts.append(self._format_side_effect_outcomes())

        return "\n".join(parts)

    def _format_side_effect_outcomes(self) -> str:
        """Format side-effect outcomes as a business outcome summary."""
        lines: list[str] = []
        sep = "-" * 60
        lines.append(sep)
        lines.append("[trace-examiner] SIDE-EFFECT OUTCOMES")
        lines.append(sep)
        for outcome in self.side_effect_outcomes:
            indicator = "OK" if outcome.succeeded else "FAILED"
            lines.append(f"  [{indicator}] {outcome.cc_code}: {outcome.result_status}")
        lines.append(sep)
        return "\n".join(lines)
