"""Aligning STRIPPED binaries — no surviving symbol names, graphs not even
isomorphic — through the seed-propagation pass.

These cases model the hard end of the spectrum the module is built for: the
binary's node ids do not correspond to the source's, no label carries a usable
symbol, and the compiled graph differs from the source (an injected stub, a dead
branch) so Weisfeiler-Lehman colours diverge for most nodes. The only honest
signal left is *structure grown from a confident anchor*: match the few nodes
whose local shape still pins them down, then propagate outward to their
neighbours. The no-guessing rule still holds — where two candidates are equally
good, the aligner abstains rather than inventing a correspondence.

Runs against whatever ``reagraph.fingerprint`` resolves to inside ``align`` (the
real sibling, else align's stdlib stub); both honour the documented signatures.
"""

from reagraph.align import align


def _node(nid):
    # No `label`: name_key falls back to the id. Source ids ("s*") and binary
    # ids ("b*") share no alphabet, so no name ever matches across the two
    # graphs — exactly as on a stripped binary where the symbol table is gone.
    return {"id": nid}


def _edge(s, t, relation="calls"):
    return {"source": s, "target": t, "relation": relation}


def _one_to_one(result):
    bins = [m["bin"] for m in result["matches"].values()]
    assert len(bins) == len(set(bins)), "a bin node was claimed twice"


def _confidences_in_range(result):
    for m in result["matches"].values():
        assert 0.0 <= m["confidence"] <= 1.0


def _reversed(graph):
    # Same graph, reversed node/edge declaration order — for determinism checks.
    return {
        "nodes": list(reversed(graph["nodes"])),
        "links": list(reversed(graph["links"])),
    }


def _stripped_chain_pair():
    """A directed call-chain s0->..->s7 (source) and its stripped binary b0->..->b7
    with an extra injected stub ``bX`` hanging off b5.

    No names survive. The injected stub perturbs the WL colours of the interior
    nodes, so structure-only matching anchors just the ends (whose
    neighbourhoods the stub does not reach); the middle of the chain can only be
    recovered by propagating from those anchors."""
    n = 8
    src = {
        "nodes": [_node("s%d" % i) for i in range(n)],
        "links": [_edge("s%d" % i, "s%d" % (i + 1)) for i in range(n - 1)],
    }
    bing = {
        "nodes": [_node("b%d" % i) for i in range(n)] + [_node("bX")],
        "links": [_edge("b%d" % i, "b%d" % (i + 1)) for i in range(n - 1)]
        + [_edge("b5", "bX")],
    }
    return src, bing


def test_stripped_chain_propagation_recovers_more_than_anchors_alone():
    src, bing = _stripped_chain_pair()
    r = align(src, bing)

    matches = r["matches"]
    by_via = {}
    for sid, m in matches.items():
        by_via.setdefault(m["via"], []).append(sid)

    # No name ever matches (disjoint id alphabets, no labels).
    assert "name" not in by_via and "both" not in by_via

    # Propagation recovered strictly more nodes than the anchor (structure) pass.
    propagated = by_via.get("propagation", [])
    structural = by_via.get("structure", [])
    assert len(propagated) > len(structural) >= 1, (matches, r["stats"])

    # Every propagated match is the correct positional pairing s_i <-> b_i,
    # carried at the propagation tier (below the 0.70 structural floor).
    for sid in propagated:
        assert matches[sid]["bin"] == "b" + sid[1:]
        assert 0.55 <= matches[sid]["confidence"] <= 0.68

    # Concretely: the chain interior propagates from the anchored ends.
    for sid in ("s2", "s3", "s4", "s5"):
        assert matches[sid]["via"] == "propagation"
        assert matches[sid]["bin"] == "b" + sid[1:]

    # The injected binary stub has no source counterpart and is left unmatched.
    assert "bX" in r["bin_only"]
    assert "bX" not in [m["bin"] for m in matches.values()]

    _one_to_one(r)
    _confidences_in_range(r)
    assert 0.0 <= r["stats"]["mean_confidence"] <= 1.0


def test_stripped_propagation_is_deterministic_under_reordering():
    src, bing = _stripped_chain_pair()
    a = align(src, bing)
    b = align(_reversed(src), _reversed(bing))
    assert a["matches"] == b["matches"]
    assert a["src_only"] == b["src_only"]
    assert a["bin_only"] == b["bin_only"]


def test_symmetric_stripped_case_abstains_not_guesses():
    # A stripped chain a0->a1->a2 ending in TWO structurally identical leaves.
    # The anchors (a0,a1,a2) pin down by shape, but the two leaves are mutually
    # indistinguishable — both to WL (same colour) and to propagation (each has
    # the single matched neighbour a2). The aligner must abstain on both, never
    # guess a leaf<->leaf correspondence.
    src = {
        "nodes": [_node("a0"), _node("a1"), _node("a2"), _node("l1"), _node("l2")],
        "links": [_edge("a0", "a1"), _edge("a1", "a2"),
                  _edge("a2", "l1"), _edge("a2", "l2")],
    }
    bing = {
        "nodes": [_node("b0"), _node("b1"), _node("b2"), _node("m1"), _node("m2")],
        "links": [_edge("b0", "b1"), _edge("b1", "b2"),
                  _edge("b2", "m1"), _edge("b2", "m2")],
    }
    r = align(src, bing)

    # The two symmetric leaves are abstained, not matched to m1/m2.
    assert "l1" not in r["matches"] and "l2" not in r["matches"]
    assert "l1" in r["src_only"] and "l2" in r["src_only"]
    assert "m1" in r["bin_only"] and "m2" in r["bin_only"]

    # The unambiguous backbone still anchors (so there WERE seeds to propagate
    # from — the leaves abstained on their own merits, not for lack of anchors).
    assert r["matches"]["a2"]["bin"] == "b2"

    _one_to_one(r)
    _confidences_in_range(r)

    # Deterministic under reordering.
    r2 = align(_reversed(src), _reversed(bing))
    assert r["matches"] == r2["matches"] and r["src_only"] == r2["src_only"]
