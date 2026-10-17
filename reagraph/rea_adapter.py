"""Map REA MCP tool *results* onto node-link graph fragments REAgraph can merge.

REAgraph vendors no REA code and depends on nothing from REA at runtime. This
module only translates the *output shapes* a REA MCP server returns into the
same node-link fragment shape the rest of REAgraph speaks (see `reagraph.memory`
and `reagraph.ingest`), so a fragment produced here folds straight into the
persistent graph via `reagraph.memory.merge`.

Grounding (read-only research, not vendored): the primary REA tool we map is
`analyze_javascript_application`. Its result carries a *JavaScript Application
Graph* (REA's provider-neutral domain model) under
`structuredContent.normalized_result` -- see REA's
`docs/javascript-application-graph.md` and
`src/domain/javascript/javascriptApplicationGraphSchemas.ts`. That graph has the
top-level shape::

    {"schema": "JavaScriptApplicationGraph",
     "graph_id": "jag_<sha256>",
     "root_node_ids": ["jag_node_<sha256>", ...],
     "nodes":  [{"node_id", "kind", "identity",
                 "observations": [{"label", "properties", "evidence"}, ...]}],
     "edges":  [{"edge_id", "source_node_id", "target_node_id",
                 "relation", "properties", "evidence"}],
     "coverage": {...}, "limitations": [...]}

Every observation and edge carries an `evidence` block whose `state` field is
the epistemic marker (`observed` / `inferred` / `unknown` / `unavailable`) and
whose `location` points at an artifact path. The directed relation vocabulary is
`contains, loads, imports, maps_to, exposes, sends, invokes, handles, calls,
persists_to, observed_as, changed_from`; native add-ons/exports and their
`imports`/`exposes`/`calls` relations appear as ordinary JAG nodes/edges, so
native-symbol and import inspection is covered by the same mapping.

Epistemic -> confidence mirrors `reagraph.ingest`: observed -> EXTRACTED,
inferred -> INFERRED; an `unavailable` edge is dropped (nothing to assert); the
original relation is preserved as `context = "rea:<relation>"` so provenance is
never lost. Static structure with no explicit state defaults to EXTRACTED;
anything heuristic resolves to INFERRED.

REA's format moves fast and this adapter is deliberately best-effort and
FAIL-OPEN: an unknown tool, a missing field, or an unexpected shape never
raises. Anything we cannot map is wrapped as a single evidence node carrying a
raw summary, so a finding is never silently lost and a bad result never aborts a
stream.
"""

from __future__ import annotations

import hashlib
import json
import re

# REA tool names whose results we map specially. `analyze_javascript_application`
# emits a JavaScript Application Graph (static JS/Electron structure *plus*
# native add-on/export nodes and import edges), which is the shape we ground
# here. Anything not in this set fails open to an evidence node.
SUPPORTED_TOOLS: set[str] = {"analyze_javascript_application"}

# REA epistemic `state` -> graphify confidence. Absent/other -> handled below.
_STATE_TO_CONFIDENCE = {"observed": "EXTRACTED", "inferred": "INFERRED"}

# JAG directed relation vocabulary (for recognising a graph by shape).
_JAG_RELATIONS = frozenset(
    {
        "contains", "loads", "imports", "maps_to", "exposes", "sends",
        "invokes", "handles", "calls", "persists_to", "observed_as",
        "changed_from",
    }
)


def to_fragment(tool_name: str, result: dict) -> dict:
    """Map a REA MCP tool result to a node-link fragment {"nodes", "links"}.

    For a supported tool whose result carries a JavaScript Application Graph,
    entities become nodes and relationships become edges, with confidence drawn
    from each evidence `state` and `context = "rea:<relation>"`. For an unknown
    tool, or a supported tool returning an unexpected shape, FAIL OPEN: the whole
    result is wrapped as one evidence node so nothing is lost. Never raises.
    """
    try:
        if isinstance(result, dict) and tool_name in SUPPORTED_TOOLS:
            graph = _find_jag(result)
            if graph is not None:
                fragment = _map_jag(graph)
                if fragment["nodes"] or fragment["links"]:
                    return fragment
    except Exception:
        pass  # fall through to fail-open; this adapter never raises
    return _fail_open(tool_name, result)


# --- helpers (kept small; below the public API) -----------------------------


def _find_jag(result: dict) -> dict | None:
    """Locate the JavaScript Application Graph inside a REA tool result.

    Verified against real `rea-agents` output: `analyze_javascript_application`
    returns an *Evidence envelope* whose operation payload is under
    `normalized_result`, and the JAG itself is nested one level deeper under
    `normalized_result.graph` (the envelope's normalized_result also carries
    input_path/format/inventory ids, so it is NOT itself the graph). We therefore
    probe each plausible base AND its `.graph` child, also accepting the graph
    passed in directly or lightly wrapped under `structuredContent`. Returns the
    graph dict or None.
    """
    structured = result.get("structuredContent")
    bases = [
        result,
        result.get("normalized_result"),
        result.get("result"),
        structured if isinstance(structured, dict) else None,
        structured.get("normalized_result") if isinstance(structured, dict) else None,
    ]
    candidates: list = []
    for base in bases:
        if not isinstance(base, dict):
            continue
        candidates.append(base)
        nested = base.get("graph")  # REA's real nesting: ...normalized_result.graph
        if isinstance(nested, dict):
            candidates.append(nested)
    for cand in candidates:
        if _looks_like_jag(cand):
            return cand
    return None


def _looks_like_jag(graph: dict) -> bool:
    """True if `graph` has the JAG node/edge shape (schema or structural markers)."""
    if graph.get("schema") == "JavaScriptApplicationGraph":
        return True
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
        if "node_id" in nodes[0] or "observations" in nodes[0]:
            return True
    if isinstance(edges, list) and edges and isinstance(edges[0], dict):
        if "source_node_id" in edges[0]:
            return True
    return False


def _map_jag(graph: dict) -> dict:
    """Map a JAG graph to a node-link fragment. Each item is guarded; never raises."""
    nodes = []
    for n in graph.get("nodes", []) or []:
        if not isinstance(n, dict):
            continue
        nid = n.get("node_id") or n.get("id")
        if nid is None:
            continue
        node = {
            "id": nid,
            "label": _node_label(n, nid),
            "file_type": "code",
            "confidence": _node_confidence(n),
        }
        if n.get("kind"):
            node["rea_kind"] = n["kind"]
        src = _node_source_file(n)
        if src:
            node["source_file"] = src
        nodes.append(node)

    links = []
    for e in graph.get("edges", []) or graph.get("links", []) or []:
        if not isinstance(e, dict):
            continue
        state = _evidence_state(e.get("evidence"))
        if state == "unavailable":
            continue  # nothing to assert
        source = e.get("source_node_id") or e.get("source")
        target = e.get("target_node_id") or e.get("target")
        if source is None or target is None:
            continue
        rel = e.get("relation", "references")
        links.append(
            {
                "source": source,
                "target": target,
                "relation": rel,
                "confidence": _state_to_confidence(state),
                "context": f"rea:{rel}",
            }
        )

    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


def _state_to_confidence(state) -> str:
    """observed -> EXTRACTED, inferred -> INFERRED; absent -> EXTRACTED (static
    structure is observed by default); unknown/other -> INFERRED (heuristic)."""
    if state is None:
        return "EXTRACTED"
    return _STATE_TO_CONFIDENCE.get(state, "INFERRED")


def _evidence_state(evidence):
    """The epistemic `state` from an evidence block (or None if absent)."""
    if isinstance(evidence, dict):
        return evidence.get("state")
    return None


def _node_confidence(node: dict) -> str:
    """Strongest confidence across a node's observations (re-observation upgrades)."""
    best = None  # None -> no observation seen; default to EXTRACTED like static structure
    for obs in node.get("observations", []) or []:
        if not isinstance(obs, dict):
            continue
        conf = _state_to_confidence(_evidence_state(obs.get("evidence")))
        if conf == "EXTRACTED":
            return "EXTRACTED"
        best = "INFERRED"
    return best or "EXTRACTED"


def _node_label(node: dict, nid: str) -> str:
    """First non-null observation label, else the node id."""
    for obs in node.get("observations", []) or []:
        if isinstance(obs, dict) and obs.get("label"):
            return obs["label"]
    return node.get("label") or nid


def _node_source_file(node: dict):
    """Source path from the first observation's evidence location, if present.

    Real REA locations are a ``source-range`` whose file is under
    ``location.value.source`` (e.g. "src/store.js"); older/other shapes use
    ``path``. Accept either so the node carries its origin file when known.
    """
    for obs in node.get("observations", []) or []:
        if not isinstance(obs, dict):
            continue
        loc = (obs.get("evidence") or {}).get("location") if isinstance(obs.get("evidence"), dict) else None
        value = loc.get("value") if isinstance(loc, dict) else None
        if isinstance(value, dict):
            src = value.get("source") or value.get("path")
            if src:
                return src
    return None


def _fail_open(tool_name, result) -> dict:
    """Wrap an unmapped result as a single evidence node so nothing is lost."""
    try:
        name = str(tool_name) if tool_name is not None else "unknown"
        digest = _stable_hash(result)
        node_id = f"rea_evidence_{_slug(name)}_{digest}"
        node = {
            "id": node_id,
            "label": f"REA {name} result",
            "file_type": "evidence",
            "confidence": "INFERRED",
            "rea_tool": name,
            "rea_summary": _summary(result),
        }
        return {"directed": True, "multigraph": False, "nodes": [node], "links": []}
    except Exception:
        # Absolute last resort: a constant node, so we still never raise.
        return {
            "directed": True,
            "multigraph": False,
            "nodes": [{"id": "rea_evidence_unknown", "label": "REA result",
                       "file_type": "evidence", "confidence": "INFERRED"}],
            "links": [],
        }


def _stable_hash(obj) -> str:
    """A short, deterministic hash of a result (order-independent for dicts)."""
    try:
        blob = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        blob = repr(obj)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _summary(obj, limit: int = 500) -> str:
    """A bounded, readable summary of a raw result for the evidence attribute."""
    try:
        text = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        text = repr(obj)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _slug(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z._-]+", "_", name) or "unknown"
