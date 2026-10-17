"""Fixes for the REA MCP client, tested with NEITHER mcp NOR node NOR a server.

Covers two fixes in ``reagraph.rea_client``:

  FIX 1 -- a fully-cached pull skips the whole MCP spawn/handshake. ``findings()``
  pre-resolves every requested tool against the cache via ``_cached_only()`` and
  returns those fragments without ever calling ``_connect`` (no spawn, no mcp
  import). A miss (or nothing to pre-resolve) falls through to the live path.

  FIX 2 -- tolerant tool-name matching in ``collect()``: REA's advertised id may
  use dashes while the adapter grounds the underscore form; the two still match.
"""

import asyncio

from reagraph import cache as cachemod
from reagraph import rea_client


# -- a tiny fake MCP session (duck-typed: async list_tools / call_tool) ------


class _Tool:
    def __init__(self, name):
        self.name = name


class _Listed:
    def __init__(self, names):
        self.tools = [_Tool(n) for n in names]


class FakeSession:
    """Records call_tool invocations and returns a trivial result."""

    def __init__(self, names):
        self.names = names
        self.calls = []

    async def list_tools(self):
        return _Listed(self.names)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"ok": name, "args": args}


DEFAULT_TOOL = "analyze_javascript_application"
DASHED_TOOL = "analyze-javascript-application"


def _stub_adapter(monkeypatch, supported):
    """Fragment is trivially derived from the (adapter) tool name it is handed."""
    monkeypatch.setattr(
        rea_client.rea_adapter,
        "to_fragment",
        lambda tool, res: {"nodes": [{"id": tool}], "links": []},
        raising=False,
    )
    monkeypatch.setattr(
        rea_client.rea_adapter, "SUPPORTED_TOOLS", set(supported), raising=False
    )


# -- FIX 1: fully-cached pull does NOT spawn ---------------------------------


def test_fully_cached_pull_does_not_spawn(tmp_path, monkeypatch):
    # A real artifact + a real on-disk Cache (temp dir OUTSIDE the target).
    target = tmp_path / "app.js"
    target.write_text("console.log('hi')", encoding="utf-8")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))

    cache = cachemod.Cache()
    # Pre-populate the cache for the default tool on this target.
    frag = {"nodes": [{"id": "main", "label": "main"}], "links": []}
    key = cache.key_for(str(target), DEFAULT_TOOL, {"path": str(target)})
    cache.put(key, frag, meta={"tool": DEFAULT_TOOL, "target": str(target)})

    # If _connect is ever reached, the fast path failed -> hard error.
    def _boom(*a, **k):
        raise AssertionError("fully-cached pull must not spawn / call _connect")

    monkeypatch.setattr(rea_client.ReaClient, "_connect", _boom)

    client = rea_client.ReaClient(
        target=str(target), tools=[DEFAULT_TOOL], cache=cache
    )
    out = list(client.findings())
    assert out == [frag]  # the cached fragment, yielded without a spawn


def test_cached_only_returns_fragments_when_all_hit(tmp_path, monkeypatch):
    target = tmp_path / "app.js"
    target.write_text("x=1", encoding="utf-8")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))

    cache = cachemod.Cache()
    frag = {"nodes": [{"id": "main"}], "links": []}
    key = cache.key_for(str(target), DEFAULT_TOOL, {"path": str(target)})
    cache.put(key, frag, meta={})

    client = rea_client.ReaClient(target=str(target), tools=[DEFAULT_TOOL], cache=cache)
    assert client._cached_only() == [frag]


# -- FIX 1: partial / empty cache still connects -----------------------------


def test_empty_cache_falls_through_to_connect(tmp_path, monkeypatch):
    target = tmp_path / "app.js"
    target.write_text("x=1", encoding="utf-8")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))  # empty cache dir

    cache = cachemod.Cache()
    client = rea_client.ReaClient(target=str(target), tools=[DEFAULT_TOOL], cache=cache)

    # Nothing cached -> the fast path must decline.
    assert client._cached_only() is None

    # ... and findings() must then go through _connect. Monkeypatch _connect to
    # return a sentinel (it is awaited via asyncio.run) and assert it is used.
    sentinel = [{"nodes": [{"id": "from-connect"}], "links": []}]

    async def _fake_connect(self):
        return sentinel

    monkeypatch.setattr(rea_client.ReaClient, "_connect", _fake_connect)
    assert list(client.findings()) == sentinel


def test_no_cache_configured_falls_through(tmp_path):
    # No cache at all -> fast path declines (None), normal path would connect.
    client = rea_client.ReaClient(target=str(tmp_path / "app.js"), tools=[DEFAULT_TOOL])
    assert client._cached_only() is None


def test_refresh_bypasses_the_fast_path(tmp_path, monkeypatch):
    target = tmp_path / "app.js"
    target.write_text("x=1", encoding="utf-8")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))

    cache = cachemod.Cache()
    frag = {"nodes": [{"id": "main"}], "links": []}
    key = cache.key_for(str(target), DEFAULT_TOOL, {"path": str(target)})
    cache.put(key, frag, meta={})

    # Even with a full cache, refresh=True must force the live path.
    client = rea_client.ReaClient(
        target=str(target), tools=[DEFAULT_TOOL], cache=cache, refresh=True
    )
    assert client._cached_only() is None


def test_partial_cache_falls_through(tmp_path, monkeypatch):
    # Two tools requested, only one cached -> not fully cached -> fall through.
    target = tmp_path / "app.js"
    target.write_text("x=1", encoding="utf-8")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))

    cache = cachemod.Cache()
    monkeypatch.setattr(
        rea_client.rea_adapter,
        "SUPPORTED_TOOLS",
        {DEFAULT_TOOL, "second_tool"},
        raising=False,
    )
    frag = {"nodes": [{"id": "main"}], "links": []}
    key = cache.key_for(str(target), DEFAULT_TOOL, {"path": str(target)})
    cache.put(key, frag, meta={})  # only the first tool is cached

    client = rea_client.ReaClient(
        target=str(target), tools=[DEFAULT_TOOL, "second_tool"], cache=cache
    )
    assert client._cached_only() is None


def test_cached_only_is_fail_open_on_cache_error(tmp_path, monkeypatch):
    # A cache that raises during the pre-check must not block the pull.
    target = tmp_path / "app.js"
    target.write_text("x=1", encoding="utf-8")

    class _BoomCache:
        def key_for(self, *a, **k):
            raise RuntimeError("cache exploded")

        def get(self, *a, **k):  # pragma: no cover - never reached
            raise RuntimeError("cache exploded")

    client = rea_client.ReaClient(
        target=str(target), tools=[DEFAULT_TOOL], cache=_BoomCache()
    )
    assert client._cached_only() is None


# -- FIX 2: tolerant tool-name matching --------------------------------------


def test_collect_matches_dashed_server_name(monkeypatch):
    # Server advertises the DASHED id; self.tools / SUPPORTED_TOOLS use the
    # underscore form. The tool must still be called (by the server's dashed
    # name) and a fragment produced (via the adapter's underscore grounding).
    _stub_adapter(monkeypatch, {DEFAULT_TOOL})
    client = rea_client.ReaClient("app.asar", tools=(DEFAULT_TOOL,))
    session = FakeSession([DASHED_TOOL])

    fragments = asyncio.run(client.collect(session))

    assert len(fragments) == 1
    # call_tool used the SERVER's actual advertised (dashed) id.
    assert session.calls == [(DASHED_TOOL, {"path": "app.asar"})]
    # to_fragment was handed the adapter's grounded (underscore) name.
    assert fragments[0]["nodes"][0]["id"] == DEFAULT_TOOL


def test_collect_exact_underscore_still_works(monkeypatch):
    # No regression: an exact-underscore server behaves as before.
    _stub_adapter(monkeypatch, {DEFAULT_TOOL})
    client = rea_client.ReaClient("app.asar", tools=(DEFAULT_TOOL,))
    session = FakeSession([DEFAULT_TOOL])

    fragments = asyncio.run(client.collect(session))

    assert len(fragments) == 1
    assert session.calls == [(DEFAULT_TOOL, {"path": "app.asar"})]
    assert fragments[0]["nodes"][0]["id"] == DEFAULT_TOOL


def test_collect_dashed_requested_name_also_matches(monkeypatch):
    # The requested name may itself be dashed while the adapter grounds the
    # underscore form; it must still match and map through the adapter.
    _stub_adapter(monkeypatch, {DEFAULT_TOOL})
    client = rea_client.ReaClient("app.asar", tools=(DASHED_TOOL,))
    session = FakeSession([DASHED_TOOL])

    fragments = asyncio.run(client.collect(session))

    assert len(fragments) == 1
    assert session.calls == [(DASHED_TOOL, {"path": "app.asar"})]
    # Adapter is handed its grounded underscore name, not the dashed request.
    assert fragments[0]["nodes"][0]["id"] == DEFAULT_TOOL


# -- determinism + one-to-one unaffected -------------------------------------


def test_collect_is_deterministic_and_one_to_one(monkeypatch):
    names = ["analyze_app", "list_strings"]
    _stub_adapter(monkeypatch, names)
    client = rea_client.ReaClient("app.asar", tools=tuple(names))

    run1 = asyncio.run(client.collect(FakeSession(names)))
    run2 = asyncio.run(client.collect(FakeSession(names)))

    # One fragment per requested tool, same order every run.
    assert run1 == run2
    assert [f["nodes"][0]["id"] for f in run1] == names
