"""
cli.py — Token-native CLI entry point for the runtime.

Commands:
    run           — Execute a workflow against the tokenized snapshot.
    examine       — Analyze a completed trace file and print a diagnostic report.
    behavior-logic — Render execution-path PNG from a completed trace file.

Execution path (run):
    1. Load tokenized snapshot for the domain via loader.load_domain()
       — verifies topology hash against trust attestation; fails hard on mismatch
    2. Generate deterministic trace ID from (domain, wf_fqdn, payload)
    3. Open TraceWriter at traces/<domain>/<wf_code>/<trace_id>/
    4. Drive workflow topology via scheduler.run_wf()
    5. Print result summary; exit 1 on non-SUCCESS
    6. If --behavior-logic: invoke evidence projection (trace_viz) to render PNG

All runtime behavior comes from the compiled tokenized_snapshot.
The CLI does not implement any domain logic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from runtime.api import run_workflow
from runtime.boot import boot, default_snapshot_root


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protocol_runtime",
        description="PGC token-native workflow runtime — warm-boots and executes an assembled snapshot",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    # ── run ──────────────────────────────────────────────────────
    run_p = subs.add_parser("run", help="Execute a workflow")

    run_p.add_argument(
        "--wf",
        required=True,
        metavar="FQDN",
        help="Workflow FQDN (e.g. blockchain::WF_REGISTER_ACTOR_UNVERIFIED_V0)",
    )
    run_p.add_argument(
        "--payload",
        metavar="FILE",
        help="Path to JSON payload file (omit for empty payload)",
    )
    run_p.add_argument(
        "--data-root",
        dest="data_root",
        metavar="PATH",
        help="Absolute instance root for CS state + traces (or set PGC_DATA_ROOT)",
    )
    run_p.add_argument(
        "--snapshot",
        dest="snapshot",
        metavar="PATH",
        help="Assembled snapshot root (or set PGC_SNAPSHOT_ROOT); manifest.json lives here. "
             "Default: sibling ../snapshot",
    )

    # ── run: optional behavior-logic flag ────────────────────────
    run_p.add_argument(
        "--behavior-logic",
        action="store_true",
        dest="behavior_logic",
        help="Render execution-path PNG after run (requires graphviz)",
    )

    # ── boot ──────────────────────────────────────────────────────
    boot_p = subs.add_parser(
        "boot",
        help="Warm-boot the assembled snapshot (load + hash-verify all manifest domains)",
    )
    boot_p.add_argument(
        "--snapshot",
        dest="snapshot",
        metavar="PATH",
        help="Assembled snapshot root (or set PGC_SNAPSHOT_ROOT); default: sibling ../snapshot",
    )

    # ── examine ───────────────────────────────────────────────────
    ex_p = subs.add_parser("examine", help="Analyze a completed trace file")
    ex_p.add_argument(
        "trace_file",
        metavar="FILE",
        help="Path to a completed .jsonl trace file",
    )

    # ── behavior-logic ────────────────────────────────────────────
    bl_p = subs.add_parser(
        "behavior-logic",
        help="Render execution-path PNG from a completed trace file",
    )
    bl_p.add_argument(
        "trace_file",
        metavar="FILE",
        help="Path to a completed .jsonl trace file",
    )
    bl_p.add_argument(
        "--workspace",
        metavar="PATH",
        help="Absolute path to pgs_workspace root (or set PGS_WORKSPACE)",
    )

    return parser


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_run(args: argparse.Namespace) -> None:
    wf_fqdn = args.wf

    # Extract domain from FQDN (part before ::)
    if "::" not in wf_fqdn:
        _fatal(f"Invalid WF FQDN (expected <domain>::<CODE>): {wf_fqdn!r}")
    domain = wf_fqdn.split("::")[0]

    # Resolve paths from args or environment
    data_root_str = args.data_root or os.environ.get("PGC_DATA_ROOT")
    if not data_root_str:
        _fatal("--data-root PATH or PGC_DATA_ROOT is required (instance root for CS state + traces)")
    data_root = Path(data_root_str)
    if not data_root.is_absolute():
        _fatal(f"--data-root must be an absolute path, got: {data_root_str}")

    # Snapshot root: arg or env, else the default sibling ../snapshot (resolved by boot).
    snapshot_str = args.snapshot or os.environ.get("PGC_SNAPSHOT_ROOT")
    snapshot_root = Path(snapshot_str) if snapshot_str else default_snapshot_root()
    if not snapshot_root.is_absolute():
        _fatal(f"--snapshot must be an absolute path, got: {snapshot_str}")

    # Load payload
    payload = _load_payload(args.payload)

    # Drive the workflow via the programmatic API. The API warm-boots the snapshot (manifest root
    # of trust), opens the trace under the instance root, and returns status + surface.
    print(f"[runtime] Booting snapshot for {domain}...")
    t0 = time.monotonic()
    try:
        run = run_workflow(wf_fqdn=wf_fqdn, payload=payload,
                           data_root=str(data_root), snapshot_root=snapshot_root)
    except KeyError as exc:
        _fatal(f"WF FQDN not in vocab: {exc}")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fatal(str(exc))
    except Exception as exc:
        _fatal(f"Runtime error: {exc}")

    result_status, surface = run.status, run.surface
    trace_id, trace_dir = run.trace_id, run.trace_dir
    duration_ms = int((time.monotonic() - t0) * 1000)

    print(f"[runtime] Workflow:  {wf_fqdn}")
    print(f"[runtime] Trace ID:  {trace_id}")
    print(f"[runtime] Trace dir: {trace_dir}")
    print()

    # Evidence projection: render execution-path PNG if requested
    trace_path = trace_dir / f"{trace_id}.jsonl"
    png_path = None
    if args.behavior_logic:
        png_path = _render_behavior_logic(snapshot_root, trace_path)

    print("=" * 60)
    print("[runtime] Workflow Complete")
    print("=" * 60)
    print(f"Workflow:   {wf_fqdn}")
    print(f"Status:     {result_status}")
    print(f"Trace ID:   {trace_id}")
    print(f"Duration:   {duration_ms}ms")
    if surface:
        print("Output:")
        print(_format_surface(surface))
    if png_path:
        print(f"Graph:      {png_path}")
    elif args.behavior_logic:
        print("Graph:      (graphviz not available — PNG skipped)")
    print("=" * 60)

    if result_status not in ("SUCCESS", "ALREADY_EXISTS"):
        sys.exit(1)


def _handle_boot(args: argparse.Namespace) -> None:
    snapshot_str = args.snapshot or os.environ.get("PGC_SNAPSHOT_ROOT")
    snapshot_root = Path(snapshot_str) if snapshot_str else default_snapshot_root()

    print(f"[runtime] Warm-booting assembled snapshot: {snapshot_root}")
    try:
        booted = boot(snapshot_root)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fatal(str(exc))
    except Exception as exc:
        _fatal(f"Boot error: {exc}")

    print("=" * 60)
    print("[runtime] Warm reboot complete — snapshot resident + hash-verified")
    print("=" * 60)
    print(booted.summary())
    print("=" * 60)
    print(_health_line(snapshot_root, booted))
    print("=" * 60)


def _health_line(snapshot_root: Path, booted) -> str:
    """Consolidated completion attestation: what warm boot verified, in one line.

    Reports only checks that actually ran: every manifest domain was made resident and its hashes
    verified against the root of trust. Governance provenance is surfaced where a domain carries it
    (bound at compile, verified at assembly) so the health line reflects the full trust chain.

    A governance surface imports no governance — it *is* the governance other domains compile
    against, so it carries no `imported_governance` and is not counted as unbound. Counting it as
    such reported a permanent shortfall (`4/5`) on a healthy snapshot and invited the same
    investigation on every boot. The surface is derived from what the other attestations name, not
    hardcoded, so a composition with a differently-named surface reports correctly too.
    """
    import json

    n = len(booted.domains)
    bound: set[str] = set()
    surfaces: set[str] = set()
    for name in booted.domains:
        att = snapshot_root / "trust" / name / "structure_attestation.json"
        try:
            imported = json.loads(att.read_text(encoding="utf-8")).get("imported_governance")
        except (OSError, ValueError):
            continue
        if imported:
            bound.add(name)
            source = imported.get("import_domain")
            if source:
                surfaces.add(source)

    if not bound:
        return f"[runtime] ✓ Snapshot healthy — {n} domain(s) resident and hash-verified. No issues."

    importing = [d for d in booted.domains if d not in surfaces]
    unbound = sorted(d for d in importing if d not in bound)
    surface_note = f", {'/'.join(sorted(surfaces))} is the governance surface" if surfaces else ""

    if unbound:
        gov = (
            f"; governance provenance bound for {len(bound)}/{len(importing)} importing domain(s)"
            f"{surface_note} — UNBOUND: {', '.join(unbound)}"
        )
        return f"[runtime] ✓ Snapshot healthy — {n} domain(s) resident and hash-verified{gov}."

    gov = (
        f"; governance provenance bound for all {len(importing)} importing domain(s)"
        f"{surface_note}"
    )
    return f"[runtime] ✓ Snapshot healthy — {n} domain(s) resident and hash-verified{gov}. No issues."


def _handle_behavior_logic(args: argparse.Namespace) -> None:
    trace_path = Path(args.trace_file)
    if not trace_path.exists():
        _fatal(f"Trace file not found: {args.trace_file}")

    workspace_str = args.workspace or os.environ.get("PGS_WORKSPACE")
    if not workspace_str:
        _fatal("--workspace PATH or PGS_WORKSPACE is required")
    workspace = Path(workspace_str)
    if not workspace.is_absolute():
        _fatal(f"--workspace must be an absolute path, got: {workspace_str}")

    png_path = _render_behavior_logic(workspace, trace_path)
    if png_path:
        print(f"[runtime] Execution path PNG: {png_path}")
    else:
        print(
            "[runtime] Behavior logic render skipped — graphviz (dot) not available.",
            file=sys.stderr,
        )
        sys.exit(1)


def _handle_examine(args: argparse.Namespace) -> None:
    trace_path = Path(args.trace_file)
    if not trace_path.exists():
        _fatal(f"Trace file not found: {args.trace_file}")

    # Delegate to the examine module (reads JSONL trace format)
    try:
        from runtime.examine import analyze, TraceParseError
    except ImportError:
        _fatal(
            "Trace examiner unavailable — runtime may not be fully installed.\n"
            "  Re-install with: pip install -e /path/to/protocol_runtime"
        )

    try:
        report = analyze(trace_path)
    except Exception as exc:
        _fatal(f"Trace parse error: {exc}")

    print(report.format())

    if report.has_structural_failure:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _render_behavior_logic(workspace: Path, trace_path: Path) -> "Path | None":
    """Invoke evidence projection to render execution-path PNG. Best-effort."""
    from runtime.trace_viz import render_trace_png
    try:
        return render_trace_png(workspace, trace_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[runtime] Behavior logic render error: {exc}", file=sys.stderr)
        return None


def _load_payload(payload_path: str | None) -> dict:
    if not payload_path:
        return {}
    path = Path(payload_path)
    if not path.exists():
        _fatal(f"Payload file not found: {payload_path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fatal(f"Payload file is not valid JSON: {exc}")


def _format_surface(surface: dict) -> str:
    """Readable, domain-agnostic rendering of a WF result surface.

    Top-level keys each on their own line; a nested dict value (e.g. per-seed sequences) expands one
    level so each entry lands on its own line with a compact value. Purely presentational — no
    knowledge of any specific workflow.
    """
    lines: list[str] = []
    for key, value in surface.items():
        if isinstance(value, dict) and value:
            lines.append(f"  {key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"    {sub_key}: {json.dumps(sub_value, separators=(',', ':'))}")
        else:
            lines.append(f"  {key}: {json.dumps(value, separators=(',', ':'))}")
    return "\n".join(lines)


def _fatal(message: str) -> None:
    print(f"[runtime] Error: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        _handle_run(args)
    elif args.command == "boot":
        _handle_boot(args)
    elif args.command == "examine":
        _handle_examine(args)
    elif args.command == "behavior-logic":
        _handle_behavior_logic(args)


if __name__ == "__main__":
    main()
