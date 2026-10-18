"""Demand-scoped re-analysis: source_paths restricts REA to selected inventory files.

No REA/mcp/node needed -- these assert how ReaClient translates source_paths into
tool arguments (MCP) and CLI flags, and that scope participates in the cache key.
"""

import json
import sys
import textwrap

from reagraph import rea_client

ANALYZE = "analyze_javascript_application"


def test_source_paths_added_to_analyze_args_only():
    client = rea_client.ReaClient("app.asar", source_paths=["src/main.js", "src/auth.js"])
    args = client._args_for(ANALYZE)
    assert args["input_path"] == "app.asar"
    assert args["source_paths"] == ["src/main.js", "src/auth.js"]
    # A non-analyze tool does not get source_paths.
    other = client._args_for("list_strings")
    assert "source_paths" not in other


def test_no_source_paths_means_no_arg():
    client = rea_client.ReaClient("app.asar")
    assert "source_paths" not in client._args_for(ANALYZE)
    client2 = rea_client.ReaClient("app.asar", source_paths=[])
    assert client2.source_paths is None  # empty == unscoped


def test_scope_changes_the_cache_key(tmp_path, monkeypatch):
    from reagraph import cache as cachemod
    target = tmp_path / "app.js"
    target.write_text("x")
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))
    cache = cachemod.Cache()
    full = rea_client.ReaClient(str(target))
    scoped = rea_client.ReaClient(str(target), source_paths=["src/main.js"])
    k_full = cache.key_for(str(target), ANALYZE, full._args_for(ANALYZE))
    k_scoped = cache.key_for(str(target), ANALYZE, scoped._args_for(ANALYZE))
    assert k_full != k_scoped  # scoped analysis is a distinct cache entry


def test_cli_transport_appends_source_path_flags(monkeypatch):
    recorded = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": True, "data": {"normalized_result": {"graph": {
            "schema": "JavaScriptApplicationGraph", "nodes": [], "edges": []}}}})
        stderr = ""

    def fake_run(cmd, **kw):
        recorded["cmd"] = cmd
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = rea_client.ReaClient("/app.asar", cli_cmd=["rea-agents"],
                                  source_paths=["src/main.js", "src/auth.js"])
    list(client.findings())
    assert recorded["cmd"] == [
        "rea-agents", "analyze", "/app.asar", "--format", "json", "--full-output",
        "--source-path", "src/main.js", "--source-path", "src/auth.js",
    ]
