"""attest: the "does shipped == source?" top layer.

Given a SOURCE graph (produced by graphify from the program's source) and a
SHIPPED/BINARY graph (produced by REA via REAgraph from the actual artifact),
align the two and surface the *residual the source cannot explain* -- the
supply-chain signal -- together with a confidence-weighted integrity score.

Both graphs use graphify's node-link JSON shape (see `reagraph.memory`):

    {"nodes": [{"id": ...}, ...],
     "links": [{"source": ..., "target": ..., "relation": ...}, ...]}

The alignment itself is done by the sibling `reagraph.align` module (built in
parallel). We only depend on its *contract*:

    align(src, bing, *, min_confidence=0.5) -> {
        "matches":  {src_id: {"bin": bin_id, "confidence": float, "via": str}},
        "src_only": [src_id, ...],   # in source, not confidently in the binary
        "bin_only": [bin_id, ...],   # in the binary, not confidently in source
        "stats":    {...},
    }

`bin_only` is the money bucket: binary nodes with no confident source match --
injected code, hidden telemetry, vendored libs. We also flag binary *edges*
between two matched nodes whose relation has no counterpart in source: a new
call/flow that the source does not account for.

Pure stdlib, deterministic, no over-claiming. Structural alignment is strong
where symbols survive and abstains where they do not, so "unexplained" means
"could not confidently match", NOT "proven malicious".
"""

from __future__ import annotations

try:  # the sibling may not be built yet; tests always inject a fake align_fn.
    from . import align as _align
except ImportError:  # pragma: no cover - exercised only when align is absent
    class _AlignStub:
        @staticmethod
        def align(src, bing, *, min_confidence=0.5):
            raise RuntimeError(
                "reagraph.align is unavailable; pass align_fn=... to attest()"
            )

    _align = _AlignStub()


# A deliberately honest caveat. Kept as a module constant so callers (and tests)
# can rely on it staying stable.
CAVEAT = (
    "Structural alignment is strong where symbols survive and abstains where "
    "they do not: 'unexplained' can mean 'could not confidently match', not "
    "proven-malicious."
)


def _nodes(graph) -> list:
    """Fail-open: the dict node entries of a graph. A non-dict graph / non-list
    nodes yield []; non-dict node entries are skipped -- never raises."""
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes", []) or []
    if not isinstance(nodes, list):
        return []
    return [n for n in nodes if isinstance(n, dict)]


def _edge_triple(e) -> tuple:
    return (e.get("source"), e.get("target"), e.get("relation", ""))


def _residual_clusters(bing, unexplained, *, max_cluster_size=None, max_labels=5):
    """Group the shipped-but-unexplained nodes into reviewable CLUSTERS.

    A flat list of 40 unexplained node ids reads as noise; the same 40 nodes shown
    as "one connected 40-node subsystem (52 internal edges; sendBeacon, encrypt,
    ...)" reads as a single finding. So we cluster the subgraph INDUCED by the
    unexplained nodes (edges whose BOTH endpoints are unexplained, treated
    undirected) into connected components -- one injected subsystem -> one cluster.

    Optionally, a component larger than ``max_cluster_size`` is further split by
    each node's internal-relation signature (shared relation/neighbourhood), so a
    single giant blob still breaks into roles instead of one wall of ids. With the
    default ``max_cluster_size=None`` no such split happens and a connected
    subsystem is always reported whole.

    Each cluster records::

        {"size": int,            # number of nodes
         "nodes": [id, ...],     # the unexplained node ids (sorted)
         "internal_edges": int,  # edges with both endpoints in this cluster
         "relations": [rel, ...],# relation types on those internal edges (sorted)
         "labels": [str, ...]}   # up to ``max_labels`` representative labels,
                                 # the most internally-connected nodes first

    Fail-safe and deterministic: an empty/malformed graph or empty residual yields
    ``[]``; everything is sorted before iterating; clusters are returned largest
    first (ties broken by internal edge count, then first node id).
    """
    resid = set(unexplained or [])
    if not resid:
        return []

    label_of: dict = {}
    for n in _nodes(bing):
        if isinstance(n, dict):
            nid = n.get("id")
            if nid in resid:
                label_of[nid] = n.get("label") or nid

    # Induced undirected adjacency + internal edges, over residual nodes only.
    adj: dict = {nid: set() for nid in resid}
    internal_rel: dict = {nid: [] for nid in resid}  # relations incident internally
    internal_edges = []  # (s, t, rel) with both endpoints unexplained
    for e in (bing.get("links", []) or bing.get("edges", []) or []):
        if not isinstance(e, dict):
            continue
        s, t, rel = _edge_triple(e)
        if s in resid and t in resid:
            internal_edges.append((s, t, rel))
            internal_rel[s].append(rel)
            if s != t:
                internal_rel[t].append(rel)
                adj[s].add(t)
                adj[t].add(s)

    # Connected components, deterministic (iterate sorted ids / neighbours).
    seen: set = set()
    components = []
    for start in sorted(resid, key=str):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        comp = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in sorted(adj[cur], key=str):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        components.append(comp)

    # Optionally subdivide an oversized component by relation signature.
    groups = []
    for comp in components:
        if max_cluster_size is not None and len(comp) > max_cluster_size:
            by_sig: dict = {}
            for nid in comp:
                sig = tuple(sorted(set(internal_rel[nid]))) or ("",)
                by_sig.setdefault(sig, []).append(nid)
            for sig in sorted(by_sig, key=lambda s: tuple(str(x) for x in s)):
                groups.append(by_sig[sig])
        else:
            groups.append(comp)

    clusters = []
    for grp in groups:
        grpset = set(grp)
        ie = 0
        rels: set = set()
        for (s, t, rel) in internal_edges:
            if s in grpset and t in grpset:
                ie += 1
                rels.add(rel)
        # Representatives: the most internally-connected nodes first, then by id.
        deg = {nid: len(adj[nid] & grpset) for nid in grp}
        reps = sorted(grp, key=lambda i: (-deg[i], str(i)))
        clusters.append({
            "size": len(grp),
            "nodes": sorted(grp, key=str),
            "internal_edges": ie,
            "relations": sorted(rels, key=str),
            "labels": [str(label_of.get(i, i)) for i in reps[: max(0, int(max_labels))]],
        })

    clusters.sort(
        key=lambda c: (-c["size"], -c["internal_edges"],
                       str(c["nodes"][0]) if c["nodes"] else "")
    )
    return clusters


def attest(src, bing, *, align_fn=None, min_confidence=0.5,
           max_cluster_size=None, max_cluster_labels=5) -> dict:
    """Align a SOURCE graph against a SHIPPED/BINARY graph and report the residual.

    Returns a dict:
        integrity          confidence-weighted fraction of the binary that
                           source explains, in [0, 1].
        shipped_unexplained  binary node ids with no confident source match
                             (the supply-chain signal).
        residual_clusters    the shipped_unexplained nodes grouped into connected
                             components of the unexplained subgraph (see
                             _residual_clusters): one injected subsystem -> one
                             cluster with its size, node ids, internal edge count,
                             relation types, and representative labels. Empty when
                             there is no residual.
        unexplained_edges    [[s, t, rel], ...] binary edges between two matched
                             nodes whose relation has no counterpart in source.
        source_only          source node ids that did not ship (dead/tree-shaken).
        matched              number of matched binary nodes.
        bin_nodes            total nodes in the binary graph.
        src_nodes            total nodes in the source graph.

    ``max_cluster_size``/``max_cluster_labels`` are forwarded to the clusterer:
    the former optionally subdivides an oversized component by relation signature
    (default ``None`` keeps connected subsystems whole), the latter caps the
    representative labels kept per cluster.

    Fail-open: a non-dict ``src``/``bing`` (or non-dict node/edge entries within)
    is treated as an empty graph -- align abstains, the residual is empty, and an
    empty binary scores integrity 1.0; never raises.
    """
    if not isinstance(src, dict):
        src = {}
    if not isinstance(bing, dict):
        bing = {}
    run = align_fn or _align.align
    result = run(src, bing, min_confidence=min_confidence)

    matches = result.get("matches", {}) or {}
    shipped_unexplained = list(result.get("bin_only", []) or [])
    source_only = list(result.get("src_only", []) or [])

    # bin id -> source id, from the alignment matches.
    bin_to_src = {}
    # bin id -> match confidence (how sure we are this binary node is that source node).
    bin_conf = {}
    for src_id, m in matches.items():
        bid = m.get("bin")
        if bid is None:
            continue
        bin_to_src[bid] = src_id
        try:
            bin_conf[bid] = float(m.get("confidence", 0.0))
        except (TypeError, ValueError):
            bin_conf[bid] = 0.0

    bin_nodes_list = _nodes(bing)
    bin_nodes = len(bin_nodes_list)
    src_nodes = len(_nodes(src))

    # Only count matches that land on a node actually present in the binary.
    bin_ids = {n.get("id") for n in bin_nodes_list}
    matched_bin = [bid for bid in bin_conf if bid in bin_ids]
    matched = len(matched_bin)

    # INTEGRITY: confidence-weighted fraction of the BINARY explained by source.
    # Unexplained nodes contribute nothing (they are not in bin_conf); a
    # low-confidence match counts only partially. Empty binary -> fully explained.
    if bin_nodes == 0:
        integrity = 1.0
    else:
        integrity = sum(bin_conf[bid] for bid in matched_bin) / bin_nodes
    integrity = max(0.0, min(1.0, integrity))

    # Source edges, keyed on (source_id, target_id, relation), for lookup.
    # Malformed (non-dict) edge entries are tolerated and skipped -- fail-safe.
    src_edge_set = {
        _edge_triple(e) for e in (src.get("links", []) or []) if isinstance(e, dict)
    }

    # Unexplained edges: both endpoints matched to source, but the corresponding
    # relation does not exist in source -> a new call/flow the source can't explain.
    unexplained_edges = []
    for e in (bing.get("links", []) or []):
        if not isinstance(e, dict):
            continue
        bs, bt, rel = _edge_triple(e)
        ss = bin_to_src.get(bs)
        st = bin_to_src.get(bt)
        if ss is None or st is None:
            continue  # an endpoint is itself unexplained; captured by the node bucket.
        if (ss, st, rel) not in src_edge_set:
            unexplained_edges.append([bs, bt, rel])

    residual_clusters = _residual_clusters(
        bing, shipped_unexplained,
        max_cluster_size=max_cluster_size, max_labels=max_cluster_labels,
    )

    return {
        "integrity": integrity,
        "shipped_unexplained": shipped_unexplained,
        "residual_clusters": residual_clusters,
        "unexplained_edges": unexplained_edges,
        "source_only": source_only,
        "matched": matched,
        "bin_nodes": bin_nodes,
        "src_nodes": src_nodes,
    }


def format_report(result, *, max_clusters=10, max_cluster_labels=5) -> str:
    """A short, honest report. Leads with the integrity score and the count of
    shipped-but-unexplained nodes/edges, surfaces the residual grouped into
    clusters (so a new subsystem reads as one finding, not N), lists the top
    unexplained items, and ends with a one-line caveat so we never over-claim
    malice. Output is bounded: at most ``max_clusters`` clusters and
    ``max_cluster_labels`` labels per cluster, and 10 items per flat list."""
    integrity = result.get("integrity", 0.0)
    unexplained = result.get("shipped_unexplained", []) or []
    clusters = result.get("residual_clusters", []) or []
    edges = result.get("unexplained_edges", []) or []
    matched = result.get("matched", 0)
    bin_nodes = result.get("bin_nodes", 0)

    lines = []
    lines.append(f"Integrity: {integrity:.3f}  (0=none of the binary explained, 1=all)")
    lines.append(
        f"Shipped-but-unexplained: {len(unexplained)} node(s), {len(edges)} edge(s)"
    )
    lines.append(f"Binary nodes matched to source: {matched}/{bin_nodes}")

    if clusters:
        lines.append("")
        lines.append(
            f"Residual clusters (grouped unexplained structure): {len(clusters)}"
        )
        for i, c in enumerate(clusters[: max(0, int(max_clusters))], start=1):
            labs = c.get("labels", [])[: max(0, int(max_cluster_labels))]
            tail = (" e.g. " + ", ".join(str(x) for x in labs)) if labs else ""
            lines.append(
                f"  [{i}] {c.get('size', 0)} node(s), "
                f"{c.get('internal_edges', 0)} internal edge(s)" + tail
            )
        if len(clusters) > max_clusters:
            lines.append(f"  ... and {len(clusters) - max_clusters} more cluster(s)")

    if unexplained:
        lines.append("")
        lines.append("Top unexplained nodes (no confident source match):")
        for nid in unexplained[:10]:
            lines.append(f"  - {nid}")
        if len(unexplained) > 10:
            lines.append(f"  ... and {len(unexplained) - 10} more")

    if edges:
        lines.append("")
        lines.append("Top unexplained edges (new flows between known nodes):")
        for s, t, rel in edges[:10]:
            lines.append(f"  - {s} -[{rel}]-> {t}")
        if len(edges) > 10:
            lines.append(f"  ... and {len(edges) - 10} more")

    lines.append("")
    lines.append(f"Caveat: {CAVEAT}")
    return "\n".join(lines)
