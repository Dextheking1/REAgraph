"""Unit tests for the hosted shared-cache server over a real loopback socket.

No mocks of the wire: each test starts a real ``ThreadingHTTPServer`` on an
ephemeral port in a daemon thread and talks to it with ``urllib.request``. This
exercises the actual HTTP parsing, routing, auth, size cap, payload validation,
and the never-crash 500 path end to end.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from reagraph.cache import LocalCacheBackend
from reagraph.cache_server import make_server

VALID_KEY = "a" * 64  # a well-formed sha256-hex storage key
ENTRY = json.dumps({"fragment": {"nodes": [], "links": []}, "meta": {}}).encode("utf-8")


# -- test harness -----------------------------------------------------------
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


def _request(base, key, method="GET", data=None, headers=None):
    """Return (status, body_bytes). HTTPError is unwrapped into (code, body)."""
    url = f"{base}/{key}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def server(tmp_path):
    srv = make_server(backend=LocalCacheBackend(tmp_path / "store"), port=0)
    running = _RunningServer(srv)
    yield running
    running.close()


# -- round-trip -------------------------------------------------------------
def test_put_then_get_round_trips_identical_bytes(server):
    status, _ = _request(server.base, VALID_KEY, method="PUT", data=ENTRY)
    assert status == 201
    status, body = _request(server.base, VALID_KEY, method="GET")
    assert status == 200
    assert body == ENTRY


def test_get_missing_key_is_404(server):
    status, _ = _request(server.base, "b" * 64, method="GET")
    assert status == 404


# -- key validation / path-traversal defence --------------------------------
class _SpyBackend(LocalCacheBackend):
    """Records every key handed to the backend so we can prove a bad key never
    reaches it (the key is validated before any backend call)."""

    def __init__(self, directory):
        super().__init__(directory)
        self.touched = []

    def get(self, key):
        self.touched.append(("get", key))
        return super().get(key)

    def put(self, key, value):
        self.touched.append(("put", key))
        return super().put(key, value)


@pytest.mark.parametrize(
    "bad",
    [
        "..%2Fetc%2Fpasswd",  # url-encoded path traversal
        "nothex",
        "g" * 64,  # right length, not hex
        "a" * 63,  # one char short
        "A" * 64,  # uppercase is not [0-9a-f]
    ],
)
def test_bad_key_is_400_and_never_reaches_backend(tmp_path, bad):
    backend = _SpyBackend(tmp_path / "store")
    srv = make_server(backend=backend, port=0)
    running = _RunningServer(srv)
    try:
        status, _ = _request(running.base, bad, method="GET")
        assert status == 400
        status, _ = _request(running.base, bad, method="PUT", data=ENTRY)
        assert status == 400
        # The malformed segment never touched the backend (no fs path built).
        assert backend.touched == []
    finally:
        running.close()


# -- keys listing -----------------------------------------------------------
def test_keys_lists_stored_keys_as_object(server):
    _request(server.base, VALID_KEY, method="PUT", data=ENTRY)
    status, body = _request(server.base, "keys", method="GET")
    assert status == 200
    payload = json.loads(body)
    assert payload == {"keys": [VALID_KEY]}


# -- delete -----------------------------------------------------------------
def test_delete_then_get_is_404(server):
    _request(server.base, VALID_KEY, method="PUT", data=ENTRY)
    status, _ = _request(server.base, VALID_KEY, method="DELETE")
    assert status == 204
    status, _ = _request(server.base, VALID_KEY, method="GET")
    assert status == 404


# -- health -----------------------------------------------------------------
def test_health_is_ok_json(server):
    status, body = _request(server.base, "health", method="GET")
    assert status == 200
    assert json.loads(body) == {"service": "reagraph-cache", "ok": True}

    # root path is health too
    status, body = _request(server.base, "", method="GET")
    assert status == 200
    assert json.loads(body)["ok"] is True


# -- size cap ---------------------------------------------------------------
def test_size_cap_rejects_oversized_body(tmp_path):
    srv = make_server(backend=LocalCacheBackend(tmp_path / "s"), port=0, max_value_bytes=10)
    running = _RunningServer(srv)
    try:
        status, _ = _request(running.base, VALID_KEY, method="PUT", data=b"x" * 11)
        assert status == 413
        # 10 bytes is accepted (validation off so junk-but-small is fine)
        srv2 = make_server(
            backend=LocalCacheBackend(tmp_path / "s2"),
            port=0,
            max_value_bytes=10,
            validate_payload=False,
        )
        running2 = _RunningServer(srv2)
        try:
            status, _ = _request(running2.base, VALID_KEY, method="PUT", data=b"x" * 10)
            assert status == 201
        finally:
            running2.close()
    finally:
        running.close()


# -- payload validation -----------------------------------------------------
def test_validate_payload_rejects_non_cache_entry(server):
    status, _ = _request(server.base, VALID_KEY, method="PUT", data=b"not json at all")
    assert status == 422
    status, _ = _request(
        server.base, VALID_KEY, method="PUT", data=json.dumps({"nope": 1}).encode()
    )
    assert status == 422
    # a well-formed entry is accepted
    status, _ = _request(server.base, VALID_KEY, method="PUT", data=ENTRY)
    assert status == 201


def test_validate_payload_disabled_accepts_junk(tmp_path):
    srv = make_server(
        backend=LocalCacheBackend(tmp_path / "s"), port=0, validate_payload=False
    )
    running = _RunningServer(srv)
    try:
        status, _ = _request(running.base, VALID_KEY, method="PUT", data=b"junk bytes")
        assert status == 201
        status, body = _request(running.base, VALID_KEY, method="GET")
        assert status == 200
        assert body == b"junk bytes"
    finally:
        running.close()


# -- auth -------------------------------------------------------------------
def test_write_token_gates_writes_but_read_is_public(tmp_path):
    srv = make_server(
        backend=LocalCacheBackend(tmp_path / "s"), port=0, write_token="w"
    )
    running = _RunningServer(srv)
    try:
        # PUT with no Bearer -> 401
        status, _ = _request(running.base, VALID_KEY, method="PUT", data=ENTRY)
        assert status == 401
        # PUT with the right Bearer -> ok
        status, _ = _request(
            running.base,
            VALID_KEY,
            method="PUT",
            data=ENTRY,
            headers={"Authorization": "Bearer w"},
        )
        assert status == 201
        # GET with no token -> still public
        status, _ = _request(running.base, VALID_KEY, method="GET")
        assert status == 200
    finally:
        running.close()


def test_read_token_gates_reads(tmp_path):
    srv = make_server(backend=LocalCacheBackend(tmp_path / "s"), port=0, read_token="r")
    running = _RunningServer(srv)
    try:
        status, _ = _request(running.base, VALID_KEY, method="GET")
        assert status == 401
        status, _ = _request(
            running.base,
            VALID_KEY,
            method="GET",
            headers={"Authorization": "Bearer r"},
        )
        assert status == 404  # authorized, just a miss
        # health needs no token even when read is gated
        status, _ = _request(running.base, "health", method="GET")
        assert status == 200
    finally:
        running.close()


# -- the never-crash 500 path -----------------------------------------------
class _BoomBackend(LocalCacheBackend):
    def get(self, key):
        raise RuntimeError("backend exploded")


def test_backend_error_is_500_and_server_stays_alive(tmp_path):
    srv = make_server(backend=_BoomBackend(tmp_path / "s"), port=0)
    running = _RunningServer(srv)
    try:
        status, body = _request(running.base, VALID_KEY, method="GET")
        assert status == 500
        assert json.loads(body) == {"error": "internal"}
        # the server survived: a second request still works
        status, _ = _request(running.base, "health", method="GET")
        assert status == 200
    finally:
        running.close()
