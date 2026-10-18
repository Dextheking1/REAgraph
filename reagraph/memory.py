"""Persistent graph memory: fold findings in with dedup and confidence upgrade.

Graphs use graphify's node-link JSON shape so graphify can query them directly:

    {"directed": true, "multigraph": false,
     "nodes": [{"id": "...", "label": "...", "confidence": "EXTRACTED", ...}],
     "links": [{"source": "...", "target": "...", "relation": "calls",
                "confidence": "INFERRED", "context": "rea:invokes", ...}]}

The one rule that makes a *memory* out of a stream of findings: when the same
node or edge is seen again, we never duplicate it — we keep the union of its
attributes and UPGRADE its confidence (a later `observed`/EXTRACTED fact promotes
an earlier `inferred`/INFERRED one). Re-observing a thing makes the graph more
certain, not larger.
"""

from __future__ import annotations

import json
from pathlib import Path

# Higher wins. graphify's vocab: EXTRACTED (observed) > INFERRED (guessed).
_CONF_RANK = {"EXTRACTED": 2, "INFERRED": 1, "": 0}


def _rank(conf) -> int:
    return _CONF_RANK.get(conf or "", 0)


def empty_graph() -> dict:
    return {"directed": True, "multigraph": False, "nodes": [], "links": []}


def load(path) -> dict:
    p = Path(path)
    if not p.exists():
        return empty_graph()
    data = json.loads(p.read_text(encoding="utf-8"))
    # Tolerate the "edges" spelling some tools use.
    if "links" not in data and "edges" in data:
        data = {**data, "links": data["edges"]}
    data.setdefault("nodes", [])
    data.setdefault("links", [])
    return data


def save(graph: dict, path) -> None:
    Path(path).write_text(json.dumps(graph, indent=2, sort_keys=False), encoding="utf-8")


def edge_key(e: dict) -> tuple:
    """Identity of an edge = (source, target, relation). Deliberately NOT a byte
    offset or file location, so a thing that merely moved is still the same edge."""
    return (e.get("source"), e.get("target"), e.get("relation", ""))


def _merge_attrs(into: dict, new: dict) -> int:
    """Fill missing attrs; upgrade `confidence` to the stronger of the two.
    Returns 1 if the confidence was upgraded, else 0."""
    upgraded = 0
    if _rank(new.get("confidence")) > _rank(into.get("confidence")):
        into["confidence"] = new.get("confidence")
        # The stronger source's label/location win too.
        for k in ("label", "source_file", "source_location"):
            if new.get(k):
                into[k] = new[k]
        upgraded = 1
    for k, v in new.items():
        into.setdefault(k, v)
    return upgraded


def merge(base: dict, fragment: dict) -> dict:
    """Fold `fragment` into `base` (returns `base`, mutated). Dedups nodes by id
    and edges by (source,target,relation); upgrades confidence on re-observation.

    Fail-open: a non-dict `base`/`fragment` is treated as an empty graph, and
    non-dict entries inside nodes/links are skipped -- never raises."""
    if not isinstance(base, dict):
        base = empty_graph()
    if not isinstance(fragment, dict):
        fragment = empty_graph()

    base_nodes = base.get("nodes", [])
    if not isinstance(base_nodes, list):
        base_nodes = []
    nodes = {n["id"]: n for n in base_nodes if isinstance(n, dict) and "id" in n}

    frag_nodes = fragment.get("nodes", [])
    if not isinstance(frag_nodes, list):
        frag_nodes = []
    for n in frag_nodes:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if nid is None:
            continue
        if nid in nodes:
            _merge_attrs(nodes[nid], n)
        else:
            nodes[nid] = dict(n)

    base_links = base.get("links", [])
    if not isinstance(base_links, list):
        base_links = []
    edges = {edge_key(e): e for e in base_links if isinstance(e, dict)}

    frag_links = fragment.get("links", []) or fragment.get("edges", [])
    if not isinstance(frag_links, list):
        frag_links = []
    for e in frag_links:
        if not isinstance(e, dict):
            continue
        k = edge_key(e)
        if k in edges:
            _merge_attrs(edges[k], e)
        else:
            edges[k] = dict(e)

    base["nodes"] = list(nodes.values())
    base["links"] = list(edges.values())
    base.setdefault("directed", True)
    base.setdefault("multigraph", False)
    return base


def stats(graph: dict) -> dict:
    """Totals and edge-confidence breakdown. Fail-open: a non-dict graph is
    treated as empty and non-dict edges are skipped -- never raises."""
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    links = graph.get("links", []) if isinstance(graph, dict) else []
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(links, list):
        links = []
    conf = {"EXTRACTED": 0, "INFERRED": 0, "other": 0}
    for e in links:
        if not isinstance(e, dict):
            continue
        c = e.get("confidence")
        conf[c if c in conf else "other"] += 1
    return {
        "nodes": len(nodes),
        "edges": len(links),
        "edge_confidence": conf,
    }
