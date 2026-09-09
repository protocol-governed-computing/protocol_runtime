"""
boot.py — warm reboot: bring the assembled snapshot resident and hash-verified.

The manifest is the ROOT OF TRUST. The runtime boots *through* it, never by scanning the
filesystem. Contract: snapshot_assembler/CONTRACT.md

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
    snapshot_root: Path
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

    # 2. ACCEPTANCE — all four conditions of `3b` §7, established from content.
    #
    # The runtime does not carry its own weaker copy of this. It previously recomputed the composite
    # over the manifest's RECORDED per-domain hashes, which detects a tampered manifest and not a
    # tampered constituent — and a snapshot with an edited projection booted and reported healthy.
    # `assembler.core.verify_snapshot` recomputes every constituent from its bytes; there is one
    # acceptance determination and both the assembler and the runtime reach it.
    #
    # Importing it is not a layering breach: acceptance is a determination ABOUT a snapshot (`3b` §7),
    # not part of assembling one, and a second implementation of one determination is two things that
    # can disagree.
    from assembler.core import AssemblyError, verify_snapshot
    try:
        verify_snapshot(root)
    except AssemblyError as exc:
        raise RuntimeError(f"Snapshot refused at acceptance: {exc}") from exc
    recomputed = manifest.get("snapshot_id")

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
        snapshot_root=root,
        manifest=manifest,
        domains=domains,
    )
