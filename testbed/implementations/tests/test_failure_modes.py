"""
Test Suite 3: Failure Mode Tests

Tests that runtime fails fast and explicitly on errors.
v0.3.0: failure modes are surfaced via load_domain (FileNotFoundError,
RuntimeError), ct_executor (CTExecutionError), and dispatcher internals.
"""

import json
import tempfile
import unittest
from pathlib import Path

from runtime.loader import load_domain
from runtime.ct_executor import CTExecutor, CTExecutionError


def _failing_atom(inputs: dict) -> dict:
    raise ValueError("simulated failure")


def _write_snapshot(workspace: Path, domain: str, projection_hash: str = "abc123") -> None:
    """Write a minimal valid snapshot to workspace/tokenized/<domain>."""
    tok_dir = workspace / "tokenized" / domain
    trust_dir = workspace / "trust" / domain
    vocab_dir = workspace / "vocabulary" / domain
    tok_dir.mkdir(parents=True)
    trust_dir.mkdir(parents=True)
    vocab_dir.mkdir(parents=True)

    (tok_dir / "dispatch.json").write_text(
        json.dumps({"routing": {}, "pipeline": {}, "entry": {}, "bindings": {}})
    )
    (tok_dir / "handlers.json").write_text(
        json.dumps({"ct": {}, "cs": {}, "rb_policy": {}})
    )
    (tok_dir / "metadata.json").write_text(
        json.dumps({"projection_hash": projection_hash})
    )
    (trust_dir / "structure_attestation.json").write_text(
        json.dumps({"tokenized_projection_hash": projection_hash})
    )
    (vocab_dir / "forward.json").write_text(json.dumps({}))
    (vocab_dir / "reverse.json").write_text(json.dumps({}))


class TestFailureModes(unittest.TestCase):
    """Tests for error handling and fail-fast behavior."""

    # ── load_domain failure modes ─────────────────────────────────────────

    def test_fail_on_missing_workspace(self):
        """load_domain MUST raise FileNotFoundError if workspace doesn't exist."""
        with self.assertRaises(FileNotFoundError):
            load_domain("/tmp/no_such_pgs_workspace_xyz123", "blockchain")

    def test_fail_on_missing_dispatch_json(self):
        """load_domain MUST raise FileNotFoundError if dispatch.json is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            tok_dir = workspace / "tokenized" / domain
            tok_dir.mkdir(parents=True)
            # No dispatch.json written
            with self.assertRaises(FileNotFoundError):
                load_domain(workspace, domain)

    def test_fail_on_missing_handlers_json(self):
        """load_domain MUST raise FileNotFoundError if handlers.json is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            tok_dir = workspace / "tokenized" / domain
            tok_dir.mkdir(parents=True)
            (tok_dir / "dispatch.json").write_text(
                json.dumps({"routing": {}, "pipeline": {}, "entry": {}, "bindings": {}})
            )
            # No handlers.json written
            with self.assertRaises(FileNotFoundError):
                load_domain(workspace, domain)

    def test_fail_on_hash_mismatch(self):
        """load_domain MUST raise RuntimeError on projection_hash mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="correct_hash")

            # Tamper: overwrite attestation with wrong hash
            trust_dir = workspace / "trust" / domain
            (trust_dir / "structure_attestation.json").write_text(
                json.dumps({"tokenized_projection_hash": "wrong_hash"})
            )

            with self.assertRaises(RuntimeError) as ctx:
                load_domain(workspace, domain)
            self.assertIn("integrity", str(ctx.exception).lower())

    def test_fail_on_empty_projection_hash(self):
        """load_domain MUST raise RuntimeError if projection_hash is empty string."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="")

            with self.assertRaises(RuntimeError):
                load_domain(workspace, domain)

    # ── CT executor failure modes ─────────────────────────────────────────

    def test_ct_executor_fails_on_missing_atom_stream(self):
        """CTExecutor MUST raise CTExecutionError if atom_stream is absent."""
        executor = CTExecutor()
        with self.assertRaises(CTExecutionError):
            executor.execute(ct_ir={}, inputs={})

    def test_ct_executor_fails_on_missing_handler_ref(self):
        """CTExecutor MUST raise CTExecutionError if handler_ref is absent."""
        ct_ir = {
            "atom_stream": [
                {
                    "atom": "test::CT_NO_HANDLER_V0",
                    "args": {},
                    "out": "result",
                    # No handler_ref
                }
            ]
        }
        executor = CTExecutor()
        with self.assertRaises(CTExecutionError):
            executor.execute(ct_ir=ct_ir, inputs={})

    def test_ct_executor_fails_when_atom_raises(self):
        """CTExecutor MUST raise CTExecutionError if the atom function raises."""
        _THIS_MODULE = "testbed.implementations.tests.test_failure_modes"

        ct_ir = {
            "atom_stream": [
                {
                    "atom": "test::CT_FAIL_V0",
                    "handler_ref": {
                        "module": _THIS_MODULE,
                        "callable": "_failing_atom",
                    },
                    "args": {},
                    "out": "result",
                }
            ]
        }
        executor = CTExecutor()
        with self.assertRaises(CTExecutionError):
            executor.execute(ct_ir=ct_ir, inputs={})

    # ── Snapshot load success path ────────────────────────────────────────

    def test_valid_minimal_snapshot_loads(self):
        """load_domain MUST succeed with a minimal but structurally valid snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="deadbeef")

            pkg = load_domain(workspace, domain)
            self.assertEqual(pkg.domain, domain)
            self.assertIsNotNone(pkg.dispatch)
            self.assertIsNotNone(pkg.handlers)
            self.assertIsNotNone(pkg.vocab)


if __name__ == '__main__':
    unittest.main()
