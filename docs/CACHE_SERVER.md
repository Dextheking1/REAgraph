# REAgraph shared cache server

Status: new. The server (`reagraph.cache_server`) is a thin stdlib HTTP layer over
the same `CacheBackend` the local cache uses. It answers the wire contract that the
shipped client (`reagraph.remote_cache.RemoteCacheBackend`) already speaks, so a
team or community can SHARE analysis results instead of each machine re-running the
same expensive analysis. There is no hosted instance; you run your own.

Read [docs/CACHE.md](CACHE.md) first for what the cache is, how keys are derived,
and the privacy constraints a shared tier must respect. This document is the
operator and client side: how to run the server, how to point a client at it, and
the trust model that makes sharing safe.

## What the server is (and is not)

The expensive thing in REAgraph is analysis: running a REA tool over an artifact to
produce a graph fragment. `reagraph.cache` content-addresses that result so it is
paid for once per machine. `RemoteCacheBackend` extends that one tier up so the
result is paid for once per *group*: the first person to analyze an artifact writes
the entry, everyone after fetches it.

The server is the store that answers those fetches. It is a dumb, opaque byte store
keyed by cache keys:

- It stores validated blobs and serves them back byte-for-byte.
- It NEVER executes a stored blob. It has no idea what a fragment means.
- It is an optimization, never a source of truth. Every entry is reproducible by
  re-running analysis, so the store is regenerable, not precious.

## Run it

```bash
python -m reagraph.cache_server --dir /var/lib/reagraph-cache --port 8787
```

Flags:

- `--dir` — storage directory (or `REAGRAPH_CACHE_SERVER_DIR`). Required; this is
  where entries live.
- `--port` — listen port (default 8787).
- `--host` — bind address.
- `--max-value-mb` — reject request bodies larger than this (returns 413).
- `--no-validate-payload` — disable payload-shape validation (see below). On by
  default; leave it on for a shared store.

Tokens are read from the ENVIRONMENT ONLY, never from CLI flags (flags are visible
to any user via `ps` and would leak the secret):

- `REAGRAPH_CACHE_READ_TOKEN` — if set, every GET requires
  `Authorization: Bearer <token>`. If unset, reads are PUBLIC.
- `REAGRAPH_CACHE_WRITE_TOKEN` — if set, every PUT and DELETE requires
  `Authorization: Bearer <token>`. If unset, writes are OPEN.

An open-write public server is a bad idea: anyone who can reach it can fill your
disk and push arbitrary (if validated-shape) blobs. Always set a write token. A
read token is optional and depends on whether even key enumeration is sensitive to
you (see Trust and privacy).

```bash
export REAGRAPH_CACHE_WRITE_TOKEN="$(openssl rand -hex 32)"
# optional, to gate reads too:
export REAGRAPH_CACHE_READ_TOKEN="$(openssl rand -hex 32)"
python -m reagraph.cache_server --dir /var/lib/reagraph-cache --port 8787
```

## Wire contract

This is exactly what `RemoteCacheBackend`'s `UrllibTransport` expects.

| Method | Path | Success | Notes |
| --- | --- | --- | --- |
| GET | `/{key}` | 200 + raw bytes | 404 on a miss |
| PUT | `/{key}` | 201 | request body is the raw bytes |
| DELETE | `/{key}` | 204 | |
| GET | `/keys` | 200 + `{"keys": [...]}` | for operators |
| GET | `/health` | 200 + ok | liveness check |

Rules the server enforces:

- A key MUST be sha256 hex, matching `^[0-9a-f]{64}$`. Anything else returns 400.
  These are the cache keys from `Cache.key_for(...)`; the server never derives them.
- A body over `--max-value-mb` returns 413.
- With payload validation on (the default), a body that is not a REAgraph cache
  entry (a JSON dict containing a `"fragment"` key, the shape written by
  `Cache.put`) returns 422. This keeps a public store from becoming a dumping
  ground for arbitrary bytes.

The client is fail-open: any non-success, timeout, or network error is treated as a
plain miss (`get` returns `None`) or a no-op (`put`/`delete`). A down or slow server
never breaks a local build, it just falls through to local analysis.

## Point a client at it

### Read-only consumer (the safe default)

Pull from the shared store, upload nothing. This is the recommended default for
almost everyone: you benefit from others' analyses without any risk of leaking your
own. `token` is `None` if the server's reads are public, otherwise the read token.

```python
from reagraph.cache import Cache
from reagraph.remote_cache import RemoteCacheBackend

cache = Cache(backend=RemoteCacheBackend(
    "https://cache.example.com",
    token=READ_TOKEN_or_None,
))
```

`RemoteCacheBackend` is `read_only=True` by default, so `put`/`delete` are logged
no-ops even if you call them.

### Contributor (opt in deliberately)

To upload entries you must both turn off read-only AND satisfy the `allow_push`
guard. This two-step opt-in is what stops analysis of a private artifact from being
uploaded by accident.

```python
# Contribute everything (use only when every artifact you analyze is public):
RemoteCacheBackend(
    url,
    token=WRITE_TOKEN,
    read_only=False,
    allow_push=True,
)
```

For per-entry control, pass a predicate `(key, value) -> bool`. It is evaluated for
each entry before upload and fails closed: if it raises, the entry is NOT pushed.

```python
# Upload only artifacts you have explicitly marked public:
def is_public(key: str, value: bytes) -> bool:
    return key in MY_PUBLIC_KEYS   # your own allowlist

RemoteCacheBackend(
    url,
    token=WRITE_TOKEN,
    read_only=False,
    allow_push=is_public,
)
```

### Local-first with shared fallback

Keep every pull offline-fast while still benefiting from the shared store. `get`
checks local first; on a local miss it falls through to the remote and, on a remote
hit, warms the local tier so the next pull stays offline.

```python
from reagraph.cache import Cache, LocalCacheBackend
from reagraph.remote_cache import RemoteCacheBackend, TieredBackend

cache = Cache(backend=TieredBackend(
    LocalCacheBackend(),
    RemoteCacheBackend(url),   # read-only by default: nothing is ever pushed
))
```

Writes and deletes go to both tiers, but the remote enforces its own
read-only/`allow_push` posture, so with a default `RemoteCacheBackend` nothing
leaves the machine.

## Trust and privacy

This is the product's posture, not a footnote.

- What is shared is a DERIVED graph fragment keyed by the artifact's CONTENT HASH.
  The artifact's bytes are never uploaded, and neither is source. But a fragment
  can still reveal the structure of a private program, which is why the client's
  default is read-only / push-nothing and the server should token-gate writes.
- Keying is content-addressed. Anyone who already holds the same artifact can
  compute the same key and fetch its cached analysis. That is the point for public,
  popular artifacts: the first analysis pays, everyone else gets it instantly.
  Someone who does NOT hold the artifact cannot enumerate meaningful keys, because
  keys are sha256 digests.
- `/keys` exists for operators, but it hands out the full key set to anyone who can
  call it. If even key enumeration is sensitive to you, set
  `REAGRAPH_CACHE_READ_TOKEN` so listing and reads require a token.
- The server stores opaque, shape-validated blobs and never executes them. The size
  cap plus payload-shape validation are what keep a public store from becoming a
  dumping ground.

The combined guarantee: the compounding benefit of a shared cache for public
artifacts, with no path for a private analysis to leave a machine by accident.

## Deploy

- Run behind TLS. The stdlib server speaks plain HTTP; terminate TLS at a reverse
  proxy (nginx, Caddy, a cloud load balancer) in front of it.
- Set the write token as a secret through the environment, never on the command
  line. Set a read token too if key enumeration is sensitive.
- Persist `--dir` on a durable volume so entries survive restarts.
- Back up the dir or treat it as regenerable: every entry is reproducible by
  re-running analysis, so the store is a cache, never a source of truth. Losing it
  costs re-analysis time, nothing more.

A reference `Dockerfile` and fly.io config are in [`deploy/`](../deploy/).

See also [docs/CACHE.md](CACHE.md) for the cache internals, key derivation, and the
local backend this server is built on.
