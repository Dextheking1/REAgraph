"""REAgraph: a persistent, queryable knowledge graph for reverse engineering.

The two pieces that work today:
  - `memory`: fold findings into a durable graph with dedup + confidence upgrade.
  - `diff`:   structural, byte-agnostic diff of two graphs.
"""

from . import (
    memory, diff, compare, ingest, summarize, coverage, bridge, serve,
    rea_adapter, rea_client, artifact, cache, cached,
    fingerprint, align, attest, remote_cache, cache_server, doctor,
)

__all__ = [
    "memory", "diff", "compare", "ingest", "summarize", "coverage",
    "bridge", "serve", "rea_adapter", "rea_client",
    "artifact", "cache", "cached", "fingerprint", "align", "attest",
    "remote_cache", "cache_server", "doctor",
]
__version__ = "0.0.1"
