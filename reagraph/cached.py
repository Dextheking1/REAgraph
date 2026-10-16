"""Wrap any analysis with the cache so `pull` never re-runs work it has done.

This is the one small primitive that turns a repeated, expensive REA tool call
into a cheap lookup: given an artifact + tool + args, we first ask the cache for
a stored fragment and, only on a miss, actually run the analysis and store the
result. The analysis is passed in as a plain callable (`analyze_fn`), so this
module depends on nothing from REA -- it is testable offline with a fake cache
and a fake analyzer, and reusable for any "compute once, remember" step.

The cache is `reagraph.cache` today, a *local* backend keyed by content. A
shared/remote backend (team-wide reuse) is a drop-in: it only has to keep the
same `key_for` / `get` / `put` surface, so nothing here changes.
"""

from __future__ import annotations

import sys

try:
    from . import cache as _cache  # noqa: F401  (imported for availability/parity)
except ImportError:  # pragma: no cover - tiny stub so this module imports standalone
    class _cache:  # type: ignore
        """Placeholder so importing `reagraph.cached` never fails before the
        sibling `reagraph.cache` lands. Callers always pass a real (or fake)
        cache object into `cache_analysis`, so this stub is never exercised."""


def cache_analysis(
    cache,
    artifact_path,
    tool,
    args,
    analyze_fn,
    *,
    extractor_version: str = "",
    refresh: bool = False,
) -> "tuple[dict, bool]":
    """Return `(fragment, was_cache_hit)` for one analysis, running it at most once.

    On a cache hit we return the stored fragment WITHOUT calling `analyze_fn`
    (the whole point: no REA call when we already know the answer). On a miss --
    or when `refresh=True` forces a recompute -- we call `analyze_fn()` (where the
    real REA tool call happens), store the fragment, and return it.

    Fail-open: if the cache raises on `get`/`put`, we log to stderr and behave as
    if it were a miss, so a broken cache degrades to plain analysis rather than
    breaking it.
    """
    key = cache.key_for(artifact_path, tool, args, extractor_version=extractor_version)

    if not refresh:
        hit = _safe_get(cache, key)
        if hit is not None:
            return hit["fragment"], True

    fragment = analyze_fn()
    _safe_put(cache, key, fragment, meta={"artifact": artifact_path, "tool": tool})
    return fragment, False


def _safe_get(cache, key):
    """cache.get(key) but never raise: a broken cache is treated as a miss."""
    try:
        return cache.get(key)
    except Exception as exc:  # fail-open: a broken cache must not break analysis
        print(f"reagraph.cached: cache get failed ({exc!r}); treating as miss", file=sys.stderr)
        return None


def _safe_put(cache, key, fragment, meta):
    """cache.put(...) but never raise: failing to remember must not fail the run."""
    try:
        cache.put(key, fragment, meta=meta)
    except Exception as exc:  # fail-open: storing is best-effort
        print(f"reagraph.cached: cache put failed ({exc!r}); result not stored", file=sys.stderr)
