"""The cache primitive guarantee: an analysis already done is never re-run.

These tests use a FAKE in-memory cache (dict-backed, duck-typed to the
`key_for` / `get` / `put` surface `reagraph.cache` exposes) and an `analyze_fn`
that counts its own calls. They depend on nothing from REA or the real cache.
"""

from reagraph.cached import cache_analysis


class FakeCache:
    """Dict-backed stand-in for `reagraph.cache.Cache`."""

    def __init__(self):
        self.store = {}

    def key_for(self, artifact_path, tool="", args=None, extractor_version=""):
        # A stable identity for (artifact, tool, args, version).
        return (artifact_path, tool, tuple(sorted((args or {}).items())), extractor_version)

    def get(self, key):
        return self.store.get(key)

    def put(self, key, fragment, meta=None):
        self.store[key] = {"fragment": fragment, "meta": meta}


class Counter:
    """An analyze_fn that records how many times it actually ran."""

    def __init__(self, fragment):
        self.fragment = fragment
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.fragment


def test_first_call_is_a_miss_and_runs_the_analysis():
    cache = FakeCache()
    frag = {"nodes": [{"id": "a"}], "links": []}
    analyze = Counter(frag)

    result, hit = cache_analysis(cache, "bin/app", "analyze_js", {"depth": 1}, analyze)

    assert hit is False
    assert result == frag
    assert analyze.calls == 1
    # The entry is now stored for next time.
    key = cache.key_for("bin/app", "analyze_js", {"depth": 1})
    assert cache.store[key]["fragment"] == frag
    assert cache.store[key]["meta"] == {"artifact": "bin/app", "tool": "analyze_js"}


def test_second_identical_call_is_a_hit_and_does_not_rerun():
    cache = FakeCache()
    frag = {"nodes": [{"id": "a"}], "links": []}
    analyze = Counter(frag)

    first, first_hit = cache_analysis(cache, "bin/app", "analyze_js", {"depth": 1}, analyze)
    second, second_hit = cache_analysis(cache, "bin/app", "analyze_js", {"depth": 1}, analyze)

    # The point of the whole module: analyze_fn ran exactly once across two
    # identical requests; the second request was served from cache.
    assert analyze.calls == 1
    assert first_hit is False and second_hit is True
    assert second == first == frag


def test_different_args_miss_again():
    cache = FakeCache()
    analyze = Counter({"nodes": [], "links": []})

    cache_analysis(cache, "bin/app", "analyze_js", {"depth": 1}, analyze)
    cache_analysis(cache, "bin/app", "analyze_js", {"depth": 2}, analyze)

    # Different args => different key => a real analysis each time.
    assert analyze.calls == 2


def test_refresh_forces_rerun_even_on_a_stored_key():
    cache = FakeCache()
    analyze = Counter({"nodes": [], "links": []})

    cache_analysis(cache, "bin/app", "analyze_js", {"depth": 1}, analyze)
    result, hit = cache_analysis(
        cache, "bin/app", "analyze_js", {"depth": 1}, analyze, refresh=True
    )

    assert analyze.calls == 2
    assert hit is False  # refresh always recomputes, so it reports a non-hit


def test_broken_cache_get_is_treated_as_a_miss():
    class BrokenGetCache(FakeCache):
        def get(self, key):
            raise RuntimeError("backend down")

    cache = BrokenGetCache()
    frag = {"nodes": [{"id": "x"}], "links": []}
    analyze = Counter(frag)

    # Fail-open: no exception propagates, analysis still runs.
    result, hit = cache_analysis(cache, "bin/app", "analyze_js", {"depth": 1}, analyze)

    assert result == frag
    assert hit is False
    assert analyze.calls == 1
