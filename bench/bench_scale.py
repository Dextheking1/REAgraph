#!/usr/bin/env python3
"""SCALE / throughput benchmark for REAgraph's own graph operations.

What this measures (be honest about it)
----------------------------------------
This times REAgraph's *graph* operations on synthetic node-link graphs:

    memory.merge   -- fold a fragment into a persistent graph (dedup + upgrade)
    diff.diff      -- byte-agnostic structural diff of two graphs
    summarize.summarize -- bounded overview (god-nodes, components, totals)
    align.align    -- match nodes of two graphs for the same program

It does NOT measure REA's binary analysis. REA (the reverse-engineering engine
that produces the binary graphs) is an external tool; REAgraph only consumes and
operates on the node-link graphs. These numbers are about how REAgraph's graph
layer scales, not about disassembly speed.

Graphs use graphify's node-link JSON shape:

    {"directed": true, "multigraph": false,
     "nodes": [{"id": "fn_0", "label": "fn_0", ...}],
     "links": [{"source": "fn_0", "target": "fn_1", "relation": "calls", ...}]}

Pure stdlib. Self-contained: it carries its own deterministic graph generator and
does not import any sibling bench helper. Run it with the repo venv:

    .venv/bin/python bench/bench_scale.py
"""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
import time

# Make `import reagraph` work whether or not the package is pip-installed:
# this file lives in <repo>/bench, so the repo root is one directory up.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from reagraph import align, diff, memory, summarize  # noqa: E402


# --------------------------------------------------------------------------- #
# Deterministic graph generator
# --------------------------------------------------------------------------- #
def make_graph(n, seed=0):
    """A connected directed graph of `n` nodes with a realistic-ish degree
    spread: a spanning tree keeps it weakly connected, a handful of hubs soak up
    a lot of edges, and the long tail of ordinary nodes keeps degree ~1-3.

    Deterministic for a given (n, seed). Node ids/labels are "fn_<i>".
    """
    rng = random.Random(seed)
    nodes = [{"id": "fn_%d" % i, "label": "fn_%d" % i} for i in range(n)]
    links = []

    if n <= 0:
        return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}

    # Spanning tree: every node (except the root) calls an earlier node. This
    # alone guarantees the graph is weakly connected (one component).
    for i in range(1, n):
        j = rng.randint(0, i - 1)
        links.append({
            "source": "fn_%d" % i,
            "target": "fn_%d" % j,
            "relation": "calls",
            "confidence": "EXTRACTED",
        })

    # A few hubs that everything touches (the "god nodes"). Each hub pulls in
    # many incoming edges from random callers, producing a long-tailed degree
    # distribution instead of a uniform one.
    num_hubs = max(1, int(n ** 0.5) // 4)
    fanout = max(10, n // 100)
    hub_ids = rng.sample(range(n), min(num_hubs, n))
    seen = {(e["source"], e["target"]) for e in links}
    for h in hub_ids:
        hub = "fn_%d" % h
        for _ in range(fanout):
            caller = "fn_%d" % rng.randrange(n)
            if caller == hub:
                continue
            key = (caller, hub)
            if key in seen:
                continue
            seen.add(key)
            links.append({
                "source": caller,
                "target": hub,
                "relation": "calls",
                "confidence": "INFERRED",
            })

    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


def relocate(graph, seed=0):
    """A 'relocated' copy: the SAME program whose bytes merely moved. Ids, edges
    and the (source,target,relation) structure are identical, but per-node byte
    locations change. REAgraph's diff is byte-agnostic, so this should read as
    entirely UNCHANGED -- that is the point of the exercise."""
    rng = random.Random(seed + 1)
    nodes = []
    for nd in graph["nodes"]:
        nn = dict(nd)
        base = rng.randint(0, 10_000_000)
        nn["source_location"] = "0x%x" % base       # moved address
        nn["source_file"] = "mod_%d.c" % (base % 97)  # moved file
        nodes.append(nn)
    links = [dict(e) for e in graph["links"]]
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


# --------------------------------------------------------------------------- #
# Timing harness
# --------------------------------------------------------------------------- #
def time_op(fn, repeats=3):
    """Warm up once, then run `fn` `repeats` times with perf_counter. Returns
    (best_ms, median_ms, last_result)."""
    fn()  # warm up (also surfaces errors before we trust any timing)
    samples = []
    result = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return min(samples), statistics.median(samples), result


def _throughput(n, ms):
    if ms <= 0:
        return float("inf")
    return n / (ms / 1000.0)


def _fmt_int(x):
    return "{:,}".format(int(round(x)))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    big_sizes = [1_000, 10_000, 100_000]
    align_sizes = [500, 2_000, 5_000]
    repeats = 3

    rows = []          # (operation, size, best_ms, median_ms, throughput, note)
    summary_sizes = []  # (n, summary_json_bytes, god_nodes, clusters_reported)
    diff_notes = []
    align_notes = []

    print("REAgraph SCALE / throughput benchmark")
    print("=" * 78)
    print("Timing REAgraph's own graph ops (not REA binary analysis -- REA is")
    print("external). perf_counter, 1 warm-up + %d timed runs, best & median.\n"
          % repeats)

    # ----- merge / diff / summarize on the big sizes -----
    for n in big_sizes:
        g = make_graph(n, seed=1)
        g2 = relocate(g, seed=1)
        n_edges = len(g["links"])
        print("  building graphs done: n=%s nodes, %s edges"
              % (_fmt_int(n), _fmt_int(n_edges)))

        # memory.merge(empty, g): fold the whole graph into a fresh memory.
        best, med, _ = time_op(
            lambda: memory.merge(memory.empty_graph(), g), repeats)
        rows.append(("merge", n, best, med, _throughput(n, best), ""))

        # diff.diff(g, g2): g2 is a relocated copy -> should be fully unchanged.
        best, med, dres = time_op(lambda: diff.diff(g, g2), repeats)
        rows.append(("diff", n, best, med, _throughput(n, best), ""))
        s = dres["summary"]
        diff_notes.append(
            "n=%s: unchanged=%s rewired=%s nodes+/-=%s/%s edges+/-=%s/%s"
            % (_fmt_int(n), _fmt_int(s["unchanged_nodes"]),
               _fmt_int(s["rewired_nodes"]), s["nodes_added"], s["nodes_removed"],
               s["edges_added"], s["edges_removed"]))

        # summarize.summarize(g): bounded overview regardless of n.
        best, med, sres = time_op(lambda: summarize.summarize(g), repeats)
        rows.append(("summarize", n, best, med, _throughput(n, best), ""))
        sbytes = len(json.dumps(sres))
        summary_sizes.append((n, sbytes, len(sres["god_nodes"]),
                              len(sres["largest_clusters"]),
                              sres["num_components"]))

    # ----- align on the SMALLER sizes (it is heavier) -----
    print("\n  align uses SMALLER sizes %s (it is heavier: WL labelling +\n"
          "  one-to-one matching + seed propagation over the whole graph).\n"
          % align_sizes)
    for n in align_sizes:
        g = make_graph(n, seed=2)
        g2 = relocate(g, seed=2)  # same names survive -> strong name matches
        best, med, ares = time_op(lambda: align.align(g, g2), repeats)
        rows.append(("align", n, best, med, _throughput(n, best),
                     "smaller size"))
        st = ares["stats"]
        align_notes.append(
            "n=%s: matched=%s/%s mean_conf=%s"
            % (_fmt_int(n), _fmt_int(st["matched"]), _fmt_int(st["src_nodes"]),
               st["mean_confidence"]))

    # 100k align is intentionally skipped: WL + matching is super-linear and
    # would blow the "couple of minutes" budget. We say so rather than run it.
    align_skip_note = ("align at 100,000 nodes is SKIPPED on purpose: it is "
                       "super-linear (WL relabelling + one-to-one matching + "
                       "seed-propagation passes) and would exceed the runtime "
                       "budget. See align scaling in the smaller sizes above.")

    # ----- results table -----
    print("\nRESULTS")
    print("-" * 78)
    hdr = "%-11s %12s %12s %12s %16s  %s" % (
        "operation", "size(nodes)", "best_ms", "median_ms", "throughput(n/s)",
        "note")
    print(hdr)
    print("-" * 78)
    for op, n, best, med, thr, note in rows:
        print("%-11s %12s %12.2f %12.2f %16s  %s" % (
            op, _fmt_int(n), best, med, _fmt_int(thr), note))
    print("-" * 78)

    # ----- bounded-summary evidence -----
    print("\nSUMMARIZE OUTPUT STAYS BOUNDED (size must NOT grow with n):")
    print("  %-14s %-16s %-12s %-14s %s" % (
        "graph nodes", "summary bytes", "god_nodes", "clusters shown",
        "components(total)"))
    for n, sbytes, gods, clus, comps in summary_sizes:
        print("  %-14s %-16s %-12s %-14s %s" % (
            _fmt_int(n), _fmt_int(sbytes), gods, clus, _fmt_int(comps)))

    # ----- diff / align observations -----
    print("\nDIFF (byte-agnostic: a relocated copy should read as unchanged):")
    for ln in diff_notes:
        print("  " + ln)

    print("\nALIGN (matching + confidence; smaller sizes):")
    for ln in align_notes:
        print("  " + ln)
    print("  " + align_skip_note)

    print("\nNote: these measure REAgraph's own graph operations, not REA "
          "analysis\n(REA is external). Throughput = graph nodes processed "
          "per second.")


if __name__ == "__main__":
    main()
