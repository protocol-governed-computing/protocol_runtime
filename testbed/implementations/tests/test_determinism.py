"""
Test Suite 5: Determinism Tests

Tests that runtime identifiers and snapshot loading are deterministic.
v0.3.0: determinism of workflow execution is guaranteed by the token-native
architecture. These tests focus on the deterministic components that can be
unit-tested without a full compiled tokenized snapshot.
"""

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from runtime.evidence import make_trace_id
from runtime.loader import load_domain


def generate_deterministic_id(prefix: str, content: dict) -> str:
    """
    Generate deterministic ID from content using SHA256 hash.
    Used for testing deterministic ID generation.
    """
    content_json = json.dumps(content, sort_keys=True, separators=(',', ':'))
    content_hash = hashlib.sha256(content_json.encode('utf-8')).hexdigest()
    return f"{prefix}_{content_hash[:16]}"


def _write_snapshot(workspace: Path, domain: str, projection_hash: str = "abc123") -> None:
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


class TestDeterminism(unittest.TestCase):
    """Tests for execution determinism."""

    def test_deterministic_id_generation(self):
        """Deterministic ID generation MUST produce same ID for same content."""
        content1 = {"field1": "value1", "field2": "value2"}
        content2 = {"field1": "value1", "field2": "value2"}
        content3 = {"field2": "value2", "field1": "value1"}  # Different key order

        id1 = generate_deterministic_id("AC", content1)
        id2 = generate_deterministic_id("AC", content2)
        id3 = generate_deterministic_id("AC", content3)

        self.assertEqual(id1, id2)
        self.assertEqual(id1, id3)

        content_different = {"field1": "different"}
        id_different = generate_deterministic_id("AC", content_different)
        self.assertNotEqual(id_different, id1)

    def test_json_serialization_deterministic(self):
        """JSON serialization for ID generation MUST be deterministic."""
        content_a = {"z": 1, "a": 2, "m": 3}
        content_b = {"a": 2, "m": 3, "z": 1}
        content_c = {"m": 3, "z": 1, "a": 2}

        id_a = generate_deterministic_id("TEST", content_a)
        id_b = generate_deterministic_id("TEST", content_b)
        id_c = generate_deterministic_id("TEST", content_c)

        self.assertEqual(id_a, id_b)
        self.assertEqual(id_b, id_c)

    def test_make_trace_id_format(self):
        """make_trace_id MUST return YYYYMMDDTHHMMSSmmmZ__WF_CODE__XXXX format."""
        import re
        domain = "blockchain"
        wf_fqdn = "blockchain::WF_REGISTER_ACTOR_UNVERIFIED_V0"
        payload = {"actor_id": "alice", "email": "alice@example.com"}

        trace_id = make_trace_id(domain, wf_fqdn, payload)

        # Format: 20260524T151422183Z__WF_REGISTER_ACTOR_UNVERIFIED_V0__A7K2
        pattern = r"^\d{8}T\d{6}\d{3}Z__WF_REGISTER_ACTOR_UNVERIFIED_V0__[0-9A-F]{4}$"
        self.assertRegex(trace_id, pattern, f"trace_id {trace_id!r} does not match expected format")

    def test_make_trace_id_embeds_wf_code(self):
        """make_trace_id MUST embed the WF code (extracted from wf_fqdn) in the ID."""
        domain = "blockchain"
        wf_fqdn = "blockchain::WF_CREATE_WALLET_V0"
        payload = {"wallet_id": "W_abc"}

        trace_id = make_trace_id(domain, wf_fqdn, payload)
        self.assertIn("WF_CREATE_WALLET_V0", trace_id)

    def test_make_trace_id_hash_suffix_differs_for_different_payloads(self):
        """Hash suffix in trace ID MUST differ for different payloads."""
        domain = "blockchain"
        wf_fqdn = "blockchain::WF_REGISTER_ACTOR_UNVERIFIED_V0"

        id_alice = make_trace_id(domain, wf_fqdn, {"actor_id": "alice"})
        id_bob = make_trace_id(domain, wf_fqdn, {"actor_id": "bob"})

        # Extract suffix (last 4 chars after final __)
        suffix_alice = id_alice.rsplit("__", 1)[-1]
        suffix_bob = id_bob.rsplit("__", 1)[-1]
        self.assertNotEqual(suffix_alice, suffix_bob)

    def test_make_trace_id_hash_suffix_differs_for_different_domains(self):
        """Hash suffix MUST differ for different domains."""
        payload = {"key": "value"}
        id1 = make_trace_id("blockchain", "blockchain::WF_SOME_V0", payload)
        id2 = make_trace_id("ai_governance", "ai_governance::WF_SOME_V0", payload)

        suffix1 = id1.rsplit("__", 1)[-1]
        suffix2 = id2.rsplit("__", 1)[-1]
        self.assertNotEqual(suffix1, suffix2)

    def test_make_trace_id_hash_suffix_payload_key_order_invariant(self):
        """Hash suffix MUST be invariant to payload key ordering."""
        domain = "blockchain"
        wf_fqdn = "blockchain::WF_REGISTER_ACTOR_UNVERIFIED_V0"

        payload_a = {"b": 2, "a": 1}
        payload_b = {"a": 1, "b": 2}

        id_a = make_trace_id(domain, wf_fqdn, payload_a)
        id_b = make_trace_id(domain, wf_fqdn, payload_b)

        suffix_a = id_a.rsplit("__", 1)[-1]
        suffix_b = id_b.rsplit("__", 1)[-1]
        self.assertEqual(suffix_a, suffix_b)

    def test_load_domain_is_idempotent(self):
        """load_domain MUST return identical structure on repeated calls."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            domain = "testdomain"
            _write_snapshot(workspace, domain, projection_hash="cafebabe")

            pkg1 = load_domain(workspace, domain)
            pkg2 = load_domain(workspace, domain)

            self.assertEqual(pkg1.domain, pkg2.domain)
            self.assertEqual(pkg1.dispatch.routing, pkg2.dispatch.routing)
            self.assertEqual(pkg1.dispatch.pipeline, pkg2.dispatch.pipeline)
            self.assertEqual(pkg1.handlers.ct, pkg2.handlers.ct)
            self.assertEqual(pkg1.vocab.forward, pkg2.vocab.forward)


if __name__ == '__main__':
    unittest.main()
