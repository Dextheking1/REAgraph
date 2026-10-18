"""attest tests, driven by a FAKE align_fn so they never touch the real sibling.

The fake honors the `align` contract exactly:

    align(src, bing, *, min_confidence=0.5) -> {
        "matches":  {src_id: {"bin", "confidence", "via"}},
        "src_only": [...],
        "bin_only": [...],
        "stats":    {...},
    }
"""

from reagraph import attest


def _graph(node_ids, edges):
    return {
        "nodes": [{"id": n} for n in node_ids],
        "links": [{"source": s, "target": t, "relation": r} for (s, t, r) in edges],
    }


def _fake_align(matches, src_only, bin_only):
    """Build an align_fn that just returns a canned result (ignores its inputs)."""

    def _align(src, bing, *, min_confidence=0.5):
        return {
            "matches": matches,
            "src_only": list(src_only),
            "bin_only": list(bin_only),
            "stats": {"min_confidence": min_confidence},
        }

    return _align


def test_clean_case_integrity_near_one_and_no_residual():
    # Every binary node matches a source node at high confidence.
    src = _graph(["main", "helper"], [("main", "helper", "calls")])
    bing = _graph(["main", "helper"], [("main", "helper", "calls")])
    align_fn = _fake_align(
        matches={
            "main": {"bin": "main", "confidence": 1.0, "via": "symbol"},
            "helper": {"bin": "helper", "confidence": 0.98, "via": "symbol"},
        },
        src_only=[],
        bin_only=[],
    )

    r = attest.attest(src, bing, align_fn=align_fn)

    assert r["shipped_unexplained"] == []
    assert r["unexplained_edges"] == []
    assert r["source_only"] == []
    assert r["matched"] == 2 and r["bin_nodes"] == 2 and r["src_nodes"] == 2
    # (1.0 + 0.98) / 2 = 0.99
    assert r["integrity"] > 0.95
    assert 0.0 <= r["integrity"] <= 1.0


def test_injected_case_drops_integrity_and_flags_node_and_edge():
    # Source: main -> helper. Binary ships an extra "evil" node, a new flow
    # between two MATCHED nodes (main -> helper via "sends"), and a call into evil.
    src = _graph(["main", "helper"], [("main", "helper", "calls")])
    bing = _graph(
        ["main", "helper", "evil"],
        [
            ("main", "helper", "calls"),   # explained: exists in source
            ("main", "helper", "sends"),   # UNEXPLAINED edge: both matched, new relation
            ("main", "evil", "calls"),     # endpoint unexplained -> captured by node bucket
        ],
    )
    align_fn = _fake_align(
        matches={
            "main": {"bin": "main", "confidence": 1.0, "via": "symbol"},
            "helper": {"bin": "helper", "confidence": 1.0, "via": "symbol"},
        },
        src_only=[],
        bin_only=["evil"],
    )

    r = attest.attest(src, bing, align_fn=align_fn)

    # The injected node surfaces in the money bucket.
    assert "evil" in r["shipped_unexplained"]
    # The new flow between known nodes surfaces as an unexplained edge.
    assert ["main", "helper", "sends"] in r["unexplained_edges"]
    # The explained edge is NOT flagged.
    assert ["main", "helper", "calls"] not in r["unexplained_edges"]
    # Integrity drops below the clean case: 2 of 3 binary nodes explained.
    assert r["integrity"] < 1.0
    assert abs(r["integrity"] - (2.0 / 3.0)) < 1e-9
    assert 0.0 <= r["integrity"] <= 1.0


def test_tree_shaken_source_node_is_source_only_not_an_integrity_hit():
    # Source has a dead/tree-shaken "unused" that did not ship. The binary is
    # fully explained, so integrity stays high -- integrity is about the BINARY.
    src = _graph(["main", "unused"], [])
    bing = _graph(["main"], [])
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=["unused"],
        bin_only=[],
    )

    r = attest.attest(src, bing, align_fn=align_fn)

    assert "unused" in r["source_only"]
    assert r["shipped_unexplained"] == []
    # Every binary node explained -> full integrity, despite the unshipped source node.
    assert r["integrity"] == 1.0
    assert 0.0 <= r["integrity"] <= 1.0


def test_integrity_is_bounded_and_low_confidence_counts_partially():
    src = _graph(["a"], [])
    bing = _graph(["a", "b"], [])  # b unmatched
    align_fn = _fake_align(
        matches={"a": {"bin": "a", "confidence": 0.5, "via": "fuzzy"}},
        src_only=[],
        bin_only=["b"],
    )

    r = attest.attest(src, bing, align_fn=align_fn)

    # One partial (0.5) match over two binary nodes -> 0.25.
    assert abs(r["integrity"] - 0.25) < 1e-9
    assert 0.0 <= r["integrity"] <= 1.0


def test_format_report_mentions_score_and_stable_caveat():
    src = _graph(["main", "helper"], [("main", "helper", "calls")])
    bing = _graph(
        ["main", "helper", "evil"],
        [("main", "helper", "calls"), ("main", "helper", "sends"), ("main", "evil", "calls")],
    )
    align_fn = _fake_align(
        matches={
            "main": {"bin": "main", "confidence": 1.0, "via": "symbol"},
            "helper": {"bin": "helper", "confidence": 1.0, "via": "symbol"},
        },
        src_only=[],
        bin_only=["evil"],
    )
    r = attest.attest(src, bing, align_fn=align_fn)

    report = attest.format_report(r)
    assert isinstance(report, str)
    # Leads with the score.
    assert "Integrity:" in report
    assert f"{r['integrity']:.3f}" in report
    # Names the unexplained residual.
    assert "evil" in report
    # Stable, honest caveat substring -- never over-claims malice.
    assert "not proven-malicious" in report
    assert "could not confidently match" in report


# --- Residual clustering (GOAL 2) ----------------------------------------------


def test_clean_case_has_no_residual_clusters():
    src = _graph(["main"], [])
    bing = _graph(["main"], [])
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=[], bin_only=[],
    )
    r = attest.attest(src, bing, align_fn=align_fn)
    assert r["residual_clusters"] == []


def test_injected_subgraph_is_one_connected_cluster():
    # A single foreign 4-node subsystem (f0->f1->f2->f3->f0) bridged to a matched
    # node must land ENTIRELY in the residual and read as ONE cluster of size 4.
    src = _graph(["main"], [])
    bing = _graph(
        ["main", "f0", "f1", "f2", "f3"],
        [
            ("main", "f0", "calls"),   # bridge: main matched -> not an internal edge
            ("f0", "f1", "calls"),
            ("f1", "f2", "calls"),
            ("f2", "f3", "sends"),
            ("f3", "f0", "loops"),
        ],
    )
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=[], bin_only=["f0", "f1", "f2", "f3"],
    )
    r = attest.attest(src, bing, align_fn=align_fn)

    clusters = r["residual_clusters"]
    assert len(clusters) == 1
    c = clusters[0]
    assert c["size"] == 4
    assert set(c["nodes"]) == {"f0", "f1", "f2", "f3"}
    assert c["internal_edges"] == 4  # the 4 edges among the foreign nodes
    assert set(c["relations"]) == {"calls", "sends", "loops"}
    assert c["labels"]  # non-empty representative labels


def test_two_separate_subsystems_are_two_clusters():
    src = _graph(["main"], [])
    bing = _graph(
        ["main", "a0", "a1", "a2", "b0", "b1"],
        [
            ("main", "a0", "calls"),
            ("a0", "a1", "calls"), ("a1", "a2", "calls"),  # subsystem A (3 nodes)
            ("main", "b0", "calls"),
            ("b0", "b1", "sends"),                          # subsystem B (2 nodes)
        ],
    )
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=[], bin_only=["a0", "a1", "a2", "b0", "b1"],
    )
    r = attest.attest(src, bing, align_fn=align_fn)

    clusters = r["residual_clusters"]
    assert len(clusters) == 2
    # Largest first.
    assert [c["size"] for c in clusters] == [3, 2]
    assert set(clusters[0]["nodes"]) == {"a0", "a1", "a2"}
    assert set(clusters[1]["nodes"]) == {"b0", "b1"}


def test_oversized_component_subdivides_by_relation_signature():
    # One connected component, but two distinct internal relation roles. With a
    # small max_cluster_size it splits by relation/neighbourhood instead of
    # reporting one blob.
    nodes = ["main"] + ["x%d" % i for i in range(3)] + ["y%d" % i for i in range(3)]
    edges = [("main", "x0", "calls")]
    # x-chain via "reads", y-chain via "writes", bridged x0->y0 so it is ONE
    # connected component.
    edges += [("x0", "x1", "reads"), ("x1", "x2", "reads")]
    edges += [("y0", "y1", "writes"), ("y1", "y2", "writes")]
    edges += [("x0", "y0", "reads")]
    bing = _graph(nodes, edges)
    src = _graph(["main"], [])
    unexplained = ["x0", "x1", "x2", "y0", "y1", "y2"]
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=[], bin_only=unexplained,
    )

    whole = attest.attest(src, bing, align_fn=align_fn)
    assert len(whole["residual_clusters"]) == 1  # default: one connected subsystem

    split = attest.attest(src, bing, align_fn=align_fn, max_cluster_size=4)
    assert len(split["residual_clusters"]) >= 2  # subdivided by relation signature


def test_residual_clusters_are_deterministic():
    src = _graph(["main"], [])
    nodes = ["main", "f0", "f1", "f2"]
    edges = [("main", "f0", "calls"), ("f0", "f1", "calls"), ("f1", "f2", "calls")]
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=[], bin_only=["f0", "f1", "f2"],
    )
    a = attest.attest(_graph(nodes, edges), _graph(nodes, edges), align_fn=align_fn)
    # reversed declaration order -> identical clustering
    rev_nodes = list(reversed(nodes))
    rev_edges = list(reversed(edges))
    b = attest.attest(_graph(rev_nodes, rev_edges), _graph(rev_nodes, rev_edges),
                      align_fn=align_fn)
    assert a["residual_clusters"] == b["residual_clusters"]


def test_empty_and_malformed_graphs_do_not_raise():
    # Fail-safe: empty residual -> no clusters; malformed edges tolerated.
    empty = attest.attest({"nodes": [], "links": []}, {"nodes": [], "links": []},
                          align_fn=_fake_align({}, [], []))
    assert empty["residual_clusters"] == []

    bing = {"nodes": [{"id": "g0"}, {"id": "g1"}, {"not_id": 1}],
            "links": [{"source": "g0", "target": "g1", "relation": "x"},
                      "bogus", {"source": "g0"}]}
    align_fn = _fake_align(matches={}, src_only=[], bin_only=["g0", "g1"])
    r = attest.attest({"nodes": [], "links": []}, bing, align_fn=align_fn)
    assert len(r["residual_clusters"]) == 1
    assert r["residual_clusters"][0]["size"] == 2


def test_format_report_lists_clusters_bounded():
    src = _graph(["main"], [])
    bing = _graph(
        ["main", "f0", "f1", "f2"],
        [("main", "f0", "calls"), ("f0", "f1", "calls"), ("f1", "f2", "calls")],
    )
    align_fn = _fake_align(
        matches={"main": {"bin": "main", "confidence": 1.0, "via": "symbol"}},
        src_only=[], bin_only=["f0", "f1", "f2"],
    )
    r = attest.attest(src, bing, align_fn=align_fn)
    report = attest.format_report(r)
    assert "Residual clusters" in report
    # Bounded output: capping clusters shown is honoured.
    tiny = attest.format_report(r, max_clusters=0)
    assert "Residual clusters (grouped unexplained structure): 1" in tiny
