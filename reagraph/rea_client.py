"""Live REA MCP client: pull findings from REA and fold them into the graph.

REAgraph does NOT vendor REA. At runtime this client spawns the REA MCP server
(github.com/morluto/rea) over stdio, initializes a session, lists its tools, runs
the requested analyses, and converts each tool result into a node-link graph
fragment via the sibling ``reagraph.rea_adapter``. The fragments are exactly the
dicts ``reagraph.bridge.ingest_stream`` already accepts, so:

    from reagraph import bridge
    from reagraph.rea_client import ReaClient
    bridge.ingest_stream(ReaClient("app.asar"), "reagraph.json")

folds live REA findings into the persistent graph crash-safely.

Design for testability: the connection (spawn + MCP handshake) is isolated in
``_connect``; the connection-INDEPENDENT logic (iterate tool results -> fragments
via the adapter, fail-open on a bad tool) lives in the async ``collect`` method,
which takes an already-connected session-like object and can be unit-tested with
a tiny fake. ``mcp`` is imported lazily inside ``_connect`` only, so this module
imports fine without ``mcp`` (or node/npx) installed.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Iterator, Mapping, Sequence

# The adapter is a sibling module (built in parallel). Import it per the shared
# contract; stay importable and independently testable even if it is not present
# yet — tests stub ``to_fragment`` and real runs will have it installed.
try:
    from . import rea_adapter
except ImportError:  # pragma: no cover - exercised only before the sibling lands
    import types

    rea_adapter = types.ModuleType("reagraph.rea_adapter")

    def _to_fragment_missing(tool_name: str, result: dict) -> dict:
        raise RuntimeError(
            "reagraph.rea_adapter is not available; cannot convert REA results"
        )

    rea_adapter.to_fragment = _to_fragment_missing  # type: ignore[attr-defined]
    rea_adapter.SUPPORTED_TOOLS = set()  # type: ignore[attr-defined]


# npx-based default: run REA straight from npm with no local checkout. Override
# via the constructor or the REAGRAPH_REA_CMD env var.
DEFAULT_SERVER_CMD = ["npx", "-y", "rea-agents@latest"]

# Sensible defaults for static JS / Electron analysis, which needs no Ghidra or
# native disassembly. These are intersected with the server's actual ``list_tools``
# and the adapter's ``SUPPORTED_TOOLS``, so a name REA does not offer is simply
# skipped rather than fatal. Override via the ``tools=`` constructor argument.
DEFAULT_TOOLS = ("analyze_app", "list_functions", "list_strings")


class ReaClient:
    """A ``bridge.ReaSource`` backed by a live REA MCP server over stdio."""

    def __init__(
        self,
        target: str = ".",
        tools: Sequence[str] | None = None,
        *,
        server_cmd: Sequence[str] | None = None,
        tool_args: Mapping[str, Mapping] | None = None,
        cache=None,
        refresh: bool = False,
    ) -> None:
        self.target = target
        self.tools = tuple(tools) if tools is not None else DEFAULT_TOOLS
        self.server_cmd = self._resolve_cmd(server_cmd)
        self.tool_args = {k: dict(v) for k, v in (tool_args or {}).items()}
        # Optional content-addressed cache (reagraph.cache.Cache). When set, a
        # (artifact, tool, args) hit skips the REA call entirely; a miss stores
        # the fragment after. refresh=True forces re-analysis past a hit.
        self.cache = cache
        self.refresh = refresh

    # -- construction helpers (no I/O, no process spawn) ------------------

    @staticmethod
    def _resolve_cmd(server_cmd: Sequence[str] | None) -> list[str]:
        """Explicit arg > REAGRAPH_REA_CMD env > npx default. Never spawns."""
        if server_cmd:
            return list(server_cmd)
        env = os.environ.get("REAGRAPH_REA_CMD")
        if env:
            return shlex.split(env)
        return list(DEFAULT_SERVER_CMD)

    def _args_for(self, tool: str) -> dict:
        """Call arguments for a tool: per-tool override, else the target path."""
        if tool in self.tool_args:
            return dict(self.tool_args[tool])
        return {"path": self.target}

    # -- ReaSource interface ---------------------------------------------

    def findings(self) -> Iterator[dict]:
        """Yield node-link fragments from a live REA run (bridge.ReaSource)."""
        # Collected inside the live session, then yielded: the MCP session must
        # stay open while tools run. bridge.ingest_stream still saves after each
        # merge, so crash-safety is preserved as fragments are consumed.
        import asyncio

        for fragment in asyncio.run(self._connect()):
            yield fragment

    # -- connection-INDEPENDENT logic (unit-tested with a fake session) ---

    async def collect(self, session) -> list[dict]:
        """Run the requested analyses on an already-connected session-like object.

        ``session`` must expose async ``list_tools()`` and ``call_tool(name, args)``
        (the shape of ``mcp.ClientSession``). Returns one fragment per tool that
        succeeded. Fail-open: a tool whose call or conversion raises is logged to
        stderr and skipped, so one failing REA tool never aborts the whole pull.
        """
        available = await self._list_tool_names(session)
        supported = set(getattr(rea_adapter, "SUPPORTED_TOOLS", set()) or ())

        fragments: list[dict] = []
        for tool in self.tools:
            if tool not in available:
                continue  # REA did not offer this tool
            if supported and tool not in supported:
                continue  # adapter cannot convert it
            args = self._args_for(tool)
            # Cache check: a hit means we never call REA for this artifact+tool.
            key = None
            if self.cache is not None and not self.refresh:
                try:
                    key = self.cache.key_for(self.target, tool, args)
                    hit = self.cache.get(key)
                except Exception:  # fail-open: a broken cache never blocks a pull
                    key, hit = None, None
                if hit:
                    fragments.append(hit["fragment"])
                    continue
            try:
                result = await session.call_tool(tool, args)
                fragment = rea_adapter.to_fragment(tool, _result_payload(result))
            except Exception as exc:  # fail-open: skip one bad tool, keep going
                print(
                    f"reagraph.rea_client: skipped tool {tool}: {exc}",
                    file=sys.stderr,
                )
                continue
            if self.cache is not None:  # populate for next time (best-effort)
                try:
                    k = key if key is not None else self.cache.key_for(self.target, tool, args)
                    self.cache.put(k, fragment, meta={"tool": tool, "target": self.target})
                except Exception:
                    pass
            fragments.append(fragment)
        return fragments

    @staticmethod
    async def _list_tool_names(session) -> set:
        """Tool names the server offers (tolerant of result/list shapes)."""
        listed = await session.list_tools()
        tools = getattr(listed, "tools", listed)
        return {getattr(t, "name", t) for t in tools}

    # -- connection (lazy mcp import; the only part that spawns a process) -

    async def _connect(self) -> list[dict]:
        """Spawn REA over stdio, handshake, and hand the session to ``collect``."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError(
                "reagraph.rea_client needs the 'mcp' package to reach REA. "
                "Install it (pip install mcp) and point REAGRAPH_REA_CMD at your "
                "REA MCP server command (default: "
                f"{' '.join(DEFAULT_SERVER_CMD)})."
            ) from exc

        params = StdioServerParameters(
            command=self.server_cmd[0], args=list(self.server_cmd[1:])
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await self.collect(session)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"could not launch REA MCP server {self.server_cmd!r}: {exc}. "
                "Install node/npx (the default uses 'npx') or set REAGRAPH_REA_CMD "
                "to a working REA MCP server command."
            ) from exc


def _result_payload(result) -> dict:
    """Normalize an MCP tool result into the plain dict the adapter expects.

    Plain dicts (fakes, already-decoded results) pass through. An ``mcp``
    ``CallToolResult`` is reduced to its structured content, else the first text
    block parsed as JSON, else an empty dict.
    """
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", None) or ():
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return {"text": text}
    return {}
