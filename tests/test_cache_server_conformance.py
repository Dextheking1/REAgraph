"""Conformance: the REAL remote-cache client against the REAL server over a live
socket. This is the money test -- it proves the server speaks exactly the wire
contract ``reagraph.remote_cache`` expects, with nothing mocked in between.

A ``ThreadingHTTPServer`` runs on an ephemeral port in a daemon thread; the real
``RemoteCacheBackend`` / ``TieredBackend`` / ``Cache`` drive it. We assert the
cross-machine round-trip, the privacy posture (read-only pushes nothing), the
tiered warm-on-remote-hit, and fail-open against a dead port.
"""

import socket
import threading
import urllib.error
import urllib.request

import pytest

from reagraph.cache import Cache, LocalCacheBackend
from reagraph.cache_server import make_server
from reagraph.remote_cache import RemoteCacheBackend, TieredBackend


class _RunningServer:
    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    srv = make_server(
        backend=LocalCacheBackend(tmp_path / "server-store"), port=0, write_token="w"
    )
    running = _RunningServer(srv)
    yield running
    running.close()


def _artifact(tmp_path, name="app.js", content=b"console.log(1)\n"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_cross_machine_round_trip(server, tmp_path):
    """A writer with a token pushes an analysis; a tokenless, read-only reader --
    standing in for a different machine -- pulls it back byte-for-byte."""
    artifact = _artifact(tmp_path)
    fragment = {"nodes": [{"id": "n1", "label": "main"}], "links": []}

    writer = Cache(
        backend=RemoteCacheBackend(
            server.base, token="w", read_only=False, allow_push=True
        )
    )
    key = writer.key_for(
        artifact, "analyze_javascript_application", {"input_path": str(artifact)}
    )
    writer.put(key, fragment, meta={"tool": "x"})

    # A separate reader: defaults are read_only=True and NO token.
    reader = Cache(backend=RemoteCacheBackend(server.base))
    got = reader.get(key)
    assert got is not None
    assert got["fragment"] == fragment


def test_read_only_backend_pushes_nothing(server):
    """read_only (the default) makes put a no-op end to end: the key never lands
    on the server, confirmed by a direct GET returning 404."""
    key = "a" * 64
    ro = RemoteCacheBackend(server.base)  # read_only defaults True
    ro.put(key, b'{"fragment": {}}')  # silently refused

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{server.base}/{key}", timeout=5)
    assert exc.value.code == 404


def test_tiered_backend_warms_local_on_remote_hit(server, tmp_path):
    """A TieredBackend with an empty local tier fetches from the server on a miss
    and populates local, so the next read is served offline."""
    artifact = _artifact(tmp_path, name="lib.js", content=b"export const a = 1\n")
    fragment = {"nodes": [{"id": "x"}], "links": []}

    # Pre-store on the server with a write-enabled client.
    writer = Cache(
        backend=RemoteCacheBackend(
            server.base, token="w", read_only=False, allow_push=True
        )
    )
    key = writer.key_for(artifact, "analyze_javascript_application", {"p": str(artifact)})
    writer.put(key, fragment)

    local = LocalCacheBackend(tmp_path / "fresh-local")
    tiered = TieredBackend(local, RemoteCacheBackend(server.base))
    cache = Cache(backend=tiered)

    got = cache.get(key)
    assert got is not None
    assert got["fragment"] == fragment

    # The local tier is now warm: it holds the entry on its own.
    assert local.get(key) is not None
    # And a read with the remote pointed at a dead url still returns from local.
    dead = Cache(backend=TieredBackend(local, RemoteCacheBackend("http://127.0.0.1:1")))
    assert dead.get(key)["fragment"] == fragment


def test_fail_open_against_dead_port():
    """A remote pointed at a port nobody is listening on returns a miss (None),
    never an exception -- a down shared cache can't break a build."""
    # Grab a free port, then release it so nothing is listening there.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    backend = RemoteCacheBackend(f"http://127.0.0.1:{dead_port}", timeout=1.0)
    assert backend.get("a" * 64) is None
