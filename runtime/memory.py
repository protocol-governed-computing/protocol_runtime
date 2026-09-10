"""
memory.py — Execution context for a single workflow run.

Holds the initial payload and accumulates CC result surfaces as the
workflow progresses. Provides JSONPath resolution for CC input bindings.

Path grammar (compile-time allocated, runtime resolved):
    $.payload.<field>              — field from the workflow payload
    $.inputs.<field>               — alias for $.payload.<field> (CC-step scope)
    $.results.<cc_addr>.<field>    — field from a previous CC's result surface
    <anything else>                — literal value (returned as-is)

No dynamic path construction. All paths are emitted by the compiler.
"""

from __future__ import annotations

from typing import Any


class ExecutionContext:
    """
    Mutable execution context for one workflow invocation.

    - payload: initial input dict (never mutated after construction)
    - results: accumulated CC result surfaces keyed by integer CC address
    - actor:   the Authority actor context this workflow executes under (FQDN), bound at WF entry
               and carried through the run. Present for attribution/propagation only — no
               authorization is enforced here (that belongs to the authority model).
    """

    __slots__ = ("_payload", "_results", "_actor")

    def __init__(self, payload: dict[str, Any], actor: str | None = None) -> None:
        self._payload: dict[str, Any] = dict(payload)
        self._results: dict[int, dict[str, Any]] = {}
        self._actor: str | None = actor

    def record_result(self, cc_addr: int, surface: dict[str, Any]) -> None:
        """Store a CC's output surface for downstream bindings."""
        self._results[cc_addr] = dict(surface)

    def resolve(self, path: str) -> Any:
        """
        Resolve a binding path to its value.

        Supports:
            $.payload.<field>           — payload lookup (nested via dots)
            $.inputs.<field>            — same as $.payload.<field>
            $.results.<cc_addr>.<field> — previous CC result lookup
            <literal>                   — returned as-is
        """
        if not isinstance(path, str):
            if isinstance(path, dict):
                return {k: self.resolve(v) for k, v in path.items()}
            if isinstance(path, (list, tuple)):
                return [self.resolve(v) for v in path]
            return path  # int, float, bool, None — returned as-is

        if path.startswith("$.payload."):
            return _nested_get(self._payload, path[len("$.payload."):])

        if path.startswith("$.inputs."):
            return _nested_get(self._payload, path[len("$.inputs."):])

        if path.startswith("$.results."):
            # Format: $.results.<cc_addr>.<field>[.<nested>...]
            after = path[len("$.results."):]
            dot = after.find(".")
            if dot < 0:
                return None  # malformed path
            try:
                cc_addr = int(after[:dot])
            except ValueError:
                return None  # non-integer CC addr in path
            field_path = after[dot + 1:]
            surface = self._results.get(cc_addr)
            if surface is None:
                return None
            return _nested_get(surface, field_path)

        # Literal value
        return path

    def resolve_inputs(self, bindings: dict[str, Any]) -> dict[str, Any]:
        """
        Resolve a full bindings dict → concrete input values.

        Each value is a path string or a literal. Returns a plain dict
        with the same keys and resolved values.
        """
        return {k: self.resolve(v) for k, v in bindings.items()}

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    @property
    def actor(self) -> str | None:
        """Actor context (FQDN) this workflow executes under. None if no actor is bound."""
        return self._actor

    @property
    def results(self) -> dict[int, dict[str, Any]]:
        return dict(self._results)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _nested_get(obj: Any, dotted_key: str) -> Any:
    """
    Traverse a nested dict by a dot-separated key path.

    Example: _nested_get({"a": {"b": 3}}, "a.b") → 3
    Returns None for any missing key.
    """
    parts = dotted_key.split(".")
    current = obj
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current
