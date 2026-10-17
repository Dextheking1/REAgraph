"""Hosted shared-cache server: the other side of ``reagraph.remote_cache``.

``reagraph.cache`` makes a local build pay for each (artifact, scope) analysis
only once. ``reagraph.remote_cache`` lifts that one tier up with a
``RemoteCacheBackend`` that pulls byte-for-byte cache entries from a *shared*
store so a repeat analysis -- already run by someone else, on another machine --
is served without re-running the tool. This module is that shared store: a tiny
stdlib HTTP server speaking exactly the wire contract documented on
``reagraph.remote_cache.UrllibTransport``.

THE WIRE CONTRACT (must match the client byte-for-byte)
-------------------------------------------------------
  * ``GET    {base}/{key}``  -> 200 + raw bytes (application/octet-stream), 404 miss.
  * ``PUT    {base}/{key}``  -> 2xx; request body is the raw bytes to store.
  * ``DELETE {base}/{key}``  -> 2xx (404 tolerated by the client as already-gone).
  * ``GET    {base}/keys``   -> 200 + JSON ``{"keys": [...]}``.
  * Auth, when configured, is ``Authorization: Bearer <token>``.

SAFETY POSTURE
--------------
  * A storage key MUST be a sha256 hex digest (``^[0-9a-f]{64}$``) -- exactly what
    ``Cache.key_for`` emits. Anything else is a 400. This is also the whole
    path-traversal defence: the server NEVER builds a filesystem path from the
    request; it only ever hands a validated 64-hex key to the backend, which
    shards it safely. No file is ever opened by raw request path.
  * Tokens gate reads and writes independently (constant-time compared). A split
    posture -- public read, token-gated write -- is the common hosted shape.
  * Every handler wraps its work: an unexpected error is a 500, logged to stderr;
    it never crashes the serving thread.

Pure stdlib.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cache import LocalCacheBackend

# A storage key is a sha256 hex digest -- precisely what Cache.key_for() emits.
# Validating against this is both correctness (reject junk) and the path-traversal
# defence (we never touch the filesystem with an unvalidated request segment).
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

# Default upload cap: a cache fragment is JSON node-link data, not a blob store.
DEFAULT_MAX_VALUE_BYTES = 5 * 1024 * 1024

# Env var serve() reads for the on-disk store directory (falls back to the
# LocalCacheBackend default when unset).
ENV_DIR = "REAGRAPH_CACHE_SERVER_DIR"
# Tokens are sourced from the environment ONLY (never a CLI arg: argv leaks in ps).
ENV_READ_TOKEN = "REAGRAPH_CACHE_READ_TOKEN"
ENV_WRITE_TOKEN = "REAGRAPH_CACHE_WRITE_TOKEN"


def _log(msg: str) -> None:
    """Server-side diagnostics on stderr (stdout stays clean for piping)."""
    print(f"[reagraph.cache_server] {msg}", file=sys.stderr)


class _CacheHandler(BaseHTTPRequestHandler):
    """One request handler for the shared cache. Settings are injected as class
    attributes by :func:`make_server` (a per-server subclass), so the handler
    closes over its backend/tokens without any module-level global state."""

    # -- injected by make_server (defaults make the class usable standalone) --
    backend = None
    read_token = None
    write_token = None
    max_value_bytes = DEFAULT_MAX_VALUE_BYTES
    validate_payload = True

    # Keep the default per-request stderr spew out of the test output.
    def log_message(self, format, *args):  # noqa: A002 - signature is fixed by base
        return

    # -- small response helpers (always set Content-Length) -----------------
    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- auth ---------------------------------------------------------------
    def _authorized(self, required_token) -> bool:
        """True if this request may proceed for the given required token.

        A ``None`` token means that class of request is public. Otherwise the
        ``Authorization: Bearer <token>`` header must match, compared in constant
        time to avoid leaking the token through timing."""
        if required_token is None:
            return True
        presented = self.headers.get("Authorization", "")
        expected = f"Bearer {required_token}"
        return hmac.compare_digest(presented, expected)

    # -- request parsing ----------------------------------------------------
    def _route(self):
        """Return (kind, key). kind is 'health', 'keys', or 'key'."""
        path = urllib.parse.urlsplit(self.path).path
        segments = [s for s in path.split("/") if s]
        if not segments or (len(segments) == 1 and segments[0] == "health"):
            return "health", None
        if len(segments) == 1 and segments[0] == "keys":
            return "keys", None
        # Treat the last path segment as the (url-encoded) key. We validate it
        # before it ever reaches the backend; we never build a path from it.
        return "key", urllib.parse.unquote(segments[-1])

    def _read_body(self):
        """Read exactly Content-Length bytes, enforcing the size cap. Returns the
        body bytes, or None if the cap was exceeded (a 413 has been sent)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > self.max_value_bytes:
            self._send_json(413, {"error": "payload too large"})
            return None
        body = self.rfile.read(length) if length > 0 else b""
        # Guard the actual read too, in case Content-Length lied low.
        if len(body) > self.max_value_bytes:
            self._send_json(413, {"error": "payload too large"})
            return None
        return body

    # -- HTTP verbs ---------------------------------------------------------
    def do_GET(self):
        try:
            kind, key = self._route()
            if kind == "health":
                self._send_json(200, {"service": "reagraph-cache", "ok": True})
                return
            # Reads (keys + entries, but not health) honour the read token.
            if not self._authorized(self.read_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            if kind == "keys":
                self._send_json(200, {"keys": list(self.backend.keys())})
                return
            if not _KEY_RE.match(key):
                self._send_json(400, {"error": "bad key"})
                return
            value = self.backend.get(key)
            if value is None:
                self._send_json(404, {"error": "not found"})
                return
            self._send_bytes(200, value)
        except Exception as exc:  # never crash the serving thread
            self._fail(exc)

    def do_PUT(self):
        try:
            kind, key = self._route()
            if kind != "key":
                self._send_json(400, {"error": "bad key"})
                return
            if not self._authorized(self.write_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            if not _KEY_RE.match(key):
                self._send_json(400, {"error": "bad key"})
                return
            body = self._read_body()
            if body is None:
                return  # 413 already sent
            if self.validate_payload and not self._looks_like_cache_entry(body):
                self._send_json(422, {"error": "unexpected payload shape"})
                return
            self.backend.put(key, body)
            self._send_json(201, {"stored": key})
        except Exception as exc:
            self._fail(exc)

    def do_DELETE(self):
        try:
            kind, key = self._route()
            if kind != "key":
                self._send_json(400, {"error": "bad key"})
                return
            if not self._authorized(self.write_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            if not _KEY_RE.match(key):
                self._send_json(400, {"error": "bad key"})
                return
            self.backend.delete(key)  # idempotent; backend tolerates a miss
            self._send_empty(204)
        except Exception as exc:
            self._fail(exc)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _looks_like_cache_entry(body: bytes) -> bool:
        """A stored value must be the reagraph cache entry shape: JSON object with
        a "fragment" key (see reagraph.cache.Cache.put)."""
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return False
        return isinstance(payload, dict) and "fragment" in payload

    def _fail(self, exc: Exception) -> None:
        """Last-ditch 500 for an unexpected error: log, respond, stay alive."""
        _log(f"{self.command} {self.path} failed: {exc!r}")
        try:
            self._send_json(500, {"error": "internal"})
        except Exception:  # the connection is probably gone; swallow it
            pass


def make_server(
    backend=None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    read_token=None,
    write_token=None,
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
    validate_payload: bool = True,
) -> ThreadingHTTPServer:
    """Build (and bind) a threading HTTP cache server, ready to ``serve_forever``.

    ``port=0`` picks a free ephemeral port; read it back from
    ``server.server_address[1]``. ``backend`` defaults to a ``LocalCacheBackend``.
    The returned server is bound but NOT yet serving -- the caller runs it (on a
    thread in tests, or ``serve_forever`` in :func:`serve`)."""
    if backend is None:
        backend = LocalCacheBackend()

    # Per-server subclass so settings live on the class, not a module global.
    class Handler(_CacheHandler):
        pass

    Handler.backend = backend
    Handler.read_token = read_token
    Handler.write_token = write_token
    Handler.max_value_bytes = max_value_bytes
    Handler.validate_payload = validate_payload

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    directory=None,
    read_token=None,
    write_token=None,
    max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
    validate_payload: bool = True,
) -> None:
    """Build a LocalCacheBackend-backed server and serve_forever().

    The store directory comes from ``directory``, else the ``REAGRAPH_CACHE_SERVER_DIR``
    env var, else the LocalCacheBackend default. Prints a one-line startup banner
    to stderr describing the posture."""
    if directory is None:
        directory = os.environ.get(ENV_DIR)
    backend = LocalCacheBackend(directory)
    server = make_server(
        backend,
        host=host,
        port=port,
        read_token=read_token,
        write_token=write_token,
        max_value_bytes=max_value_bytes,
        validate_payload=validate_payload,
    )
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    read_posture = "read token required" if read_token else "public read"
    write_posture = "writes: token required" if write_token else "writes: open"
    _log(
        f"serving on http://{bound_host}:{bound_port}  "
        f"dir={getattr(backend, 'dir', '?')}  {read_posture}  {write_posture}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    """CLI entry point. Tokens are sourced from the environment ONLY, never from a
    flag: a token on argv would leak to anyone who can read the process list
    (``ps``). Set ``REAGRAPH_CACHE_READ_TOKEN`` / ``REAGRAPH_CACHE_WRITE_TOKEN``."""
    parser = argparse.ArgumentParser(
        prog="python -m reagraph.cache_server",
        description="Hosted shared cache for REAgraph analysis fragments.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    parser.add_argument(
        "--dir",
        default=None,
        help=f"store directory (default ${ENV_DIR} or the LocalCacheBackend default)",
    )
    parser.add_argument(
        "--max-value-mb",
        type=float,
        default=5.0,
        help="reject uploads larger than this many MiB (default 5)",
    )
    parser.add_argument(
        "--no-validate-payload",
        action="store_true",
        help="accept any bytes (skip the reagraph cache-entry shape check)",
    )
    args = parser.parse_args(argv)

    # Tokens: environment only (never a CLI arg -- argv leaks via `ps`).
    read_token = os.environ.get(ENV_READ_TOKEN) or None
    write_token = os.environ.get(ENV_WRITE_TOKEN) or None

    serve(
        host=args.host,
        port=args.port,
        directory=args.dir,
        read_token=read_token,
        write_token=write_token,
        max_value_bytes=int(args.max_value_mb * 1024 * 1024),
        validate_payload=not args.no_validate_payload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
