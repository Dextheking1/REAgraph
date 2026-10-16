#!/usr/bin/env bash
#
# REAgraph LIVE demo: the whole pipeline end to end, WITHOUT installing REA
# or Node.
#
#   fake REA MCP server  ->  MCP client  ->  bridge  ->  persistent graph
#
# Instead of real REA, this drives a tiny FAKE REA MCP server
# (examples/fake_rea_mcp_server.py) that speaks the MCP tool protocol over
# stdio and returns a canned JS-analysis finding. A small inline MCP client
# connects to it, lists its tools, calls "analyze-javascript-application",
# and streams the finding through reagraph.bridge into a durable /tmp graph.
# Then it prints `reagraph stats` and `reagraph summary`.
#
# To run against REAL REA instead of the fake server, point REAGRAPH_REA_CMD
# at the real MCP command and re-run -- the inline client will launch that
# instead of the fake server:
#
#     export REAGRAPH_REA_CMD="npx -y rea-agents@latest"
#     bash examples/rea_live_demo.sh
#
# DEGRADES GRACEFULLY: the live loop needs the optional `mcp` package. If `mcp`
# is not installed, this script prints a clear note and falls back to the
# offline path (feeding examples/sample_findings/*.jag.json through
# `reagraph add`) so it always succeeds and demonstrates the same end state.

set -euo pipefail

# Resolve paths relative to this script, so it runs from anywhere.
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
FINDINGS="$HERE/sample_findings"
SERVER="$HERE/fake_rea_mcp_server.py"
GRAPH="/tmp/reagraph_live_demo.json"

# Make `import reagraph` / `python -m reagraph.cli` work regardless of cwd.
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

# Prefer the project venv's python/reagraph; fall back to the module form.
if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY="python3"
fi
if [ -x "$REPO/.venv/bin/reagraph" ]; then
  REAGRAPH="$REPO/.venv/bin/reagraph"
else
  REAGRAPH="$PY -m reagraph.cli"
fi

echo "== REAgraph LIVE demo (no real REA / Node required)"
echo "== python : $PY"
echo "== reagraph: $REAGRAPH"
echo "== server : $SERVER (fake REA MCP server)"
echo "== graph  : $GRAPH"
echo
echo "   Real REA instead of the fake server:"
echo "     export REAGRAPH_REA_CMD=\"npx -y rea-agents@latest\"  # then re-run"
echo

# Start from a clean slate so the demo is deterministic and re-runnable.
rm -f "$GRAPH"

# The offline path: fold the checked-in sample findings into the graph with
# `reagraph add`. Reaches the same end state as the live loop, with no `mcp`.
run_offline() {
  echo "  Feeding examples/sample_findings/*.jag.json through 'reagraph add'"
  echo "  reaches the SAME end state: a durable, merged graph."
  rm -f "$GRAPH"
  for f in "$FINDINGS"/*.jag.json; do
    echo "  + add $(basename "$f")"
    $REAGRAPH add "$f" --graph "$GRAPH"
  done
}

# The live loop as a function so a failure can fall back without aborting the
# script. The inline MCP client connects to the fake server (or REAGRAPH_REA_CMD
# if set), lists tools, calls the analyze tool, and folds the finding into the
# graph via reagraph.bridge. Kept inline so this demo needs no extra files and
# does NOT depend on the sibling rea_client/rea_adapter modules.
run_live() {
  REAGRAPH_GRAPH="$GRAPH" REAGRAPH_SERVER="$SERVER" "$PY" <<'PYEOF'
import asyncio
import json
import os
import shlex
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reagraph import bridge

GRAPH = os.environ["REAGRAPH_GRAPH"]
SERVER = os.environ["REAGRAPH_SERVER"]
TOOL = "analyze-javascript-application"

# Point at real REA with REAGRAPH_REA_CMD; otherwise launch the fake server.
rea_cmd = os.environ.get("REAGRAPH_REA_CMD")
if rea_cmd:
    parts = shlex.split(rea_cmd)
    params = StdioServerParameters(command=parts[0], args=parts[1:])
    print(f"  using REAGRAPH_REA_CMD: {rea_cmd}")
else:
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    print(f"  launching fake server: {SERVER}")


def _text_of(result) -> str:
    """Pull the text payload out of an MCP tool-call result, tolerantly."""
    # Newer SDKs expose structured content; fall back to text content blocks.
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    raise RuntimeError("tool result had no text content")


async def main() -> int:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"  tools advertised: {names}")

            result = await session.call_tool(TOOL, {"path": "."})
            finding = json.loads(_text_of(result))
            print(
                f"  finding received: {len(finding.get('nodes', []))} nodes, "
                f"{len(finding.get('edges', []))} edges "
                f"(artifact: {finding.get('artifact', '?')})"
            )

    # Stream the finding through the bridge: merged AND saved to disk.
    def _on_merge(stats):
        print(
            f"  merged -> graph now {stats['nodes']} nodes, "
            f"{stats['edges']} edges"
        )

    bridge.ingest_stream([finding], graph_path=GRAPH, on_merge=_on_merge)
    return 0


raise SystemExit(asyncio.run(main()))
PYEOF
}

# ---------------------------------------------------------------------------
# Does this interpreter have the optional `mcp` package? That decides whether
# we run the real live loop or the offline fallback. Either way we exit 0.
# ---------------------------------------------------------------------------
if $PY -c "import mcp" >/dev/null 2>&1; then
  echo "---------------------------------------------------------------"
  echo "LIVE  mcp present -> fake REA server -> MCP client -> bridge -> graph"
  echo "  An MCP client launches the server over stdio, lists its tools,"
  echo "  calls 'analyze-javascript-application', and streams the finding"
  echo "  into a durable graph (saved after every merge, crash-safe)."
  echo "---------------------------------------------------------------"
  if run_live; then
    :
  else
    echo
    echo "  (the live loop failed -- e.g. an incompatible 'mcp' build or a"
    echo "   mis-set REAGRAPH_REA_CMD. Falling back to the offline path so the"
    echo "   demo still reaches the same end state.)"
    run_offline
  fi
  echo
else
  echo "---------------------------------------------------------------"
  echo "OFFLINE  'mcp' not installed -> falling back to the offline path"
  echo "  (install it with 'pip install mcp' to run the real live loop)."
  echo "---------------------------------------------------------------"
  run_offline
  echo
fi

echo "---------------------------------------------------------------"
echo "reagraph stats  ->  what the accumulated graph now knows"
echo "---------------------------------------------------------------"
$REAGRAPH stats --graph "$GRAPH"
echo

echo "---------------------------------------------------------------"
echo "reagraph summary  ->  bounded map (god nodes + clusters)"
echo "---------------------------------------------------------------"
$REAGRAPH summary --graph "$GRAPH"
echo

echo "== done. persistent graph left at: $GRAPH"
