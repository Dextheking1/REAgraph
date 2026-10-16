"""Structural fingerprints of graph nodes -- the signals an aligner matches on.

When we have a source-level graph of a program and a graph recovered from its
binary, the two describe the *same* program but share almost no literal ids:
the binary's nodes are addresses, the source's are file:line symbols. To line
them up we lean on signals that survive the compiler:

  - `degrees`     : total incident-edge count per node -- the crudest shape.
  - `wl_labels`   : a Weisfeiler-Lehman color for each node, a stable hash of
                    its neighbourhood out to `rounds` hops. Two nodes with the
                    same local structure get the same color in EITHER graph, so
                    colors are candidate-match keys even across relabelling.
  - `name_key`    : a normalized symbol identity, for the lucky cases where a
                    name survives into the binary (exports, imports, RTTI).
  - `god_nodes`   : the highest-degree hubs -- anchors to seed an alignment from.

Pure stdlib (hashlib only), deterministic, order-independent, and linear per WL
round so it still runs on a million-node graph. Mirrors `summarize.py`'s plain
dict degree counting; it does NOT import it.
"""

from __future__ import annotations

import hashlib

_HASH_LEN = 16  # hex chars of a WL color -- short but collision-safe enough.


def _hash(text: str) -> str:
    """A short, stable, process-independent hash (NOT Python's salted hash())."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _node_ids(graph: dict) -> list:
    """Every node id in the graph, INCLUDING ids that appear only as an edge
    endpoint with no node entry. Order is deterministic: declared nodes first in
    declaration order, then endpoint-only ids sorted by id."""
    seen = set()
    ordered = []
    for n in graph.get("nodes", []):
        nid = n.get("id")
        if nid is not None and nid not in seen:
            seen.add(nid)
            ordered.append(nid)
    extra = set()
    for e in graph.get("links", []) or graph.get("edges", []):
        for nid in (e.get("source"), e.get("target")):
            if nid is not None and nid not in seen:
                extra.add(nid)
    ordered.extend(sorted(extra, key=str))
    return ordered


def degrees(graph: dict) -> dict:
    """Total degree (in + out) per node id, counting every incident edge.

    Endpoint-only ids (seen on an edge but with no node entry) are included."""
    deg: dict = {nid: 0 for nid in _node_ids(graph)}
    for e in graph.get("links", []) or graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s is not None:
            deg[s] = deg.get(s, 0) + 1
        if t is not None:
            deg[t] = deg.get(t, 0) + 1
    return deg


def _in_out_degrees(graph: dict) -> tuple:
    """(in_degree, out_degree) dicts keyed by node id (endpoint-only ids too)."""
    ids = _node_ids(graph)
    ind: dict = {nid: 0 for nid in ids}
    outd: dict = {nid: 0 for nid in ids}
    for e in graph.get("links", []) or graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s is not None:
            outd[s] = outd.get(s, 0) + 1
        if t is not None:
            ind[t] = ind.get(t, 0) + 1
    return ind, outd


def wl_labels(graph: dict, rounds: int = 3) -> dict:
    """Weisfeiler-Lehman color refinement -- a structural label per node id.

    Each node starts coloured by its (in_degree, out_degree). Every round, a
    node's new colour is a short stable hash of its current colour plus the
    SORTED multiset of its neighbours' colours over BOTH in- and out-neighbours.
    After `rounds` rounds, two nodes that have structurally identical
    neighbourhoods (up to `rounds` hops, as undirected-reachable-by-colour) share
    the same colour -- in this graph OR in any isomorphic relabelling of it.

    Deterministic and order-independent: the result does not depend on node or
    edge declaration order. Linear per round. Endpoint-only ids are coloured too
    (as degree-only nodes)."""
    ids = _node_ids(graph)
    ind, outd = _in_out_degrees(graph)

    # Initial colour from the (in, out) degree signature.
    labels = {nid: _hash(f"{ind.get(nid, 0)}|{outd.get(nid, 0)}") for nid in ids}

    # Build neighbour adjacency once (both directions, undirected multiset).
    neighbours: dict = {nid: [] for nid in ids}
    for e in graph.get("links", []) or graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s is None or t is None:
            continue
        neighbours[s].append(t)  # out-neighbour
        neighbours[t].append(s)  # in-neighbour

    for _ in range(max(0, rounds)):
        new_labels = {}
        for nid in ids:
            neigh = sorted(labels[nid2] for nid2 in neighbours[nid])
            new_labels[nid] = _hash(labels[nid] + "::" + ",".join(neigh))
        labels = new_labels
    return labels


# Punctuation we strip when normalizing a symbol into a name key.
_PUNCT = "()[]{}<>.,:;*&!?\"'`~@#$%^+=|\\/ \t\n"
_TRANS = {ord(c): None for c in _PUNCT}


def name_key(node) -> str | None:
    """A normalized identity from a node's label/id, for exact-name matching
    where a symbol survives into the binary.

    Lowercases and strips punctuation / parentheses / call-arg noise so that
    e.g. `authenticate()` and `Authenticate` collapse to the same key. Accepts a
    node dict (prefers `label`, falls back to `id`) or a raw string. Returns None
    when there is nothing useful left (empty, or only punctuation)."""
    if node is None:
        return None
    if isinstance(node, dict):
        raw = node.get("label") or node.get("id")
    else:
        raw = node
    if raw is None:
        return None
    text = str(raw)
    # Drop an argument list / call tail: everything from the first '(' onward.
    cut = text.find("(")
    if cut != -1:
        text = text[:cut]
    key = text.lower().translate(_TRANS)
    return key or None


def god_nodes(graph: dict, top: int = 25) -> list:
    """Node ids ranked by total degree descending, capped at `top`.

    Ties broken deterministically by id ascending. Returns a plain list of ids
    (the anchors an aligner seeds from); see `summarize.god_nodes` for the richer
    {id,label,degree} form."""
    deg = degrees(graph)
    ranked = sorted(deg.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [nid for nid, _ in ranked[: max(0, top)]]
