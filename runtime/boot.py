"""
boot.py — warm reboot: bring the assembled snapshot resident and hash-verified.

The manifest is the ROOT OF TRUST. The runtime boots *through* it, never by scanning the
filesystem. Contract: standards/doc/SNAPSHOT_ASSEMBLY_CONTRACT.md

Boot sequence:
    1. load manifest.json
    2. recompute composite_hash from the identity view of domains[]  →  MUST equal manifest's
       (verifier INDEPENDENCE: the runtime recomputes per the contract; it does not import the
        assembler's code to verify the assembler's output)
    3. per domain: load_domain(..., expected_tokenized_hash=<manifest>) — anchors on-disk to manifest
    4. build one RuntimePackage per domain
    5. warm reboot complete = all manifest domains resident + hash-verified

There is no WF to traverse in a surface-only snapshot (e.g. platform); warm reboot proves the
substrate loads and verifies. Execution is a separate step against an already-booted universe.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.loader import RuntimePackage, load_domain


@dataclass(frozen=True)
class BootedSnapshot:
    """The resident, verified executable universe."""
    snapshot_id: str
    manifest: dict[str, Any]
    domains: dict[str, RuntimePackage]

    # The nine execution concerns, in flow order (shown when present in a domain).
    _CONCERNS = ("TI", "AC", "IN", "WF", "CC", "CT", "CS", "EV", "TE")
    _SYSTEM_NS = ("node_kind", "edge_kind", "outcome", "transition")

    def summary(self) -> str:
        from collections import Counter

        lines = [
            f"snapshot_id: {self.snapshot_id}",
            f"domains:     {len(self.domains)}",
        ]
        for name, pkg in self.domains.items():
            # Concern breakdown straight from the domain vocabulary (counts every declared artifact,
            # including AC_/EV_ which are not in the executable dispatch/handlers tables).
            counts: Counter = Counter()
            for fqdn in pkg.vocab.forward.values():
                if "::" not in fqdn:
                    continue
                ns, code = fqdn.split("::", 1)
                if ns in self._SYSTEM_NS:
                    continue
                counts[code.split("_")[0]] += 1
            concerns = " ".join(f"{counts[c]} {c}" for c in self._CONCERNS if counts.get(c))
            lines.append(f"  - {name}: {concerns or '(no execution concerns)'} · {len(pkg.vocab.forward)} addr")
        return "\n".join(lines)


def default_snapshot_root() -> Path:
    """PGC_SNAPSHOT_ROOT, or the sibling `../snapshot` of this repo (umbrella product dir)."""
    env = os.environ.get("PGC_SNAPSHOT_ROOT")
    if env:
        return Path(env)
    # runtime/boot.py → runtime/ (pkg) → protocol_runtime/ (repo) → protocol-governed-computing/
    return Path(__file__).resolve().parents[2] / "snapshot"


# --- composite hash: independent reimplementation of the assembly contract -----------------

def _identity_view(domains: list[dict]) -> list[dict]:
    view = [
        {
            "domain": d["domain"],
            "tokenized_projection_hash":  d["projections"]["tokenized"]["projection_hash"],
            "vocabulary_projection_hash": d["projections"]["vocabulary"]["projection_hash"],
            # Canonical joins the view because every other member is graph-derived, and STRUCTURE
            # artifacts never enter the semantic graph — without it a STRUCTURE artifact could
            # change inside a sealed snapshot and boot would still attest the same identity.
            "canonical_projection_hash":  d["projections"]["canonical"]["projection_hash"],
            "attestation_hash":           d["projections"]["trust"]["attestation_hash"],
            "graph_address_hash":         d["graph_address_hash"],
        }
        for d in domains
    ]
    return sorted(view, key=lambda e: e["domain"])


def _composite_hash(domains: list[dict]) -> str:
    canonical = json.dumps(_identity_view(domains), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_manifest(snapshot_root: Path) -> dict:
    path = snapshot_root / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"Assembled snapshot manifest missing: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def boot(snapshot_root: str | Path | None = None) -> BootedSnapshot:
    """Warm-boot the assembled snapshot: verify the manifest, load + anchor every domain."""
    root = Path(snapshot_root) if snapshot_root is not None else default_snapshot_root()
    manifest = _load_manifest(root)
    domains_meta = manifest.get("domains", [])

    # 2. domain-set integrity — recompute the composite and compare to the manifest's claim
    recomputed = _composite_hash(domains_meta)
    claimed = manifest.get("composite_hash")
    if recomputed != claimed:
        raise RuntimeError(
            f"Snapshot composite_hash mismatch: recomputed {recomputed!r} != manifest {claimed!r} "
            f"(domain-set tamper or stale manifest)."
        )

    # 3-4. per-domain load, anchored to the manifest's tokenized hash
    domains: dict[str, RuntimePackage] = {}
    for d in domains_meta:
        name = d["domain"]
        domains[name] = load_domain(
            root, name,
            expected_tokenized_hash=d["projections"]["tokenized"]["projection_hash"],
        )

    return BootedSnapshot(
        snapshot_id=manifest.get("snapshot_id", recomputed),
        manifest=manifest,
        domains=domains,
    )
