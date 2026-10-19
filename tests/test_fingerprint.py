"""Structural fingerprints: the signals an aligner matches source<->binary on.

Small hand-built graphs are enough to pin down the guarantees: degrees count
every incident edge; name_key normalizes symbols; god_nodes ranks hubs. The
structural (Weisfeiler-Lehman) signal lives in ``reagraph.align`` and is covered
by the align tests.
"""

from reagraph import fingerprint


def _diamond():
    """A symmetric diamond:  d -> a, d -> b, a -> c, b -> c.

    `a` and `b` are structurally identical (each: one in-edge from d, one
    out-edge to c). `c` (sink) and `d` (source) are structurally different."""
    nodes = [{"id": nid, "label": f"{nid}()"} for nid in ("a", "b", "c", "d")]
    links = [
        {"source": "d", "target": "a", "relation": "calls"},
        {"source": "d", "target": "b", "relation": "calls"},
        {"source": "a", "target": "c", "relation": "calls"},
        {"source": "b", "target": "c", "relation": "calls"},
    ]
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


def test_degrees_count_every_incident_edge():
    g = _diamond()
    deg = fingerprint.degrees(g)
    assert deg == {"a": 2, "b": 2, "c": 2, "d": 2}


def test_degrees_include_endpoint_only_nodes():
    # `ghost` never appears as a node entry, only as an edge target.
    g = {"nodes": [{"id": "x"}],
         "links": [{"source": "x", "target": "ghost", "relation": "calls"}]}
    deg = fingerprint.degrees(g)
    assert deg == {"x": 1, "ghost": 1}


def test_name_key_normalizes_call_and_case():
    assert fingerprint.name_key({"label": "authenticate()"}) == \
        fingerprint.name_key({"label": "Authenticate"})
    assert fingerprint.name_key("authenticate()") == "authenticate"


def test_name_key_handles_arg_and_punctuation_noise():
    assert fingerprint.name_key("do_login(user, pw)") == "do_login"
    assert fingerprint.name_key("Foo::bar()") == "foobar"
    # Falls back to id when no label.
    assert fingerprint.name_key({"id": "Reset()"}) == "reset"


def test_name_key_returns_none_for_empty():
    assert fingerprint.name_key("") is None
    assert fingerprint.name_key("()") is None
    assert fingerprint.name_key(None) is None
    assert fingerprint.name_key({}) is None


def test_god_nodes_rank_hubs_first_capped_and_deterministic():
    # hub with 10 callers; plus a scattering of leaves (degree 1).
    nodes = [{"id": "hub"}]
    links = []
    for i in range(10):
        nid = f"n{i}"
        nodes.append({"id": nid})
        links.append({"source": nid, "target": "hub", "relation": "calls"})
    g = {"nodes": nodes, "links": links}

    gods = fingerprint.god_nodes(g, top=5)
    assert gods[0] == "hub"            # highest degree first
    assert len(gods) == 5              # capped at top
    # Deterministic: same call -> same list.
    assert fingerprint.god_nodes(g, top=5) == gods


def test_god_nodes_tie_break_by_id_ascending():
    # Three nodes all degree 1 (a-b, c alone target) -> tie-break by id.
    g = {"nodes": [{"id": "b"}, {"id": "a"}, {"id": "c"}],
         "links": [{"source": "a", "target": "b"},
                   {"source": "c", "target": "a"}]}
    # degrees: a=2, b=1, c=1 -> a first, then b, c by id ascending.
    gods = fingerprint.god_nodes(g, top=10)
    assert gods[0] == "a"
    assert gods[1:] == ["b", "c"]
