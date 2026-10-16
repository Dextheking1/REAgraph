"""Structural, byte-agnostic diff of two graphs.

The bug this fixes (REA #4520): diffing by byte offset reports a module as
`changed` when its bytes merely moved, so a real app-version diff can come back
"0 unchanged". Here, identity is the node id and the (source,target,relation)
edge key -- never a file location -- so a thing that only moved reads as
UNCHANGED, and only real structural change (added/removed nodes, rewired calls)
shows up.
"""

from __future__ import annotations

from .memory import edge_key


def _edge_set(graph: dict) -> set:
    return {edge_key(e) for e in graph.get("links", []) or graph.get("edges", [])}


def diff(a: dict, b: dict) -> dict:
    a_nodes = {n["id"] for n in a.get("nodes", []) if "id" in n}
    b_nodes = {n["id"] for n in b.get("nodes", []) if "id" in n}

    a_edges = _edge_set(a)
    b_edges = _edge_set(b)

    nodes_added = sorted(b_nodes - a_nodes)
    nodes_removed = sorted(a_nodes - b_nodes)

    edges_added = sorted(b_edges - a_edges)
    edges_removed = sorted(a_edges - b_edges)

    # A common node is "rewired" if any incident edge appeared or disappeared.
    touched = {ek[0] for ek in edges_added} | {ek[1] for ek in edges_added}
    touched |= {ek[0] for ek in edges_removed} | {ek[1] for ek in edges_removed}
    rewired = sorted((touched & a_nodes & b_nodes))

    unchanged = sorted((a_nodes & b_nodes) - set(rewired))

    return {
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "edges_added": [list(e) for e in edges_added],
        "edges_removed": [list(e) for e in edges_removed],
        "rewired_nodes": rewired,
        "unchanged_nodes": unchanged,
        "summary": {
            "nodes_added": len(nodes_added),
            "nodes_removed": len(nodes_removed),
            "edges_added": len(edges_added),
            "edges_removed": len(edges_removed),
            "rewired_nodes": len(rewired),
            "unchanged_nodes": len(unchanged),
        },
    }


def format_report(report: dict) -> str:
    s = report["summary"]
    lines = [
        "Structural diff (byte-agnostic):",
        f"  unchanged nodes : {s['unchanged_nodes']}",
        f"  added nodes     : {s['nodes_added']}",
        f"  removed nodes   : {s['nodes_removed']}",
        f"  rewired nodes   : {s['rewired_nodes']}",
        f"  edges +{s['edges_added']} / -{s['edges_removed']}",
    ]
    for nid in report["nodes_added"][:20]:
        lines.append(f"    + {nid}")
    for nid in report["nodes_removed"][:20]:
        lines.append(f"    - {nid}")
    return "\n".join(lines)
