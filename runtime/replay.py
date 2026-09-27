"""
Replay — reproducing an execution from its sealed composition, its inputs and its recorded outcomes.

The platform's determination is deterministic relative to the outcomes it records: every result an
atom declared not deterministic produced is kept as determining evidence when it is produced
(capability_transforms::CONSTITUTION_NONDETERMINISTIC_ATOMS_V0). A replay runs the same workflow on
the same inputs against the same initial state, substitutes each recorded outcome for its atom, and
runs nothing that is not deterministic. Its trace must then agree with the original in everything
the trace declares determinative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

NONDETERMINISTIC_PURITY = "ct_impure"

# Content that varies between two faithful executions without any governed consequence varying:
# when it happened, and which execution it was. Whether a step was replayed is how the replay
# differs from the original by construction, not a difference in what was determined.
_OBSERVATIONAL = {"trace_id", "ts_ns"}
_REPLAY_MARKER = "replayed"


def _events(trace_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(trace_path).read_text(encoding="utf-8").splitlines() if line.strip()]


def recorded_outcomes(trace_path: Path) -> dict[tuple, Any]:
    """Every recorded outcome of a non-deterministic atom, keyed by (cc, step, path)."""
    out: dict[tuple, Any] = {}
    for e in _events(trace_path):
        d = e.get("detail") or {}
        if e.get("event_type") == "CT_STEP" and d.get("purity") == NONDETERMINISTIC_PURITY:
            out[(e.get("cc_addr"), e.get("step_addr"), d.get("path"))] = d.get("outcome")
    return out


def determinative(trace_path: Path) -> list[dict[str, Any]]:
    """The trace with its observational content removed — what two faithful executions share."""
    kept = []
    for e in _events(trace_path):
        e = {k: v for k, v in e.items() if k not in _OBSERVATIONAL}
        if e.get("event_type") == "CT_STEP":
            e["detail"] = {k: v for k, v in (e.get("detail") or {}).items() if k != _REPLAY_MARKER}
        kept.append(e)
    return kept


def compare(original: Path, replayed: Path) -> tuple[bool, str]:
    """Whether a replay reproduced the original, and where it first did not."""
    a, b = determinative(original), determinative(replayed)
    for n, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return False, f"event {n} differs: {x.get('event_type')} vs {y.get('event_type')}"
    if len(a) != len(b):
        return False, f"the original has {len(a)} events and the replay {len(b)}"
    return True, f"{len(a)} determinative events agree"
