"""End-to-end: a cached analysis means the REA call never happens again."""

import asyncio

from reagraph import cache as cachemod
from reagraph import rea_client


class _Tool:
    def __init__(self, name):
        self.name = name


class FakeSession:
    """Counts how many times REA's call_tool is actually invoked."""

    def __init__(self, counter):
        self.counter = counter

    async def list_tools(self):
        return [_Tool("analyze_javascript_application")]

    async def call_tool(self, name, args):
        self.counter["n"] += 1
        return {"nodes": [{"id": "main", "label": "main"}], "links": []}


def _client(target, cache, **kw):
    return rea_client.ReaClient(
        target=target, tools=["analyze_javascript_application"], cache=cache, **kw
    )


def test_cache_hit_skips_the_rea_call(tmp_path, monkeypatch):
    # Target is a file (stable hash); cache dir is OUTSIDE the target.
    target = tmp_path / "app.js"
    target.write_text("console.log('hi')", encoding="utf-8")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(rea_client.rea_adapter, "to_fragment", lambda tool, res: res, raising=False)
    monkeypatch.setattr(
        rea_client.rea_adapter, "SUPPORTED_TOOLS", {"analyze_javascript_application"}, raising=False
    )

    cache = cachemod.Cache()
    counter = {"n": 0}

    # First pull: cache miss -> REA is called once, result stored.
    frags1 = asyncio.run(_client(str(target), cache).collect(FakeSession(counter)))
    assert counter["n"] == 1 and frags1

    # Second pull, same artifact+tool: cache HIT -> REA is NOT called again.
    frags2 = asyncio.run(_client(str(target), cache).collect(FakeSession(counter)))
    assert counter["n"] == 1, "cache hit must skip the REA call"
    assert frags2 == frags1

    # refresh=True forces re-analysis past the hit.
    asyncio.run(_client(str(target), cache, refresh=True).collect(FakeSession(counter)))
    assert counter["n"] == 2

    # A different artifact is a different key -> miss -> REA called.
    other = tmp_path / "other.js"
    other.write_text("console.log('bye')", encoding="utf-8")
    asyncio.run(_client(str(other), cache).collect(FakeSession(counter)))
    assert counter["n"] == 3
