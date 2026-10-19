"""Structural fingerprints of graph nodes -- the signals an aligner matches on.

When we have a source-level graph of a program and a graph recovered from its
binary, the two describe the *same* program but share almost no literal ids:
the binary's nodes are addresses, the source's are file:line symbols. To line
them up we lean on signals that survive the compiler:

  - `degrees`     : total incident-edge count per node -- the crudest shape.
  - `name_key`    : a normalized symbol identity, for the lucky cases where a
                    name survives into the binary (exports, imports, RTTI).
  - `god_nodes`   : the highest-degree hubs -- anchors to seed an alignment from.

The structural (Weisfeiler-Lehman) signal that matches stripped nodes lives in
`reagraph.align._struct_labels`, a stronger directed, relation-typed variant; it
consumes only `degrees`/`name_key`/`god_nodes` from here.

Pure stdlib, deterministic, order-independent, and linear so it still runs on a
million-node graph. Mirrors `summarize.py`'s plain dict degree counting; it does
NOT import it.
"""

from __future__ import annotations


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
