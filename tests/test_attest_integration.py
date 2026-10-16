"""End-to-end: does the shipped artifact match the source? (real chain, no fakes)

Exercises fingerprint -> align -> attest together on a source graph and two
shipped graphs: a clean one (should match) and a tampered one (an injected node
and a new flow should surface as the residual and drag integrity down).
"""

from reagraph import attest


def _src():
    # A tiny source graph (symbols present, as in JS/Electron/.NET/WASM).
    return {
        "nodes": [
            {"id": "s1", "label": "authenticate"},
            {"id": "s2", "label": "checkPassword"},
            {"id": "s3", "label": "loadUser"},
        ],
        "links": [
            {"source": "s1", "target": "s2", "relation": "calls"},
            {"source": "s1", "target": "s3", "relation": "calls"},
        ],
    }


def _shipped_clean():
    # Same program, different ids (relocated), same symbol names -> name match.
    return {
        "nodes": [
            {"id": "b17", "label": "authenticate"},
            {"id": "b42", "label": "checkPassword"},
            {"id": "b99", "label": "loadUser"},
        ],
        "links": [
            {"source": "b17", "target": "b42", "relation": "calls"},
            {"source": "b17", "target": "b99", "relation": "calls"},
        ],
    }


def _shipped_tampered():
    g = _shipped_clean()
    # Injected node with no source counterpart (the money bucket)...
    g["nodes"].append({"id": "bX", "label": "collect.evil-metrics"})
    # ...an exfil edge to it, and a NEW flow between two known nodes.
    g["links"].append({"source": "b17", "target": "bX", "relation": "sends"})
    g["links"].append({"source": "b42", "target": "b99", "relation": "sends"})
    return g


def test_clean_build_attests_as_matching():
    r = attest.attest(_src(), _shipped_clean())
    assert r["integrity"] > 0.9
    assert r["shipped_unexplained"] == []
    assert r["unexplained_edges"] == []


def test_tampered_build_surfaces_the_residual():
    r = attest.attest(_src(), _shipped_tampered())
    # Integrity drops because one shipped node is unexplained.
    assert r["integrity"] < 1.0
    # The injected node is the headline residual.
    assert "bX" in r["shipped_unexplained"]
    # The new flow between two known nodes is an unexplained edge.
    assert ["b42", "b99", "sends"] in r["unexplained_edges"]
    # A report reads cleanly and does not over-claim malice.
    text = attest.format_report(r)
    assert "Integrity" in text and "bX" in text


def test_integrity_is_bounded():
    for graphs in ((_src(), _shipped_clean()), (_src(), _shipped_tampered())):
        assert 0.0 <= attest.attest(*graphs)["integrity"] <= 1.0
