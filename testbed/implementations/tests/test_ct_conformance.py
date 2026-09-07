"""
Test Suite 6: CT Conformance Tests

Tests for CT execution via handler_ref embedded in CT-IR at compile time.
CT handler_ref binding is compile-time sealed — no runtime registry.
"""

import unittest

from runtime.ct_executor import CTExecutor, CTExecutionError


# ── Minimal atom functions used as handler_ref targets ──────────────
def _atom_double(inputs: dict) -> dict:
    return {"value": inputs["x"] * 2}


def _atom_concat(inputs: dict) -> dict:
    return {"value": inputs["a"] + inputs["b"]}


def _atom_failing(inputs: dict) -> dict:
    raise ValueError("Simulated atom failure")


def _atom_returns_none(inputs: dict) -> None:
    return None


_THIS_MODULE = "testbed.implementations.tests.test_ct_conformance"


class TestCTConformance(unittest.TestCase):
    """Tests for CT execution via compile-time-sealed handler_ref."""

    def _make_ct_ir(self, steps: list) -> dict:
        return {"atom_stream": steps, "input_types": {}}

    def test_handler_ref_execution(self):
        """CTExecutor MUST execute atom via handler_ref."""
        ct_ir = self._make_ct_ir([
            {
                "atom": "test::CT_DOUBLE_V0",
                "handler_ref": {"module": _THIS_MODULE, "callable": "_atom_double"},
                "args": {"x": "$.inputs.x"},
                "out": "result",
            }
        ])

        executor = CTExecutor()
        result = executor.execute(ct_ir=ct_ir, inputs={"x": 5})

        self.assertEqual(result["result"], {"value": 10})

    def test_handler_ref_input_resolution(self):
        """CTExecutor MUST resolve $.inputs.* expressions from CT context."""
        ct_ir = self._make_ct_ir([
            {
                "atom": "test::CT_CONCAT_V0",
                "handler_ref": {"module": _THIS_MODULE, "callable": "_atom_concat"},
                "args": {"a": "$.inputs.first", "b": "$.inputs.second"},
                "out": "result",
            }
        ])

        executor = CTExecutor()
        result = executor.execute(ct_ir=ct_ir, inputs={"first": "hello_", "second": "world"})

        self.assertEqual(result["result"], {"value": "hello_world"})

    def test_missing_handler_ref_raises(self):
        """CTExecutor MUST raise CTExecutionError if handler_ref is absent."""
        ct_ir = self._make_ct_ir([
            {
                "atom": "test::CT_NO_HANDLER_V0",
                "args": {},
                "out": "result",
            }
        ])

        executor = CTExecutor()
        with self.assertRaises(CTExecutionError):
            executor.execute(ct_ir=ct_ir, inputs={})

    def test_atom_returning_none_raises(self):
        """CTExecutor MUST raise CTExecutionError if atom returns None."""
        ct_ir = self._make_ct_ir([
            {
                "atom": "test::CT_NONE_V0",
                "handler_ref": {"module": _THIS_MODULE, "callable": "_atom_returns_none"},
                "args": {},
                "out": "result",
            }
        ])

        executor = CTExecutor()
        with self.assertRaises(CTExecutionError):
            executor.execute(ct_ir=ct_ir, inputs={})

    def test_missing_atom_stream_raises(self):
        """CTExecutor MUST raise CTExecutionError if atom_stream is absent."""
        ct_ir = {}

        executor = CTExecutor()
        with self.assertRaises(CTExecutionError):
            executor.execute(ct_ir=ct_ir, inputs={})

    def test_multi_step_result_chaining(self):
        """CTExecutor MUST support chaining results across steps via $.results.*."""
        ct_ir = self._make_ct_ir([
            {
                "atom": "test::CT_DOUBLE_V0",
                "handler_ref": {"module": _THIS_MODULE, "callable": "_atom_double"},
                "args": {"x": "$.inputs.n"},
                "out": "step1",
            },
            {
                "atom": "test::CT_DOUBLE_V0",
                "handler_ref": {"module": _THIS_MODULE, "callable": "_atom_double"},
                "args": {"x": "$.results.step1.value"},
                "out": "step2",
            },
        ])

        executor = CTExecutor()
        result = executor.execute(ct_ir=ct_ir, inputs={"n": 3})

        self.assertEqual(result["step1"], {"value": 6})
        self.assertEqual(result["step2"], {"value": 12})


if __name__ == '__main__':
    unittest.main()
