"""End-to-end workflow benchmark: the full REAgraph pipeline, timed per stage.

Runs the REAL reagraph code -- MCP client -> adapter -> bridge -> cache ->
summarize / coverage / diff / attest -- over a real MCP stdio transport. The REA
analysis engine is a stand-in MCP server (bench/fake_rea_server_e2e.py) so this
runs without installing REA; point REAGRAPH_REA_CMD at `npx -y rea-agents@latest`
to run the identical workflow against the real REA instead.

Stages timed:
  1. pull v1            (cold: spawn REA, analyze, ingest -> graph)   [1 analysis]
  2. pull v1 (cached)   (fully cached -> no REA spawn, near-instant)  [0 analyses]
  3. summary            (bounded map of the graph)
  4. coverage           (observed vs inferred vs unverified)
  5. pull v2            (a changed build of the same app)             [1 analysis]
  6. diff v1 vs v2      (structural, byte-agnostic)
  7. attest v2 vs source(does shipped == source? integrity + residual)
"""

from __future__ import annotations

import os
import sys
import time
import tempfile
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from reagraph import memory, summarize, coverage, diff, attest, cache as cachemod  # noqa: E402
from reagraph import bridge, rea_client  # noqa: E402


def _timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    ms = (time.perf_counter() - t0) * 1000.0
    return out, ms


def _source_graph():
    # A graphify-style SOURCE graph of the same app (the clean build), by name.
    labels = ["acme-desktop", "main.js", "renderer.js", "preload.js", "electron",
              "https", "app:getUserProfile", "authenticate", "createSession"]
    nodes = [{"id": f"s{i}", "label": lbl} for i, lbl in enumerate(labels)]
    links = [
        {"source": "s0", "target": "s1", "relation": "contains"},
        {"source": "s0", "target": "s2", "relation": "contains"},
        {"source": "s1", "target": "s3", "relation": "loads"},
        {"source": "s1", "target": "s4", "relation": "imports"},
        {"source": "s1", "target": "s7", "relation": "calls"},
        {"source": "s7", "target": "s8", "relation": "calls"},
    ]
    return {"nodes": nodes, "links": links}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="reagraph_e2e_"))
    os.environ["REAGRAPH_CACHE"] = str(work / "cache")
    # Make the latency visible so the cache win shows up end-to-end (modeled).
    os.environ.setdefault("REA_LATENCY", "0.5")

    # Real target paths (so the cache's content hash has bytes to key on).
    app = work / "app"; app.mkdir()
    (app / "main.js").write_text("// app v1\n", encoding="utf-8")
    appv2 = work / "app-v2"; appv2.mkdir()
    (appv2 / "main.js").write_text("// app v2 (tampered)\n", encoding="utf-8")

    # REA command: real REA if REAGRAPH_REA_CMD is set, else our stand-in server.
    using_real = bool(os.environ.get("REAGRAPH_REA_CMD"))
    server_cmd = None if using_real else [sys.executable, str(HERE / "fake_rea_server_e2e.py")]

    g1 = str(work / "v1.json")
    g2 = str(work / "v2.json")
    cache = cachemod.Cache()

    print("=" * 70)
    print("REAgraph end-to-end workflow benchmark")
    print(f"  REA engine : {'REAL (REAGRAPH_REA_CMD)' if using_real else 'stand-in MCP server'}"
          f"  | modeled analysis latency: {os.environ['REA_LATENCY']}s")
    print("=" * 70)

    timings = {}
    try:
        def pull(target, graph):
            client = rea_client.ReaClient(target=target, server_cmd=server_cmd, cache=cache)
            return bridge.ingest_stream(client, graph)

        s1, t1 = _timed("pull v1 (cold)", lambda: pull(str(app), g1))
        timings["1. pull v1 (cold, 1 analysis)"] = t1
        _, t2 = _timed("pull v1 (cached)", lambda: pull(str(app), g1))
        timings["2. pull v1 (fully cached -> no REA spawn)"] = t2

        gv1 = memory.load(g1)
        _, t3 = _timed("summary", lambda: summarize.summarize(gv1))
        timings["3. summary"] = t3
        _, t4 = _timed("coverage", lambda: coverage.coverage(gv1))
        timings["4. coverage"] = t4

        s5, t5 = _timed("pull v2 (cold)", lambda: pull(str(appv2), g2))
        timings["5. pull v2 (cold, 1 analysis)"] = t5

        gv2 = memory.load(g2)
        dres, t6 = _timed("diff v1 vs v2", lambda: diff.diff(gv1, gv2))
        timings["6. diff v1 vs v2"] = t6

        ares, t7 = _timed("attest v2 vs source", lambda: attest.attest(_source_graph(), gv2))
        timings["7. attest v2 vs source"] = t7

        print("\nSTAGE TIMINGS")
        for k, v in timings.items():
            print(f"  {k:<40} {v:8.1f} ms")

        print("\nCACHE WIN (stage 1 cold vs stage 2 fully-cached)")
        if t2 > 0:
            print(f"  cold {t1:.0f} ms vs hit {t2:.0f} ms  ->  {t1 / t2:.1f}x faster")
        # Expected behavior once the fast path lands: when every requested tool is
        # already cached, findings() returns cached fragments WITHOUT spawning the
        # REA MCP server, so the hit should be a tiny fraction of the cold time
        # (single-digit ms) rather than paying ~300ms of spawn/handshake again.
        #
        # Verdict heuristic: a real no-spawn fast path collapses the hit to well
        # under a quarter of the cold time. If the hit is still ~= cold minus the
        # modeled analysis latency, REA was re-spawned (fast path absent/ineffective).
        spawn_skipped = t2 < (t1 * 0.25)
        try:
            lat_ms = float(os.environ.get("REA_LATENCY", "0")) * 1000.0
        except ValueError:
            lat_ms = 0.0
        print(f"  spawn skipped: {'YES' if spawn_skipped else 'NO'} "
              f"(hit {t2:.0f} ms {'<' if spawn_skipped else '>='} 25% of cold {t1:.0f} ms)")
        if spawn_skipped:
            print("    -> fully-cached pull returned without spawning REA (fast path active).")
        else:
            print(f"    -> hit still carries spawn/handshake cost (~cold {t1:.0f} ms minus "
                  f"~{lat_ms:.0f} ms analysis latency); REA was re-spawned (fast path "
                  "not present / not effective).")

        print("\nRESULTS")
        print(f"  v1 graph : {memory.stats(gv1)['nodes']} nodes, {memory.stats(gv1)['edges']} edges")
        print(f"  v2 graph : {memory.stats(gv2)['nodes']} nodes, {memory.stats(gv2)['edges']} edges")
        print(f"  diff     : +{dres['summary']['nodes_added']} nodes / "
              f"+{dres['summary']['edges_added']} edges, "
              f"{dres['summary']['unchanged_nodes']} unchanged")
        print(f"  attest   : integrity {ares['integrity']:.3f}, "
              f"shipped-unexplained {len(ares['shipped_unexplained'])} node(s): "
              f"{ares['shipped_unexplained']}")
        ok = len(gv1.get('nodes', [])) > 0 and ares['integrity'] < 1.0 and ares['shipped_unexplained']
        print("\nE2E:", "OK -- full pipeline ran; tamper surfaced" if ok else "ran, but check output above")
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
