"""
errors.py — Structured error types for CT execution.

All CT execution exceptions that carry structured error metadata
MUST subclass StructuredError. This enables the workflow runner
to emit deterministic, schema-compliant error trace events.

Never raise StructuredError without error_code.
"""


class StructuredError(RuntimeError):
    """
    Base exception carrying structured error metadata.

    Fields:
        error_code: One of the codes defined in STRUCTURE_TRACE_SCHEMA_V0 §10.2
        node_category: One of WF, IN, CC, CT, CS per §10.3
        message: Human-readable description
        cause: Original exception if wrapping an unstructured error
    """

    def __init__(
        self,
        error_code: str,
        node_category: str,
        message: str,
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.node_category = node_category
        self.cause = cause
