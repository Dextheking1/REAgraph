"""Live-ingest bridge: fold REA findings into the persistent graph AS they arrive.

This is the "help the agent *during* RE" loop. Instead of running a full
reverse-engineering pass and only *then* building a graph, a source streams
findings one at a time and each one is merged into the on-disk graph
immediately — and the graph is SAVED after every merge. If the run crashes on
finding N, findings 1..N-1 are already durably on disk. That crash-safety is the
core selling point over REA, whose Swift-symbol ZodError on one bad node discards
*all* partial findings.

The source is kept abstract (`ReaSource`) so we are not coupled to REA today: a
real REA-MCP client just needs to yield JAG / node-link dicts from `findings()`.

Fail-open is the other half of the pitch: a single malformed finding is logged to
stderr and skipped, never aborting the stream.
"""

from __future__ import annotations

import sys
from typing import Callable, Iterable, Protocol, runtime_checkable

from . import ingest, memory


@runtime_checkable
class ReaSource(Protocol):
    """Anything that can yield reverse-engineering findings as dicts.

    A finding is a JAG doc (nodes/edges with `epistemic`) or an already
    node-link doc (nodes + links/edges). A real REA-MCP client implements this
    later; keeping it a Protocol means we never import or depend on REA here.
    """

    def findings(self) -> Iterable[dict]:
        ...


def _normalize(doc: dict) -> dict:
    """ingest.load_any, but for a dict already in hand (no file on disk).

    Mirrors ingest.load_any's auto-detection: node-link passes through, JAG docs
    are mapped via ingest_jag, plain {nodes, edges} is rewritten to node-link.
    Raises on anything that is not a usable finding so the caller can skip it.
    """
    if not isinstance(doc, dict):
        raise ValueError(f"finding is not a dict: {type(doc).__name__}")
    # Reject a finding carrying none of the graph keys (the bridge is stricter
    # than the file loaders: a malformed finding must be skippable). Anything with
    # at least one of these routes through the shared autodetect below.
    if "links" not in doc and "nodes" not in doc and "edges" not in doc:
        raise ValueError("finding has no nodes/links/edges")
    return ingest.normalize_doc(doc)


def _is_empty(fragment: dict) -> bool:
    return not fragment.get("nodes") and not fragment.get("links")


def ingest_stream(
    source: "ReaSource | Iterable[dict]",
    graph_path="reagraph.json",
    on_merge: Callable[[dict], None] | None = None,
) -> dict:
    """Stream findings from `source` into the graph at `graph_path`.

    For each finding: normalize it, merge it into the on-disk graph, and SAVE —
    so a crash never loses already-merged progress. `on_merge(stats)` is called
    after each successful save. A malformed or empty finding is logged to stderr
    and skipped, never aborting the stream. Returns the final stats dict.

    `source` may be a ReaSource (its `findings()` is iterated) or any iterable of
    dicts directly.
    """
    items = source.findings() if isinstance(source, ReaSource) else source

    graph = memory.load(graph_path)
    # Make sure the file exists from the start so crash-safety holds even if the
    # very first finding is the one that crashes the process.
    memory.save(graph, graph_path)

    for i, doc in enumerate(items):
        try:
            fragment = _normalize(doc)
            if _is_empty(fragment):
                raise ValueError("finding normalized to an empty fragment")
        except Exception as exc:  # fail-open: one bad finding must not abort the run
            print(f"reagraph.bridge: skipped finding {i}: {exc}", file=sys.stderr)
            continue
        memory.merge(graph, fragment)
        memory.save(graph, graph_path)
        if on_merge is not None:
            on_merge(memory.stats(graph))

    return memory.stats(graph)


def ingest_many(docs: Iterable[dict], graph_path="reagraph.json", on_merge=None) -> dict:
    """Convenience: stream a plain list/iterable of finding dicts into the graph."""
    return ingest_stream(docs, graph_path=graph_path, on_merge=on_merge)


class FileReaSource:
    """A ReaSource that reads JAG / node-link findings from files on disk.

    Useful for testing and batch use: point it at a list of paths and it yields
    one finding dict per file via ingest.load_any. A file that fails to load is
    skipped (logged to stderr) so one bad file does not abort the batch.
    """

    def __init__(self, paths: Iterable):
        self.paths = list(paths)

    def findings(self) -> Iterable[dict]:
        for path in self.paths:
            try:
                yield ingest.load_any(path)
            except Exception as exc:
                print(f"reagraph.bridge: skipped file {path}: {exc}", file=sys.stderr)
                continue
