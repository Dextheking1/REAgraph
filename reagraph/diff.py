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


# Node kinds whose SOURCE FILE is not a stable part of their identity, because the
# entity legitimately spans more than one file and REA attributes it to one or the
# other from run to run. An Electron IPC channel is the canonical case: the same
# channel is `ipcMain.handle(...)` in the main process and `ipcRenderer.invoke(...)`
# in the preload, and REA may file the channel node under either. Its NAME (the
# channel string) is the stable identity, so these kinds are keyed on kind+label
# only -- including source_file would make an untouched channel flip add/remove.
_LOCATION_UNSTABLE_KINDS = frozenset({"ipc-channel"})


def rea_signature(node: dict) -> str:
    """A build-stable signature for a REA node: kind + normalized label (+ source file).

    REA node_ids are not stable across builds; this signature is, because it is
    built from what the node MEANS (its kind, its symbol/label with any source
    range normalized out, and -- for file-local entities -- the file it lives in)
    rather than how this build happened to hash it. For kinds that legitimately
    span files and whose source attribution drifts between runs
    (``_LOCATION_UNSTABLE_KINDS``, e.g. an IPC channel), the source file is left
    OUT so an untouched entity is not reported as moved. Used as
    ``diff(a, b, signature=rea_signature)`` to diff two REA builds without the
    per-build id churn (or that attribution drift) swamping the result.
    """
    kind = node.get("rea_kind") or node.get("kind") or ""
    label = normalize_label(node.get("label") or "")
    if kind in _LOCATION_UNSTABLE_KINDS:
        return f"{kind}|{label}"
    source = node.get("source_file") or ""
    return f"{kind}|{label}|{source}"


def _nodes_list(graph) -> list:
    """Fail-open: the node list of a graph, or [] for a non-dict graph / non-list
    nodes. Non-dict node entries are left for callers to skip."""
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes", [])
    return nodes if isinstance(nodes, list) else []


def _links_list(graph) -> list:
    """Fail-open: the link list of a graph (tolerating the ``edges`` spelling), or
    [] for a non-dict graph / non-list links."""
    if not isinstance(graph, dict):
        return []
    links = graph.get("links", []) or graph.get("edges", [])
    return links if isinstance(links, list) else []


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
    for n in _nodes_list(graph):
        if not isinstance(n, dict) or "id" not in n:
            continue
        sig = signature(n)
        idx = counts.get(sig, 0)
        counts[sig] = idx + 1
        id_map[n["id"]] = f"{sig}#{idx}"

    nodes = []
    for n in _nodes_list(graph):
        if not isinstance(n, dict) or "id" not in n:
            continue
        m = dict(n)
        m["id"] = id_map[n["id"]]
        nodes.append(m)

    links = []
    for e in _links_list(graph):
        if not isinstance(e, dict):
            continue
        m = dict(e)
        for key in ("source", "target", "source_node_id", "target_node_id"):
            if key in m and m[key] in id_map:
                m[key] = id_map[m[key]]
        links.append(m)

    return {"nodes": nodes, "links": links}


def _edge_set(graph: dict) -> set:
    return {edge_key(e) for e in _links_list(graph) if isinstance(e, dict)}


def diff(a: dict, b: dict, *, signature=None) -> dict:
    """Structural diff of two graphs. By default nodes are keyed by id (correct for
    stable-id graphs); pass ``signature`` (e.g. ``rea_signature``) to match nodes by
    a build-stable signature instead, for diffing two REA builds. Return shape is
    identical either way (in signature mode the reported ids are signature keys)."""
    if signature is not None:
        a = _remap_by_signature(a, signature)
        b = _remap_by_signature(b, signature)

    a_nodes = {n["id"] for n in _nodes_list(a) if isinstance(n, dict) and "id" in n}
    b_nodes = {n["id"] for n in _nodes_list(b) if isinstance(n, dict) and "id" in n}

    a_edges = _edge_set(a)
    b_edges = _edge_set(b)

    # Sort with key=str so a graph whose ids are of mixed/non-orderable types, or
    # whose edge keys contain None (a malformed edge with no relation/endpoint),
    # never raises a TypeError -- the result stays deterministic by string order.
    nodes_added = sorted(b_nodes - a_nodes, key=str)
    nodes_removed = sorted(a_nodes - b_nodes, key=str)

    edges_added = sorted(b_edges - a_edges, key=str)
    edges_removed = sorted(a_edges - b_edges, key=str)

    # A common node is "rewired" if any incident edge appeared or disappeared.
    touched = {ek[0] for ek in edges_added} | {ek[1] for ek in edges_added}
    touched |= {ek[0] for ek in edges_removed} | {ek[1] for ek in edges_removed}
    rewired = sorted((touched & a_nodes & b_nodes), key=str)

    unchanged = sorted((a_nodes & b_nodes) - set(rewired), key=str)

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
