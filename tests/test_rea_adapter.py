"""The REA-output adapter, tested against realistic REA tool-result shapes.

No REA/MCP server is needed: the fixtures reproduce the output shapes REA's
`analyze_javascript_application` tool returns -- a JavaScript Application Graph
(nodes with `node_id`/`kind`/`observations`, edges with
`source_node_id`/`target_node_id`/`relation`, each carrying an `evidence` block
whose `state` is the epistemic marker) under `structuredContent.normalized_result`.
Shapes grounded in REA's `docs/javascript-application-graph.md` and
`src/domain/javascript/javascriptApplicationGraphSchemas.ts`.

We assert the mapping (relations, confidence from epistemic state, context
`rea:<relation>`, inferred -> INFERRED, unavailable edge dropped), that an
unknown/garbage result fails open to one evidence node without raising, and that
every fragment merges cleanly via `reagraph.memory.merge`.
"""

from reagraph import memory, rea_adapter


def _evidence(state, path="dist/app.js", authority="ast-static-analysis"):
    """A minimal REA evidence block in the real shape (state is the epistemic marker)."""
    return {
        "authority": authority,
        "state": state,
        "confidence": "exact" if state == "observed" else "high",
        "artifact": {"available": True, "artifact_id": "art_" + "2" * 64, "sha256": "2" * 64},
        "location": {"available": True, "value": {"kind": "artifact-path", "path": path}},
        "extractor": {"name": "rea", "version": "1.0.0", "operation": "observe", "executable_sha256": None},
        "coverage": {"status": "complete", "truncated": False, "omitted_count": 0, "limits": []},
        "limitations": [] if state == "observed" else ["Inferred from static syntax."],
        "evidence_ids": [],
    }


def _node(node_id, kind, label, state, path="dist/app.js", authority="ast-static-analysis"):
    return {
        "node_id": node_id,
        "kind": kind,
        "identity": {"strategy": "artifact-local-key", "stability": "artifact-version"},
        "observations": [
            {"label": label, "properties": {}, "evidence": _evidence(state, path, authority)}
        ],
    }


def _edge(source, target, relation, state, path="dist/app.js"):
    return {
        "edge_id": f"jag_edge_{relation}_{source[-4:]}_{target[-4:]}",
        "source_node_id": source,
        "target_node_id": target,
        "relation": relation,
        "properties": {},
        "evidence": _evidence(state, path),
    }


def _js_analysis_result():
    """A realistic `analyze_javascript_application` MCP result: Electron app with
    a package -> asar -> preload chain, a contextBridge API, an IPC channel +
    handler, a native add-on import, and one `unavailable` (ambiguous) edge."""
    pkg = "jag_node_" + "a" * 64
    asar = "jag_node_" + "b" * 64
    preload = "jag_node_" + "c" * 64
    bridge = "jag_node_" + "d" * 64
    channel = "jag_node_" + "e" * 64
    handler = "jag_node_" + "f" * 64
    asset = "jag_node_" + "1" * 64
    addon = "jag_node_" + "2" * 64
    graph = {
        "schema": "JavaScriptApplicationGraph",
        "graph_id": "jag_" + "9" * 64,
        "root_node_ids": [pkg],
        "nodes": [
            _node(pkg, "package", "Desktop package", "observed", "package.json", "artifact-bytes"),
            _node(asar, "artifact", "resources/app.asar", "observed", "resources/app.asar", "artifact-bytes"),
            _node(preload, "electron-preload", "preload", "observed", "dist/preload.js"),
            _node(bridge, "context-bridge-api", "desktopApi", "observed", "dist/preload.js"),
            _node(channel, "ipc-channel", "project:open", "observed", "dist/preload.js"),
            _node(handler, "ipc-handler", "project open handler", "observed", "dist/main.js"),
            _node(asset, "javascript-asset", "dist/main.js", "observed", "dist/main.js"),
            _node(addon, "native-addon", "synthetic.node", "observed", "native/synthetic.node", "native-analysis-provider"),
        ],
        "edges": [
            _edge(pkg, asar, "contains", "observed", "resources/app.asar"),
            _edge(asar, preload, "contains", "observed", "dist/preload.js"),
            _edge(preload, bridge, "exposes", "observed", "dist/preload.js"),
            # Heuristic static pairing of a contextBridge call to an IPC channel.
            _edge(bridge, channel, "invokes", "inferred", "dist/preload.js"),
            _edge(channel, handler, "handles", "observed", "dist/main.js"),
            # Static `require` of a native binding -> import edge.
            _edge(asset, addon, "imports", "observed", "dist/main.js"),
            # Dynamic/ambiguous IPC: retained by REA without a resolvable target.
            _edge(channel, handler, "sends", "unavailable", "dist/main.js"),
        ],
        "coverage": {"status": "complete", "truncated": False, "omitted_count": 0, "limits": []},
        "limitations": [],
    }
    # Evidence-producing tools nest the graph under structuredContent.normalized_result.
    return {
        "structuredContent": {"evidence_id": "ev_" + "0" * 32, "normalized_result": graph},
        "isError": False,
    }


def test_js_analysis_maps_nodes_edges_confidence_and_context():
    frag = rea_adapter.to_fragment("analyze_javascript_application", _js_analysis_result())

    # All 8 entities became nodes, keyed by REA node_id.
    assert len(frag["nodes"]) == 8
    by_id = {n["id"]: n for n in frag["nodes"]}
    preload = next(n for n in frag["nodes"] if n["label"] == "preload")
    assert preload["confidence"] == "EXTRACTED"  # observed structure
    assert preload["rea_kind"] == "electron-preload"
    assert preload["source_file"] == "dist/preload.js"

    # The native add-on is carried as a node (native-symbol/import coverage).
    assert any(n["rea_kind"] == "native-addon" for n in frag["nodes"])

    # Relations preserved; every edge context is rea:<relation>.
    rels = {(e["relation"], e["confidence"], e["context"]) for e in frag["links"]}
    assert ("contains", "EXTRACTED", "rea:contains") in rels
    assert ("exposes", "EXTRACTED", "rea:exposes") in rels
    assert ("handles", "EXTRACTED", "rea:handles") in rels
    assert ("imports", "EXTRACTED", "rea:imports") in rels
    for e in frag["links"]:
        assert e["context"] == f"rea:{e['relation']}"


def test_inferred_becomes_INFERRED_and_unavailable_edge_dropped():
    frag = rea_adapter.to_fragment("analyze_javascript_application", _js_analysis_result())

    # The heuristic `invokes` edge is INFERRED, not EXTRACTED.
    invokes = [e for e in frag["links"] if e["relation"] == "invokes"]
    assert len(invokes) == 1
    assert invokes[0]["confidence"] == "INFERRED"
    assert invokes[0]["context"] == "rea:invokes"

    # The `unavailable` (ambiguous) `sends` edge asserts nothing -> dropped.
    assert not any(e["relation"] == "sends" for e in frag["links"])
    # 7 edges in, 1 dropped -> 6 links out.
    assert len(frag["links"]) == 6


def test_fragment_merges_cleanly_into_memory():
    frag = rea_adapter.to_fragment("analyze_javascript_application", _js_analysis_result())
    graph = memory.merge(memory.empty_graph(), frag)
    stats = memory.stats(graph)
    assert stats["nodes"] == 8
    assert stats["edges"] == 6
    assert stats["edge_confidence"]["EXTRACTED"] == 5  # all but the inferred invokes
    assert stats["edge_confidence"]["INFERRED"] == 1

    # Re-merging the same fragment dedups rather than duplicating.
    memory.merge(graph, frag)
    assert memory.stats(graph)["nodes"] == 8
    assert memory.stats(graph)["edges"] == 6


def test_unknown_tool_fails_open_to_single_evidence_node():
    # An unknown REA tool returning some opaque payload must not be lost or fatal.
    result = {"structuredContent": {"normalized_result": {"functions": [1, 2, 3]}}}
    frag = rea_adapter.to_fragment("inspect_macho", result)

    assert len(frag["nodes"]) == 1
    assert frag["links"] == []
    node = frag["nodes"][0]
    assert node["file_type"] == "evidence"
    assert node["rea_tool"] == "inspect_macho"
    assert node["id"].startswith("rea_evidence_inspect_macho_")
    assert "functions" in node["rea_summary"]  # raw summary preserved, nothing lost

    # Stable id: the same result hashes to the same evidence node.
    again = rea_adapter.to_fragment("inspect_macho", result)
    assert again["nodes"][0]["id"] == node["id"]

    # And it merges cleanly.
    merged = memory.merge(memory.empty_graph(), frag)
    assert memory.stats(merged)["nodes"] == 1


def test_garbage_input_never_raises_and_fails_open():
    # Supported tool, but a totally unexpected shape -> fail open, no raise.
    for bad in [None, {}, {"unexpected": object()}, "not a dict", 42, []]:
        frag = rea_adapter.to_fragment("analyze_javascript_application", bad)
        assert frag["links"] == []
        assert len(frag["nodes"]) == 1
        assert frag["nodes"][0]["file_type"] == "evidence"
        # Always mergeable.
        memory.merge(memory.empty_graph(), frag)
