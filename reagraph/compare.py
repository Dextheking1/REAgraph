"""Authoritative cross-build comparison via REA's ``compare_application_versions``.

``reagraph.diff`` matches two builds by a build-stable *signature* -- a strong,
offline heuristic (see its docstring). REA also ships an authoritative comparator,
``compare_application_versions``, which pairs two JavaScript Application Graphs by
exact node identity, content digest, module-source digest, source-map identity, and
structural fingerprint (REA's own matching, "no fuzzy pairing"). This module maps
that tool's result into a small, readable REAgraph report -- the "do it properly"
counterpart to the signature diff.

Grounding (verified against real rea-agents): the tool returns an Evidence envelope
whose ``normalized_result`` carries::

    {"comparison_id": "...",
     "left":  {"evidence_id", "graph_id", "root_artifact_sha256"},
     "right": {...},
     "summary":  {"unchanged", "added", "removed", "changed", "unknown"},   # counts
     "matching": {"exact_node_identity", "exact_content_digest",
                  "exact_module_source_digest", "source_map_identity",
                  "structural_fingerprint", "semantic_key", "ambiguous",
                  "unmatched"},                                             # counts
     "items": [{"item_id", "node_kind", "status", "left_node_id",
                "right_node_id", "dimensions", ...}],
     "graph": {...}, "coverage": {...}, "limitations": [...]}

This module is pure/offline: it never calls REA. ``reagraph.rea_client.ReaClient
.compare`` runs the tool and hands the result here. FAIL-OPEN: a missing field or an
unexpected shape yields an empty-but-valid report, never an exception.
"""

from __future__ import annotations

_STATUSES = ("unchanged", "added", "removed", "changed", "unknown")


def to_report(normalized_result: dict) -> dict:
    """Map REA's comparison ``normalized_result`` to a REAgraph comparison report.

    Returns ``{"comparison_id", "summary", "items", "matching", "limitations"}``
    where ``summary`` is a count per status and ``items`` buckets the per-node
    comparison items by status. Never raises.
    """
    try:
        nr = normalized_result if isinstance(normalized_result, dict) else {}
        raw_items = nr.get("items") if isinstance(nr.get("items"), list) else []
        buckets: dict[str, list] = {s: [] for s in _STATUSES}
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            status = it.get("status") or "unknown"
            buckets.setdefault(status, []).append({
                "kind": it.get("node_kind"),
                "status": status,
                "left": it.get("left_node_id"),
                "right": it.get("right_node_id"),
                "dimensions": it.get("dimensions") or [],
            })

        # Prefer REA's own summary counts; fall back to counting the items.
        raw_summary = nr.get("summary") if isinstance(nr.get("summary"), dict) else {}
        summary = {}
        for s in _STATUSES:
            val = raw_summary.get(s)
            summary[s] = int(val) if isinstance(val, int) else len(buckets.get(s, []))

        return {
            "comparison_id": nr.get("comparison_id"),
            "summary": summary,
            "items": buckets,
            "matching": nr.get("matching") if isinstance(nr.get("matching"), dict) else {},
            "limitations": nr.get("limitations") if isinstance(nr.get("limitations"), list) else [],
        }
    except Exception:
        return {"comparison_id": None, "summary": {s: 0 for s in _STATUSES},
                "items": {s: [] for s in _STATUSES}, "matching": {}, "limitations": []}


def format_report(report: dict, *, top: int = 20) -> str:
    """A bounded, readable rendering of a comparison report (authoritative diff)."""
    s = report.get("summary", {})
    lines = [
        "Authoritative comparison (REA compare_application_versions):",
        f"  unchanged : {s.get('unchanged', 0)}",
        f"  added     : {s.get('added', 0)}",
        f"  removed   : {s.get('removed', 0)}",
        f"  changed   : {s.get('changed', 0)}",
        f"  unknown   : {s.get('unknown', 0)}",
    ]
    items = report.get("items", {})
    for status in ("added", "removed", "changed"):
        rows = items.get(status, []) or []
        for it in rows[:top]:
            label = it.get("kind") or "?"
            ident = it.get("right") or it.get("left") or ""
            lines.append(f"    {status[0].upper()} {label}  {ident}")
        if len(rows) > top:
            lines.append(f"    ... (+{len(rows) - top} more {status})")
    matching = report.get("matching") or {}
    if matching:
        shown = ", ".join(f"{k}={v}" for k, v in sorted(matching.items()) if v)
        if shown:
            lines.append(f"  matched by: {shown}")
    return "\n".join(lines)
