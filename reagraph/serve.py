"""MCP server: expose reagraph as reverse-engineering memory to any agent.

Runs as an MCP stdio server (Claude Code / opencode / Codex) exposing four
tools over the same persistent node-link graph the CLI uses:

    remember(finding) -> fold a JAG/node-link dict OR a file path into memory
    recall(query)     -> a compact relevant slice (delegates to graphify, or
                         falls back to top-degree nodes when graphify is absent)
    diff(a, b)        -> structural, byte-agnostic diff report of two graphs
    stats()           -> accumulated-graph coverage

The tool bodies live as plain `_remember/_recall/_diff/_stats` functions so they
are unit-testable without `mcp` or a live client. `mcp` is imported lazily inside
`build_server()`, so this module imports fine when `mcp` is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import diff as diffmod
from . import memory
from .ingest import load_any

DEFAULT_GRAPH = "reagraph.json"


def _graph_path(graph: str | None = None) -> str:
    """Resolve the persistent graph path: explicit arg > env > default."""
    return graph or os.environ.get("REAGRAPH_GRAPH") or DEFAULT_GRAPH


def _as_fragment(finding) -> dict:
    """Accept a node-link/JAG dict, a JSON string, or a file path -> fragment."""
    if isinstance(finding, dict):
        return load_any_dict(finding)
    if isinstance(finding, str):
        p = Path(finding)
        if p.exists():
            return load_any(p)
        # Not a path: try parsing it as inline JSON.
        try:
            return load_any_dict(json.loads(finding))
        except (ValueError, TypeError):
            raise ValueError(f"finding is neither an existing file nor valid JSON: {finding!r}")
    raise TypeError(f"unsupported finding type: {type(finding).__name__}")


def load_any_dict(doc: dict) -> dict:
    """In-memory twin of ingest.load_any: node-link vs JAG auto-detect on a dict."""
    # Write-free reuse of ingest's detection by round-tripping through ingest_jag
    # when the doc looks like a JAG, else treat as node-link.
    from .ingest import ingest_jag

    if "links" in doc:
        out = dict(doc)
        out.setdefault("nodes", [])
        return out
    looks_like_jag = any(
        "epistemic" in e for e in doc.get("edges", []) if isinstance(e, dict)
    ) or any("epistemic" in n for n in doc.get("nodes", []) if isinstance(n, dict))
    if looks_like_jag:
        return ingest_jag(doc)
    return {
        "directed": True,
        "multigraph": False,
        "nodes": doc.get("nodes", []),
        "links": doc.get("edges", []),
    }


def _remember(graph_path: str, finding) -> dict:
    """Fold a finding into the persistent graph; return the stats delta."""
    graph = memory.load(graph_path)
    before = memory.stats(graph)
    memory.merge(graph, _as_fragment(finding))
    memory.save(graph, graph_path)
    after = memory.stats(graph)
    return {
        "graph": graph_path,
        "before": before,
        "after": after,
        "delta": {
            "nodes": after["nodes"] - before["nodes"],
            "edges": after["edges"] - before["edges"],
        },
    }


def _top_degree_nodes(graph: dict, limit: int = 10) -> list[dict]:
    """Fallback relevance: nodes ranked by incident-edge count."""
    deg: dict = {}
    for e in graph.get("links", []) or graph.get("edges", []):
        for endpoint in (e.get("source"), e.get("target")):
            if endpoint is not None:
                deg[endpoint] = deg.get(endpoint, 0) + 1
    labels = {n.get("id"): n.get("label", n.get("id")) for n in graph.get("nodes", [])}
    ranked = sorted(deg.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [{"id": nid, "label": labels.get(nid, nid), "degree": d} for nid, d in ranked[:limit]]


def _recall(graph_path: str, query: str) -> str:
    """Return a compact relevant slice. Uses graphify if on PATH, else a
    top-degree fallback. Never crashes if graphify is absent or errors."""
    if shutil.which("graphify"):
        try:
            proc = subprocess.run(
                ["graphify", "query", query, "--graph", graph_path],
                capture_output=True,
                text=True,
            )
            out = (proc.stdout or "").strip()
            if proc.returncode == 0 and out:
                return out
            # Fall through to the local fallback on non-zero/empty output.
        except OSError:
            pass  # graphify vanished between the which() and the run(); fall back.
    graph = memory.load(graph_path)
    top = _top_degree_nodes(graph)
    if not top:
        return f"(graphify unavailable; empty graph at {graph_path})"
    lines = [f"(graphify unavailable) top {len(top)} nodes by degree for {query!r}:"]
    lines += [f"  {n['degree']:>3}  {n['id']}  ({n['label']})" for n in top]
    return "\n".join(lines)


def _diff(a, b) -> str:
    """Structural, byte-agnostic diff report of two graph files/dicts."""
    ga = a if isinstance(a, dict) else load_any(a)
    gb = b if isinstance(b, dict) else load_any(b)
    return diffmod.format_report(diffmod.diff(ga, gb))


def _stats(graph_path: str) -> dict:
    """Accumulated-graph coverage for the persistent graph."""
    return memory.stats(memory.load(graph_path))


def build_server():
    """Create the MCP server with the four tools registered.

    Lazily imports `mcp`; raises a clear RuntimeError if it is not installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without mcp
        raise RuntimeError(
            "the 'mcp' package is required to run the reagraph MCP server; "
            "install it with: pip install mcp"
        ) from exc

    server = FastMCP("reagraph")

    @server.tool()
    def remember(finding, graph: str | None = None) -> dict:
        """Fold a JAG/node-link finding (dict, JSON string, or file path) into
        persistent memory. Returns the stats delta."""
        return _remember(_graph_path(graph), finding)

    @server.tool()
    def recall(query: str, graph: str | None = None) -> str:
        """Return a compact relevant slice of the graph for a query."""
        return _recall(_graph_path(graph), query)

    @server.tool()
    def diff(a: str, b: str) -> str:
        """Structural, byte-agnostic diff report of two graph files."""
        return _diff(a, b)

    @server.tool()
    def stats(graph: str | None = None) -> dict:
        """Show accumulated-graph coverage."""
        return _stats(_graph_path(graph))

    return server


def main(argv=None) -> int:
    """Run the MCP stdio server."""
    build_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
