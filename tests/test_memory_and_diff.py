"""The two core guarantees, tested without REA/Ghidra installed."""

from reagraph import diff, memory
from reagraph.ingest import ingest_jag


def test_merge_dedups_and_upgrades_confidence():
    # First pass: we only INFERRED that check() calls net().
    base = {
        "nodes": [{"id": "check", "label": "check()", "confidence": "INFERRED"}],
        "links": [{"source": "check", "target": "net", "relation": "calls", "confidence": "INFERRED"}],
    }
    # Second pass actually OBSERVED the same node and edge.
    frag = {
        "nodes": [{"id": "check", "label": "check()", "confidence": "EXTRACTED"},
                  {"id": "net", "label": "net()", "confidence": "EXTRACTED"}],
        "links": [{"source": "check", "target": "net", "relation": "calls", "confidence": "EXTRACTED"}],
    }
    g = memory.merge(dict(base), frag)
    s = memory.stats(g)
    # No duplicate node or edge; the new node was added.
    assert s["nodes"] == 2 and s["edges"] == 1
    # Re-observation UPGRADED confidence instead of duplicating.
    assert s["edge_confidence"]["EXTRACTED"] == 1 and s["edge_confidence"]["INFERRED"] == 0
    check = next(n for n in g["nodes"] if n["id"] == "check")
    assert check["confidence"] == "EXTRACTED"


def test_diff_is_byte_agnostic():
    # Same program, but every symbol MOVED in the file (REA #4520 scenario).
    v1 = {
        "nodes": [{"id": "a", "source_location": "L10"}, {"id": "b", "source_location": "L50"}],
        "links": [{"source": "a", "target": "b", "relation": "calls"}],
    }
    v2 = {
        "nodes": [{"id": "a", "source_location": "L900"}, {"id": "b", "source_location": "L950"}],
        "links": [{"source": "a", "target": "b", "relation": "calls"}],
    }
    r = diff.diff(v1, v2)
    # Byte location changed, structure did not -> nothing is "changed".
    assert r["summary"] == {
        "nodes_added": 0, "nodes_removed": 0, "edges_added": 0,
        "edges_removed": 0, "rewired_nodes": 0, "unchanged_nodes": 2,
    }


def test_diff_catches_real_change():
    v1 = {"nodes": [{"id": "a"}, {"id": "b"}], "links": [{"source": "a", "target": "b", "relation": "calls"}]}
    # v2 adds an exfil call a -> evil.
    v2 = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "evil"}],
        "links": [{"source": "a", "target": "b", "relation": "calls"},
                  {"source": "a", "target": "evil", "relation": "sends"}],
    }
    r = diff.diff(v1, v2)
    assert r["nodes_added"] == ["evil"]
    assert ["a", "evil", "sends"] in r["edges_added"]
    assert "a" in r["rewired_nodes"]


def test_ingest_jag_maps_epistemic_and_drops_unavailable():
    jag = {
        "nodes": [{"id": "f", "epistemic": "observed"}, {"id": "g", "epistemic": "inferred"}],
        "edges": [
            {"source": "f", "target": "g", "relation": "invokes", "epistemic": "observed"},
            {"source": "g", "target": "h", "relation": "calls", "epistemic": "unavailable"},
        ],
    }
    frag = ingest_jag(jag)
    conf = {n["id"]: n["confidence"] for n in frag["nodes"]}
    assert conf == {"f": "EXTRACTED", "g": "INFERRED"}
    # unavailable edge dropped; observed edge kept with provenance preserved.
    assert len(frag["links"]) == 1
    assert frag["links"][0]["context"] == "rea:invokes"
