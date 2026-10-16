"""Turn a reverse-engineering finding into a node-link graph fragment.

Accepts either:
  - a REA-style JAG doc: nodes/edges carrying an `epistemic` state
    (observed / inferred / unavailable), plus optional coverage/limitations, or
  - an already node-link graph (nodes + links/edges).

REA epistemic state maps to graphify confidence: observed -> EXTRACTED,
inferred -> INFERRED. `unavailable` edges are dropped (nothing to assert). The
original relation is preserved in `context` as `rea:<relation>` so provenance is
never lost. This mirrors graphify's `extract --rea` ingester so the fragments
merge cleanly into the same graph.
"""

from __future__ import annotations

import json
from pathlib import Path

_EPISTEMIC_TO_CONFIDENCE = {"observed": "EXTRACTED", "inferred": "INFERRED"}


def ingest_jag(doc: dict) -> dict:
    """Map a JAG doc -> node-link fragment. Pure; never raises on a missing field."""
    nodes = []
    for n in doc.get("nodes", []):
        nid = n.get("id")
        if nid is None:
            continue
        conf = _EPISTEMIC_TO_CONFIDENCE.get(n.get("epistemic", "observed"), "INFERRED")
        node = {
            "id": nid,
            "label": n.get("label", nid),
            "file_type": n.get("file_type", "code"),
            "confidence": conf,
        }
        if n.get("source_file"):
            node["source_file"] = n["source_file"]
        nodes.append(node)

    links = []
    for e in doc.get("edges", []) or doc.get("links", []):
        epi = e.get("epistemic", "observed")
        if epi == "unavailable":
            continue  # nothing to assert
        rel = e.get("relation", "references")
        links.append(
            {
                "source": e.get("source"),
                "target": e.get("target"),
                "relation": rel,
                "confidence": _EPISTEMIC_TO_CONFIDENCE.get(epi, "INFERRED"),
                "context": f"rea:{rel}",
            }
        )

    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


def load_any(path) -> dict:
    """Load a file as a node-link fragment, auto-detecting JAG vs node-link."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    # Already node-link? (has links, or edges without an epistemic marker)
    if "links" in doc:
        doc.setdefault("nodes", [])
        return doc
    looks_like_jag = any("epistemic" in e for e in doc.get("edges", []) if isinstance(e, dict)) or any(
        "epistemic" in n for n in doc.get("nodes", []) if isinstance(n, dict)
    )
    if looks_like_jag:
        return ingest_jag(doc)
    # Plain {nodes, edges} node-link with the "edges" spelling.
    return {"directed": True, "multigraph": False, "nodes": doc.get("nodes", []), "links": doc.get("edges", [])}
