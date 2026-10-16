"""The REA MCP client, tested with NEITHER mcp NOR node NOR a real REA server.

The client's connection-independent logic (run tools -> fragments via the adapter,
fail-open on a bad tool) is exercised with a tiny fake session. The adapter's
``to_fragment`` is stubbed so this module is tested independently of the sibling
``reagraph.rea_adapter``.
"""

import asyncio
import sys
import types

from reagraph import memory, rea_client


# -- a tiny fake MCP session (duck-typed: async list_tools / call_tool) ------


class _Tool:
    def __init__(self, name):
        self.name = name


class _Listed:
    def __init__(self, names):
        self.tools = [_Tool(n) for n in names]


class FakeSession:
    """Offers three tools; two return results, one raises on call."""

    def __init__(self, names):
        self.names = names
        self.calls = []

    async def list_tools(self):
        return _Listed(self.names)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "boom":
            raise RuntimeError("REA tool exploded")
        return {"ok": name, "args": args}


def _stub_adapter(monkeypatch, names):
    """Stub the adapter so a fragment is trivially derived from the tool name."""
    monkeypatch.setattr(
        rea_client.rea_adapter,
        "to_fragment",
        lambda tool, res: {"nodes": [{"id": tool}], "links": []},
    )
    monkeypatch.setattr(rea_client.rea_adapter, "SUPPORTED_TOOLS", set(names))


def test_imports_without_mcp_installed():
    # mcp is not a dependency; importing the module must not require it.
    assert "mcp" not in sys.modules
    assert hasattr(rea_client, "ReaClient")


def test_construction_never_spawns_a_process(monkeypatch):
    # Lazy mcp import happens only inside _connect; construction must not reach it.
    def _boom(*a, **k):
        raise AssertionError("construction must not connect/spawn")

    monkeypatch.setattr(rea_client.ReaClient, "_connect", _boom)
    client = rea_client.ReaClient("app.asar", tools=("analyze_app",))
    assert client.server_cmd == rea_client.DEFAULT_SERVER_CMD
    assert "mcp" not in sys.modules  # no MCP import triggered by constructing


def test_env_var_overrides_server_cmd(monkeypatch):
    monkeypatch.setenv("REAGRAPH_REA_CMD", "myrea --stdio serve")
    client = rea_client.ReaClient()
    assert client.server_cmd == ["myrea", "--stdio", "serve"]


def test_collect_yields_fragments_and_skips_the_erroring_tool(monkeypatch):
    names = ["analyze_app", "boom", "list_strings"]
    _stub_adapter(monkeypatch, names)

    client = rea_client.ReaClient("app.asar", tools=tuple(names))
    session = FakeSession(names)

    fragments = asyncio.run(client.collect(session))

    # Three tools offered; the erroring one is skipped -> two fragments.
    assert len(fragments) == 2
    assert [f["nodes"][0]["id"] for f in fragments] == ["analyze_app", "list_strings"]
    # The target path was passed through as the default tool argument.
    assert ("analyze_app", {"path": "app.asar"}) in session.calls

    # The yielded dicts are mergeable into an empty graph by reagraph.memory.
    graph = memory.empty_graph()
    for fragment in fragments:
        memory.merge(graph, fragment)
    stats = memory.stats(graph)
    assert stats["nodes"] == 2
    ids = {n["id"] for n in graph["nodes"]}
    assert ids == {"analyze_app", "list_strings"}


def test_tools_not_offered_by_server_are_skipped(monkeypatch):
    names = ["analyze_app"]
    _stub_adapter(monkeypatch, ["analyze_app", "not_offered"])

    # Request a tool the fake server does not list; it must be skipped silently.
    client = rea_client.ReaClient("x", tools=("analyze_app", "not_offered"))
    fragments = asyncio.run(client.collect(FakeSession(names)))
    assert [f["nodes"][0]["id"] for f in fragments] == ["analyze_app"]


def test_adapter_error_is_fail_open(monkeypatch, capsys):
    names = ["analyze_app"]

    def _boom_adapter(tool, res):
        raise ValueError("adapter cannot convert")

    monkeypatch.setattr(rea_client.rea_adapter, "to_fragment", _boom_adapter)
    monkeypatch.setattr(rea_client.rea_adapter, "SUPPORTED_TOOLS", set(names))

    client = rea_client.ReaClient("x", tools=tuple(names))
    fragments = asyncio.run(client.collect(FakeSession(names)))
    assert fragments == []  # the single tool failed to convert, skipped not fatal
    assert "skipped tool analyze_app" in capsys.readouterr().err


def test_connect_without_mcp_raises_actionable_error(monkeypatch):
    # Ensure mcp truly is absent, then assert the error names REAGRAPH_REA_CMD.
    monkeypatch.setitem(sys.modules, "mcp", None)  # import mcp -> ImportError
    client = rea_client.ReaClient("x")
    try:
        asyncio.run(client._connect())
    except RuntimeError as exc:
        assert "REAGRAPH_REA_CMD" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a RuntimeError when mcp is missing")


def test_result_payload_handles_dicts_and_calltoolresult():
    assert rea_client._result_payload({"a": 1}) == {"a": 1}

    block = types.SimpleNamespace(text='{"nodes": [], "links": []}')
    result = types.SimpleNamespace(structuredContent=None, content=[block])
    assert rea_client._result_payload(result) == {"nodes": [], "links": []}

    structured = types.SimpleNamespace(structuredContent={"k": "v"}, content=[])
    assert rea_client._result_payload(structured) == {"k": "v"}
