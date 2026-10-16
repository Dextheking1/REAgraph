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
