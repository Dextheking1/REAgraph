#!/usr/bin/env python3
"""bench_diff_quality.py -- the DIFF-QUALITY benchmark for REAgraph.

What this measures
------------------
REAgraph's headline claim is a *byte-agnostic structural diff* (the fix for REA
issue #4520, where a byte-offset diff reports "everything changed" the moment
code merely moves). `reagraph.diff.diff(graphA, graphB)` already identifies nodes
by id and edges by (source, target, relation) rather than by file location, so a
thing that only moved reads as UNCHANGED. This benchmark puts an OBJECTIVE number
on how well that diff recovers the TRUE semantic change between two builds -- and
in particular the critical case where two builds are semantically IDENTICAL but
byte-different (the ideal diff is empty).

The catch, and why we score at the SEMANTIC level
-------------------------------------------------
`diff` reports changes by node id, but real REA node ids are PER-BUILD: analyse
the same source twice and every id can be different. So raw ids cannot be compared
across builds -- a naive reader of `diff` on two identical-but-rebuilt graphs sees
"every node added, every node removed", which is exactly the #4520 failure mode one
level up. REAgraph stores a stable *descriptor* on every node -- (rea_kind, label,
source_file) -- and that is what we score against:

  * Map every node id in each graph to its descriptor.
  * Project diff's added / removed id sets to descriptor multisets, then cancel
    churn: a descriptor reported "added" that is also reported "removed" is the
    same semantic thing with a fresh id, not a change. What survives is the
    descriptor-level signal the diff actually recovered.
  * TRUE semantic change is computed directly from the two graphs' descriptor
    multisets: descriptors in B not in A are added, in A not in B are removed.
    This is ground-truth-free -- it needs no human labels, just the two graphs.
  * When the manifest declares an `expected` change, we also score against that.

Headline metrics (per pair and aggregate)
------------------------------------------
  * spurious_changes -- changes diff reports that are NOT real semantic changes.
    Reported at two granularities:
      - raw-id spurious: id-level changes diff reports beyond the true count. For a
        semantically-identical, rebuilt pair this is the WHOLE graph (the #4520
        shape at the id level).
      - descriptor-level spurious: churn-cancelled descriptor changes that are not
        real. For a semantically-identical pair this is 0 -- the point of the fix.
  * precision / recall / F1 of semantic added+removed detection vs the true
    descriptor delta (and vs the manifest's `expected`, when present).
  * an AGGREGATE F1 (micro-averaged across pairs) and an AGGREGATE "noise floor" =
    mean descriptor-level spurious on semantically-identical pairs (ideal 0).

The final line is machine-greppable for an optimizer loop:
    HEADLINE: aggregate_f1=<x> noise_floor=<y>

Run:
    .venv/bin/python bench/bench_diff_quality.py                 # self-test (no corpus needed)
    .venv/bin/python bench/bench_diff_quality.py <manifest.json> # score a real corpus

Everything here is stdlib. With no manifest arg the benchmark SYNTHESIZES a tiny
corpus that mimics REA's per-build id churn, writes it to a temp dir, scores it,
and asserts the metric behaves (identical pair -> descriptor spurious 0 though
raw-id spurious is large; the truly-added node detected). That self-test both
exercises the harness and demonstrates the metric.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

# Make `import reagraph` work when run from a checkout without an install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from reagraph import memory
from reagraph.diff import diff as structural_diff
from reagraph.diff import normalize_label, rea_signature


# -- descriptors ---------------------------------------------------------------
# A descriptor is the stable, cross-build identity of a node: the fields REAgraph
# stores regardless of where the bytes landed. Never crash on a missing field.
# The label is range-normalized (reagraph.diff.normalize_label) so a symbol whose
# source line:col merely shifted is still the same descriptor.

def descriptor(node: dict) -> tuple:
    """The (rea_kind, normalized label, source_file) identity of a node."""
    return (
        node.get("rea_kind") or "",
        normalize_label(node.get("label") or ""),
        node.get("source_file") or "",
    )


def _sig_desc(sig_key: str) -> tuple:
    """Parse a signature-mode node id ("kind|label|source#ordinal") back to a
    (kind, label, source) descriptor tuple so product-diff output scores against
    the same descriptor space as everything else."""
    s = str(sig_key)
    hash_i = s.rfind("#")
    if hash_i != -1 and s[hash_i + 1:].isdigit():
        s = s[:hash_i]
    parts = s.split("|")
    while len(parts) < 3:
        parts.append("")
    return (parts[0], parts[1], "|".join(parts[2:]))


def id_to_descriptor(graph: dict) -> dict:
    """Map every node id in a graph to its descriptor tuple."""
    out = {}
    for n in graph.get("nodes", []):
        nid = n.get("id")
        if nid is not None:
            out[nid] = descriptor(n)
    return out


def descriptor_multiset(graph: dict) -> Counter:
    """Counter of descriptors in a graph. A multiset, because several nodes can
    legitimately share (rea_kind, label, source_file)."""
    return Counter(descriptor(n) for n in graph.get("nodes", []) if "id" in n)


def _project(ids, id2desc: dict) -> Counter:
    """Project a set/list of node ids to a descriptor multiset."""
    return Counter(id2desc[i] for i in ids if i in id2desc)


# -- scoring -------------------------------------------------------------------

def _prf(detected: Counter, truth: Counter) -> dict:
    """Precision / recall / F1 of a detected multiset against a truth multiset.

    Keys carry a direction tag (see score_pair) so an "added" is never credited
    against a "removed". TP = multiset intersection; FP = detected not in truth;
    FN = truth not in detected. When nothing is predicted and nothing is true the
    diff is perfectly right about there being no change, so P=R=F1=1."""
    tp = sum((detected & truth).values())
    fp = sum((detected - truth).values())
    fn = sum((truth - detected).values())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1}


def _expected_to_counter(expected: dict | None) -> Counter | None:
    """Turn a manifest `expected` block into a direction-tagged truth multiset.

    Each descriptor tuple is [kind, label, source_file]; added -> "+",
    removed -> "-", changed -> "~"."""
    if expected is None:
        return None
    c = Counter()
    for tag, key in (("+", "added"), ("-", "removed"), ("~", "changed")):
        for item in expected.get(key, []) or []:
            c[(tag, tuple(item))] += 1
    return c


def score_pair(graph_a: dict, graph_b: dict, expected: dict | None = None) -> dict:
    """Score one build-pair. Returns all per-pair metrics.

    How each number is computed:
      - true delta (ground-truth-free): TA = descriptors in B not in A (added),
        TR = descriptors in A not in B (removed), from the raw descriptor
        multisets of the two graphs.
      - raw diff: structural_diff gives id-level nodes_added / nodes_removed /
        rewired_nodes.
      - projection: project those id sets to descriptor multisets through each
        graph's id->descriptor map (added via B, removed via A, rewired via A).
      - churn cancellation: DA = reported_added - reported_removed, DR =
        reported_removed - reported_added. A descriptor reported on both sides is
        the same thing rebuilt under a new id, so it cancels.
      - raw-id spurious = id changes reported beyond the true semantic count.
      - descriptor-level spurious = churn-cancelled detected changes that are not
        in the true delta (the false positives of DA/DR vs TA/TR).
    """
    a_desc = id_to_descriptor(graph_a)
    b_desc = id_to_descriptor(graph_b)

    # True, label-free semantic delta, straight from the two descriptor multisets.
    ms_a = descriptor_multiset(graph_a)
    ms_b = descriptor_multiset(graph_b)
    true_added = ms_b - ms_a
    true_removed = ms_a - ms_b

    # What the raw structural diff reports, by id.
    d = structural_diff(graph_a, graph_b)
    raw_id_changes = (len(d["nodes_added"]) + len(d["nodes_removed"])
                      + len(d["rewired_nodes"]))

    # Project reported id changes to descriptor multisets.
    reported_added = _project(d["nodes_added"], b_desc)
    reported_removed = _project(d["nodes_removed"], a_desc)
    reported_rewired = _project(d["rewired_nodes"], a_desc)

    # Cancel per-build id churn: a descriptor seen as both added and removed is
    # the same node rebuilt under a new id, not a real change.
    detected_added = reported_added - reported_removed
    detected_removed = reported_removed - reported_added

    # Score the churn-cancelled detection against the true delta (+/- directions
    # kept distinct so an add is never credited against a remove).
    detected_t = Counter()
    for desc, c in detected_added.items():
        detected_t[("+", desc)] += c
    for desc, c in detected_removed.items():
        detected_t[("-", desc)] += c
    truth_t = Counter()
    for desc, c in true_added.items():
        truth_t[("+", desc)] += c
    for desc, c in true_removed.items():
        truth_t[("-", desc)] += c
    vs_true = _prf(detected_t, truth_t)

    # Spurious: changes reported that are not real semantic changes.
    true_change_count = sum(true_added.values()) + sum(true_removed.values())
    raw_id_spurious = max(0, raw_id_changes - true_change_count)
    descriptor_spurious = vs_true["fp"]  # churn-cancelled changes not in true delta

    identical = (true_change_count == 0)

    # PRODUCT diff: what `reagraph diff` ACTUALLY reports now -- signature mode
    # (match nodes by build-stable signature). Its ids ARE signatures, so its
    # added/removed are already the real change (no churn-cancellation needed).
    # This measures the shipped command, not just the achievable ceiling.
    pd = structural_diff(graph_a, graph_b, signature=rea_signature)
    prod_added = Counter(_sig_desc(s) for s in pd["nodes_added"])
    prod_removed = Counter(_sig_desc(s) for s in pd["nodes_removed"])
    prod_t = Counter()
    for desc, c in prod_added.items():
        prod_t[("+", desc)] += c
    for desc, c in prod_removed.items():
        prod_t[("-", desc)] += c
    truth_t2 = Counter()
    for desc, c in true_added.items():
        truth_t2[("+", desc)] += c
    for desc, c in true_removed.items():
        truth_t2[("-", desc)] += c
    vs_true_product = _prf(prod_t, truth_t2)
    product_changes = len(pd["nodes_added"]) + len(pd["nodes_removed"])
    product_spurious = vs_true_product["fp"]

    result = {
        "nodes_a": len(a_desc),
        "nodes_b": len(b_desc),
        "raw_id_changes": raw_id_changes,
        "raw_id_spurious": raw_id_spurious,
        "descriptor_spurious": descriptor_spurious,
        "identical": identical,
        "detected_added": detected_added,
        "detected_removed": detected_removed,
        "true_added": true_added,
        "true_removed": true_removed,
        "vs_true": vs_true,
        "vs_true_product": vs_true_product,
        "product_changes": product_changes,
        "product_spurious": product_spurious,
        "product_added": prod_added,
        "product_removed": prod_removed,
        "vs_expected": None,
    }

    # Optionally score the churn-cancelled detection against the manifest's
    # declared expectation, including declared rewiring ("changed").
    exp_truth = _expected_to_counter(expected)
    if exp_truth is not None:
        detected_e = Counter(detected_t)
        for desc, c in reported_rewired.items():
            detected_e[("~", desc)] += c
        result["vs_expected"] = _prf(detected_e, exp_truth)

    return result


# -- manifest driver -----------------------------------------------------------

def run_manifest(manifest_path: Path) -> dict:
    """Load a manifest, score every pair, print a table + aggregate, and return
    the aggregate metrics. Paths in the manifest are relative to its directory."""
    manifest_path = Path(manifest_path)
    base_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pairs = manifest.get("pairs", [])

    print("=" * 78)
    print("REAgraph DIFF-QUALITY benchmark")
    print("=" * 78)
    print(f"manifest : {manifest_path}")
    print(f"pairs    : {len(pairs)}")
    print()

    header = (f"{'pair':<14}{'A/B':>9}{'raw_chg':>9}{'raw_spur':>10}"
              f"{'prod_chg':>9}{'prod_spur':>10}{'prodF1':>8}{'ceilF1':>8}")
    print(header)
    print("-" * len(header))

    agg = Counter()       # pooled tp/fp/fn for the churn-cancelled ceiling F1
    agg_prod = Counter()  # pooled tp/fp/fn for the PRODUCT (signature) diff F1
    identical_spurious = []        # ceiling descriptor-spurious on identical pairs
    identical_prod_spurious = []   # PRODUCT spurious on identical pairs (what ships)
    scored = []

    for p in pairs:
        name = p.get("name", "?")
        graph_a = memory.load(base_dir / p["a"])
        graph_b = memory.load(base_dir / p["b"])
        r = score_pair(graph_a, graph_b, p.get("expected"))
        scored.append((name, r))

        agg["tp"] += r["vs_true"]["tp"]
        agg["fp"] += r["vs_true"]["fp"]
        agg["fn"] += r["vs_true"]["fn"]
        agg_prod["tp"] += r["vs_true_product"]["tp"]
        agg_prod["fp"] += r["vs_true_product"]["fp"]
        agg_prod["fn"] += r["vs_true_product"]["fn"]
        if r["identical"]:
            identical_spurious.append(r["descriptor_spurious"])
            identical_prod_spurious.append(r["product_spurious"])

        print(f"{name:<14}{str(r['nodes_a']) + '/' + str(r['nodes_b']):>9}"
              f"{r['raw_id_changes']:>9}{r['raw_id_spurious']:>10}"
              f"{r['product_changes']:>9}{r['product_spurious']:>10}"
              f"{r['vs_true_product']['f1']:>8.2f}{r['vs_true']['f1']:>8.2f}")

    def _f1(c):
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        p_ = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        r_ = tp / (tp + fn) if (tp + fn) > 0 else 1.0
        return (2 * p_ * r_ / (p_ + r_) if (p_ + r_) > 0 else 0.0), p_, r_

    agg_f1, agg_p, agg_r = _f1(agg)
    prod_f1, prod_p, prod_r = _f1(agg_prod)
    noise_floor = (sum(identical_spurious) / len(identical_spurious)
                   if identical_spurious else 0.0)
    prod_noise = (sum(identical_prod_spurious) / len(identical_prod_spurious)
                  if identical_prod_spurious else 0.0)

    print("-" * len(header))
    print("prod_* = what `reagraph diff` reports now (signature mode); "
          "ceilF1 = churn-cancelled ceiling.")
    print(f"PRODUCT  (signature diff) : precision={prod_p:.3f} recall={prod_r:.3f} "
          f"F1={prod_f1:.3f}  noise_floor={prod_noise:.3f}  (ideal F1 1, noise 0)")
    print(f"CEILING  (churn-cancelled): F1={agg_f1:.3f}  noise_floor={noise_floor:.3f}")
    print("=" * 78)
    print(f"HEADLINE: aggregate_f1={prod_f1:.4f} noise_floor={prod_noise:.4f} "
          f"ceiling_f1={agg_f1:.4f}")

    return {"aggregate_f1": prod_f1, "noise_floor": prod_noise,
            "ceiling_f1": agg_f1, "ceiling_noise": noise_floor,
            "scored": scored, "tp": agg_prod["tp"], "fp": agg_prod["fp"],
            "fn": agg_prod["fn"]}


# -- self-test corpus ----------------------------------------------------------

def _node(nid, kind, label, source_file, confidence="EXTRACTED"):
    return {"id": nid, "rea_kind": kind, "label": label,
            "source_file": source_file, "confidence": confidence}


def _synthesize_corpus(root: Path) -> Path:
    """Write a tiny corpus that mimics REA's per-build id churn and return the
    manifest path. Three pairs:

      base  -- the reference build.
      fmt   -- base rebuilt with ALL node ids rewritten and identical descriptors
               (reformatted / moved code; the ideal diff is empty).
      feat  -- base rebuilt with churned ids PLUS one genuinely-added endpoint.
      drop  -- base rebuilt with churned ids with one node genuinely removed.
    """
    graphs = root / "graphs"
    graphs.mkdir(parents=True, exist_ok=True)

    # The reference build. ids are "a*" to make per-build churn obvious.
    base = memory.empty_graph()
    base_nodes = [
        _node("a1", "module", "main", "src/main.js"),
        _node("a2", "endpoint", "api.example/login", "src/auth.js"),
        _node("a3", "function", "validate", "src/auth.js"),
        _node("a4", "module", "util", "src/util.js"),
    ]
    base["nodes"] = base_nodes
    base["links"] = [
        {"source": "a1", "target": "a2", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "a2", "target": "a3", "relation": "calls", "confidence": "EXTRACTED"},
    ]

    def _churn(nodes, prefix):
        """Copy nodes with brand-new ids (prefix*) but identical descriptors."""
        out = []
        for i, n in enumerate(nodes, 1):
            m = dict(n)
            m["id"] = f"{prefix}{i}"
            out.append(m)
        return out

    # fmt: identical descriptors, every id rewritten. Edges rewired to the new ids.
    fmt = memory.empty_graph()
    fmt["nodes"] = _churn(base_nodes, "b")
    fmt["links"] = [
        {"source": "b1", "target": "b2", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "b2", "target": "b3", "relation": "calls", "confidence": "EXTRACTED"},
    ]

    # feat: churned ids + one genuinely new endpoint (the #4520 "find the needle").
    feat = memory.empty_graph()
    feat_nodes = _churn(base_nodes, "c")
    added = _node("c99", "endpoint", "notes-collect.evil.example/sync", "src/main.js")
    feat_nodes.append(added)
    feat["nodes"] = feat_nodes
    feat["links"] = [
        {"source": "c1", "target": "c2", "relation": "calls", "confidence": "EXTRACTED"},
        {"source": "c1", "target": "c99", "relation": "calls", "confidence": "EXTRACTED"},
    ]

    # drop: churned ids with the util module genuinely removed.
    drop = memory.empty_graph()
    drop_nodes = _churn([n for n in base_nodes if n["id"] != "a4"], "d")
    drop["nodes"] = drop_nodes
    drop["links"] = [
        {"source": "d1", "target": "d2", "relation": "calls", "confidence": "EXTRACTED"},
    ]

    memory.save(base, graphs / "base.json")
    memory.save(fmt, graphs / "fmt.json")
    memory.save(feat, graphs / "feat.json")
    memory.save(drop, graphs / "drop.json")

    manifest = {
        "pairs": [
            {"name": "fmt", "a": "graphs/base.json", "b": "graphs/fmt.json",
             "expected": {"added": [], "removed": [], "changed": []},
             "note": "reformatted, semantically identical -> ideal diff is empty"},
            {"name": "feat", "a": "graphs/base.json", "b": "graphs/feat.json",
             "expected": {"added": [["endpoint",
                                     "notes-collect.evil.example/sync",
                                     "src/main.js"]],
                          "removed": [], "changed": []},
             "note": "one endpoint added under full id churn"},
            {"name": "drop", "a": "graphs/base.json", "b": "graphs/drop.json",
             "expected": {"added": [],
                          "removed": [["module", "util", "src/util.js"]],
                          "changed": []},
             "note": "one module removed under full id churn"},
        ]
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def self_test() -> int:
    """Synthesize a corpus, run the benchmark on it, and assert the metric behaves.
    Returns a process exit code (0 = green)."""
    tmp_root = Path(tempfile.mkdtemp(prefix="reagraph-bench-diff-"))
    try:
        manifest_path = _synthesize_corpus(tmp_root)
        out = run_manifest(manifest_path)
        print()

        by_name = dict(out["scored"])

        # fmt: identical builds under full id churn.
        fmt = by_name["fmt"]
        assert fmt["descriptor_spurious"] == 0, (
            f"fmt descriptor-spurious should be 0, got {fmt['descriptor_spurious']}")
        assert fmt["raw_id_spurious"] > 0, (
            "fmt raw-id spurious should be large (every node churned a new id)")
        assert fmt["vs_true"]["f1"] == 1.0, "fmt should score a perfect F1 vs true"

        # feat: the added endpoint must be detected despite the id churn.
        feat = by_name["feat"]
        added_desc = ("endpoint", "notes-collect.evil.example/sync", "src/main.js")
        assert feat["detected_added"].get(added_desc, 0) == 1, (
            "feat added endpoint should be detected at descriptor level")
        assert feat["descriptor_spurious"] == 0, "feat should have no descriptor spurious"
        assert feat["vs_true"]["f1"] == 1.0, "feat should score a perfect F1 vs true"
        assert feat["vs_expected"]["f1"] == 1.0, "feat should match the manifest expected"

        # drop: the removed module must be detected despite the id churn.
        drop = by_name["drop"]
        removed_desc = ("module", "util", "src/util.js")
        assert drop["detected_removed"].get(removed_desc, 0) == 1, (
            "drop removed module should be detected at descriptor level")
        assert drop["vs_true"]["f1"] == 1.0, "drop should score a perfect F1 vs true"

        # Aggregate sanity: everything detected -> perfect, nothing spurious.
        assert out["aggregate_f1"] == 1.0, "aggregate F1 should be perfect"
        assert out["noise_floor"] == 0.0, "noise floor should be 0"

        print("self-test: OK -- identical pair has descriptor-spurious 0 (raw-id "
              f"spurious {fmt['raw_id_spurious']}), added/removed detected under "
              "full id churn.")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        manifest_path = Path(argv[1])
        if not manifest_path.exists():
            print(f"manifest not found: {manifest_path}", file=sys.stderr)
            return 2
        run_manifest(manifest_path)
        return 0
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
