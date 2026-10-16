# REAgraph analysis cache

Status: the LOCAL cache ships. Content addressing (`reagraph.artifact`), the
content-addressed store (`reagraph.cache`), the cache-aware wrapper
(`reagraph.cached`), and cache-aware `reagraph pull` (default on; `--no-cache`,
`--refresh`) are in the tree and tested: a repeat analysis of the same artifact
is served with no REA call. The SHARED tier has a shipped CLIENT backend
(`reagraph.remote_cache`, read-only and privacy-guarded by default); the hosted
SERVER it talks to is the next step, not yet built. Both are described below with
the privacy constraints the shared tier must respect.

## Why a cache at all

Reverse engineering is slow and expensive. A single `reagraph pull` spawns REA,
decompiles, and streams findings back; that costs wall-clock time and tokens, and
today it costs the same every time even when nothing about the target has changed.

The cache removes that repeated cost. A result is keyed by the content hash of the
artifact being analyzed plus the exact analysis scope that produced it. The same
artifact analyzed the same way is therefore never recomputed. On a cache hit,
`pull` returns the stored fragment immediately, with **no REA call** at all.

This is the compounding asset the project is built around. Every analysis that
runs writes a result that makes the next identical lookup instant. The store does
not just remember facts; it amortizes the cost of discovering them. The more
artifacts that pass through, the more future lookups land as hits. That is the
moat: value accumulates on disk instead of evaporating with each session.

## How it works: content addressing

The cache turns "have we already analyzed this?" into a pure hash comparison, so
it never depends on filenames, paths, timestamps, or where bytes happen to sit.

### Artifact identity (`reagraph.artifact`)

An artifact's identity is derived from its content, not its location:

- For a file: a streaming SHA-256 of its bytes, read in chunks so a large binary
  never has to be held in memory.
- For a directory tree (an unpacked app, an Electron bundle, a folder of modules):
  a deterministic digest of the tree. The digest walks the tree in a fixed,
  sorted order and folds in each file's relative path and content hash, so the
  same tree always produces the same `artifact_id` regardless of walk order or
  the absolute path it lives under.

The result is a stable `artifact_id` that identifies *what* was analyzed by its
content alone. Two copies of the same binary in two different directories hash to
the same identity.

### Cache key (identity + scope)

Identity alone is not enough, because the same artifact can be analyzed in
different ways and upgraded tools produce different results. The cache key folds
the analysis scope into the identity:

```
cache_key = hash(artifact_id + tool + canonical(args) + extractor_version)
```

- `artifact_id` — the content identity above.
- `tool` — which analysis was run.
- `canonical(args)` — the analysis arguments in a canonical form, so that two
  argument sets that mean the same thing produce the same key regardless of
  ordering or formatting.
- `extractor_version` — the version of the analysis that produced the result.

Two consequences fall out of this key, both intended:

- **A different analysis is a different entry.** Changing the tool or the args
  produces a different key, so distinct analyses of the same artifact never
  collide or overwrite each other.
- **A tool upgrade invalidates cleanly.** Bumping `extractor_version` changes every
  key it touches, so stale results from an older extractor are simply never looked
  up again. There is no cache to manually purge; the old entries become
  unreachable and the new version recomputes once, then caches.

### Storage (`reagraph.cache`)

Results are kept in a local content-addressed store behind a `CacheBackend`
interface. The default implementation is `LocalCacheBackend`, which writes entries
under `$REAGRAPH_CACHE` when that environment variable is set, otherwise under
`~/.cache/reagraph`.

The store is **fail-open**. The cache is an optimization, never a source of truth,
so anything that goes wrong with it is non-fatal: a corrupt, unreadable, or
partially written entry is ignored and treated as a miss, and analysis proceeds as
if the cache were empty. A broken cache slows you down to the uncached baseline; it
never breaks a `pull` and never returns a wrong answer.

### Consulting the cache (`reagraph.cached`)

`pull` consults the cache through a `cache_analysis()` wrapper in
`reagraph.cached`. The wrapper computes the artifact identity and cache key, checks
the backend for a stored fragment, returns it on a hit, and on a miss runs the real
REA analysis, stores the resulting fragment under the key, and returns it. The
cache sits in front of the existing REA path; the fold-into-the-graph behavior
downstream is unchanged.

## Local now, shared later

The backend is pluggable behind the `CacheBackend` interface so the store can move
off the local machine. Two backends ship: `cache.LocalCacheBackend` (default) and
`remote_cache.RemoteCacheBackend` — the client for a shared tier.

`RemoteCacheBackend` is **read-only by default**: it GETs from a shared store so
you benefit from others' public analyses, and uploads nothing. Contributing is
opt-in (`read_only=False`) and still gated by an `allow_push` flag/predicate that
fails closed, so analysis of a private artifact is never uploaded by accident. It
is fail-open: a down or slow shared store falls through to local, never raising.
`remote_cache.TieredBackend(local, remote)` checks local first, then remote, and
warms local on a remote hit. Point `pull` at it with
`Cache(backend=remote_cache.RemoteCacheBackend(base_url, ...))`.

What is NOT shipped: the hosted **server** that answers these requests. Its wire
contract is documented on `RemoteCacheBackend`'s transport (a content-addressed
byte store keyed by the same cache keys, with auth and a public/private visibility
model backing the `allow_push` contribution flow). Building that server, with the
privacy constraints below, is the next step.

## Privacy and safety for a future shared cache

Keying by artifact hash is what makes sharing powerful, and it is also what makes
sharing dangerous if done carelessly. Because the key is derived purely from
content, two people analyzing the *same* artifact get the same key and would share
the same cached result. For public and popular artifacts this is exactly what you
want: the first person to analyze a widely used library pays the cost, and everyone
after gets it instantly.

But the same mechanism means a shared backend could expose analysis of a private or
proprietary binary to anyone who happens to hold the same bytes, and uploading that
analysis at all may leak the fact that a given artifact was analyzed. A shared tier
must therefore treat this as a hard design constraint:

- **Opt-in only.** A shared backend must never upload analysis of a user's
  artifacts without explicit consent. Local-only must remain the default, and the
  local cache must stay fully usable with no sharing enabled.
- **Private stays local.** Analysis of private or proprietary artifacts stays on
  the local backend by default and is never published implicitly as a side effect
  of running `pull`.
- **Share deliberately.** Sharing should be limited to artifacts the user has
  explicitly marked public or known (for example, well-known public releases), or
  pushed by an explicit action. Publishing is a deliberate step, not the default
  path.

The goal is to get the compounding benefit for public artifacts without ever making
a private analysis leave the machine by accident.

## Lookup flow

```
  pull target
      |
      v
  artifact_id = sha256(file bytes)            # or deterministic digest of a dir tree
      |
      v
  cache_key = hash(artifact_id + tool + canonical(args) + extractor_version)
      |
      v
  look up cache_key in backend (LocalCacheBackend by default)
      |
      +-- HIT  --> return stored fragment immediately        (no REA call)
      |
      '-- MISS --> call REA, analyze, store fragment under cache_key, return it
                        |
                        v
                   (next identical lookup is a HIT)
```

A hit is a pure read: the stored fragment comes straight back and REA is never
invoked. A miss pays the full analysis cost once, writes the result, and turns every
later identical lookup into a hit. On a corrupt or unreadable entry the lookup
degrades to a miss rather than failing.
