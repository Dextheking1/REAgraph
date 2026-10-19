"""Content-addressed cache: round-trip, fail-open, deterministic keys, persistence.

All offline and deterministic: the cache dir is a tmp_path, and we stub
`artifact.artifact_id` to a fixed value so we never depend on the sibling module.
"""

import json
import threading

from reagraph import cache
from reagraph.cache import Cache, LocalCacheBackend


FRAGMENT = {
    "directed": True,
    "multigraph": False,
    "nodes": [{"id": "check", "label": "check()", "confidence": "EXTRACTED"}],
    "links": [{"source": "check", "target": "net", "relation": "calls",
               "confidence": "INFERRED", "context": "rea:calls"}],
}


def _cache(tmp_path):
    return Cache(LocalCacheBackend(tmp_path / "cache"))


def test_put_then_get_round_trips(tmp_path):
    c = _cache(tmp_path)
    c.put("k1", FRAGMENT, meta={"tool": "ghidra"})
    got = c.get("k1")
    assert got is not None
    assert got["fragment"] == FRAGMENT
    # meta is enriched with a timestamp + schema tag, caller's keys preserved.
    assert got["meta"]["tool"] == "ghidra"
    assert got["meta"]["schema"] == cache.SCHEMA
    assert "created_at" in got["meta"]


def test_missing_key_returns_none(tmp_path):
    c = _cache(tmp_path)
    assert c.get("does-not-exist") is None


def test_corrupt_entry_returns_none_no_raise(tmp_path):
    c = _cache(tmp_path)
    c.put("bad", FRAGMENT)
    # Clobber the on-disk file with garbage bytes.
    path = c.backend._path("bad")
    path.write_bytes(b"{ this is not json ")
    # Fail-open: a corrupt entry is a miss, never an exception.
    assert c.get("bad") is None


def test_stats_counts_entries_and_bytes(tmp_path):
    c = _cache(tmp_path)
    assert c.stats()["entries"] == 0
    c.put("a", FRAGMENT)
    c.put("b", FRAGMENT)
    s = c.stats()
    assert s["entries"] == 2
    assert s["bytes"] > 0
    assert s["dir"] == str(tmp_path / "cache")


def test_clear_empties(tmp_path):
    c = _cache(tmp_path)
    c.put("a", FRAGMENT)
    c.put("b", FRAGMENT)
    assert c.clear() == 2
    assert c.stats()["entries"] == 0
    assert c.get("a") is None


def test_key_for_stable_and_scope_sensitive(tmp_path, monkeypatch):
    c = _cache(tmp_path)
    # Stub the sibling so the key depends only on the fixed id + scope.
    monkeypatch.setattr(cache.artifact, "artifact_id", lambda path: "sha256:deadbeef")

    k = c.key_for("/some/artifact", tool="ghidra", args={"depth": 2})
    # Same (hash, tool, args) -> identical key (even with reordered arg dict).
    assert k == c.key_for("/some/artifact", tool="ghidra", args={"depth": 2})
    assert k == c.key_for("/other/path", tool="ghidra", args={"depth": 2})  # path-agnostic

    # Different tool, args, or extractor version -> different key.
    assert k != c.key_for("/some/artifact", tool="radare2", args={"depth": 2})
    assert k != c.key_for("/some/artifact", tool="ghidra", args={"depth": 3})
    assert k != c.key_for("/some/artifact", tool="ghidra", args={"depth": 2},
                          extractor_version="2.0")


def test_key_tracks_artifact_content(tmp_path, monkeypatch):
    c = _cache(tmp_path)
    monkeypatch.setattr(cache.artifact, "artifact_id", lambda path: "sha256:aaaa")
    k1 = c.key_for("/x", tool="t")
    monkeypatch.setattr(cache.artifact, "artifact_id", lambda path: "sha256:bbbb")
    k2 = c.key_for("/x", tool="t")
    # Same path, different content id -> different key (content-addressed).
    assert k1 != k2


def test_put_records_value_sha256_and_get_verifies(tmp_path):
    c = _cache(tmp_path)
    c.put("k1", FRAGMENT)
    got = c.get("k1")
    assert got is not None
    # put stamps the value's own digest into meta...
    recorded = got["meta"]["value_sha256"]
    assert recorded == cache._value_sha256(FRAGMENT)
    # ...and a clean round-trip passes the integrity check unchanged.
    assert got["fragment"] == FRAGMENT


def test_tampered_value_is_a_corrupt_miss(tmp_path):
    c = _cache(tmp_path)
    c.put("bad", FRAGMENT)
    # Rewrite the fragment bytes on disk but LEAVE the old value_sha256 in meta,
    # simulating corruption/truncation/tampering of a stored value.
    path = c.backend._path("bad")
    payload = json.loads(path.read_bytes())
    assert "value_sha256" in payload["meta"]
    payload["fragment"]["nodes"][0]["label"] = "tampered()"
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    # The recorded digest no longer matches the stored value -> corrupt miss.
    assert c.get("bad") is None


def test_entry_without_value_sha256_still_loads(tmp_path):
    # Back-compat: an older entry predating the integrity check carries no
    # value_sha256 and must still load.
    c = _cache(tmp_path)
    key = "legacy"
    legacy_payload = {
        "fragment": FRAGMENT,
        "meta": {"schema": cache.SCHEMA, "created_at": 0.0},  # no value_sha256
    }
    c.backend.put(key, json.dumps(legacy_payload).encode("utf-8"))
    got = c.get(key)
    assert got is not None
    assert got["fragment"] == FRAGMENT
    assert "value_sha256" not in got["meta"]


def test_concurrent_put_get_never_tears(tmp_path):
    # The atomicity claim (tmp file + os.replace) under real concurrency: N threads
    # hammer put/get on a mix of shared and distinct keys against one cache dir.
    # Every get that returns must be a complete, valid payload -- never torn/empty.
    c = _cache(tmp_path)
    n_threads = 12
    iters = 40
    shared_keys = [f"shared-{i}" for i in range(4)]
    barrier = threading.Barrier(n_threads)
    errors = []

    def worker(tid):
        barrier.wait()  # release all threads at once for maximum overlap
        for i in range(iters):
            try:
                distinct = f"t{tid}-{i}"
                c.put(distinct, FRAGMENT, meta={"tid": tid, "i": i})
                shared = shared_keys[(tid + i) % len(shared_keys)]
                c.put(shared, FRAGMENT, meta={"shared": True})
                for key in (distinct, shared):
                    got = c.get(key)
                    # A concurrent reader must see a whole, valid payload or nothing
                    # (nothing only before its own write landed, which never happens
                    # for `distinct` here since we just put it).
                    assert got is not None, f"torn/empty read for {key}"
                    assert got["fragment"] == FRAGMENT
            except Exception as exc:  # record, don't swallow, any failure
                errors.append((tid, repr(exc)))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # Every distinct key resolves to a valid payload afterwards.
    for tid in range(n_threads):
        for i in range(iters):
            got = c.get(f"t{tid}-{i}")
            assert got is not None
            assert got["fragment"] == FRAGMENT
    for key in shared_keys:
        got = c.get(key)
        assert got is not None
        assert got["fragment"] == FRAGMENT


def test_two_instances_share_entries(tmp_path):
    # Persistence: a second Cache over the same dir sees the first's writes.
    dir_ = tmp_path / "cache"
    c1 = Cache(LocalCacheBackend(dir_))
    c1.put("shared", FRAGMENT, meta={"n": 1})

    c2 = Cache(LocalCacheBackend(dir_))
    got = c2.get("shared")
    assert got is not None
    assert got["fragment"] == FRAGMENT
    assert got["meta"]["n"] == 1
    assert c2.stats()["entries"] == 1
