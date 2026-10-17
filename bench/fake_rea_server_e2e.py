"""A stand-in REA MCP server for the end-to-end benchmark.

It speaks the real MCP stdio protocol and advertises REA's static-JS tool
(`analyze_javascript_application`), returning a realistic JavaScript Application
Graph (JAG) so the REAL reagraph client/adapter/bridge run against a real MCP
transport. The analysis itself is canned -- point REAGRAPH_REA_CMD at
`npx -y rea-agents@latest` to run against the real REA instead.

Two variants keyed on the analyzed path: a clean Electron app, and a "v2" with a
relocated layout plus an injected exfil node, so diff and attest have something to
find.
"""

from __future__ import annotations

import json
import sys


def _obs(label, state="observed"):
    return [{"label": label, "properties": {}, "evidence": {"state": state, "location": {"artifact": "app.asar"}}}]


def _node(nid, kind, label, state="observed"):
    return {"node_id": nid, "kind": kind, "identity": nid, "observations": _obs(label, state)}


def _edge(eid, s, t, rel, state="observed"):
    return {"edge_id": eid, "source_node_id": s, "target_node_id": t, "relation": rel,
            "properties": {}, "evidence": {"state": state, "location": {"artifact": "app.asar"}}}


def _clean():
    nodes = [
        _node("n_pkg", "package", "acme-desktop"),
        _node("n_main", "module", "main.js"),
        _node("n_renderer", "module", "renderer.js"),
        _node("n_preload", "module", "preload.js"),
        _node("n_electron", "import", "electron"),
        _node("n_https", "import", "https"),
        _node("n_ipc", "ipc-channel", "app:getUserProfile"),
        _node("n_auth", "function", "authenticate"),
        _node("n_session", "function", "createSession"),
    ]
    edges = [
        _edge("e1", "n_pkg", "n_main", "contains"),
        _edge("e2", "n_pkg", "n_renderer", "contains"),
        _edge("e3", "n_main", "n_preload", "loads"),
        _edge("e4", "n_main", "n_electron", "imports"),
        _edge("e5", "n_main", "n_https", "imports"),
        _edge("e6", "n_preload", "n_ipc", "exposes"),
        _edge("e7", "n_main", "n_ipc", "handles"),
        _edge("e8", "n_main", "n_auth", "calls"),
        _edge("e9", "n_auth", "n_session", "calls", state="inferred"),
    ]
    return {"schema": "JavaScriptApplicationGraph", "root_node_ids": ["n_pkg"], "nodes": nodes, "edges": edges}


def _tampered():
    g = _clean()
    # Injected exfil node + a new flow -- the thing attest/diff must surface.
    g["nodes"].append(_node("n_evil", "module", "collect.evil-metrics"))
    g["edges"].append(_edge("e10", "n_renderer", "n_evil", "sends"))
    g["edges"].append(_edge("e11", "n_session", "n_evil", "sends", state="inferred"))
    return g


def _jag_for(path: str) -> dict:
    return _tampered() if str(path).rstrip("/").endswith("v2") else _clean()


def main() -> None:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("fake-rea")

    @server.tool(name="analyze_javascript_application",
                 description="Static analysis of a JS/Electron app -> JavaScript Application Graph (stand-in).")
    def analyze_javascript_application(input_path: str = ".") -> str:  # noqa: D401
        # Arg name mirrors real REA's schema (required ``input_path``), so the
        # real reagraph client drives this stand-in with the exact arguments it
        # sends to REA. Model REA's analysis latency (real REA takes seconds to
        # minutes); 0 by default.
        import os as _os
        import time as _time
        lat = float(_os.environ.get("REA_LATENCY", "0") or 0)
        if lat > 0:
            _time.sleep(lat)
        # Return the JAG as a JSON string; the reagraph client parses the text block.
        return json.dumps(_jag_for(input_path))

    server.run(transport="stdio")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"fake_rea_server_e2e: {exc}", file=sys.stderr)
        raise
