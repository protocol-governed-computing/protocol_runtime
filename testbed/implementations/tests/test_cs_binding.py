"""
Test Suite 2: CS Binding Tests

Tests policy template expansion and load_domain error paths.
The v0.3.0 runtime has no RuntimeLoader class — CS binding is handled
by the dispatcher._expand_policy() function and the compiled handlers.json.
"""

import json
import tempfile
import unittest
from pathlib import Path

from runtime.dispatcher import _expand_policy
from runtime.loader import load_domain


class TestCSBinding(unittest.TestCase):
    """Tests for CS policy template expansion and snapshot loading errors."""

    # ── _expand_policy tests ──────────────────────────────────────────────

    def test_expand_policy_substitutes_module_data_root(self):
        """_expand_policy MUST substitute {{module_data_root}} with data_root."""
        policy_raw = {"path": "{{module_data_root}}/storage.json"}
        result = _expand_policy(policy_raw, "/abs/data/root")
        self.assertEqual(result["path"], "/abs/data/root/storage.json")

    def test_expand_policy_strips_trailing_slash(self):
        """_expand_policy MUST strip trailing slash from data_root before substitution."""
        policy_raw = {"path": "{{module_data_root}}/storage.json"}
        result = _expand_policy(policy_raw, "/abs/data/root/")
        self.assertEqual(result["path"], "/abs/data/root/storage.json")

    def test_expand_policy_nested_substitution(self):
        """_expand_policy MUST substitute in nested policy dicts."""
        policy_raw = {
            "primary": "{{module_data_root}}/primary.json",
            "secondary": "{{module_data_root}}/secondary.json",
        }
        result = _expand_policy(policy_raw, "/data")
        self.assertEqual(result["primary"], "/data/primary.json")
        self.assertEqual(result["secondary"], "/data/secondary.json")

    def test_expand_policy_no_template_passes_through(self):
        """_expand_policy MUST pass through policy with no template strings."""
        policy_raw = {"key": "literal_value", "count": 3}
        result = _expand_policy(policy_raw, "/any/root")
        self.assertEqual(result["key"], "literal_value")
        self.assertEqual(result["count"], 3)

    def test_expand_policy_empty_returns_empty(self):
        """_expand_policy MUST return empty dict for empty policy."""
        result = _expand_policy({}, "/data")
        self.assertEqual(result, {})

    def test_expand_policy_none_returns_empty(self):
        """_expand_policy MUST return empty dict for None policy."""
        result = _expand_policy(None, "/data")
        self.assertEqual(result, {})

    # ── load_domain error path tests ─────────────────────────────────────

    def test_load_domain_missing_workspace_raises(self):
        """load_domain MUST raise FileNotFoundError if workspace doesn't exist."""
        with self.assertRaises(FileNotFoundError):
            load_domain("/tmp/does_not_exist_pgs_workspace_xyz", "blockchain")

    def test_load_domain_missing_dispatch_raises(self):
        """load_domain MUST raise FileNotFoundError if dispatch.json is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            # Create directory structure but omit dispatch.json
            tok_dir = workspace / "tokenized" / "blockchain"
            tok_dir.mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                load_domain(workspace, "blockchain")

    def test_load_domain_hash_mismatch_raises(self):
        """load_domain MUST raise RuntimeError on projection_hash mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"

            tok_dir = workspace / "tokenized" / domain
            trust_dir = workspace / "trust" / domain
            vocab_dir = workspace / "vocabulary" / domain
            tok_dir.mkdir(parents=True)
            trust_dir.mkdir(parents=True)
            vocab_dir.mkdir(parents=True)

            # Write all required files with mismatched hashes
            (tok_dir / "dispatch.json").write_text(
                json.dumps({"routing": {}, "pipeline": {}, "entry": {}, "bindings": {}})
            )
            (tok_dir / "handlers.json").write_text(
                json.dumps({"ct": {}, "cs": {}, "rb_policy": {}})
            )
            (tok_dir / "metadata.json").write_text(
                json.dumps({"projection_hash": "aabbccdd"})
            )
            (trust_dir / "structure_attestation.json").write_text(
                json.dumps({"tokenized_projection_hash": "11223344"})  # MISMATCH
            )
            (vocab_dir / "forward.json").write_text(json.dumps({}))
            (vocab_dir / "reverse.json").write_text(json.dumps({}))

            with self.assertRaises(RuntimeError) as ctx:
                load_domain(workspace, domain)
            self.assertIn("integrity", str(ctx.exception).lower())

    def test_load_domain_empty_hash_raises(self):
        """load_domain MUST raise RuntimeError if projection_hash is empty."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"

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
                json.dumps({"projection_hash": ""})  # empty hash
            )
            (trust_dir / "structure_attestation.json").write_text(
                json.dumps({"tokenized_projection_hash": ""})  # both empty
            )
            (vocab_dir / "forward.json").write_text(json.dumps({}))
            (vocab_dir / "reverse.json").write_text(json.dumps({}))

            with self.assertRaises(RuntimeError):
                load_domain(workspace, domain)


if __name__ == '__main__':
    unittest.main()
