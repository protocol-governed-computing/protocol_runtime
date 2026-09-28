"""
trace_viz.py — Evidence projection: execution path behavior logic.

Reads a completed trace (.jsonl) and the compiled workflow graph (.graph.json)
from the protocol snapshot behavior_logic directory, then generates a PNG with
the actual execution path overlaid in red on the static compiled graph.

This is Evidence Projection — not a runtime execution feature:

    topology (graph.json)
    +
    evidence (trace.jsonl)
    ───────────────────────
    → execution path PNG

Inputs (both already materialized, read-only):
    protocol_snapshot/behavior_logic/<WF_CODE>/<WF_CODE>.graph.json
    traces/<domain>/<wf_code>/<trace_id>/<trace_id>.jsonl

Output:
    traces/<domain>/<wf_code>/<trace_id>/<trace_id>.png

Architectural invariant:
    This module reads ONLY from:
      - protocol_snapshot/behavior_logic/  (compiled graph artifacts)
      - the caller-supplied trace .jsonl   (execution evidence)
    It does NOT walk protocol_snapshot/artifacts/ or any other canonical
    protocol location. Behavior logic overlay is read-only — no protocol interpretation.

Uses graphviz (dot) — returns None silently if dot is not available.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_trace_png(
    snapshot_root: Path,
    trace_path: Path,
) -> Optional[Path]:
    """
    Generate execution-path overlay PNG for a completed trace.

    Reads CC_COMPLETE events from trace_path to reconstruct the actual
    execution path, then overlays it (red) on the compiled workflow graph.

    Args:
        snapshot_root: The assembled snapshot the trace was executed against.
        trace_path:  Path to the completed .jsonl trace file.

    Returns:
        Path to the generated PNG, or None if graphviz is unavailable.

    Raises:
        FileNotFoundError: trace_path or graph.json does not exist.
        ValueError:        Trace is empty or missing WF_START event.
    """
    if not trace_path.exists():
        raise FileNotFoundError(f"Trace file not found: {trace_path}")

    # Parse trace events
    events: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    if not events:
        raise ValueError(f"Trace file is empty: {trace_path}")

    # Extract WF code from WF_START event
    wf_start = next((e for e in events if e["event_type"] == "WF_START"), None)
    if wf_start is None:
        raise ValueError(f"No WF_START event found in: {trace_path}")

    wf_fqdn = wf_start["detail"]["wf_fqdn"]
    wf_code = wf_fqdn.split("::")[-1]  # e.g. "WF_REGISTER_ACTOR_UNVERIFIED_V0"

    # Load compiled graph from protocol_snapshot/behavior_logic/
    # The assembled snapshot publishes each workflow's graph under its domain.
    domain = wf_fqdn.split("::")[0]
    graph_path = snapshot_root / "behavior_logic" / domain / wf_code / f"{wf_code}.graph.json"
    if not graph_path.exists():
        raise FileNotFoundError(
            f"Compiled graph not found: {graph_path}\n"
            f"Is this the snapshot the trace was executed against?"
        )

    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    # Reconstruct actual execution path from trace events + graph edges
    path = _extract_execution_path(events, graph)

    # Build visited node and taken edge sets
    visited_nodes: set[str] = set()
    taken_edges: set[tuple[str, str, str]] = set()  # (from_node, to_node, condition)
    for from_node, condition, to_node in path:
        visited_nodes.add(from_node)
        visited_nodes.add(to_node)
        taken_edges.add((from_node, to_node, condition))

    # Generate DOT source with execution-path overlay
    dot_content = _generate_dot(graph, visited_nodes, taken_edges)

    # Render: write DOT, invoke graphviz, clean up DOT
    png_path = trace_path.with_suffix(".png")
    dot_path = trace_path.with_suffix(".dot")
    dot_path.write_text(dot_content, encoding="utf-8")

    try:
        subprocess.run(
            ["dot", "-Tpng", str(dot_path), "-o", str(png_path)],
            check=True,
            capture_output=True,
        )
        dot_path.unlink()
        return png_path
    except (subprocess.CalledProcessError, FileNotFoundError):
        dot_path.unlink(missing_ok=True)
        return None


# ---------------------------------------------------------------------------
# Path reconstruction
# ---------------------------------------------------------------------------

def _extract_execution_path(
    events: list[dict],
    graph: dict,
) -> list[tuple[str, str, str]]:
    """
    The run's path, [(from_node, condition, to_node), ...], as the trace records it.

    Every routing determination is a `WF_ROUTE` event naming the node it left, the outcome, and the
    node or declared ending it reached. The path is read from those, not inferred: an earlier walk
    matched `CC_COMPLETE` events to nodes by contract code, and lost the path at the first place
    whose key is not its contract's code. A route that reached neither routing nor an ending has no
    `to_node`, and the path stops at the node that produced it.

    Args:
        events: Parsed JSONL trace events.
        graph:  Compiled graph dict (from graph.json) — unused for the path, kept for the signature.
    """
    path: list[tuple[str, str, str]] = []
    for event in events:
        if event.get("event_type") != "WF_ROUTE":
            continue
        detail = event.get("detail") or {}
        if "from_node" not in detail:
            raise ValueError("WF_ROUTE carries no node keys — the trace predates them; re-run it")
        if detail.get("to_node") is None:
            break
        path.append((detail["from_node"], event.get("result_status"), detail["to_node"]))
    return path


# ---------------------------------------------------------------------------
# DOT generation
# ---------------------------------------------------------------------------

def _generate_dot(
    graph: dict,
    visited_nodes: set[str],
    taken_edges: set[tuple[str, str, str]],
) -> str:
    """
    Generate Graphviz DOT with actual execution path highlighted in red.

    Visited nodes:  red border + solid red fill.
    Taken edges:    red, bold, red label.
    Unvisited:      standard fill, grey border, grey edges.
    """
    lines = [
        f'digraph "{graph["wf_id"]}" {{',
        "  rankdir=LR;",
        '  node [fontname="Arial"];',
        "",
    ]

    # --- Nodes ---
    for node in graph["nodes"]:
        node_id = node["id"]
        node_type = node["type"]
        visited = node_id in visited_nodes

        if node_type == "IN":
            fill = "tomato" if visited else "lightblue"
            shape = "ellipse"
        elif node_type == "CC":
            fill = "tomato" if visited else "lightgreen"
            shape = "box"
        elif node_type == "EXIT":
            fill = "tomato" if visited else "lightcoral"
            shape = "ellipse"
        else:
            fill = "white"
            shape = "box"

        if visited:
            lines.append(
                f'  "{node_id}" [label="{node_id}", shape={shape},'
                f" style=filled, fillcolor={fill}, color=red, penwidth=2.5];"
            )
        else:
            lines.append(
                f'  "{node_id}" [label="{node_id}", shape={shape},'
                f" style=filled, fillcolor={fill}, color=gray];"
            )

    lines.append("")

    # --- Edges ---
    for edge in graph["edges"]:
        from_id = edge["from"]
        to_id = edge["to"]
        condition = edge["condition"]
        taken = (from_id, to_id, condition) in taken_edges

        if taken:
            lines.append(
                f'  "{from_id}" -> "{to_id}"'
                f' [label="{condition}", color=red, penwidth=2.5, fontcolor=red];'
            )
        else:
            lines.append(
                f'  "{from_id}" -> "{to_id}"'
                f' [label="{condition}", color=gray, fontcolor=gray];'
            )

    lines.append("}")
    return "\n".join(lines)
