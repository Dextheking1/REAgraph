"""Honesty guarantees: observed vs inferred vs unverified, tested without REA."""

from reagraph import coverage


def _sample_graph():
    """A small graph mixing EXTRACTED and INFERRED, with one edge pointing at a
    target that has no node entry of its own ("ghost")."""
    return {
        "directed": True,
        "multigraph": False,
        "nodes": [
            {"id": "main", "label": "main()", "confidence": "EXTRACTED"},
            {"id": "auth", "label": "auth()", "confidence": "EXTRACTED"},
            {"id": "maybe_crypto", "label": "crypto?", "confidence": "INFERRED"},
        ],
        "links": [
            # Fully observed: both ends are nodes, edge is EXTRACTED.
            {"source": "main", "target": "auth", "relation": "calls",
             "confidence": "EXTRACTED"},
            # Inferred edge into an INFERRED-only node -> frontier.
            {"source": "auth", "target": "maybe_crypto", "relation": "calls",
             "confidence": "INFERRED"},
            # Inferred edge into a ghost target (no node entry) -> frontier.
            {"source": "main", "target": "ghost_fn", "relation": "calls",
             "confidence": "INFERRED"},
        ],
    }


def test_coverage_counts_and_verified_fraction():
    cov = coverage.coverage(_sample_graph())

    assert cov["nodes"]["total"] == 3
    assert cov["nodes"]["confidence"]["EXTRACTED"] == 2
    assert cov["nodes"]["confidence"]["INFERRED"] == 1

    assert cov["edges"]["total"] == 3
    assert cov["edges"]["confidence"]["EXTRACTED"] == 1
    assert cov["edges"]["confidence"]["INFERRED"] == 2

    # Only "ghost_fn" is referenced by an edge but has no node entry.
    assert cov["ghost_targets"] == ["ghost_fn"]

    # 1 of 3 edges is observed.
    assert cov["verified_fraction"] == 1 / 3


def test_frontier_is_exactly_the_unverified_hops():
    front = coverage.frontier(_sample_graph())

    pairs = {(f["source"], f["target"], f["relation"]) for f in front}
    # The two inferred, not-yet-verified hops...
    assert ("auth", "maybe_crypto", "calls") in pairs
    assert ("main", "ghost_fn", "calls") in pairs
    # ...and NOT the fully-observed edge.
    assert ("main", "auth", "calls") not in pairs
    assert len(front) == 2

    # Deterministic ordering.
    assert front == coverage.frontier(_sample_graph())
    assert [f["source"] for f in front] == ["auth", "main"]

    # Every frontier item carries a human reason.
    assert all(f["reason"] for f in front)


def test_frontier_excludes_inferred_edge_into_observed_node():
    # An INFERRED edge whose target IS observed is not a verification gap.
    g = {
        "nodes": [
            {"id": "a", "confidence": "EXTRACTED"},
            {"id": "b", "confidence": "EXTRACTED"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls",
             "confidence": "INFERRED"},
        ],
    }
    assert coverage.frontier(g) == []


def test_gaps_report_is_honest_about_absence():
    report = coverage.gaps_report(_sample_graph())

    # The anti-false-confidence framing is present (stable substring).
    assert "Absence of a finding is NOT proof of absence" in report

    # It names the unverified items plainly.
    assert "ghost_fn" in report
    assert "maybe_crypto" in report
    # It distinguishes observed from inferred.
    assert "OBSERVED" in report
    assert "ONLY INFERRED" in report
    assert "UNVERIFIED / UNKNOWN" in report


def test_empty_graph_has_zero_verified_fraction():
    cov = coverage.coverage({"nodes": [], "links": []})
    assert cov["verified_fraction"] == 0.0
    assert cov["ghost_targets"] == []
    assert coverage.frontier({"nodes": [], "links": []}) == []
