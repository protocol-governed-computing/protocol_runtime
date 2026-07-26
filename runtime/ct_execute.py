"""
execute.py — CT-IR execution entry point.

Adapter between the dispatcher and CTExecutor.
"""

from typing import Any

from runtime.ct_errors import StructuredError
from runtime.ct_executor import CTExecutor

# Module-level executor singleton — avoids re-creating per call,
# preserves federated IR directory cache and atom loading.
# Lazy initialization to avoid import-time side effects (bootstrap requirement)
_executor: CTExecutor | None = None


def _get_executor() -> CTExecutor:
    """Get or create the CT executor singleton (lazy initialization)."""
    global _executor
    if _executor is None:
        _executor = CTExecutor()
    return _executor


def execute_ct(ct_ir: dict[str, Any], inputs: dict[str, Any]) -> Any:
    """
    Execute CT-IR and adapt result to CC expectation.

    CONTRACT (STRICT):
    - CT-IR MUST be pre-validated by compiler before reaching execution
    - CTExecutor blindly executes pre-validated CT-IR
    - CT-IR declares exactly one output
    - That output is returned directly (unwrapped)

    SOVEREIGNTY: Execution layer is a blind executor.
    Validation is compiler's responsibility.
    """

    # ---- Execution (CT-IR assumed pre-validated by compiler) ----
    symbol_table = _get_executor().execute(
        ct_ir=ct_ir,
        inputs=inputs,
    )

    # ---- Output adaptation ----
    outputs = ct_ir.get("outputs")
    if not outputs:
        raise StructuredError(
            error_code="CT_EXECUTION_FAILED",
            node_category="CT",
            message="CT must declare at least one output",
        )

    # Build CT outputs by mapping from symbol table
    ct_outputs = {}
    for output_name, spec in outputs.items():
        from_symbol = spec["from"]

        if from_symbol not in symbol_table:
            raise StructuredError(
                error_code="CT_EXECUTION_FAILED",
                node_category="CT",
                message=f"CT output symbol '{from_symbol}' was not produced by CT execution",
            )

        symbol_value = symbol_table[from_symbol]

        # Extract output field from atom result dict
        # Atoms return a single dict containing all output fields
        # e.g., {"valid": True, "failed_rule": None}
        if isinstance(symbol_value, dict):
            if output_name in symbol_value:
                # Multi-output atom: extract this specific output field
                symbol_value = symbol_value[output_name]
            elif len(symbol_value) == 1:
                # Single-output atom: unwrap the single field
                symbol_value = next(iter(symbol_value.values()))

        ct_outputs[output_name] = symbol_value

    # Return contract-shaped outputs (always as dict, never unwrap)
    # Contract declares output shape - return exactly that shape
    return ct_outputs
