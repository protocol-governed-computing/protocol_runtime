"""
Test Suite 1: Snapshot Loading

Tests that load_domain() correctly loads and verifies the tokenized snapshot.
v0.3.0: snapshot loading is handled by runtime.loader.load_domain().
"""

import json
import tempfile
import unittest
from pathlib import Path

from runtime.loader import load_domain


def _write_snapshot(
    workspace: Path,
    domain: str,
    projection_hash: str = "abc123",
    forward: dict | None = None,
    reverse: dict | None = None,
) -> None:
    """Write a minimal valid tokenized snapshot to workspace."""
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
    (vocab_dir / "forward.json").write_text(json.dumps(forward or {}))
    (vocab_dir / "reverse.json").write_text(json.dumps(reverse or {}))


class TestSnapshotLoading(unittest.TestCase):
    """Tests for snapshot artifact loading."""

    def test_snapshot_root_must_exist(self):
        """load_domain MUST raise FileNotFoundError if workspace doesn't exist."""
        with self.assertRaises(FileNotFoundError):
            load_domain("/tmp/no_such_pgs_workspace_abc999", "blockchain")

    def test_tokenized_dir_must_exist(self):
        """load_domain MUST raise FileNotFoundError if tokenized/<domain> is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            # Workspace exists but no tokenized/blockchain
            with self.assertRaises(FileNotFoundError):
                load_domain(workspace, "blockchain")

    def test_dispatch_json_must_exist(self):
        """load_domain MUST raise FileNotFoundError if dispatch.json is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            tok_dir = workspace / "tokenized" / domain
            tok_dir.mkdir(parents=True)
            # dispatch.json missing
            with self.assertRaises(FileNotFoundError):
                load_domain(workspace, domain)

    def test_attestation_hash_mismatch_raises(self):
        """load_domain MUST raise RuntimeError on projection_hash mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="correct")

            # Overwrite attestation with wrong hash
            trust_dir = workspace / "trust" / domain
            (trust_dir / "structure_attestation.json").write_text(
                json.dumps({"tokenized_projection_hash": "tampered"})
            )

            with self.assertRaises(RuntimeError):
                load_domain(workspace, domain)

    def test_load_domain_returns_runtime_package(self):
        """load_domain MUST return a RuntimePackage with domain, dispatch, handlers, vocab."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="cafecafe")

            pkg = load_domain(workspace, domain)

            self.assertEqual(pkg.domain, domain)
            self.assertIsNotNone(pkg.dispatch)
            self.assertIsNotNone(pkg.handlers)
            self.assertIsNotNone(pkg.vocab)

    def test_load_domain_vocab_index_built(self):
        """load_domain MUST build VocabIndex from forward/reverse vocab files."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            # Write vocab with one entry
            forward = {"0x0001": "testdomain::WF_SOME_V0"}
            reverse = {"testdomain::WF_SOME_V0": "0x0001"}
            _write_snapshot(workspace, domain, projection_hash="deadbeef",
                            forward=forward, reverse=reverse)

            pkg = load_domain(workspace, domain)

            self.assertEqual(pkg.vocab.fqdn(1), "testdomain::WF_SOME_V0")
            self.assertEqual(pkg.vocab.addr("testdomain::WF_SOME_V0"), 1)

    def test_load_domain_dispatch_table_built(self):
        """load_domain MUST parse dispatch.json into integer-keyed DispatchTable."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="deadbeef")

            # Overwrite dispatch with non-empty data
            tok_dir = workspace / "tokenized" / domain
            (tok_dir / "dispatch.json").write_text(json.dumps({
                "routing": {"55": {"1": {"2": 3}}},
                "pipeline": {"1": [{"addr": 10, "op": None, "step_id": "s1"}]},
                "entry": {"5": {"start": 1, "rb": 2, "in": 3}},
                "bindings": {"5": {"1": {"field": "$.inputs.x"}}},
            }))

            pkg = load_domain(workspace, domain)

            self.assertIn(55, pkg.dispatch.routing)
            self.assertIn(1, pkg.dispatch.routing[55])
            self.assertIn(1, pkg.dispatch.pipeline)
            self.assertIn(5, pkg.dispatch.entry)
            self.assertEqual(pkg.dispatch.routing[55][1][2], 3)

    def test_snapshot_files_not_mutated_on_load(self):
        """load_domain MUST NOT mutate snapshot files during loading."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="deadbeef")

            tok_dir = workspace / "tokenized" / domain
            dispatch_before = (tok_dir / "dispatch.json").read_text()
            handlers_before = (tok_dir / "handlers.json").read_text()

            load_domain(workspace, domain)

            self.assertEqual((tok_dir / "dispatch.json").read_text(), dispatch_before)
            self.assertEqual((tok_dir / "handlers.json").read_text(), handlers_before)


if __name__ == '__main__':
    unittest.main()
