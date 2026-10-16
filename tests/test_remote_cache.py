"""Shared/remote cache tier: privacy-by-default, fail-open, drop-in CacheBackend.

All offline and deterministic: a dict-backed FakeTransport stands in for the
network, so no real HTTP is ever made. We assert the privacy posture (read-only is
the default and never uploads; upload is opt-in AND gated by allow_push), the
fail-open behaviour (a transport that raises never breaks get/put), and that the
backend is a genuine drop-in for the real reagraph.cache.Cache.
"""

import pytest

from reagraph import cache
from reagraph.cache import Cache, LocalCacheBackend
from reagraph.remote_cache import RemoteCacheBackend, TieredBackend, tiered


class FakeTransport:
    """In-memory dict-backed transport matching the UrllibTransport surface."""

    def __init__(self, store=None):
        self.store = {} if store is None else store

    def get(self, key):
        return self.store.get(key)

    def put(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)

    def list(self):
        return list(self.store)


class BoomTransport:
    """A transport that raises on every call, to exercise the fail-open path."""

    def get(self, key):
        raise RuntimeError("network down")

    def put(self, key, value):
        raise RuntimeError("network down")

    def delete(self, key):
        raise RuntimeError("network down")

    def list(self):
        raise RuntimeError("network down")


# -- round-trip when uploading is fully enabled ---------------------------
def test_round_trip_when_writable_and_push_allowed():
    t = FakeTransport()
    backend = RemoteCacheBackend("http://shared", transport=t, read_only=False, allow_push=True)
    backend.put("k1", b"payload")
    assert t.store == {"k1": b"payload"}
    assert backend.get("k1") == b"payload"
    assert list(backend.keys()) == ["k1"]
    assert len(backend) == 1


# -- read-only (the DEFAULT) never uploads but still serves gets ----------
def test_read_only_is_the_default():
    assert RemoteCacheBackend("http://shared", transport=FakeTransport()).read_only is True


def test_read_only_never_uploads_but_still_serves_gets():
    # Pre-seed the shared store (as if someone else's public analysis lives there).
    t = FakeTransport({"shared-key": b"someone-elses-analysis"})
    backend = RemoteCacheBackend("http://shared", transport=t)  # read_only default

    # put is a no-op: nothing new lands in the shared store.
    backend.put("mine", b"private-analysis")
    assert "mine" not in t.store
    assert t.store == {"shared-key": b"someone-elses-analysis"}

    # ...but we still benefit from what others published.
    assert backend.get("shared-key") == b"someone-elses-analysis"


# -- privacy guard: read_only off is NOT enough; allow_push must agree ----
def test_write_enabled_but_allow_push_false_still_refuses():
    t = FakeTransport()
    backend = RemoteCacheBackend("http://shared", transport=t, read_only=False)  # allow_push False
    backend.put("k1", b"payload")
    assert t.store == {}  # privacy guard refused the upload


def test_allow_push_predicate_gates_per_entry():
    t = FakeTransport()
    # Only entries whose value is explicitly marked public may leave the machine.
    backend = RemoteCacheBackend(
        "http://shared",
        transport=t,
        read_only=False,
        allow_push=lambda key, value: value.startswith(b"PUBLIC:"),
    )
    backend.put("private", b"secret bytes")
    backend.put("public", b"PUBLIC:ok")
    assert t.store == {"public": b"PUBLIC:ok"}


def test_broken_push_predicate_fails_closed():
    t = FakeTransport()

    def boom(key, value):
        raise ValueError("cannot decide")

    backend = RemoteCacheBackend("http://shared", transport=t, read_only=False, allow_push=boom)
    backend.put("k1", b"payload")
    assert t.store == {}  # a broken guard refuses, never leaks


# -- fail-open: a raising transport never breaks a local pull -------------
def test_fail_open_get_returns_none_and_put_does_not_raise():
    backend = RemoteCacheBackend("http://shared", transport=BoomTransport(), read_only=False, allow_push=True)
    assert backend.get("anything") is None  # miss, not an exception
    backend.put("k1", b"payload")           # no-op, must not raise
    assert list(backend.keys()) == []       # list failure -> behaves as empty
    backend.delete("k1")                    # must not raise


# -- read-only delete is a no-op on the shared store ----------------------
def test_read_only_delete_is_a_no_op():
    t = FakeTransport({"k1": b"x"})
    backend = RemoteCacheBackend("http://shared", transport=t)  # read_only default
    backend.delete("k1")
    assert "k1" in t.store


# -- genuine drop-in for the real reagraph.cache.Cache --------------------
def test_plugs_into_real_cache_round_trip():
    t = FakeTransport()
    backend = RemoteCacheBackend("http://shared", transport=t, read_only=False, allow_push=True)
    c = Cache(backend=backend)

    fragment = {"nodes": [{"id": "a"}], "links": []}
    c.put("key-a", fragment, meta={"tool": "ghidra"})

    got = c.get("key-a")
    assert got is not None
    assert got["fragment"] == fragment
    assert got["meta"]["tool"] == "ghidra"
    assert got["meta"]["schema"] == cache.SCHEMA
    # The value actually round-tripped through our remote byte store.
    assert "key-a" in t.store


# -- tiered helper: a remote hit populates local --------------------------
def test_tiered_remote_hit_populates_local(tmp_path):
    local = LocalCacheBackend(tmp_path / "cache")
    remote_store = FakeTransport({"warm-me": b"from-remote"})
    remote = RemoteCacheBackend("http://shared", transport=remote_store)  # read_only default
    tier = tiered(local, remote)
    assert isinstance(tier, TieredBackend)

    # Local starts cold.
    assert local.get("warm-me") is None

    # First get falls through to remote...
    assert tier.get("warm-me") == b"from-remote"
    # ...and warms the local tier, so it is now served locally.
    assert local.get("warm-me") == b"from-remote"


def test_tiered_prefers_local():
    local = FakeTransport({"k": b"local-wins"})
    remote = RemoteCacheBackend("http://shared", transport=FakeTransport({"k": b"remote-loses"}))
    tier = TieredBackend(local, remote)
    assert tier.get("k") == b"local-wins"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
