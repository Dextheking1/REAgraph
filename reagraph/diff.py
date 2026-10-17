"""Structural, byte-agnostic diff of two graphs.

The bug this fixes (REA #4520): diffing by byte offset reports a module as
`changed` when its bytes merely moved, so a real app-version diff can come back
"0 unchanged". Here, identity is the node id and the (source,target,relation)
edge key -- never a file location -- so a thing that only moved reads as
UNCHANGED, and only real structural change (added/removed nodes, rewired calls)
shows up.

Two identity regimes, because two kinds of graph flow through here:

  - graphify source graphs (and a graph re-folded from the SAME build) carry
    STABLE node ids, so the default id-keyed diff above is exactly right.
  - REA graphs of two different BUILDS do NOT: REA's node_id embeds a per-build
    hash and some identities are context-dependent (the same unchanged module can
    get a different identity scheme between builds), so an id-keyed diff reports
    nearly everything as changed -- the very #4520 shape, reintroduced by the id.
    For that, pass ``signature=`` (e.g. ``rea_signature``): nodes are matched by a
    build-stable signature -- kind + range-normalized label + source file -- with
    duplicates disambiguated by a stable ordinal, so a reformatted-but-identical
    build reads as UNCHANGED and only real movement shows up.
"""

from __future__ import annotations

import re

from .memory import edge_key

# A source span embedded in a label ("BrowserWindow@7:14-9:4"): "@"? line:col,
# optionally a range to a second line:col. Stripping it makes a label stable when
# code merely moves. Only an exact colon-separated numeric span is treated as a
# range, so a real name with digits ("base64") is never touched.
_LABEL_RANGE = re.compile(r"@?\d+:\d+(?:-\d+:\d+)?")


def normalize_label(label):
    """Strip embedded source line:col ranges from a label (for stable matching)."""
    if not isinstance(label, str):
        return label
    return _LABEL_RANGE.sub("", label).strip()


def rea_signature(node: dict) -> str:
    """A build-stable signature for a REA node: kind + normalized label + source file.

    REA node_ids are not stable across builds; this signature is, because it is
    built from what the node MEANS (its kind, its symbol/label with any source
    range normalized out, and the file it lives in) rather than how this build
    happened to hash it. Used as ``diff(a, b, signature=rea_signature)`` to diff
    two REA builds without the per-build id churn swamping the result.
    """
    kind = node.get("rea_kind") or node.get("kind") or ""
    label = normalize_label(node.get("label") or "")
    source = node.get("source_file") or ""
    return f"{kind}|{label}|{source}"


def _remap_by_signature(graph: dict, signature) -> dict:
    """Return a shallow graph copy whose node ids (and edge endpoints) are replaced
    by ``signature(node)`` plus a stable per-signature ordinal.

    The ordinal (#0, #1, ...) disambiguates several nodes that share a signature
    (e.g. two `https` references in one file) so matching is MULTISET-correct: two
    builds with the same count of a signature match all of them; a build with one
    more gets exactly one `added`. Ordinals are assigned in node order; since
    same-signature nodes are interchangeable by construction, order does not affect
    the node-level result.
    """
    counts: dict = {}
    id_map: dict = {}
    for n in graph.get("nodes", []) or []:
        if "id" not in n:
            continue
        sig = signature(n)
        idx = counts.get(sig, 0)
        counts[sig] = idx + 1
        id_map[n["id"]] = f"{sig}#{idx}"

    nodes = []
    for n in graph.get("nodes", []) or []:
        if "id" not in n:
            continue
        m = dict(n)
        m["id"] = id_map[n["id"]]
        nodes.append(m)

    links = []
    for e in graph.get("links", []) or graph.get("edges", []) or []:
        m = dict(e)
        for key in ("source", "target", "source_node_id", "target_node_id"):
            if key in m and m[key] in id_map:
                m[key] = id_map[m[key]]
        links.append(m)

    return {"nodes": nodes, "links": links}


def _edge_set(graph: dict) -> set:
    return {edge_key(e) for e in graph.get("links", []) or graph.get("edges", [])}


def diff(a: dict, b: dict, *, signature=None) -> dict:
    """Structural diff of two graphs. By default nodes are keyed by id (correct for
    stable-id graphs); pass ``signature`` (e.g. ``rea_signature``) to match nodes by
    a build-stable signature instead, for diffing two REA builds. Return shape is
    identical either way (in signature mode the reported ids are signature keys)."""
    if signature is not None:
        a = _remap_by_signature(a, signature)
        b = _remap_by_signature(b, signature)

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
