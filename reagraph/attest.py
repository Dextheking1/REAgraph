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
    return list(graph.get("nodes", []) or [])


def _edge_triple(e) -> tuple:
    return (e.get("source"), e.get("target"), e.get("relation", ""))


def attest(src, bing, *, align_fn=None, min_confidence=0.5) -> dict:
    """Align a SOURCE graph against a SHIPPED/BINARY graph and report the residual.

    Returns a dict:
        integrity          confidence-weighted fraction of the binary that
                           source explains, in [0, 1].
        shipped_unexplained  binary node ids with no confident source match
                             (the supply-chain signal).
        unexplained_edges    [[s, t, rel], ...] binary edges between two matched
                             nodes whose relation has no counterpart in source.
        source_only          source node ids that did not ship (dead/tree-shaken).
        matched              number of matched binary nodes.
        bin_nodes            total nodes in the binary graph.
        src_nodes            total nodes in the source graph.
    """
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
    src_edge_set = {_edge_triple(e) for e in (src.get("links", []) or [])}

    # Unexplained edges: both endpoints matched to source, but the corresponding
    # relation does not exist in source -> a new call/flow the source can't explain.
    unexplained_edges = []
    for e in (bing.get("links", []) or []):
        bs, bt, rel = _edge_triple(e)
        ss = bin_to_src.get(bs)
        st = bin_to_src.get(bt)
        if ss is None or st is None:
            continue  # an endpoint is itself unexplained; captured by the node bucket.
        if (ss, st, rel) not in src_edge_set:
            unexplained_edges.append([bs, bt, rel])

    return {
        "integrity": integrity,
        "shipped_unexplained": shipped_unexplained,
        "unexplained_edges": unexplained_edges,
        "source_only": source_only,
        "matched": matched,
        "bin_nodes": bin_nodes,
        "src_nodes": src_nodes,
    }


def format_report(result) -> str:
    """A short, honest report. Leads with the integrity score and the count of
    shipped-but-unexplained nodes/edges, lists the top unexplained items, and
    ends with a one-line caveat so we never over-claim malice."""
    integrity = result.get("integrity", 0.0)
    unexplained = result.get("shipped_unexplained", []) or []
    edges = result.get("unexplained_edges", []) or []
    matched = result.get("matched", 0)
    bin_nodes = result.get("bin_nodes", 0)

    lines = []
    lines.append(f"Integrity: {integrity:.3f}  (0=none of the binary explained, 1=all)")
    lines.append(
        f"Shipped-but-unexplained: {len(unexplained)} node(s), {len(edges)} edge(s)"
    )
    lines.append(f"Binary nodes matched to source: {matched}/{bin_nodes}")

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
