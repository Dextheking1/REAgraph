"""Honest coverage: make verification status and gaps first-class.

Two documented REA pain points this answers:
  - "incomplete findings can be misleading -- missing a finding does not prove
    absence." A graph that is silent about a thing is not a graph that cleared it.
  - users "not knowing what the agent actually VERIFIED." Observed facts and
    guesses look identical once they are nodes on a screen.

So here we separate, out loud, what was OBSERVED (EXTRACTED) from what was only
INFERRED, and we surface the things we point at but never actually analyzed
(edges whose endpoint has no node entry of its own -- "ghost"/unresolved
targets). The `frontier` is the concrete "we have NOT verified this yet; probe
here next" list. The `gaps_report` says all of this in plain words and refuses
to let absence read as proof of absence.

graphify's confidence vocab (see memory.py): EXTRACTED = observed,
INFERRED = inferred/guessed. Pure stdlib, deterministic.
"""

from __future__ import annotations

# The exact phrase tests and readers can rely on. Framing, not decoration:
# a silent graph has not cleared anything.
ABSENCE_DISCLAIMER = (
    "Absence of a finding is NOT proof of absence: anything not listed as "
    "observed below is simply unverified, not confirmed safe or confirmed absent."
)


def _clean_graph(graph) -> dict:
    """Fail-open coercion to a well-formed graph: a non-dict graph becomes empty,
    non-list nodes/links become empty, and non-dict entries are dropped. The
    ``edges`` spelling is folded into ``links``. On a well-formed graph this is a
    content-preserving copy, so behaviour is unchanged."""
    if not isinstance(graph, dict):
        return {"nodes": [], "links": []}
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])
    if not links:
        links = graph.get("edges", [])
    return {
        "nodes": [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else [],
        "links": [e for e in links if isinstance(e, dict)] if isinstance(links, list) else [],
    }


def _node_index(graph: dict) -> dict:
    """Map node id -> node dict, keeping the first entry for a given id."""
    index = {}
    for n in graph.get("nodes", []):
        nid = n.get("id")
        if nid is not None and nid not in index:
            index[nid] = n
    return index


def _edges(graph: dict) -> list:
    return graph.get("links", []) or graph.get("edges", []) or []


def _conf(obj: dict) -> str:
    return obj.get("confidence") or ""


def _tally(objs, conf_of) -> dict:
    tally = {"EXTRACTED": 0, "INFERRED": 0, "other": 0}
    for o in objs:
        c = conf_of(o)
        tally[c if c in ("EXTRACTED", "INFERRED") else "other"] += 1
    return tally


def coverage(graph: dict) -> dict:
    """Observed-vs-inferred counts for nodes AND edges, the unresolved endpoints
    we never analyzed, and an overall verified fraction of the edges.

    verified_fraction = EXTRACTED edges / total edges (0.0 when there are none).
    `ghost_targets` are ids that appear as an edge source or target but have no
    node entry of their own -- things we point at but never actually analyzed.

    Fail-open: a non-dict graph or non-dict node/edge entries are tolerated (an
    empty/clean graph yields the natural empty-shape result); never raises.
    """
    graph = _clean_graph(graph)
    nodes = graph.get("nodes", [])
    edges = _edges(graph)
    index = _node_index(graph)

    # Endpoints referenced by edges that have no node entry of their own.
    ghosts = set()
    for e in edges:
        for endpoint in (e.get("source"), e.get("target")):
            if endpoint is not None and endpoint not in index:
                ghosts.add(endpoint)

    edge_conf = _tally(edges, _conf)
    total_edges = len(edges)
    verified_fraction = (edge_conf["EXTRACTED"] / total_edges) if total_edges else 0.0

    return {
        "nodes": {
            "total": len(nodes),
            "confidence": _tally(nodes, _conf),
        },
        "edges": {
            "total": total_edges,
            "confidence": edge_conf,
        },
        "ghost_targets": sorted(ghosts, key=str),
        "verified_fraction": verified_fraction,
    }


def frontier(graph: dict) -> list:
    """The exploration frontier: INFERRED edges whose target is a ghost (no node
    entry) or an INFERRED-only node. These are the concrete, not-yet-verified
    hops -- "probe here next".

    Returns [{source, target, relation, reason}], deterministic (sorted).
    Fully-observed edges and edges into confirmed (EXTRACTED) nodes are excluded.

    Fail-open: a non-dict graph or non-dict node/edge entries are tolerated (an
    empty/clean graph yields an empty frontier); never raises.
    """
    graph = _clean_graph(graph)
    index = _node_index(graph)
    items = []
    for e in _edges(graph):
        if _conf(e) != "INFERRED":
            continue
        target = e.get("target")
        tnode = index.get(target)
        if tnode is None:
            reason = "target unresolved (ghost) -- pointed at but never analyzed"
        elif _conf(tnode) == "INFERRED":
            reason = "target only inferred, never observed"
        else:
            # Target is observed (or otherwise ranked); edge is not frontier.
            continue
        items.append({
            "source": e.get("source"),
            "target": target,
            "relation": e.get("relation", ""),
            "reason": reason,
        })
    items.sort(key=lambda d: (str(d["source"]), str(d["target"]), str(d["relation"])))
    return items


def gaps_report(graph: dict) -> str:
    """An honest, plain-words report of what is observed, what is only inferred,
    and what is unverified/unknown -- the anti-false-confidence feature."""
    cov = coverage(graph)
    front = frontier(graph)

    nconf = cov["nodes"]["confidence"]
    econf = cov["edges"]["confidence"]
    pct = round(cov["verified_fraction"] * 100, 1)

    lines = [
        "REAgraph coverage / honesty report",
        "=" * 36,
        "",
        ABSENCE_DISCLAIMER,
        "",
        "OBSERVED (verified, confidence=EXTRACTED):",
        f"  nodes observed : {nconf['EXTRACTED']}",
        f"  edges observed : {econf['EXTRACTED']}",
        f"  verified fraction of edges : {pct}% "
        f"({econf['EXTRACTED']}/{cov['edges']['total']})",
        "",
        "ONLY INFERRED (guessed, confidence=INFERRED, NOT verified):",
        f"  nodes inferred : {nconf['INFERRED']}",
        f"  edges inferred : {econf['INFERRED']}",
        "",
        "UNVERIFIED / UNKNOWN:",
        f"  unresolved targets we point at but never analyzed (ghosts) : "
        f"{len(cov['ghost_targets'])}",
    ]
    for gid in cov["ghost_targets"]:
        lines.append(f"    ? {gid}  (no node entry -- never analyzed)")

    lines.append("")
    lines.append(
        f"NOT-YET-VERIFIED frontier (probe here next) : {len(front)} item(s)"
    )
    for item in front:
        lines.append(
            f"    -> {item['source']} --{item['relation']}--> {item['target']}"
            f"  [{item['reason']}]"
        )

    if nconf["other"] or econf["other"]:
        lines.append("")
        lines.append(
            f"UNLABELLED confidence: {nconf['other']} node(s), "
            f"{econf['other']} edge(s) carry no confidence and cannot be "
            "treated as verified."
        )

    return "\n".join(lines)
