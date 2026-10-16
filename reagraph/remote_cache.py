"""Shared/remote cache tier: serve a repeat analysis from a store others can reach.

The expensive thing in REAgraph is *analysis*. `reagraph.cache` already makes a
local build pay for each (artifact, scope) only once. This module extends that one
tier up: a `RemoteCacheBackend` that reads byte-for-byte cache entries from a
*shared* store, so a repeat analysis of the same artifact -- already run by someone
else, on another machine -- is served without re-running the tool at all.

It is a drop-in `reagraph.cache.CacheBackend`: same tiny byte-store contract
(`get`/`put`/`keys`/`delete`/`__len__`), so `Cache(backend=RemoteCacheBackend(...))`
just works, as does composing it under `TieredBackend` below.

PRIVACY IS THE POINT, NOT A FOOTNOTE
------------------------------------
A shared cache is a place private analysis could leak. The entire posture here is
*contribute nothing unless explicitly and deliberately enabled*:

  * ``read_only=True`` is the DEFAULT. A read-only backend GETs from the shared
    store and NEVER writes to it: you benefit from others' public analyses while
    pushing nothing. ``put``/``delete`` are logged no-ops.
  * Uploading is opt-in (``read_only=False``) AND still gated by an ``allow_push``
    predicate/flag (default ``False``). Even after you flip off read-only, a
    ``put`` is refused unless ``allow_push`` is truthy -- or, when it is a
    callable ``(key, value) -> bool``, unless it returns True for *that* entry.
    This is the guard that stops analysis of a private artifact from ever being
    uploaded by accident. The safe default is: push nothing.

FAIL-OPEN, ALWAYS
-----------------
A shared cache lives across a network; it can be down, slow, or wrong. None of
that may break a local build. Every network op is wrapped: any error, timeout, or
non-200 is caught, logged to stderr, and treated as a plain miss (``get`` -> None)
or a no-op (``put``/``delete``). A short timeout bounds the wait. The worst a
broken shared tier can do is make you fall through to local analysis -- exactly
what you'd do without it.

TRANSPORT SEAM
--------------
All network I/O is isolated behind a minimal transport (``get``/``put``/``delete``
/``list`` over a ``base_url``). The default is ``UrllibTransport`` (stdlib urllib,
Bearer token, short timeout). Tests inject a dict-backed fake so no real network is
touched. The hosted server that answers these requests is OUT OF SCOPE here; see
the server contract documented on ``UrllibTransport`` -- building it is the next
step.

Pure stdlib.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Optional, Union

# Default network wait. A shared cache is an optimisation, never a dependency, so
# we give up fast and fall through to local rather than stall a build.
DEFAULT_TIMEOUT = 3.0

# allow_push may be a plain flag or a per-entry predicate (key, value) -> bool.
PushGuard = Union[bool, Callable[[str, bytes], bool]]


def _log(msg: str) -> None:
    """Fail-open diagnostics: never raise, just note it on stderr."""
    print(f"[reagraph.remote_cache] {msg}", file=sys.stderr)


class UrllibTransport:
    """The default transport: plain stdlib urllib over an HTTP(S) ``base_url``.

    Server contract (the hosted side is out of scope -- this is what it must speak,
    and the next step is to build it):

      * ``GET    {base_url}/{key}``   -> 200 + raw bytes, or 404 for a miss.
      * ``PUT    {base_url}/{key}``   -> 2xx; request body is the raw bytes.
      * ``DELETE {base_url}/{key}``   -> 2xx (404 tolerated as already-gone).
      * ``GET    {base_url}/keys``    -> 200 + JSON: either ``["k1", ...]`` or
                                         ``{"keys": [...]}``.

    A ``token``, if given, is sent as ``Authorization: Bearer <token>``. This
    transport deliberately does only the wire work: a 404 on ``get`` is reported as
    a miss (``None``); every other failure is raised and left for the backend's
    fail-open layer to swallow.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _url(self, key: str) -> str:
        return f"{self.base_url}/{urllib.parse.quote(key, safe='')}"

    def _headers(self) -> dict:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _open(self, req: urllib.request.Request):
        return urllib.request.urlopen(req, timeout=self.timeout)

    def get(self, key: str) -> Optional[bytes]:
        req = urllib.request.Request(self._url(key), headers=self._headers(), method="GET")
        try:
            with self._open(req) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def put(self, key: str, value: bytes) -> None:
        headers = {**self._headers(), "Content-Type": "application/octet-stream"}
        req = urllib.request.Request(self._url(key), data=value, headers=headers, method="PUT")
        with self._open(req) as resp:
            resp.read()

    def delete(self, key: str) -> None:
        req = urllib.request.Request(self._url(key), headers=self._headers(), method="DELETE")
        try:
            with self._open(req) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            raise

    def list(self) -> Iterable[str]:
        req = urllib.request.Request(f"{self.base_url}/keys", headers=self._headers(), method="GET")
        with self._open(req) as resp:
            obj = json.loads(resp.read())
        if isinstance(obj, dict):
            obj = obj.get("keys", [])
        return [str(k) for k in obj]


class RemoteCacheBackend:
    """A `reagraph.cache.CacheBackend` backed by a shared/remote byte store.

    Drop-in for ``Cache(backend=...)``: ``get``/``put``/``keys``/``delete``/
    ``__len__`` over opaque string keys and ``bytes`` values, exactly like
    ``LocalCacheBackend``.

    PRIVACY POSTURE (read the module docstring in full): the default is
    ``read_only=True`` -- pull from the shared store, push NOTHING. Uploading is
    opt-in via ``read_only=False`` AND still gated by ``allow_push`` (default
    ``False``). ``allow_push`` may be a bool, or a predicate ``(key, value) ->
    bool`` evaluated per entry so analysis of a private artifact is never uploaded
    by accident. Safe default: contribute nothing.

    FAIL-OPEN: every op is wrapped; a network error / timeout / non-200 becomes a
    miss (``get`` -> ``None``) or a no-op (``put``/``delete``), logged to stderr. A
    down or slow shared cache never breaks a local pull.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        transport=None,
        read_only: bool = True,
        allow_push: PushGuard = False,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url
        self.read_only = read_only
        self.allow_push = allow_push
        self.transport = (
            transport
            if transport is not None
            else UrllibTransport(base_url, token=token, timeout=timeout)
        )

    # -- privacy guard ----------------------------------------------------
    def _push_allowed(self, key: str, value: bytes) -> bool:
        """Decide whether this specific entry may leave the machine.

        A callable guard is consulted per entry (fail-closed: if the predicate
        itself raises, we refuse). A non-callable is a plain truthiness flag.
        """
        guard = self.allow_push
        if callable(guard):
            try:
                return bool(guard(key, value))
            except Exception as exc:  # fail CLOSED: never push on a broken guard
                _log(f"allow_push predicate raised for {key}; refusing push: {exc}")
                return False
        return bool(guard)

    # -- CacheBackend -----------------------------------------------------
    def get(self, key: str) -> Optional[bytes]:
        try:
            return self.transport.get(key)
        except Exception as exc:  # fail-open: any failure is just a miss
            _log(f"remote get failed for {key}: {exc}")
            return None

    def put(self, key: str, value: bytes) -> None:
        if self.read_only:
            _log(f"read-only: not uploading {key} (set read_only=False to contribute)")
            return
        if not self._push_allowed(key, value):
            _log(f"privacy guard: upload of {key} refused (allow_push not satisfied)")
            return
        try:
            self.transport.put(key, value)
        except Exception as exc:  # fail-open: a failed upload is a no-op
            _log(f"remote put failed for {key}: {exc}")

    def keys(self) -> Iterable[str]:
        try:
            return list(self.transport.list())
        except Exception as exc:  # fail-open: cannot list -> behave as empty
            _log(f"remote keys() failed: {exc}")
            return []

    def delete(self, key: str) -> None:
        # delete mutates the shared store, so it obeys the same read-only gate.
        if self.read_only:
            _log(f"read-only: not deleting {key} from the shared store")
            return
        try:
            self.transport.delete(key)
        except Exception as exc:  # fail-open
            _log(f"remote delete failed for {key}: {exc}")

    def __len__(self) -> int:
        return sum(1 for _ in self.keys())


class TieredBackend:
    """Local-first, remote-second byte store that warms local on a remote hit.

    A `reagraph.cache.CacheBackend` composed of two others. ``get`` checks
    ``local`` first; on a local miss it falls through to ``remote`` and, on a
    remote hit, populates ``local`` so the next pull stays offline. Writes and
    deletes go to both -- but ``remote`` enforces its OWN read-only / privacy
    posture, so with a default ``RemoteCacheBackend`` nothing is ever pushed.

    Everything is fail-open: it only ever composes two fail-open backends.
    """

    def __init__(self, local, remote):
        self.local = local
        self.remote = remote

    def get(self, key: str) -> Optional[bytes]:
        value = self.local.get(key)
        if value is not None:
            return value
        value = self.remote.get(key)
        if value is not None:
            try:
                self.local.put(key, value)  # warm the local tier on a remote hit
            except Exception as exc:  # fail-open: a failed warm is harmless
                _log(f"failed to populate local from remote for {key}: {exc}")
        return value

    def put(self, key: str, value: bytes) -> None:
        self.local.put(key, value)
        self.remote.put(key, value)  # remote honours its own read_only/allow_push

    def keys(self) -> Iterable[str]:
        seen = set()
        for source in (self.local, self.remote):
            for key in source.keys():
                if key not in seen:
                    seen.add(key)
                    yield key

    def delete(self, key: str) -> None:
        self.local.delete(key)
        self.remote.delete(key)

    def __len__(self) -> int:
        return sum(1 for _ in self.keys())


def tiered(local, remote) -> TieredBackend:
    """Convenience: ``Cache(backend=tiered(LocalCacheBackend(), RemoteCacheBackend(...)))``."""
    return TieredBackend(local, remote)
