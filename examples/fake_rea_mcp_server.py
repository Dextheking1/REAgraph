#!/usr/bin/env python3
"""A tiny FAKE REA MCP server, so the REAgraph pipeline can be demoed offline.

Real REA (reverse-engineering agents) is a Node tool you normally launch with
`npx -y rea-agents@latest`. You should not need it — or Node — just to see the
client -> adapter -> bridge -> graph loop work. This module stands in for REA:
it speaks the MCP tool protocol over stdio, advertises a couple of REA-like
tools, and returns a small, realistic *canned* JS-analysis finding so a client
can connect, list tools, call a tool, and get something the bridge can ingest.

The canned result is a JAG-style doc (nodes/edges carrying an `epistemic`
state), which is exactly what `reagraph.ingest` / `reagraph.bridge` already know
how to fold into the graph. It models a small Electron-style JavaScript app:
modules, a few imports, an IPC channel, and ONE injected-looking exfil edge
(a renderer POSTing user data to an external telemetry host) marked `inferred`
so you can see observed-vs-inferred confidence flow all the way through.

Run it directly as an MCP stdio server:

    python examples/fake_rea_mcp_server.py        # needs the `mcp` package

It is deliberately dependency-light: only the optional `mcp` package is needed,
and its import is guarded with a clear message so running without it fails
loudly instead of mysteriously. The canned data lives in a module constant
(CANNED_JS_ANALYSIS) so it is easy to read and tweak.
"""

from __future__ import annotations

import json
import sys

# ---------------------------------------------------------------------------
# Canned analysis result. A JAG doc: nodes/edges with an `epistemic` state
# (observed -> EXTRACTED, inferred -> INFERRED). Kept here as a plain constant
# so it is trivial to read and edit. This is what a real REA
# "analyze-javascript-application" run would (roughly) hand back.
# ---------------------------------------------------------------------------
CANNED_JS_ANALYSIS: dict = {
    "artifact": "acme-desktop v2.3.1 (Electron)",
    "coverage": "main/renderer/preload bundles + detected IPC + network egress",
    "nodes": [
        # --- modules / bundles (directly observed in the packaged app) ---
        {"id": "main", "label": "main.js", "epistemic": "observed",
         "file_type": "code", "source_file": "app/main.js"},
        {"id": "renderer", "label": "renderer.js", "epistemic": "observed",
         "file_type": "code", "source_file": "app/renderer.js"},
        {"id": "preload", "label": "preload.js", "epistemic": "observed",
         "file_type": "code", "source_file": "app/preload.js"},
        # --- imported packages (observed in the dependency graph) ---
        {"id": "electron", "label": "electron", "epistemic": "observed",
         "file_type": "dependency", "source_file": "node_modules/electron"},
        {"id": "node:fs", "label": "fs", "epistemic": "observed",
         "file_type": "dependency"},
        {"id": "node:https", "label": "https", "epistemic": "observed",
         "file_type": "dependency"},
        # --- the IPC channel wiring renderer <-> main (observed) ---
        {"id": "ipc:app:getUserProfile", "label": "ipc channel app:getUserProfile",
         "epistemic": "observed", "file_type": "channel"},
        # --- the suspicious bit: an external telemetry/exfil endpoint. We SAW
        #     the host string, but the exfil *intent* is inferred, not proven. ---
        {"id": "host:collect.evil-metrics.example", "label": "collect.evil-metrics.example",
         "epistemic": "inferred", "file_type": "endpoint"},
    ],
    "edges": [
        # Module graph (observed).
        {"source": "main", "target": "electron", "relation": "imports", "epistemic": "observed"},
        {"source": "main", "target": "node:fs", "relation": "imports", "epistemic": "observed"},
        {"source": "renderer", "target": "electron", "relation": "imports", "epistemic": "observed"},
        {"source": "preload", "target": "electron", "relation": "imports", "epistemic": "observed"},
        # IPC wiring (observed): preload exposes a channel the renderer invokes
        # and main handles.
        {"source": "preload", "target": "ipc:app:getUserProfile", "relation": "exposes", "epistemic": "observed"},
        {"source": "renderer", "target": "ipc:app:getUserProfile", "relation": "invokes", "epistemic": "observed"},
        {"source": "main", "target": "ipc:app:getUserProfile", "relation": "handles", "epistemic": "observed"},
        # The injected-looking exfil edge: renderer pulls the user profile over
        # IPC and then POSTs it to an external host. The network call is
        # observed; that it is *exfiltration* is inferred.
        {"source": "renderer", "target": "node:https", "relation": "imports", "epistemic": "observed"},
        {"source": "renderer", "target": "host:collect.evil-metrics.example",
         "relation": "exfiltrates", "epistemic": "inferred"},
    ],
}

# A second, tiny tool result: just the channels, for a client that wants a
# cheap list without the full analysis.
CANNED_CHANNELS: dict = {
    "channels": [
        {"name": "app:getUserProfile", "direction": "renderer->main", "epistemic": "observed"},
    ],
}

# Tool advertised names (REA-like, hyphenated) kept as constants for reuse.
TOOL_ANALYZE = "analyze-javascript-application"
TOOL_CHANNELS = "list-detected-ipc-channels"


def _require_server_class():
    """Return the high-level MCP server class, across SDK versions.

    The high-level server is `FastMCP` in `mcp` 1.x and was renamed to
    `MCPServer` in `mcp` 2.x; both expose the same `name` ctor, `.tool(name=,
    description=)` decorator, and `.run(transport="stdio")`. We try both and
    exit with a clear, actionable message if neither (nor `mcp` itself) is
    available.
    """
    # mcp 1.x
    try:
        from mcp.server.fastmcp import FastMCP
        return FastMCP
    except Exception:
        pass
    # mcp 2.x
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except Exception as exc:
        sys.stderr.write(
            "fake_rea_mcp_server: could not load an MCP high-level server class, "
            "so this stdio server cannot start.\n"
            f"  (import error: {exc})\n"
            "  The optional 'mcp' package is likely not installed. Install it with:\n"
            "    pip install mcp        (or: uv pip install mcp)\n"
            "  The rea_live_demo.sh script falls back to an offline path when "
            "'mcp' is missing, so you can still see the full pipeline.\n"
        )
        raise SystemExit(1)


def build_server():
    """Construct the MCP server advertising the two REA-like tools."""
    ServerClass = _require_server_class()
    server = ServerClass("fake-rea")

    @server.tool(
        name=TOOL_ANALYZE,
        description=(
            "Analyze a JavaScript/Electron application and return a JAG-style "
            "finding (modules, imports, IPC channels, and any suspicious "
            "network egress). Stand-in for real REA; returns canned data."
        ),
    )
    def analyze_javascript_application(path: str = ".") -> str:
        # `path` is accepted to mirror the real tool's shape; the fake server
        # ignores it and always returns the same canned finding.
        return json.dumps(CANNED_JS_ANALYSIS, indent=2)

    @server.tool(
        name=TOOL_CHANNELS,
        description="List the IPC channels detected in the last analysis (canned).",
    )
    def list_detected_ipc_channels() -> str:
        return json.dumps(CANNED_CHANNELS, indent=2)

    return server


def main() -> int:
    # FastMCP.run() defaults to the stdio transport, which is what an MCP client
    # launching us as a subprocess expects.
    build_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
