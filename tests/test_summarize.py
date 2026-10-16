"""The compaction guarantee: a huge graph collapses to a bounded overview."""

from reagraph import summarize


def _hub_and_two_clusters():
    """A few hundred nodes: one big star around `hubA`, a smaller star around
    `hubB`, and a third disconnected pair -- three weakly-connected clusters."""
    nodes = [{"id": "hubA", "label": "hubA()"}, {"id": "hubB", "label": "hubB()"}]
    links = []
    # hubA is the biggest hub: 200 leaves call it.
    for i in range(200):
        nid = f"a{i}"
        nodes.append({"id": nid, "label": f"{nid}()"})
        links.append({"source": nid, "target": "hubA", "relation": "calls",
                      "confidence": "EXTRACTED"})
    # hubB is a smaller hub: 50 leaves call it.
    for i in range(50):
        nid = f"b{i}"
        nodes.append({"id": nid, "label": f"{nid}()"})
        links.append({"source": nid, "target": "hubB", "relation": "calls",
                      "confidence": "INFERRED"})
    # A third, disconnected cluster: x -> y.
    nodes.append({"id": "x", "label": "x()"})
    nodes.append({"id": "y", "label": "y()"})
    links.append({"source": "x", "target": "y", "relation": "calls",
                  "confidence": "EXTRACTED"})
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


def test_god_nodes_rank_hubs_first_and_are_capped():
    g = _hub_and_two_clusters()
    gods = summarize.god_nodes(g, top=5)
    assert len(gods) == 5
    # The two hubs come first, biggest first.
    assert gods[0]["id"] == "hubA" and gods[0]["degree"] == 200
    assert gods[1]["id"] == "hubB" and gods[1]["degree"] == 50
    assert gods[0]["label"] == "hubA()"


def test_god_nodes_deterministic_tie_break_by_id():
    g = _hub_and_two_clusters()
    # All the leaves have degree 1; ties must break by id ascending.
    gods = summarize.god_nodes(g, top=50)
    tail = [x for x in gods if x["degree"] == 1]
    assert tail == sorted(tail, key=lambda d: str(d["id"]))


def test_components_find_the_three_clusters():
    g = _hub_and_two_clusters()
    comps = summarize.components(g)
    assert len(comps) == 3
    # Ordered by size descending: hubA star (201), hubB star (51), then x/y (2).
    assert [len(c) for c in comps] == [201, 51, 2]
    assert "hubA" in comps[0]
    assert sorted(comps[2]) == ["x", "y"]


def test_summarize_is_bounded_even_for_a_big_graph():
    # 5000 leaves all pointing at one hub -> a graph far too big to read raw.
    nodes = [{"id": "hub", "label": "hub()"}]
    links = []
    for i in range(5000):
        nid = f"n{i}"
        nodes.append({"id": nid})
        links.append({"source": nid, "target": "hub", "relation": "calls"})
    g = {"nodes": nodes, "links": links}

    s = summarize.summarize(g, top=20, max_clusters=10)
    assert s["nodes"] == 5001
    assert len(s["god_nodes"]) <= 20
    assert len(s["largest_clusters"]) <= 10
    # The totals still reflect the real graph size even though lists are capped.
    assert s["god_nodes"][0]["id"] == "hub"
    assert s["num_components"] == 1


def test_format_summary_is_a_short_string():
    g = _hub_and_two_clusters()
    text = summarize.format_summary(summarize.summarize(g))
    assert isinstance(text, str)
    assert "Graph overview" in text
    # Short: an overview, not a dump of the whole graph.
    assert len(text.splitlines()) < 60


def test_scale_10k_chain_summarizes_without_error():
    # A 10k-node chain: n0 -> n1 -> ... -> n9999 (one component, no hubs).
    nodes = [{"id": f"n{i}"} for i in range(10000)]
    links = [{"source": f"n{i}", "target": f"n{i + 1}", "relation": "calls"}
             for i in range(9999)]
    g = {"nodes": nodes, "links": links}

    s = summarize.summarize(g, top=20, max_clusters=10)
    assert s["nodes"] == 10000
    assert s["num_components"] == 1
    assert len(s["god_nodes"]) <= 20
    assert isinstance(summarize.format_summary(s), str)
