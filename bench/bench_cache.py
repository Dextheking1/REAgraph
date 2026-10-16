#!/usr/bin/env python3
"""bench_cache.py -- the CACHE-WIN benchmark for REAgraph.

What this measures
------------------
REAgraph's expensive step is *analysis*: running a REA tool over an artifact to
produce a graph fragment. The same artifact, analysed the same way, always
yields the same fragment -- so we should never pay for it twice. `reagraph.cache`
+ `reagraph.cached.cache_analysis` turn a repeated analysis into a cheap,
content-addressed lookup: the first call is a miss (we run the analysis once and
store it); every later call for the same artifact+tool+args is a hit that returns
the stored fragment WITHOUT calling the analyzer at all.

This benchmark makes that win concrete. It analyses the SAME artifact N times,
once WITHOUT the cache and once WITH it, counting the actual number of analysis
calls and the wall time of each run. It also measures the cache-hit lookup
latency on its own and the raw get/put throughput of the backend.

Honesty note on the "analysis"
-------------------------------
The analysis here is a STAND-IN, not a real REA call. We model it as a function
that sleeps for REA_LATENCY seconds (default 0.2s) and returns a small fragment.
A real REA analysis takes seconds to minutes, not 0.2s -- so the raw time numbers
below are only as meaningful as the latency you plug in. The *shape* of the win,
though, is real: the cache turns N analyses into 1. The wall-time saving is
(N - 1) * (real REA latency), and it compounds every time the same artifact is
re-analysed -- across sessions, across teammates (with a shared backend), and in
CI. We print a projection at a realistic latency to make that explicit.

Everything here is stdlib. The benchmark points $REAGRAPH_CACHE at a fresh temp
directory so it never touches your real cache, and removes it on exit.

Run:
    .venv/bin/python bench/bench_cache.py
Env knobs:
    REA_LATENCY   seconds per modeled analysis call   (default 0.2)
    REA_REPEATS   how many times the artifact is seen  (default 10)
    REA_THROUGHPUT_OPS  entries for the get/put throughput test (default 2000)
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import tempfile
from pathlib import Path

# Make `import reagraph` work when run from a checkout without an install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


LATENCY = _env_float("REA_LATENCY", 0.2)   # STAND-IN per-analysis cost, seconds
REPEATS = _env_int("REA_REPEATS", 10)      # times the same artifact is analysed
THROUGHPUT_OPS = _env_int("REA_THROUGHPUT_OPS", 2000)

TOOL = "bench.rea"
ARGS = {"depth": 2, "mode": "full"}
EXTRACTOR_VERSION = "bench-1"


class _Analyzer:
    """A stand-in for a real REA analysis.

    It sleeps for `latency` to represent the real cost of running a REA tool over
    an artifact, counts how many times it was actually invoked, and returns a
    small, deterministic graph fragment (node-link shape)."""

    def __init__(self, latency: float):
        self.latency = latency
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        if self.latency > 0:
            time.sleep(self.latency)
        return {
            "nodes": [
                {"id": "app", "kind": "artifact"},
                {"id": "lib.a", "kind": "module"},
                {"id": "lib.b", "kind": "module"},
            ],
            "links": [
                {"source": "app", "target": "lib.a"},
                {"source": "app", "target": "lib.b"},
            ],
        }


def _fmt_s(seconds: float) -> str:
    if seconds < 1e-3:
        return f"{seconds * 1e6:.1f}us"
    if seconds < 1.0:
        return f"{seconds * 1e3:.2f}ms"
    return f"{seconds:.3f}s"


def run_without_cache(artifact_path: str) -> tuple[int, float]:
    """Analyse the artifact REPEATS times with no cache: every call runs."""
    analyzer = _Analyzer(LATENCY)
    start = time.perf_counter()
    for _ in range(REPEATS):
        analyzer()  # a real REA call, every single time
    elapsed = time.perf_counter() - start
    return analyzer.calls, elapsed


def run_with_cache(cache, artifact_path: str) -> tuple[int, float]:
    """Analyse the artifact REPEATS times through cache_analysis: 1 miss, rest hits."""
    from reagraph.cached import cache_analysis

    analyzer = _Analyzer(LATENCY)
    start = time.perf_counter()
    for _ in range(REPEATS):
        cache_analysis(
            cache,
            artifact_path,
            TOOL,
            ARGS,
            analyzer,
            extractor_version=EXTRACTOR_VERSION,
        )
    elapsed = time.perf_counter() - start
    return analyzer.calls, elapsed


def measure_hit_latency(cache, artifact_path: str, samples: int = 1000) -> float:
    """Median wall time of one cache hit (get + no analysis). Sub-millisecond.

    The entry is already warm; we call cache_analysis repeatedly with an analyzer
    that would blow up if ever invoked, proving these are pure hits, and time each
    call. We report the median to shrug off scheduler jitter."""
    from reagraph.cached import cache_analysis

    def _must_not_run():
        raise AssertionError("analyzer invoked on what should be a cache hit")

    timings = []
    for _ in range(samples):
        t0 = time.perf_counter()
        _, hit = cache_analysis(
            cache,
            artifact_path,
            TOOL,
            ARGS,
            _must_not_run,
            extractor_version=EXTRACTOR_VERSION,
        )
        timings.append(time.perf_counter() - t0)
        assert hit, "expected a cache hit"
    timings.sort()
    return timings[len(timings) // 2]


def measure_throughput(cache, ops: int) -> tuple[float, float]:
    """Raw backend put/get throughput over many small, distinct entries.

    Returns (put_ops_per_sec, get_ops_per_sec). This isolates the cache store
    itself from the modeled analysis latency -- it's what a cold build pays to
    populate the cache, and what every warm lookup pays to read it."""
    fragment = {"nodes": [{"id": "n", "kind": "module"}], "links": []}
    keys = [f"{i:064x}" for i in range(ops)]

    t0 = time.perf_counter()
    for k in keys:
        cache.put(k, fragment, meta={"bench": True})
    put_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    for k in keys:
        cache.get(k)
    get_elapsed = time.perf_counter() - t0

    put_rate = ops / put_elapsed if put_elapsed > 0 else float("inf")
    get_rate = ops / get_elapsed if get_elapsed > 0 else float("inf")
    return put_rate, get_rate


def main() -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="reagraph-bench-cache-"))
    cache_dir = tmp_root / "cache"
    cache_dir.mkdir()
    # Point the cache at the temp dir so we never touch the user's real cache.
    os.environ["REAGRAPH_CACHE"] = str(cache_dir)

    # A real artifact file to hash: content-addressed keys need bytes on disk.
    artifact_path = tmp_root / "artifact.bin"
    artifact_path.write_bytes(b"REAgraph cache benchmark artifact\n" * 4096)
    artifact_path = str(artifact_path)

    try:
        from reagraph.cache import Cache, LocalCacheBackend

        print("=" * 68)
        print("REAgraph CACHE-WIN benchmark")
        print("=" * 68)
        print(f"modeled REA latency : {_fmt_s(LATENCY)} per analysis "
              f"(STAND-IN -- real REA takes seconds to minutes)")
        print(f"repeats (same artifact): {REPEATS}")
        print(f"cache dir           : {cache_dir}")
        print()

        # -- Scenario A: no cache -------------------------------------------
        calls_nocache, time_nocache = run_without_cache(artifact_path)

        # -- Scenario B: with cache (fresh, so entry #1 is a miss) -----------
        cache = Cache(LocalCacheBackend(cache_dir))
        calls_cache, time_cache = run_with_cache(cache, artifact_path)

        speedup = time_nocache / time_cache if time_cache > 0 else float("inf")
        calls_avoided = calls_nocache - calls_cache

        print("Analyse the SAME artifact "
              f"{REPEATS} times")
        print("-" * 68)
        print(f"  WITHOUT cache : {calls_nocache:>4} analysis calls   "
              f"wall {_fmt_s(time_nocache)}")
        print(f"  WITH cache    : {calls_cache:>4} analysis calls   "
              f"wall {_fmt_s(time_cache)}   (1 miss + "
              f"{REPEATS - calls_cache} hits)")
        print(f"  calls avoided : {calls_avoided} of {calls_nocache} "
              f"({100 * calls_avoided / calls_nocache:.0f}%)")
        print(f"  speedup       : {speedup:.1f}x")
        print()

        # -- Cache-hit lookup latency on its own ----------------------------
        hit_latency = measure_hit_latency(cache, artifact_path)
        print("Cache-hit lookup latency (what a hit actually costs)")
        print("-" * 68)
        print(f"  per hit (median): {_fmt_s(hit_latency)}  "
              f"-- vs {_fmt_s(LATENCY)} to re-analyse "
              f"({LATENCY / hit_latency:,.0f}x cheaper)")
        print()

        # -- Raw backend throughput -----------------------------------------
        # Use a separate cache dir so these filler entries don't pollute stats.
        tp_dir = tmp_root / "throughput"
        tp_dir.mkdir()
        tp_cache = Cache(LocalCacheBackend(tp_dir))
        put_rate, get_rate = measure_throughput(tp_cache, THROUGHPUT_OPS)
        print(f"Raw backend throughput (LocalCacheBackend, {THROUGHPUT_OPS} "
              f"small entries)")
        print("-" * 68)
        print(f"  put : {put_rate:>12,.0f} ops/sec")
        print(f"  get : {get_rate:>12,.0f} ops/sec")
        print()

        # -- Honest projection at a realistic REA latency -------------------
        print("What this means at real REA latencies")
        print("-" * 68)
        print("  The win is (repeats - 1) * real-REA-latency, and it compounds")
        print("  every time an artifact is re-analysed -- across sessions, across")
        print("  teammates (shared backend), and in CI. Projected saving for "
              f"{REPEATS} repeats:")
        for real_latency in (5.0, 30.0, 120.0):
            saved = (REPEATS - 1) * real_latency
            print(f"    at {real_latency:>5.0f}s/analysis, {REPEATS} repeats: "
                  f"~{saved:.0f}s saved "
                  f"({_fmt_s(hit_latency)} per hit instead)")
        print("=" * 68)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        os.environ.pop("REAGRAPH_CACHE", None)


if __name__ == "__main__":
    main()
