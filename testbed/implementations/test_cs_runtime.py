"""
Minimal test CS runtime for use in testbed integration tests.

Not a production implementation — exists only to provide a real importable
handler_ref target for tests that need a functional CS binding.
"""

from typing import Any


class TestAppendRuntime:
    """Minimal CS runtime that appends to an in-memory log."""

    capability_kind = "CS"

    def __init__(self, config: dict | None = None, metadata: dict | None = None, capability_code: str | None = None):
        self._capability_code = capability_code or "testbed::CS_TEST_APPEND_V0"
        self._config = config or {}
        self._log: list = []

    @property
    def capability_code(self) -> str:
        return self._capability_code

    @property
    def supported_operation_specs(self) -> set[str]:
        return {"APPEND", "GET_ALL"}

    def execute(self, *, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op == "APPEND":
            record = payload.get("record", {})
            self._log.append(record)
            return {"result_status": "SUCCESS", "record_id": f"rec-{len(self._log)}"}
        if op == "GET_ALL":
            return {"result_status": "SUCCESS", "entries": list(self._log)}
        return {"result_status": "BACKEND_ERROR", "error": f"Unsupported op: {op}"}
