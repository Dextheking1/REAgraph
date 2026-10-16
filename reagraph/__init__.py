"""REAgraph: a persistent, queryable knowledge graph for reverse engineering.

The two pieces that work today:
  - `memory`: fold findings into a durable graph with dedup + confidence upgrade.
  - `diff`:   structural, byte-agnostic diff of two graphs.
"""

from . import memory, diff, ingest

__all__ = ["memory", "diff", "ingest"]
__version__ = "0.0.1"
