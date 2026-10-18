"""Turn a huge graph into a COMPACT, bounded map an agent can actually read.

The pain point (REA at scale): analysis output reaches gigabyte scale, far too
big for an agent to digest. A million-node call graph does not fit in a context
window and re-reading it is hopeless. REAgraph's answer is to never hand the
agent the whole graph -- hand it a *bounded* overview whose size does not grow
with the graph:

  - `god_nodes`:  the highest-degree hubs (the few functions everything touches).
  - `components`: weakly-connected clusters, a light stand-in for community
                  detection (graphify can run Leiden later for real communities).
  - `summarize`:  a single small dict -- totals, confidence breakdown, the top
                  god-nodes and largest clusters -- capped so a 1M-node graph
                  still yields a summary a few lines long.

Pure stdlib, deterministic, and linear-ish: plain dict degree counting and a
union-find for components, never networkx.
"""

from __future__ import annotations

from . import memory


def _clean_graph(graph) -> dict:
    """Fail-open coercion to a well-formed graph: a non-dict graph becomes empty,
    non-list nodes/links become empty, and non-dict entries are dropped. The
    ``edges`` spelling is folded into ``links``. On a well-formed graph this is a
    content-preserving copy, so behaviour is unchanged."""
    if not isinstance(graph, dict):
        return {"nodes": [], "links": []}
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    if not links:
        links = graph.get("edges", [])
    return {
        "nodes": [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else [],
        "links": [e for e in links if isinstance(e, dict)] if isinstance(links, list) else [],
    }


def _node_labels(graph: dict) -> dict:
    """Map node id -> label (falling back to the id when no label is set)."""
    labels = {}
    for n in graph.get("nodes", []):
        nid = n.get("id")
        if nid is None:
            continue
        labels[nid] = n.get("label") or nid
    return labels


def _degrees(graph: dict) -> dict:
    """Total degree (in + out) per node id, counting every incident edge."""
    deg: dict = {}
    for n in graph.get("nodes", []):
        nid = n.get("id")
        if nid is not None:
            deg.setdefault(nid, 0)
    for e in graph.get("links", []) or graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s is not None:
            deg[s] = deg.get(s, 0) + 1
        if t is not None:
            deg[t] = deg.get(t, 0) + 1
    return deg


def god_nodes(graph: dict, top: int = 20) -> list:
    """The `top` highest-degree nodes as [{id, label, degree}].

    Ranked by degree (in + out) descending; ties broken deterministically by id
    ascending. Returns at most `top` entries."""
    deg = _degrees(graph)
    labels = _node_labels(graph)
    ranked = sorted(deg.items(), key=lambda kv: (-kv[1], str(kv[0])))
    out = []
    for nid, d in ranked[: max(0, top)]:
        out.append({"id": nid, "label": labels.get(nid, nid), "degree": d})
    return out


def components(graph: dict) -> list:
    """Weakly-connected components (edges treated as undirected for grouping).

    Each component is returned as a sorted list of node ids. Components are
    ordered by size descending, then by their minimum id -- a light stand-in for
    community detection (graphify can run Leiden later)."""
    parent: dict = {}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression.
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def add(x):
        if x not in parent:
            parent[x] = x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for n in graph.get("nodes", []):
        nid = n.get("id")
        if nid is not None:
            add(nid)
    for e in graph.get("links", []) or graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s is None or t is None:
            continue
        add(s)
        add(t)
        union(s, t)

    groups: dict = {}
    for nid in parent:
        groups.setdefault(find(nid), []).append(nid)

    clusters = [sorted(members, key=str) for members in groups.values()]
    clusters.sort(key=lambda c: (-len(c), str(c[0])))
    return clusters


def summarize(graph: dict, top: int = 20, max_clusters: int = 10) -> dict:
    """A bounded overview of the graph, regardless of how big the graph is.

    Caps every list so a million-node graph still yields a small summary:
      - totals + edge confidence breakdown (reuses `memory.stats`),
      - the top god-nodes (<= `top`),
      - the largest clusters (<= `max_clusters`), each with a representative
        label (the highest-degree member).

    Fail-open: a non-dict graph or non-dict node/edge entries are tolerated (an
    empty/clean graph yields the natural empty-shape summary); never raises."""
    graph = _clean_graph(graph)
    base = memory.stats(graph)
    deg = _degrees(graph)
    labels = _node_labels(graph)

    clusters = components(graph)
    cluster_summaries = []
    for members in clusters[: max(0, max_clusters)]:
        # Representative = highest-degree member, tie-broken by id.
        rep = min(members, key=lambda nid: (-deg.get(nid, 0), str(nid)))
        cluster_summaries.append({
            "size": len(members),
            "representative": rep,
            "representative_label": labels.get(rep, rep),
        })

    return {
        "nodes": base["nodes"],
        "edges": base["edges"],
        "edge_confidence": base["edge_confidence"],
        "num_components": len(clusters),
        "god_nodes": god_nodes(graph, top=top),
        "largest_clusters": cluster_summaries,
    }


def format_summary(summary: dict) -> str:
    """A short, human/agent-readable text block for a summary dict."""
    conf = summary.get("edge_confidence", {})
    lines = [
        "Graph overview (bounded):",
        f"  nodes      : {summary.get('nodes', 0)}",
        f"  edges      : {summary.get('edges', 0)}"
        f"  (EXTRACTED {conf.get('EXTRACTED', 0)} /"
        f" INFERRED {conf.get('INFERRED', 0)} /"
        f" other {conf.get('other', 0)})",
        f"  components : {summary.get('num_components', 0)}",
        "  god nodes:",
    ]
    for g in summary.get("god_nodes", []):
        lines.append(f"    {g['label']}  (degree {g['degree']})")
    lines.append("  largest clusters:")
    for c in summary.get("largest_clusters", []):
        lines.append(
            f"    {c['representative_label']}  (+{c['size'] - 1} more)"
            if c["size"] > 1
            else f"    {c['representative_label']}  (singleton)"
        )
    return "\n".join(lines)
