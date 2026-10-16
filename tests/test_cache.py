"""Content-addressed cache: round-trip, fail-open, deterministic keys, persistence.

All offline and deterministic: the cache dir is a tmp_path, and we stub
`artifact.artifact_id` to a fixed value so we never depend on the sibling module.
"""

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
