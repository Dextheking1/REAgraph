#!/usr/bin/env python3
"""Quality / accuracy benchmark for REAgraph.

This does NOT measure speed. It measures *correctness* of the two headline
claims, on synthetic-but-labelled graphs whose ground truth we generate
ourselves, so every number printed below is a real measurement against a known
answer -- not an assertion.

  PART 1 -- DIFF is byte-agnostic (the REA #4520 win)
      Identity in `reagraph.diff` is the node id and the (source, target,
      relation) edge key -- never a file location. So a program whose every node
      merely *moved* (every source_location rewritten, structure untouched)
      reads as 0 changed, while a naive location-keyed diff reports ~100%
      changed. We print both, then show a genuinely-tampered copy surfaces
      exactly the injected nodes/edge and nothing else.

  PART 2 -- ALIGN / ATTEST precision & recall (symbols present vs stripped)
      We emit a SOURCE graph with known node identities, then build SHIPPED
      variants of the *same* program and, because we also emit the ground-truth
      source<->shipped correspondence, we compute the REAL precision and recall
      of `reagraph.align`'s matches and the integrity SEPARATION between a clean
      build (~1.0) and a tampered one (<1.0).

Everything is pure stdlib, deterministic (fixed seeds), and self-contained.

Run:
    .venv/bin/python bench/bench_quality.py

Honesty note: the graphs are synthetic. What is measured is labelled at every
line. "Recall" is against a correspondence we generated; "precision" counts a
match as wrong only when it contradicts that known correspondence; "integrity"
is REAgraph's own confidence-weighted score. No real binaries are involved.
"""

from __future__ import annotations

import os
import random
import sys

# Make `reagraph` importable when run straight from the repo (no install needed).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reagraph import align as _align_mod
from reagraph import attest as _attest_mod
from reagraph import diff as _diff_mod


# --------------------------------------------------------------------------- #
# Synthetic program-graph generator (deterministic).
# --------------------------------------------------------------------------- #

RELATIONS = ("calls", "reads", "writes", "imports")


def build_source_graph(seed: int, n: int) -> dict:
    """A program-like directed call/dataflow graph with heterogeneous degrees.

    Built by (seeded) preferential-ish attachment so neighbourhoods differ node
    to node -- which is what gives most nodes a *unique* Weisfeiler-Lehman label,
    the signal the stripped-binary aligner has to rely on. Nodes carry a human
    `label` (the symbol name) and a `source_location` (file:line:byte)."""
    rng = random.Random(seed)
    nodes = []
    links = []
    degree_so_far = [0] * n
    for i in range(n):
        mod = i % 7
        nodes.append(
            {
                "id": f"src#{i}",
                "label": f"mod{mod}.func_{i}",
                "source_location": f"src/mod{mod}.py:{10 + i}:{i * 40}",
                "source_file": f"src/mod{mod}.py",
                "confidence": "EXTRACTED",
            }
        )
        if i == 0:
            continue
        # Connect to a few earlier nodes, biased toward already-popular ones.
        k = 1 + rng.randint(0, min(3, i))
        targets: set = set()
        attempts = 0
        while len(targets) < k and attempts < 20:
            attempts += 1
            if rng.random() < 0.6 and sum(degree_so_far[:i]) > 0:
                # preferential: pick weighted by current degree
                pick = rng.choices(range(i), weights=[d + 1 for d in degree_so_far[:i]])[0]
            else:
                pick = rng.randint(0, i - 1)
            targets.add(pick)
        for t in sorted(targets):
            rel = RELATIONS[(i + t) % len(RELATIONS)]
            links.append(
                {
                    "source": f"src#{i}",
                    "target": f"src#{t}",
                    "relation": rel,
                    "confidence": "EXTRACTED",
                    "context": "gen",
                }
            )
            degree_so_far[i] += 1
            degree_so_far[t] += 1
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


def relocate(graph: dict) -> dict:
    """Same ids, same labels, same edges -- but EVERY node's source_location
    (and byte offset inside the file) is rewritten. Models a recompile/reformat
    that moves bytes around without changing the program's structure."""
    nodes = []
    for idx, n in enumerate(graph["nodes"]):
        m = dict(n)
        m["source_location"] = f"relocated/blob.py:{1000 + idx * 3}:{900000 - idx * 111}"
        m["source_file"] = "relocated/blob.py"
        nodes.append(m)
    links = [dict(e) for e in graph["links"]]
    return {**graph, "nodes": nodes, "links": links}


def inject_tamper(graph: dict, k: int, seed: int) -> tuple:
    """Add K brand-new nodes and one new edge into an EXISTING flow.

    Returns (tampered_graph, injected_ids, new_edge_triple)."""
    rng = random.Random(seed)
    g = {**graph, "nodes": [dict(n) for n in graph["nodes"]], "links": [dict(e) for e in graph["links"]]}
    existing_ids = [n["id"] for n in g["nodes"]]
    injected = []
    for j in range(k):
        nid = f"inj#{j}"
        injected.append(nid)
        g["nodes"].append(
            {
                "id": nid,
                "label": f"exfil.hidden_{j}",
                "source_location": f"vendor/opaque.so:{j}",
                "confidence": "EXTRACTED",
            }
        )
    # A new flow from a real node into the first injected node (exfil path).
    anchor = existing_ids[rng.randrange(len(existing_ids))]
    new_edge = (anchor, injected[0], "calls")
    g["links"].append(
        {"source": new_edge[0], "target": new_edge[1], "relation": new_edge[2], "confidence": "EXTRACTED"}
    )
    # Wire the injected cluster together so it has its own little structure.
    for a, b in zip(injected, injected[1:]):
        g["links"].append({"source": a, "target": b, "relation": "calls", "confidence": "EXTRACTED"})
    return g, injected, new_edge


# --------------------------------------------------------------------------- #
# Shipped-variant builders (with ground-truth correspondence).
# --------------------------------------------------------------------------- #


def ship_symbols_survive(graph: dict) -> tuple:
    """Variant (a): clean build where symbols survive.

    Node ids are renamed to opaque addresses (as a real recovered-binary graph
    would have), source_location is relocated, but the human `label` (symbol
    name) still comes across. Returns (bin_graph, gt) where gt maps bin_id ->
    src_id for the known correspondence."""
    gt = {}
    id_map = {}
    nodes = []
    for idx, n in enumerate(graph["nodes"]):
        bid = f"0x{0x401000 + idx * 16:08x}"
        id_map[n["id"]] = bid
        gt[bid] = n["id"]
        nodes.append(
            {
                "id": bid,
                "label": n["label"],  # symbol survived
                "source_location": f"app.bin:{idx * 48}",
                "confidence": "EXTRACTED",
            }
        )
    links = [
        {"source": id_map[e["source"]], "target": id_map[e["target"]], "relation": e["relation"]}
        for e in graph["links"]
    ]
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}, gt


def ship_stripped(graph: dict, k_inject: int, seed: int) -> tuple:
    """Variant (b): STRIPPED build.

    Both ids AND labels are replaced with opaque address-like strings so NO
    source name survives (name_key gives nothing matchable). Structure of the
    original program is preserved, then K foreign nodes are injected with their
    own little subgraph plus one edge into the real code. Returns
    (bin_graph, gt, injected_ids)."""
    rng = random.Random(seed)
    gt = {}
    id_map = {}
    nodes = []
    for idx, n in enumerate(graph["nodes"]):
        bid = f"sub_{0x401000 + idx * 16:08x}"
        id_map[n["id"]] = bid
        gt[bid] = n["id"]
        nodes.append(
            {
                "id": bid,
                "label": bid,  # stripped: the only "name" is the address itself
                "source_location": f"stripped.bin:{idx * 32}",
                "confidence": "EXTRACTED",
            }
        )
    links = [
        {"source": id_map[e["source"]], "target": id_map[e["target"]], "relation": e["relation"]}
        for e in graph["links"]
    ]
    # Inject K foreign nodes (no source twin -> must land in the residual).
    injected = []
    real_ids = [id_map[n["id"]] for n in graph["nodes"]]
    for j in range(k_inject):
        iid = f"sub_ff{j:06x}"
        injected.append(iid)
        nodes.append({"id": iid, "label": iid, "source_location": f"stripped.bin:ff{j}"})
    for a, b in zip(injected, injected[1:]):
        links.append({"source": a, "target": b, "relation": "calls"})
    if injected:
        anchor = real_ids[rng.randrange(len(real_ids))]
        links.append({"source": anchor, "target": injected[0], "relation": "calls"})
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}, gt, injected


def ship_tampered(graph: dict, k_implant: int, seed: int) -> tuple:
    """Variant (c): symbols survive (clean-looking) but a small implant cluster
    and new flows are injected -- K foreign exfil nodes wired to the real code,
    plus one brand-new flow between two *existing* (matched) nodes, which attest
    surfaces as an unexplained edge. Returns (bin_graph, gt, injected_ids)."""
    bin_graph, gt = ship_symbols_survive(graph)
    rng = random.Random(seed)
    nodes = [dict(n) for n in bin_graph["nodes"]]
    links = [dict(e) for e in bin_graph["links"]]
    real_bin_ids = [n["id"] for n in nodes]

    injected = []
    for j in range(k_implant):
        iid = f"0xdead{j:04x}"
        injected.append(iid)
        nodes.append({"id": iid, "label": f"telemetry.beacon_{j}", "source_location": f"app.bin:ff{j}"})
    # Wire the implant cluster and hang it off a real node (exfil path).
    for a, b in zip(injected, injected[1:]):
        links.append({"source": a, "target": b, "relation": "calls"})
    anchor = real_bin_ids[rng.randrange(len(real_bin_ids))]
    links.append({"source": anchor, "target": injected[0], "relation": "calls"})

    # One NEW flow between two existing, matched nodes that the source lacks.
    src_edge_keys = {(e["source"], e["target"], e["relation"]) for e in graph["links"]}
    a2 = b2 = None
    for _ in range(200):
        x, y = rng.sample(real_bin_ids, 2)
        if (gt[x], gt[y], "calls") not in src_edge_keys:
            a2, b2 = x, y
            break
    if a2 is not None:
        links.append({"source": a2, "target": b2, "relation": "calls"})

    bin_graph = {**bin_graph, "nodes": nodes, "links": links}
    return bin_graph, gt, injected


# --------------------------------------------------------------------------- #
# Baselines & metric helpers.
# --------------------------------------------------------------------------- #


def naive_location_diff(a: dict, b: dict) -> dict:
    """A deliberately-naive diff that keys a node on (id, source_location) --
    i.e. it treats a thing that moved as a different thing. This is the #4520
    failure mode. Reports how many of the common ids 'changed' purely because
    their location moved."""
    a_loc = {n["id"]: n.get("source_location") for n in a.get("nodes", [])}
    b_loc = {n["id"]: n.get("source_location") for n in b.get("nodes", [])}
    common = set(a_loc) & set(b_loc)
    changed = [nid for nid in common if a_loc[nid] != b_loc[nid]]
    return {"common": len(common), "changed": len(changed)}


def reagraph_total_changes(report: dict) -> int:
    s = report["summary"]
    return (
        s["nodes_added"]
        + s["nodes_removed"]
        + s["edges_added"]
        + s["edges_removed"]
        + s["rewired_nodes"]
    )


def precision_recall(result: dict, gt: dict, n_true_pairs: int) -> dict:
    """REAL precision/recall of align's matches against the known gt (bin->src).

    A match src_id -> bin_id is a true positive iff gt[bin_id] == src_id. A match
    to an injected/foreign bin node (absent from gt) is a false positive. Recall
    is over the number of correspondences that actually exist (n_true_pairs)."""
    tp = fp = 0
    for src_id, m in result["matches"].items():
        bid = m["bin"]
        if gt.get(bid) == src_id:
            tp += 1
        else:
            fp += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / n_true_pairs if n_true_pairs else 0.0
    return {"tp": tp, "fp": fp, "precision": precision, "recall": recall, "matched": tp + fp}


# --------------------------------------------------------------------------- #
# Benchmark parts.
# --------------------------------------------------------------------------- #


def part1_diff() -> None:
    print("=" * 72)
    print("PART 1 -- DIFF is byte-agnostic (REA #4520)")
    print("=" * 72)

    base = build_source_graph(seed=1, n=80)
    moved = relocate(base)
    n_nodes = len(base["nodes"])
    n_edges = len(base["links"])
    print(f"  program: {n_nodes} nodes, {n_edges} edges")
    print("  scenario: every node's source_location rewritten; structure identical\n")

    # REAgraph structural diff.
    rea = _diff_mod.diff(base, moved)
    rea_changed = reagraph_total_changes(rea)

    # Naive location-keyed diff.
    naive = naive_location_diff(base, moved)
    naive_pct = 100.0 * naive["changed"] / naive["common"] if naive["common"] else 0.0

    print("  RELOCATED-ONLY (no real change):")
    print(f"    REAgraph diff.diff        -> {rea_changed} changed "
          f"(added {rea['summary']['nodes_added']}N/{rea['summary']['edges_added']}E, "
          f"removed {rea['summary']['nodes_removed']}N/{rea['summary']['edges_removed']}E, "
          f"rewired {rea['summary']['rewired_nodes']})")
    print(f"    naive (id, source_location) diff -> {naive['changed']}/{naive['common']} "
          f"changed  ({naive_pct:.0f}% false-change rate)")
    print(f"    unchanged nodes reported by REAgraph: {rea['summary']['unchanged_nodes']}/{n_nodes}")

    # Genuinely-tampered copy: inject K nodes + a new edge, on TOP of relocation.
    k = 3
    tampered, injected, new_edge = inject_tamper(moved, k=k, seed=7)
    rea_t = _diff_mod.diff(base, tampered)
    s = rea_t["summary"]
    print(f"\n  TAMPERED (+{k} nodes, +1 exfil edge, on top of full relocation):")
    print(f"    nodes_added reported : {rea_t['nodes_added']}")
    print(f"    expected injected    : {sorted(injected)}")
    added_edges = [tuple(e) for e in rea_t["edges_added"]]
    print(f"    edges_added reported : {added_edges}")
    # The intra-injected edges + the exfil edge are all genuinely new.
    expected_new_edge = tuple(new_edge)
    anchor = new_edge[0]  # the real node the exfil edge attaches to -> rewired
    exact = (
        set(rea_t["nodes_added"]) == set(injected)
        and expected_new_edge in added_edges
        and s["nodes_removed"] == 0
        and rea_t["rewired_nodes"] == [anchor]  # only the anchor is rewired, correctly
    )
    print(f"    exfil edge {expected_new_edge} present: {expected_new_edge in added_edges}")
    print(f"    rewired nodes        : {rea_t['rewired_nodes']}  (anchor of the new exfil flow)")
    print(f"    reports EXACTLY the tamper (injected nodes + new flow, nothing else): {exact}")
    print()


def part2_align_attest() -> None:
    print("=" * 72)
    print("PART 2 -- ALIGN / ATTEST precision & recall (symbols vs stripped)")
    print("=" * 72)

    src = build_source_graph(seed=2, n=80)
    n_true = len(src["nodes"])
    print(f"  source program: {n_true} nodes, {len(src['links'])} edges\n")

    # ---- (a) symbols survive: clean + relocated + renamed ids, labels kept ----
    bin_a, gt_a = ship_symbols_survive(relocate(src))
    res_a = _align_mod.align(src, bin_a, min_confidence=0.5)
    pr_a = precision_recall(res_a, gt_a, n_true)
    att_a = _attest_mod.attest(src, bin_a, min_confidence=0.5)

    # ---- (b) stripped: ids AND labels gone, structure kept, +K injected ----
    k_inject = 5
    bin_b, gt_b, injected_b = ship_stripped(src, k_inject=k_inject, seed=11)
    res_b = _align_mod.align(src, bin_b, min_confidence=0.5)
    pr_b = precision_recall(res_b, gt_b, n_true)
    att_b = _attest_mod.attest(src, bin_b, min_confidence=0.5)
    injected_in_residual = sum(1 for i in injected_b if i in att_b["shipped_unexplained"])

    # ---- (c) tampered: clean-looking but injected implant cluster + new flow ----
    k_implant = 6
    bin_c, gt_c, inj_c = ship_tampered(src, k_implant=k_implant, seed=5)
    res_c = _align_mod.align(src, bin_c, min_confidence=0.5)
    att_c = _attest_mod.attest(src, bin_c, min_confidence=0.5)
    implant_in_residual = sum(1 for i in inj_c if i in att_c["shipped_unexplained"])

    def block(title, pr, att, extra=""):
        print(f"  [{title}]")
        if pr is not None:
            print(f"    matched {pr['matched']}/{n_true}   TP={pr['tp']} FP={pr['fp']}")
            print(f"    precision = {pr['precision']:.4f}   recall = {pr['recall']:.4f}")
        print(f"    integrity = {att['integrity']:.4f}   "
              f"shipped_unexplained = {len(att['shipped_unexplained'])} node(s), "
              f"{len(att['unexplained_edges'])} edge(s)")
        if extra:
            print(f"    {extra}")
        print()

    block("(a) symbols survive  (clean, relocated, ids renamed, labels kept)",
          pr_a, att_a,
          extra="expect: recall~1.0, precision~1.0, integrity~1.0, residual empty")
    block(f"(b) STRIPPED  (ids+labels gone, structure kept, +{k_inject} injected)",
          pr_b, att_b,
          extra=f"injected nodes landing in residual: {injected_in_residual}/{k_inject}  "
                f"(structure/propagation only -> recall lower than (a), precision still high)")
    block(f"(c) TAMPERED  (symbols survive, +{k_implant}-node implant + new flows)",
          None, att_c,
          extra=f"implant nodes in residual: {implant_in_residual}/{k_implant}  "
                f"| unexplained edges (new flow between known nodes): {att_c['unexplained_edges']}")

    # ---- Headline metrics block ----
    sep = att_a["integrity"] - att_c["integrity"]
    print("  " + "-" * 60)
    print("  HEADLINE METRICS")
    print("  " + "-" * 60)
    print(f"    align precision, symbols-present (a) : {pr_a['precision']:.4f}  (abstains, no mis-match)")
    print(f"    align recall,    symbols-present (a) : {pr_a['recall']:.4f}")
    print(f"    align precision, stripped (b)        : {pr_b['precision']:.4f}  (abstains rather than guess)")
    print(f"    align recall,    stripped (b)        : {pr_b['recall']:.4f}  (structure-only, honestly lower)")
    print(f"    injected nodes caught in residual (b): {injected_in_residual}/{k_inject}")
    print(f"    integrity  clean (a)                 : {att_a['integrity']:.4f}")
    print(f"    integrity  tampered (c)              : {att_c['integrity']:.4f}")
    print(f"    clean-vs-tampered integrity SEPARATION: {sep:.4f}")
    print()


def main() -> None:
    part1_diff()
    part2_align_attest()
    print("=" * 72)
    print("All numbers above are measured against generated ground truth.")
    print("Graphs are synthetic; speed is NOT measured here -- correctness is.")
    print("=" * 72)


if __name__ == "__main__":
    main()
