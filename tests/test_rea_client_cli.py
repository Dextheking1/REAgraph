"""CLI-JSON transport: ingest large apps that overflow REA's MCP stdio budget.

REA's MCP stdio response has a size budget a large app's full graph exceeds
("resource_constraint ... exceeds the stdio response budget"); REA's CLI emits the
complete graph as JSON instead. ReaClient(cli_cmd=...) runs
``<cli_cmd> analyze <target> --format json --full-output`` and maps the
``{ok, data, meta}`` envelope through the adapter -- no mcp, no node required here:
the tests use a tiny fake CLI that prints a realistic envelope.
"""

import json
import sys
import textwrap

import pytest

from reagraph import cache as cachemod
from reagraph import rea_client


def _jag_envelope():
    """A REA CLI {ok,data,meta} envelope whose data.normalized_result.graph is a JAG."""
    graph = {
        "schema": "JavaScriptApplicationGraph",
        "root_node_ids": ["jag_node_a"],
        "nodes": [
            {"node_id": "jag_node_a", "kind": "javascript-module",
             "identity": {"strategy": "canonical-path", "path": "src/main.js"},
             "observations": [{"label": "src/main.js", "properties": {},
                               "evidence": {"state": "observed",
                                            "location": {"value": {"kind": "source-range",
                                                                   "source": "src/main.js"}}}}]},
            {"node_id": "jag_node_b", "kind": "endpoint",
             "identity": {"strategy": "artifact-local-key", "key": "https://x/y"},
             "observations": [{"label": "network endpoint", "properties": {},
                               "evidence": {"state": "observed"}}]},
        ],
        "edges": [
            {"source_node_id": "jag_node_a", "target_node_id": "jag_node_b",
             "relation": "calls", "evidence": {"state": "observed"}},
        ],
    }
    return {"ok": True, "data": {"normalized_result": {"input_path": "/app.asar",
                                                       "graph": graph}}, "meta": {}}


def _write_fake_cli(tmp_path, stdout_text, stderr_text="progress...\n", code=0):
    script = tmp_path / "fake_rea_cli.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.stderr.write({stderr_text!r})
        sys.stdout.write({stdout_text!r})
        sys.exit({code})
    """))
    return [sys.executable, str(script)]


def test_cli_transport_maps_full_graph(tmp_path):
    cli = _write_fake_cli(tmp_path, json.dumps(_jag_envelope()))
    client = rea_client.ReaClient(target="/app.asar", cli_cmd=cli)
    frags = list(client.findings())
    assert len(frags) == 1
    frag = frags[0]
    assert len(frag["nodes"]) == 2
    assert not any(n.get("file_type") == "evidence" for n in frag["nodes"])
    assert len(frag["links"]) == 1
    labels = {n["label"] for n in frag["nodes"]}
    assert "network endpoint" in labels and "src/main.js" in labels


def test_cli_transport_tolerates_progress_noise_on_stdout(tmp_path):
    # A stray progress line leaks onto stdout before the JSON document.
    noisy = '{"rea_progress":{"phase":"x"}}\n' + json.dumps(_jag_envelope())
    cli = _write_fake_cli(tmp_path, noisy)
    client = rea_client.ReaClient(target="/app.asar", cli_cmd=cli)
    frags = list(client.findings())
    assert len(frags) == 1 and len(frags[0]["nodes"]) == 2


def test_cli_transport_error_envelope_not_folded_or_cached(tmp_path):
    target = tmp_path / "app.asar"
    target.write_text("x")
    env = {"ok": False, "data": {"error": {"code": "resource_constraint",
                                           "message": "exceeds the stdio response budget"}}}
    cli = _write_fake_cli(tmp_path, json.dumps(env))
    import os
    os.environ["REAGRAPH_CACHE"] = str(tmp_path / "cache")
    cache = cachemod.Cache()
    client = rea_client.ReaClient(target=str(target), cli_cmd=cli, cache=cache)
    frags = list(client.findings())
    assert frags == []  # an error is not a finding
    key = cache.key_for(str(target), "analyze_javascript_application", {"transport": "cli"})
    assert cache.get(key) is None  # error not cached


def test_cli_transport_uses_cache_on_second_call(tmp_path):
    target = tmp_path / "app.asar"
    target.write_text("x")
    import os
    os.environ["REAGRAPH_CACHE"] = str(tmp_path / "cache")
    cache = cachemod.Cache()
    cli = _write_fake_cli(tmp_path, json.dumps(_jag_envelope()))
    c1 = rea_client.ReaClient(target=str(target), cli_cmd=cli, cache=cache)
    assert len(list(c1.findings())[0]["nodes"]) == 2
    # Second client with a BROKEN cli (would fail if run) still returns from cache.
    broken = [sys.executable, str(tmp_path / "does_not_exist.py")]
    c2 = rea_client.ReaClient(target=str(target), cli_cmd=broken, cache=cache)
    frags = list(c2.findings())
    assert len(frags) == 1 and len(frags[0]["nodes"]) == 2  # served from cache, cli not run


def test_cli_builds_the_expected_command(tmp_path, monkeypatch):
    # The command is `<cli_cmd> analyze <target> --format json --full-output`.
    recorded = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps(_jag_envelope())
        stderr = ""

    def fake_run(cmd, **kw):
        recorded["cmd"] = cmd
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = rea_client.ReaClient(target="/app.asar", cli_cmd=["rea-agents"])
    list(client.findings())
    assert recorded["cmd"] == ["rea-agents", "analyze", "/app.asar",
                               "--format", "json", "--full-output"]


def test_cli_transport_streams_and_caps_oversized_stdout(tmp_path):
    # A fake CLI printing far more than the cap must raise a clean RuntimeError
    # naming the limit -- the stdout was streamed to a temp file, so the oversize
    # is caught by the byte cap BEFORE anything is loaded into memory (no OOM).
    big = "x" * 5000  # not even JSON: the cap trips before any parse is attempted
    cli = _write_fake_cli(tmp_path, big)
    client = rea_client.ReaClient(target="/app.asar", cli_cmd=cli, cli_max_bytes=100)
    with pytest.raises(RuntimeError) as excinfo:
        list(client.findings())
    msg = str(excinfo.value)
    assert "cli_max_bytes" in msg and "100" in msg


def test_cli_transport_maps_full_graph_via_streamed_temp_file(tmp_path):
    # Same as the basic mapping test, but asserts the real streamed-temp-file
    # path (a real subprocess whose stdout is streamed to disk, then parsed back)
    # yields the identical fragment -- normal-output behavior is unchanged.
    cli = _write_fake_cli(tmp_path, json.dumps(_jag_envelope()))
    client = rea_client.ReaClient(target="/app.asar", cli_cmd=cli,
                                  cli_max_bytes=10 * 1024 * 1024)
    frags = list(client.findings())
    assert len(frags) == 1 and len(frags[0]["nodes"]) == 2
    assert len(frags[0]["links"]) == 1


def test_cli_normalizes_dash_leading_target_argv(monkeypatch):
    # Argument-injection guard: a target beginning with '-' would be read as a
    # REA flag, so it is normalized to an explicit relative path ('./-weird.asar')
    # and still passed as the positional analyze path (options unchanged).
    recorded = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps(_jag_envelope())
        stderr = ""

    def fake_run(cmd, **kw):
        recorded["cmd"] = cmd
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = rea_client.ReaClient(target="-weird.asar", cli_cmd=["rea-agents"])
    list(client.findings())
    assert recorded["cmd"] == ["rea-agents", "analyze", "./-weird.asar",
                               "--format", "json", "--full-output"]
