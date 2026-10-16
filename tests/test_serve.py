"""MCP-server tool logic, tested without `mcp` or a live client.

These call the plain `_remember/_recall/_diff/_stats` functions directly, so the
graph memory wired behind the MCP tools is verified without importing `mcp`.
"""

import json

from reagraph import serve

# A first-pass JAG finding: one inferred call edge.
JAG_PASS1 = {
    "nodes": [{"id": "check", "label": "check()", "epistemic": "inferred"}],
    "edges": [{"source": "check", "target": "net", "relation": "calls", "epistemic": "inferred"}],
}
# A second pass that OBSERVED the same edge plus a new node.
JAG_PASS2 = {
    "nodes": [
        {"id": "check", "label": "check()", "epistemic": "observed"},
        {"id": "net", "label": "net()", "epistemic": "observed"},
    ],
    "edges": [{"source": "check", "target": "net", "relation": "calls", "epistemic": "observed"}],
}


def test_module_imports_without_mcp():
    # The module must import even though `mcp` is not installed here.
    assert hasattr(serve, "build_server")
    assert hasattr(serve, "main")


def test_remember_then_stats_dict_fragment(tmp_path):
    gp = str(tmp_path / "reagraph.json")
    d1 = serve._remember(gp, JAG_PASS1)
    assert d1["delta"] == {"nodes": 1, "edges": 1}

    d2 = serve._remember(gp, JAG_PASS2)
    # Re-observing the same edge adds only the new `net` node, no dup edge.
    assert d2["delta"] == {"nodes": 1, "edges": 0}

    s = serve._stats(gp)
    assert s["nodes"] == 2 and s["edges"] == 1
    # Confidence was upgraded on re-observation, not duplicated.
    assert s["edge_confidence"]["EXTRACTED"] == 1
    assert s["edge_confidence"]["INFERRED"] == 0


def test_remember_accepts_file_path(tmp_path):
    finding = tmp_path / "finding.json"
    finding.write_text(json.dumps(JAG_PASS2), encoding="utf-8")
    gp = str(tmp_path / "reagraph.json")
    d = serve._remember(gp, str(finding))
    assert d["delta"]["nodes"] == 2 and d["delta"]["edges"] == 1


def test_remember_accepts_json_string(tmp_path):
    gp = str(tmp_path / "reagraph.json")
    d = serve._remember(gp, json.dumps(JAG_PASS1))
    assert d["delta"] == {"nodes": 1, "edges": 1}


def test_remember_accepts_node_link_dict(tmp_path):
    gp = str(tmp_path / "reagraph.json")
    frag = {
        "nodes": [{"id": "a", "confidence": "EXTRACTED"}, {"id": "b", "confidence": "EXTRACTED"}],
        "links": [{"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"}],
    }
    d = serve._remember(gp, frag)
    assert d["delta"] == {"nodes": 2, "edges": 1}


def test_diff_is_byte_agnostic(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    g = {
        "nodes": [{"id": "x"}, {"id": "y"}],
        "links": [{"source": "x", "target": "y", "relation": "calls"}],
    }
    a.write_text(json.dumps(g), encoding="utf-8")
    b.write_text(json.dumps(g), encoding="utf-8")
    report = serve._diff(str(a), str(b))
    assert "Structural diff (byte-agnostic):" in report
    assert "unchanged nodes : 2" in report


def test_diff_accepts_dicts():
    a = {"nodes": [{"id": "x"}], "links": []}
    b = {"nodes": [{"id": "x"}, {"id": "z"}], "links": []}
    report = serve._diff(a, b)
    assert "added nodes     : 1" in report
    assert "+ z" in report


def test_recall_fallback_when_graphify_absent(tmp_path, monkeypatch):
    gp = str(tmp_path / "reagraph.json")
    serve._remember(gp, JAG_PASS2)
    # Pretend graphify is not on PATH.
    monkeypatch.setattr(serve.shutil, "which", lambda _name: None)
    out = serve._recall(gp, "what calls net")
    assert "graphify unavailable" in out
    # The most-connected nodes surface in the fallback slice.
    assert "check" in out and "net" in out


def test_recall_fallback_empty_graph(tmp_path, monkeypatch):
    gp = str(tmp_path / "reagraph.json")
    monkeypatch.setattr(serve.shutil, "which", lambda _name: None)
    out = serve._recall(gp, "anything")
    assert "empty graph" in out


def test_graph_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("REAGRAPH_GRAPH", str(tmp_path / "env.json"))
    assert serve._graph_path() == str(tmp_path / "env.json")
    # Explicit arg wins over env.
    assert serve._graph_path("explicit.json") == "explicit.json"
