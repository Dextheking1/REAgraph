"""Align the nodes of a SOURCE graph (from graphify, built from source) to the
nodes of a BINARY graph (from REA via REAgraph, built from the compiled program)
for *the same program* — carrying a confidence on every match and ABSTAINING
when the evidence is ambiguous.

Why confidence (and abstention) are first-class here
----------------------------------------------------
How well we can line the two graphs up depends entirely on how much of the
source survived compilation:

  * Where symbols/strings survive the toolchain — JS/Electron, .NET, WASM,
    debug/unstripped native builds — function and type *names* come straight
    across, and a name that is unique on both sides is a near-certain match
    (we score it ~0.95). This is the strong, honest signal.
  * On a stripped/obfuscated native binary the names are gone, so all we have
    left is *shape*: the local call/type neighbourhood of each node, captured as
    a Weisfeiler-Lehman label. Structure-only matching is genuinely weaker
    (~0.70), which is exactly why we never hand back a bare mapping — every
    match carries its confidence so the caller can threshold it.

The one rule that keeps this trustworthy: when a key or a structural label is
*ambiguous* (it occurs more than once on either side) we do NOT guess which of
the candidates is the right one — we leave the node unmatched. A wrong
cross-graph match is worse than an honest "don't know", so we abstain. Matching
is one-to-one: every node is claimed by at most one node on the other side.

Everything here is pure stdlib and deterministic (candidates are considered in
sorted-id order, so the same inputs always produce the same mapping).

Signals come from the sibling ``reagraph.fingerprint`` module:
    wl_labels(graph, rounds=3) -> {id: label}   structural neighbourhood hash
    degrees(graph)             -> {id: int}      total degree per node
    name_key(node)             -> str | None     stable symbol/name of a node
    god_nodes(graph, top)      -> [id, ...]       the most-connected hubs
"""

from __future__ import annotations

try:  # the sibling module is built in parallel; tolerate it being absent.
    from . import fingerprint
except ImportError:  # pragma: no cover - exercised only before fingerprint lands
    import hashlib as _hashlib

    class _FingerprintStub:
        """A small, dependency-free stand-in with the documented signatures, so
        this module imports and runs before the real ``fingerprint`` exists."""

        @staticmethod
        def name_key(node):
            raw = node.get("label") or node.get("id") if isinstance(node, dict) else node
            if raw is None:
                return None
            text = str(raw)
            cut = text.find("(")
            if cut != -1:
                text = text[:cut]
            bad = "()[]{}<>.,:;*&!?\"'`~@#$%^+=|\\/ \t\n"
            key = text.lower().translate({ord(c): None for c in bad})
            return key or None

        @staticmethod
        def degrees(graph):
            deg = {n["id"]: 0 for n in graph.get("nodes", []) if "id" in n}
            for e in graph.get("links", []):
                s, t = e.get("source"), e.get("target")
                if s in deg:
                    deg[s] += 1
                if t in deg:
                    deg[t] += 1
            return deg

        @staticmethod
        def wl_labels(graph, rounds=3):
            ids = [n["id"] for n in graph.get("nodes", []) if "id" in n]
            out = {i: [] for i in ids}
            inc = {i: [] for i in ids}
            for e in graph.get("links", []):
                s, t = e.get("source"), e.get("target")
                rel = e.get("relation", "")
                if s in out and t in inc:
                    out[s].append((rel, t))
                    inc[t].append((rel, s))
            # Name-agnostic initial label: the (out-degree, in-degree) signature.
            label = {i: "%d:%d" % (len(out[i]), len(inc[i])) for i in ids}
            for _ in range(max(0, int(rounds))):
                nxt = {}
                for i in ids:
                    outs = sorted("%s>%s" % (r, label[j]) for r, j in out[i])
                    ins = sorted("%s<%s" % (r, label[j]) for r, j in inc[i])
                    sig = repr((label[i], outs, ins)).encode("utf-8")
                    nxt[i] = _hashlib.sha1(sig).hexdigest()[:16]
                label = nxt
            return label

        @staticmethod
        def god_nodes(graph, top):
            deg = _FingerprintStub.degrees(graph)
            ranked = sorted(deg.items(), key=lambda kv: (-kv[1], str(kv[0])))
            return [i for i, _ in ranked[: max(0, int(top))]]

    fingerprint = _FingerprintStub()


# Confidence model (all in [0, 1]). Name is the strong signal, structure weaker.
_NAME_CONF = 0.95        # unique name on both sides
_STRUCT_CONF = 0.70      # unique structural (WL) label on both sides
_DEGREE_NUDGE = 0.10     # structural match whose degrees also agree
_GOD_NUDGE = 0.05        # structural match where both nodes are hubs
_STRUCT_CAP = 0.94       # structure alone never reaches the name tier
_BOTH_CAP = 0.99         # name + structure agreeing is our strongest match


def _node_ids(graph) -> list:
    return [n["id"] for n in graph.get("nodes", []) if "id" in n]


def _by_key(ids, key_of) -> dict:
    """{key: [ids...]} for the given ids, skipping ids whose key is None."""
    out: dict = {}
    for i in ids:
        k = key_of(i)
        if k is None:
            continue
        out.setdefault(k, []).append(i)
    return out


def align(src, bing, *, min_confidence=0.5, wl_rounds=3) -> dict:
    """Match nodes of ``src`` (source/graphify graph) to ``bing`` (binary/REA
    graph) for the same program.

    Returns::

        {"matches": {src_id: {"bin": bin_id, "confidence": float,
                              "via": "name"|"structure"|"both"}},
         "src_only": [src ids with no confident match],
         "bin_only": [bin ids no src node matched to],
         "stats": {"src_nodes", "bin_nodes", "matched", "mean_confidence"}}

    Matches below ``min_confidence`` are abstained (never committed). Matching is
    one-to-one and deterministic. Accurate where symbols survive; on stripped
    binaries structure-only matching is weaker — which is why the confidence is
    always carried, and ambiguity abstains rather than guesses.
    """
    src_nodes = {n["id"]: n for n in src.get("nodes", []) if "id" in n}
    bin_nodes = {n["id"]: n for n in bing.get("nodes", []) if "id" in n}
    src_ids = sorted(src_nodes, key=lambda i: str(i))
    bin_ids = sorted(bin_nodes, key=lambda i: str(i))

    matches: dict = {}
    matched_bin: set = set()

    # --- 1. NAME match: unique-on-both-sides symbol names. Strong signal. ---
    src_by_name = _by_key(src_ids, lambda i: fingerprint.name_key(src_nodes[i]))
    bin_by_name = _by_key(bin_ids, lambda i: fingerprint.name_key(bin_nodes[i]))

    if _NAME_CONF >= min_confidence:
        for sid in src_ids:
            key = fingerprint.name_key(src_nodes[sid])
            if key is None:
                continue
            s_cands = src_by_name.get(key, [])
            b_cands = bin_by_name.get(key, [])
            # Ambiguous on either side (>1) or absent on bin -> abstain.
            if len(s_cands) != 1 or len(b_cands) != 1:
                continue
            bid = b_cands[0]
            if bid in matched_bin:
                continue
            matches[sid] = {"bin": bid, "confidence": _NAME_CONF, "via": "name"}
            matched_bin.add(bid)

    # --- Structural labels (computed once over each whole graph). ---
    src_wl = fingerprint.wl_labels(src, rounds=wl_rounds)
    bin_wl = fingerprint.wl_labels(bing, rounds=wl_rounds)

    # --- 2b. BOTH: a name match whose structure *also* agrees -> boost. ---
    for sid, m in matches.items():
        lab = src_wl.get(sid)
        if lab is not None and lab == bin_wl.get(m["bin"]):
            m["via"] = "both"
            m["confidence"] = round(min(_BOTH_CAP, m["confidence"] + 0.04), 4)

    # --- 2. STRUCTURE match among the still-unmatched nodes. Weaker signal. ---
    if _STRUCT_CONF >= min_confidence:
        unmatched_src = [i for i in src_ids if i not in matches]
        unmatched_bin = [i for i in bin_ids if i not in matched_bin]

        src_by_lab = _by_key(unmatched_src, lambda i: src_wl.get(i))
        bin_by_lab = _by_key(unmatched_bin, lambda i: bin_wl.get(i))

        src_deg = fingerprint.degrees(src)
        bin_deg = fingerprint.degrees(bing)
        god_top_src = max(1, len(src_ids) // 10)
        god_top_bin = max(1, len(bin_ids) // 10)
        god_src = set(fingerprint.god_nodes(src, god_top_src))
        god_bin = set(fingerprint.god_nodes(bing, god_top_bin))

        for sid in unmatched_src:
            lab = src_wl.get(sid)
            if lab is None:
                continue
            s_cands = src_by_lab.get(lab, [])
            b_cands = bin_by_lab.get(lab, [])
            # Ambiguous / symmetric structure -> abstain, do not guess.
            if len(s_cands) != 1 or len(b_cands) != 1:
                continue
            bid = b_cands[0]
            if bid in matched_bin:
                continue
            conf = _STRUCT_CONF
            if src_deg.get(sid) == bin_deg.get(bid):
                conf += _DEGREE_NUDGE
            if sid in god_src and bid in god_bin:
                conf += _GOD_NUDGE
            conf = round(min(conf, _STRUCT_CAP), 4)
            if conf < min_confidence:
                continue
            matches[sid] = {"bin": bid, "confidence": conf, "via": "structure"}
            matched_bin.add(bid)

    confs = [m["confidence"] for m in matches.values()]
    mean_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
    src_only = [i for i in src_ids if i not in matches]
    bin_only = [i for i in bin_ids if i not in matched_bin]

    return {
        "matches": matches,
        "src_only": src_only,
        "bin_only": bin_only,
        "stats": {
            "src_nodes": len(src_ids),
            "bin_nodes": len(bin_ids),
            "matched": len(matches),
            "mean_confidence": mean_conf,
        },
    }
