"""
trace_viz.py — Evidence projection: a run drawn over its workflow, with why it went where it went.

A client of the snapshot inspector, never a second reader of the trace. It asks two questions of
`inspector.api.query` and draws the answers:

    si.behavior_logic.show   the workflow's declared graph (every node and edge)
    si.execution.explain     the run: the path, each route's outcome, each node's determination
                             as far as the trace records it, captured inputs, how the run ended
    ──────────────────────
    → <trace_id>.png beside the trace

It derives nothing. The inspector resolves the workflow's domain, ties the trace to the snapshot
(and refuses a trace produced under another), and separates what the trace recorded from what it
joined from the snapshot. This module only chooses how each answer looks:

- **recorded** (from the trace) is drawn in red: the path, the outcome on each route taken, a
  gate's failed checks, a node's recorded outcome, errors, captured inputs;
- **joined** (from the snapshot) is drawn in grey: each node's declared capability;
- **declared but not taken** is drawn pale, as before;
- a route the run took that the graph does not declare is drawn dashed.

The inspector is imported here and nowhere else in the runtime: execution never depends on it,
and only this optional projection does (`pgc-runtime[render]`).

Uses graphviz (dot) — returns None if dot is not available.
"""

from __future__ import annotations

import subprocess
from html import escape
from pathlib import Path
from typing import Any, Optional


class ExplanationRefused(ValueError):
    """The inspector would not explain this trace against this snapshot. `str(exc)` is its reason."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_trace_png(
    snapshot_root: Path,
    trace_path: Path,
    trace_root: Path | None = None,
) -> Optional[Path]:
    """
    Render the explained run over its workflow graph, beside the trace.

    Args:
        snapshot_root: The assembled snapshot the trace names.
        trace_path:    The completed .jsonl trace.
        trace_root:    The root the trace is named under — the run's data root. Defaults to the
                       trace's own directory, which names the same file.

    Returns:
        Path to the PNG, or None if graphviz is unavailable.

    Raises:
        FileNotFoundError:   trace_path does not exist.
        ExplanationRefused:  the inspector refused the trace or has no graph for its workflow.
    """
    from inspector.api import query   # optional projection; execution never imports this

    trace_path = Path(trace_path).resolve()
    if not trace_path.is_file():
        raise FileNotFoundError(f"Trace file not found: {trace_path}")
    root = Path(trace_root).resolve() if trace_root is not None else trace_path.parent
    reference = trace_path.relative_to(root).as_posix()

    status, explanation = query("si.execution.explain", {"trace": reference}, snapshot_root,
                                trace_root=root)
    if status != "SUCCESS":
        raise ExplanationRefused(explanation.get("reason", status))
    status, logic = query("si.behavior_logic.show", {"wf": explanation["wf"]}, snapshot_root)
    if status != "SUCCESS":
        raise ExplanationRefused(logic.get("reason", status))

    dot_content = _generate_dot(logic["graph"], explanation)

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
# What the explanation says, reduced to what is drawn
# ---------------------------------------------------------------------------

def _taken(explanation: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The path as (from_node, outcome, to_node), in order — read from the explanation's visits.

    A route that reached neither routing nor an ending has no `to`, and the path stops at the node
    that produced it. A visit with no node key means the trace predates node keys: refused, because
    the path cannot be placed on the graph without guessing.
    """
    path: list[tuple[str, str, str]] = []
    for visit in explanation.get("visits", []):
        route = visit.get("route")
        if route is None:
            continue
        if not visit.get("node"):
            raise ExplanationRefused("the trace's routes carry no node keys — it predates them; re-run it")
        if route.get("to") is None:
            break
        path.append((visit["node"], route.get("outcome"), route["to"]))
    return path


def _recorded_lines(visit: dict[str, Any]) -> list[str]:
    """What the trace recorded about why this node decided as it did, as label lines."""
    lines: list[str] = []
    determination = visit.get("determination") or {}
    basis = determination.get("basis")
    if basis == "recorded admission checks":
        failed = determination.get("failed") or []
        if failed:
            lines += [f"✗ {c['field']}: {c['rule']} {c['expected']}" for c in failed]
        else:
            lines.append(f"✓ {len(determination.get('checks') or [])} check(s) held")
    elif basis == "capability outcome":
        lines.append(f"→ {determination.get('outcome')}")
    elif basis == "not recorded":
        lines.append("checks not recorded")
    for atom in visit.get("atoms") or []:
        if atom.get("captured"):
            name = (atom.get("atom") or "").split("::")[-1]
            lines.append(f"captured: {name}{' (replayed)' if atom.get('replayed') else ''}")
    for error in visit.get("errors") or []:
        lines.append(f"ERROR: {error.get('message')}")
    return lines


# ---------------------------------------------------------------------------
# DOT generation
# ---------------------------------------------------------------------------

_SHAPES = {"IN": ("ellipse", "lightblue"), "CC": ("box", "lightgreen"),
           "EXIT": ("ellipse", "lightcoral")}


def _label(node_id: str, joined: str | None, recorded: list[str]) -> str:
    """An HTML-like label: the node, then what was joined (grey), then what was recorded (red)."""
    rows = [f"<B>{escape(node_id)}</B>"]
    if joined:
        rows.append(f'<FONT POINT-SIZE="10" COLOR="gray35"><I>{escape(joined)}</I></FONT>')
    rows += [f'<FONT POINT-SIZE="10" COLOR="red3">{escape(line)}</FONT>' for line in recorded]
    return "<" + "<BR/>".join(rows) + ">"


def _generate_dot(graph: dict[str, Any], explanation: dict[str, Any]) -> str:
    """
    The declared graph, with the explained run drawn over it.

    Visited nodes: red border and fill, with what the trace recorded about the node.
    Taken edges:   red, bold, labelled with the outcome that selected them.
    Undeclared:    a route the run took that the graph does not declare — dashed red.
    The deciding node of a declared ending carries a double border.
    """
    path = _taken(explanation)
    visits = {v["node"]: v for v in explanation.get("visits", []) if v.get("node")}
    taken_edges = {(f, t, o) for f, o, t in path}
    ending = explanation.get("ending") or {}
    decided_at = ending.get("decided_at") or ending.get("at")

    tie = (explanation.get("tie") or {}).get("snapshot_id", "")
    title = (f"{explanation.get('wf')}  ·  {explanation.get('status')}  ·  "
             f"{ending.get('kind', '')}\\nsnapshot {tie[:12]} (claimed by the trace)")
    lines = [
        f'digraph "{graph["wf_id"]}" {{',
        "  rankdir=LR;",
        f'  label="{title}"; labelloc=t; fontname="Arial";',
        '  node [fontname="Arial"];',
        "",
    ]

    declared_ids = set()
    for node in graph["nodes"]:
        node_id = node["id"]
        declared_ids.add(node_id)
        shape, fill = _SHAPES.get(node["type"], ("box", "white"))
        visit = visits.get(node_id)
        capability = node.get("capability")
        joined = capability.split("::")[-1] if capability else None
        if visit is None:
            lines.append(f'  "{node_id}" [label={_label(node_id, joined, [])}, shape={shape},'
                         f" style=filled, fillcolor={fill}, color=gray];")
            continue
        extra = ", peripheries=2" if node_id == decided_at else ""
        style = "filled,dashed" if visit.get("errors") else "filled"
        lines.append(f'  "{node_id}" [label={_label(node_id, joined, _recorded_lines(visit))},'
                     f' shape={shape}, style="{style}", fillcolor=mistyrose, color=red,'
                     f" penwidth=2.5{extra}];")

    # A place the run reached that the graph does not declare is still drawn: it was recorded.
    for node_id, visit in visits.items():
        if node_id not in declared_ids:
            lines.append(f'  "{node_id}" [label={_label(node_id, None, _recorded_lines(visit))},'
                         f' shape=box, style="filled,dashed", fillcolor=mistyrose, color=red];')

    lines.append("")

    declared_edges = set()
    for edge in graph["edges"]:
        from_id, to_id, condition = edge["from"], edge["to"], edge["condition"]
        declared_edges.add((from_id, to_id, condition))
        if (from_id, to_id, condition) in taken_edges:
            lines.append(f'  "{from_id}" -> "{to_id}"'
                         f' [label="{condition}", color=red, penwidth=2.5, fontcolor=red];')
        else:
            lines.append(f'  "{from_id}" -> "{to_id}"'
                         f' [label="{condition}", color=gray, fontcolor=gray];')

    for from_id, to_id, condition in sorted(taken_edges - declared_edges):
        lines.append(f'  "{from_id}" -> "{to_id}"'
                     f' [label="{condition} (undeclared)", color=red, style=dashed, fontcolor=red];')

    lines.append("}")
    return "\n".join(lines)
