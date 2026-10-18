"""Aligning a source graph to a binary graph: match where sure, abstain where not.

These run against whatever ``reagraph.fingerprint`` resolves to inside
``align`` — the real sibling if it has landed, otherwise align's built-in
stdlib stub. Both honour the documented signatures, so the behaviour asserted
here (unique name/label -> match, ambiguous -> abstain) holds either way.
"""

from reagraph.align import align


def _node(nid, label=None, **attrs):
    # fingerprint.name_key derives a node's name from its `label` (falling back
    # to its `id`), so the symbol a match keys on goes in `label`.
    n = {"id": nid}
    if label is not None:
        n["label"] = label
    n.update(attrs)
    return n


def _edge(s, t, relation="calls"):
    return {"source": s, "target": t, "relation": relation}


def _one_to_one(result):
    bins = [m["bin"] for m in result["matches"].values()]
    assert len(bins) == len(set(bins)), "a bin node was claimed twice"


def _confidences_in_range(result):
    for m in result["matches"].values():
        assert 0.0 <= m["confidence"] <= 1.0


# --- A small asymmetric chain a->b->c; every node has a distinct shape. ---
def _chain(prefix, named=True):
    ids = [prefix + s for s in ("a", "b", "c")]
    nodes = [_node(i, label=(i if named else None)) for i in ids]
    links = [_edge(ids[0], ids[1]), _edge(ids[1], ids[2])]
    return {"directed": True, "nodes": nodes, "links": links}, ids


def test_identical_graphs_all_matched_high_confidence():
    src, sids = _chain("")
    bing, _ = _chain("")  # byte-for-byte identical, same ids and names
    r = align(src, bing)

    assert r["stats"]["matched"] == 3
    assert r["src_only"] == [] and r["bin_only"] == []
    for sid in sids:
        m = r["matches"][sid]
        assert m["bin"] == sid
        assert m["via"] in ("both", "name")
        assert m["confidence"] >= 0.95
    _one_to_one(r)
    _confidences_in_range(r)
    assert 0.0 <= r["stats"]["mean_confidence"] <= 1.0


def test_renamed_binary_matches_via_structure():
    # Same shape, different ids AND no usable names -> the name signal is dead,
    # so these can only be matched structurally. The chain is asymmetric, so
    # each WL label is unique and the match is unambiguous.
    src, sids = _chain("s_", named=False)
    bing, bids = _chain("b_", named=False)
    r = align(src, bing)

    assert r["stats"]["matched"] == 3
    for sid, bid in zip(sids, bids):  # a<->a, b<->b, c<->c positionally
        m = r["matches"][sid]
        assert m["via"] == "structure", "no names survived; must be structural"
        assert m["bin"] == bid
        assert 0.70 <= m["confidence"] <= 0.94  # structural tier, below name
    _one_to_one(r)
    _confidences_in_range(r)


def test_symmetric_structure_abstains_rather_than_guesses():
    # A root with two structurally identical leaves. With no names, the two
    # leaves share a WL label -> ambiguous -> the aligner must NOT guess.
    src = {
        "nodes": [_node("root"), _node("leaf1"), _node("leaf2")],
        "links": [_edge("root", "leaf1"), _edge("root", "leaf2")],
    }
    bing = {
        "nodes": [_node("R"), _node("L1"), _node("L2")],
        "links": [_edge("R", "L1"), _edge("R", "L2")],
    }
    r = align(src, bing)

    # The root is structurally unique, so it may match; the two leaves are
    # indistinguishable and must be abstained (left unmatched), not mis-matched.
    assert "leaf1" not in r["matches"]
    assert "leaf2" not in r["matches"]
    assert "leaf1" in r["src_only"] and "leaf2" in r["src_only"]
    _one_to_one(r)
    _confidences_in_range(r)


def test_bin_only_and_src_only_are_reported():
    # Shared, uniquely-named anchors plus one exclusive node on each side.
    src = {
        "nodes": [_node("m", label="main"), _node("h", label="helper"),
                  _node("only_src", label="src_local")],
        # only_src calls helper -> it has a distinct shape, so it can't be
        # confused with the (differently-shaped) binary-only node below.
        "links": [_edge("m", "h"), _edge("only_src", "h")],
    }
    bing = {
        "nodes": [_node("m2", label="main"), _node("h2", label="helper"),
                  _node("only_bin", label="bin_stub")],
        "links": [_edge("m2", "h2")],
    }
    r = align(src, bing)

    # Unique names match across the id renaming.
    assert r["matches"]["m"]["bin"] == "m2"
    assert r["matches"]["h"]["bin"] == "h2"
    assert r["matches"]["m"]["via"] in ("name", "both")

    # The exclusive nodes surface on the right sides and are not matched.
    assert "only_src" in r["src_only"]
    assert "only_bin" in r["bin_only"]
    assert "only_src" not in r["matches"]
    _one_to_one(r)
    _confidences_in_range(r)


def test_ambiguous_name_abstains():
    # Two source nodes share the name "dup" -> ambiguous -> neither is matched,
    # even though a single "dup" exists in the binary.
    src = {
        "nodes": [_node("a", label="dup"), _node("b", label="dup"),
                  _node("u", label="unique")],
        "links": [],
    }
    bing = {
        "nodes": [_node("x", label="dup"), _node("v", label="unique")],
        "links": [],
    }
    r = align(src, bing)

    assert "a" not in r["matches"] and "b" not in r["matches"]
    assert r["matches"]["u"]["bin"] == "v"
    _one_to_one(r)
    _confidences_in_range(r)


def test_min_confidence_filters_structure_matches():
    # Raising the bar above the structural tier should drop structure-only
    # matches but keep strong name matches.
    src, sids = _chain("s_", named=False)
    bing, _ = _chain("b_", named=False)
    r = align(src, bing, min_confidence=0.95)
    assert r["stats"]["matched"] == 0
    assert set(r["src_only"]) == set(sids)


# --- STRIPPED-binary structural recovery (GOAL 1) -------------------------------

import reagraph.align as _align_mod


def _asymmetric_dag(prefix):
    """A directed, asymmetric DAG in which every node has a distinct directed,
    relation-typed neighbourhood -- so the strengthened WL colour pins each node
    down uniquely and structure-only matching can recover the whole graph. ids
    are namespaced by ``prefix`` so the two copies share no id and no name."""
    n = lambda s: prefix + s
    nodes = [_node(n(x)) for x in ("root", "a", "b", "c", "d", "e", "leaf")]
    links = [
        _edge(n("root"), n("a"), "calls"),
        _edge(n("root"), n("b"), "reads"),
        _edge(n("a"), n("c"), "calls"),
        _edge(n("b"), n("c"), "calls"),
        _edge(n("c"), n("d"), "writes"),
        _edge(n("d"), n("e"), "calls"),
        _edge(n("e"), n("leaf"), "calls"),
        _edge(n("b"), n("leaf"), "reads"),
    ]
    return {"directed": True, "nodes": nodes, "links": links}


def test_stripped_structural_recovery_precision_one():
    # Same program, shipped stripped: ids renamed, ALL labels blanked. Only shape
    # survives. The strengthened structural signature must recover true matches
    # with precision 1.00 -- every committed match is the correct s<->b twin.
    src = _asymmetric_dag("s_")
    bing = _asymmetric_dag("b_")  # isomorphic copy, disjoint id/name space
    r = align(src, bing)

    matches = r["matches"]
    # Nothing matched by name (labels blanked, id alphabets disjoint).
    for m in matches.values():
        assert m["via"] in ("structure", "propagation")
    # PRECISION 1.00: every match maps s_<x> to its true twin b_<x>.
    for sid, m in matches.items():
        assert sid.startswith("s_")
        assert m["bin"] == "b_" + sid[2:], (sid, m)
    # And it actually recovered a non-trivial chunk of the graph structurally.
    assert len(matches) >= 5
    _one_to_one(r)
    _confidences_in_range(r)


def _ring(prefix, size=6):
    ids = [prefix + str(i) for i in range(size)]
    nodes = [_node(i) for i in ids]
    links = [_edge(ids[i], ids[(i + 1) % size]) for i in range(size)]
    return {"directed": True, "nodes": nodes, "links": links}, ids


def test_symmetric_ring_abstains_entirely():
    # A directed ring: every node is structurally identical (in1,out1, same
    # neighbourhood forever) -> every WL colour collides -> no unique-best exists
    # anywhere, and with no anchors propagation cannot start. The aligner must
    # abstain on ALL nodes rather than inventing a rotation.
    src, sids = _ring("s", 6)
    bing, _ = _ring("b", 6)
    r = align(src, bing)

    assert r["matches"] == {}, "a symmetric ring must not be guessed"
    assert set(r["src_only"]) == set(sids)
    _one_to_one(r)
    _confidences_in_range(r)


def test_confidence_tiers_are_strictly_ordered():
    # The invariant the product rests on: exact-name > unique-structural >
    # propagated, and each tier stays under its cap.
    assert _align_mod._NAME_CONF > _align_mod._STRUCT_CAP
    assert _align_mod._STRUCT_CONF >= 0.70 > _align_mod._PROP_CAP
    assert _align_mod._PROP_CAP >= _align_mod._PROP_CONF


def test_stripped_recovery_is_deterministic():
    src = _asymmetric_dag("s_")
    bing = _asymmetric_dag("b_")
    a = align(src, bing)
    # Re-run with reversed declaration order -> identical mapping.
    rev = lambda g: {"nodes": list(reversed(g["nodes"])),
                     "links": list(reversed(g["links"]))}
    b = align(rev(src), rev(bing))
    assert a["matches"] == b["matches"]
    assert a["src_only"] == b["src_only"] and a["bin_only"] == b["bin_only"]
