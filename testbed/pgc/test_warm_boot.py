"""
PGC warm-boot tests — the runtime boots the assembled snapshot through its manifest
(the root of trust) and holds a resident, hash-verified executable universe.

These are the canonical PGC runtime tests. They require an assembled snapshot (produced by
snapshot_assembler → the sibling ../snapshot). If none is present, they skip.

Legacy RI-0 PGS-workspace execution tests (testbed/implementations/tests) are gated behind
PGC_RI0_LEGACY=1 — they require a PGS workspace and a domain workflow the platform snapshot lacks.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.boot import boot, default_snapshot_root, _composite_hash

SNAPSHOT_ROOT = default_snapshot_root()
_HAVE_SNAPSHOT = (SNAPSHOT_ROOT / "manifest.json").exists()


@unittest.skipUnless(_HAVE_SNAPSHOT, f"no assembled snapshot at {SNAPSHOT_ROOT} — run assemble.sh")
class WarmBootTest(unittest.TestCase):

    def test_boot_verifies_and_loads_all_domains(self):
        booted = boot(SNAPSHOT_ROOT)
        # manifest declares at least one domain, all resident
        self.assertTrue(booted.domains, "no domains booted")
        self.assertEqual(
            set(booted.domains),
            {d["domain"] for d in booted.manifest["domains"]},
            "resident domains != manifest domains",
        )

    def test_snapshot_id_equals_composite(self):
        booted = boot(SNAPSHOT_ROOT)
        self.assertEqual(booted.snapshot_id, booted.manifest["composite_hash"])

    def test_composite_recompute_matches_manifest(self):
        # verifier independence: recompute from the identity view, must equal the manifest's claim
        booted = boot(SNAPSHOT_ROOT)
        self.assertEqual(
            _composite_hash(booted.manifest["domains"]),
            booted.manifest["composite_hash"],
        )

    def test_platform_domain_has_capability_substrate(self):
        booted = boot(SNAPSHOT_ROOT)
        if "platform" in booted.domains:
            pkg = booted.domains["platform"]
            # surface-only snapshot: capability substrate present, no executable workflow topology
            self.assertGreater(len(pkg.handlers.ct), 0, "no CT handlers in platform")
            self.assertGreater(len(pkg.handlers.cs), 0, "no CS handlers in platform")
            self.assertEqual(len(pkg.dispatch.routing), 0, "platform unexpectedly has WF routing")

    def test_tampered_composite_hash_rejected(self):
        # corrupt the manifest composite in a throwaway copy → boot must fail hard
        tmp = Path(tempfile.mkdtemp()) / "snap"
        shutil.copytree(SNAPSHOT_ROOT, tmp)
        man = json.loads((tmp / "manifest.json").read_text())
        man["composite_hash"] = "deadbeef" * 8
        (tmp / "manifest.json").write_text(json.dumps(man))
        with self.assertRaises(RuntimeError):
            boot(tmp)
        shutil.rmtree(tmp.parent)

    def test_tampered_domain_projection_rejected(self):
        # mutate a domain's on-disk tokenized hash → manifest anchor must fail
        tmp = Path(tempfile.mkdtemp()) / "snap"
        shutil.copytree(SNAPSHOT_ROOT, tmp)
        man = json.loads((tmp / "manifest.json").read_text())
        dom = man["domains"][0]["domain"]
        meta_path = tmp / "tokenized" / dom / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["projection_hash"] = "0" * 64
        meta_path.write_text(json.dumps(meta))
        with self.assertRaises(RuntimeError):
            boot(tmp)
        shutil.rmtree(tmp.parent)


if __name__ == "__main__":
    unittest.main()
