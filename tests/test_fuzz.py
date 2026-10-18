"""Fuzz / robustness proof for REAgraph's core.

Goal: show the core never crashes on adversarial / malformed input and is
deterministic. These modules are designed FAIL-OPEN / byte-agnostic; this suite
is the adversarial proof of that (and surfaces, via ``xfail``, the places where
the fail-open contract does NOT yet hold).

No external fuzzing dependency (no hypothesis): we roll our own *seeded* random
generators with the stdlib ``random`` module, so every case is fully
reproducible -- a failure prints the exact seed that produced it.

Two generators feed every target function:

  * ``gen_valid``     -- well-formed node-link graphs with every nasty-but-legal
                         shape: 0/1/small/few-hundred nodes, self-loops, duplicate
                         edges, duplicate node ids, dangling endpoints, mixed
                         confidences, labels carrying ``@line:col`` ranges.
  * ``gen_malformed`` -- graphs that stay inside the documented fail-open surface
                         (a dict with ``nodes``/``links`` lists of dicts) but junk
                         everything inside: missing ids, missing endpoints, wrong
                         (but hashable) scalar types, empty strings, a huge god
                         node, occasional ``edges`` spelling / missing containers.

Harsher, out-of-contract inputs (``None``, non-dict top level, non-dict entries,
unhashable ids) are driven separately: the adapter functions
(``compare.to_report``, ``rea_adapter.to_fragment``) survive ALL of them, and the
graph-analysis functions are fail-open on a non-dict top level and non-dict
node/edge entries -- they return the documented empty-ish shape instead of
raising (see ``test_graph_funcs_fail_open`` and ``test_diff_id_mode_mixed_type_ids``).

Targets: memory (empty_graph/merge/stats/edge_key/load/save), diff
(diff / rea_signature / normalize_label), summarize, coverage, align, attest,
compare.to_report, rea_adapter.to_fragment.
"""

from __future__ import annotations

import copy
import json
import random
import time

import pytest

from reagraph import (
    align,
    attest,
    compare,
    coverage,
    diff,
    memory,
    rea_adapter,
    summarize,
)

# --------------------------------------------------------------------------- #
# Seeds + sizes. Kept small so the whole file runs in a couple of seconds.
# --------------------------------------------------------------------------- #
SEEDS = list(range(23))
SMALL_SIZES = [0, 1, 3, 8, 25]
SCALE_SIZE = 300

_NAMES = ["main", "auth", "crypto", "base64", "parse", "render", "init",
          "handle", "connect", "load", "save", "ipcChannel", "sendBeacon"]
_KINDS = ["function", "class", "module", "method", "ipc-channel",
          "native-export", "variable", None]
_SOURCES = ["src/a.js", "src/b.js", "lib/c.ts", "preload.js", "", None]
_RELATIONS = ["calls", "imports", "invokes", "handles", "sends", "contains", ""]
_VALID_CONF = ["EXTRACTED", "INFERRED", "", None]
_MALFORMED_CONF = ["EXTRACTED", "INFERRED", "", None, "weird", 7, True]


def _canon(obj) -> str:
    """Deterministic canonical form for output-equality (determinism) checks."""
    return json.dumps(obj, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# 1. Seeded VALID-graph generator.
# --------------------------------------------------------------------------- #
def _label(rng: random.Random) -> str:
    base = rng.choice(_NAMES)
    r = rng.random()
    if r < 0.25:  # label carrying a full @line:col-line:col range
        return f"{base}@{rng.randint(1, 99)}:{rng.randint(0, 40)}-" \
               f"{rng.randint(1, 99)}:{rng.randint(0, 40)}"
    if r < 0.40:  # label carrying a single @line:col
        return f"{base}@{rng.randint(1, 99)}:{rng.randint(0, 40)}"
    if r < 0.50:
        return ""
    return base


def gen_valid(rng: random.Random, n: int, *, unique_ids: bool = False) -> dict:
    """A well-formed node-link graph with adversarial-but-legal structure."""
    nodes = []
    ids: list = []
    for i in range(n):
        if not unique_ids and ids and rng.random() < 0.15:
            nid = rng.choice(ids)  # duplicate node id
        else:
            nid = f"n{i}"
        ids.append(nid)
        node = {
            "id": nid,
            "label": _label(rng),
            "rea_kind": rng.choice(_KINDS),
            "source_file": rng.choice(_SOURCES),
            "confidence": rng.choice(_VALID_CONF),
        }
        nodes.append(node)

    links = []
    n_edges = 0 if n == 0 else rng.randint(0, n * 2)
    known = ids or ["x"]
    for _ in range(n_edges):
        roll = rng.random()
        if roll < 0.12 and ids:          # self-loop
            s = t = rng.choice(known)
        elif roll < 0.25:                # dangling endpoint (missing node)
            s = rng.choice(known)
            t = f"ghost{rng.randint(0, 5)}"
        else:
            s = rng.choice(known)
            t = rng.choice(known)
        links.append({
            "source": s,
            "target": t,
            "relation": rng.choice(_RELATIONS),
            "confidence": rng.choice(_VALID_CONF),
        })
    # Deliberately include duplicate edges by re-appending a few.
    for _ in range(min(3, len(links))):
        links.append(dict(rng.choice(links)))

    return {"directed": True, "multigraph": False, "nodes": nodes, "links": links}


# --------------------------------------------------------------------------- #
# 2. Seeded MALFORMED generator (inside the fail-open surface: dict top level,
#    nodes/links are lists of dicts, every field a *hashable* scalar or missing).
# --------------------------------------------------------------------------- #
def _junk_endpoint(rng: random.Random) -> str:
    # Endpoints / relations / ids are kept STRINGS (possibly empty) -- the
    # realistic REA contract, under which every function (incl. diff's id-keyed
    # sort) is fail-open. Mixed-type / None ids are driven by a dedicated xfail
    # characterization below, since diff.diff sorts raw id/edge sets.
    return rng.choice(["", "m0", "m1", "ghost", "GOD", "x" * rng.randint(0, 3)])


def gen_malformed(rng: random.Random, n: int) -> dict:
    """A junk-but-shaped graph: the documented fail-open surface, maximally nasty.

    Ids / edge endpoints / relations are strings (the realistic contract); every
    OTHER field is adversarially wrong-typed (int/bool/None labels, bogus
    confidences, wrong-type kinds/sources), keys are randomly omitted, endpoints
    dangle, ids duplicate, and a huge god node is sometimes attached."""
    nodes = []
    for i in range(n):
        node: dict = {}
        if rng.random() < 0.8:                    # sometimes omit "id" entirely
            node["id"] = rng.choice([f"m{i}", "", f"dup{i % 2}"])
        if rng.random() < 0.7:
            node["label"] = rng.choice([_label(rng), "", None, 5, True])
        if rng.random() < 0.6:
            node["rea_kind"] = rng.choice(_KINDS + [7, ""])
        if rng.random() < 0.6:
            node["source_file"] = rng.choice(_SOURCES + [9])
        if rng.random() < 0.7:
            node["confidence"] = rng.choice(_MALFORMED_CONF)
        nodes.append(node)

    # A huge single "god node" sometimes: one node everything points at.
    god = None
    if n and rng.random() < 0.3:
        god = "GOD"
        nodes.append({"id": god, "label": "god", "confidence": "EXTRACTED"})

    links = []
    n_edges = 0 if n == 0 else rng.randint(0, n * 2)
    for _ in range(n_edges):
        # source/target/relation are always present strings so that diff's
        # id-mode edge-key sort stays orderable (its None/missing-endpoint gap is
        # characterized separately). Everything else on the edge is junk.
        edge = {
            "source": god if (god and rng.random() < 0.5) else _junk_endpoint(rng),
            "target": god if (god and rng.random() < 0.5) else _junk_endpoint(rng),
            "relation": rng.choice(_RELATIONS),
        }
        if rng.random() < 0.6:
            edge["confidence"] = rng.choice(_MALFORMED_CONF)
        if rng.random() < 0.3:
            edge["context"] = rng.choice(["rea:calls", "", None, 7])
        links.append(edge)

    graph: dict = {}
    r = rng.random()
    if r < 0.8:
        graph["nodes"] = nodes
    if r < 0.6 or r > 0.85:
        graph["links"] = links
    elif rng.random() < 0.5:
        graph["edges"] = links            # the alternate spelling memory tolerates
    else:
        graph["links"] = links
    if rng.random() < 0.5:
        graph["directed"] = rng.choice([True, False, None])
    return graph


# --------------------------------------------------------------------------- #
# JAG / normalized-result generators for the two adapter functions.
# --------------------------------------------------------------------------- #
def gen_jag_result(rng: random.Random, n: int) -> dict:
    """A plausible-ish REA analyze_javascript_application result (valid + junk)."""
    nodes = []
    for i in range(n):
        obs = [{
            "label": rng.choice([_label(rng), None, ""]),
            "evidence": {
                "state": rng.choice(["observed", "inferred", "unknown",
                                     "unavailable", None]),
                "location": {"value": {"source": rng.choice(_SOURCES)}},
            },
        }]
        nodes.append({
            "node_id": rng.choice([f"jag_{i}", i, None]),
            "kind": rng.choice(_KINDS),
            "observations": rng.choice([obs, [], None]),
        })
    edges = []
    for _ in range(0 if n == 0 else rng.randint(0, n)):
        edges.append({
            "source_node_id": rng.choice([f"jag_{rng.randint(0, max(0, n - 1))}", None]),
            "target_node_id": rng.choice([f"jag_{rng.randint(0, max(0, n - 1))}", None]),
            "relation": rng.choice(_RELATIONS + ["loads", "calls"]),
            "evidence": {"state": rng.choice(["observed", "inferred",
                                              "unavailable", None])},
        })
    graph = {"schema": "JavaScriptApplicationGraph", "nodes": nodes, "edges": edges}
    return {"structuredContent": {"normalized_result": {"graph": graph}}}


def gen_normalized_result(rng: random.Random, n: int) -> dict:
    """A plausible-ish REA compare_application_versions normalized_result."""
    statuses = ["unchanged", "added", "removed", "changed", "unknown"]
    items = []
    for i in range(n):
        items.append({
            "item_id": f"it{i}",
            "node_kind": rng.choice(_KINDS),
            "status": rng.choice(statuses + [None, "weird"]),
            "left_node_id": rng.choice([f"L{i}", None]),
            "right_node_id": rng.choice([f"R{i}", None]),
            "dimensions": rng.choice([[], ["content"], None]),
        })
    return {
        "comparison_id": rng.choice(["cmp-1", None, 5]),
        "summary": rng.choice([{s: rng.randint(0, 9) for s in statuses}, {}, None]),
        "items": rng.choice([items, None, "junk"]),
        "matching": rng.choice([{"exact_node_identity": 3}, {}, None]),
        "limitations": rng.choice([["partial"], None]),
    }


# --------------------------------------------------------------------------- #
# Shape assertions.
# --------------------------------------------------------------------------- #
_DIFF_KEYS = {"nodes_added", "nodes_removed", "edges_added", "edges_removed",
              "rewired_nodes", "unchanged_nodes", "summary"}
_DIFF_SUMMARY_KEYS = {"nodes_added", "nodes_removed", "edges_added",
                      "edges_removed", "rewired_nodes", "unchanged_nodes"}


def _assert_diff_shape(out, where):
    assert isinstance(out, dict), where
    assert _DIFF_KEYS <= set(out), f"{where}: missing keys {_DIFF_KEYS - set(out)}"
    for k in ("nodes_added", "nodes_removed", "edges_added", "edges_removed",
              "rewired_nodes", "unchanged_nodes"):
        assert isinstance(out[k], list), f"{where}: {k} not a list"
    assert _DIFF_SUMMARY_KEYS <= set(out["summary"]), where


def _assert_summarize_shape(out, where):
    assert isinstance(out, dict), where
    for k in ("nodes", "edges", "edge_confidence", "num_components",
              "god_nodes", "largest_clusters"):
        assert k in out, f"{where}: missing {k}"
    assert isinstance(out["god_nodes"], list) and isinstance(out["largest_clusters"], list), where
    assert isinstance(out["edge_confidence"], dict), where


def _assert_coverage_shape(out, where):
    assert isinstance(out, dict), where
    assert set(out) >= {"nodes", "edges", "ghost_targets", "verified_fraction"}, where
    assert set(out["nodes"]) >= {"total", "confidence"}, where
    assert set(out["edges"]) >= {"total", "confidence"}, where
    assert isinstance(out["ghost_targets"], list), where
    assert isinstance(out["verified_fraction"], float), where


def _assert_stats_shape(out, where):
    assert isinstance(out, dict), where
    assert set(out) >= {"nodes", "edges", "edge_confidence"}, where


def _assert_align_shape(out, where):
    assert isinstance(out, dict), where
    assert set(out) >= {"matches", "src_only", "bin_only", "stats"}, where
    assert isinstance(out["matches"], dict), where


def _assert_attest_shape(out, where):
    assert isinstance(out, dict), where
    assert set(out) >= {"integrity", "shipped_unexplained", "residual_clusters",
                        "unexplained_edges", "source_only", "matched",
                        "bin_nodes", "src_nodes"}, where
    assert isinstance(out["integrity"], float), where
    assert 0.0 <= out["integrity"] <= 1.0, where


def _assert_fragment_shape(out, where):
    assert isinstance(out, dict), where
    assert "nodes" in out and "links" in out, where
    assert isinstance(out["nodes"], list) and isinstance(out["links"], list), where


def _assert_is_list(out, where):
    assert isinstance(out, list), where


def _assert_is_str(out, where):
    assert isinstance(out, str), where


def _assert_report_shape(out, where):
    assert isinstance(out, dict), where
    assert set(out) >= {"comparison_id", "summary", "items", "matching",
                        "limitations"}, where
    assert isinstance(out["summary"], dict) and isinstance(out["items"], dict), where


def _call(label, seed, inp, fn):
    """Run ``fn(inp)``; turn any exception into an assertion that prints the seed."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the whole point is to catch everything
        raise AssertionError(
            f"{label} RAISED on seed={seed}: {type(e).__name__}: {e}\n"
            f"  input (truncated): {_canon(inp)[:400]}"
        ) from e


# --------------------------------------------------------------------------- #
# NEVER-RAISES + SHAPE, across both generators and many seeds.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", SMALL_SIZES)
@pytest.mark.parametrize("gen", ["valid", "malformed"])
def test_never_raises_and_shape(gen, size, seed):
    rng = random.Random(seed)
    g = gen_valid(rng, size) if gen == "valid" else gen_malformed(rng, size)
    rng2 = random.Random(seed * 7 + 1)
    g2 = gen_valid(rng2, size) if gen == "valid" else gen_malformed(rng2, size)
    lbl = f"[{gen}]"

    _assert_stats_shape(_call(f"{lbl} stats", seed, g, lambda: memory.stats(g)),
                        f"{lbl} stats seed={seed}")

    merged = _call(f"{lbl} merge", seed, g,
                   lambda: memory.merge(copy.deepcopy(g), copy.deepcopy(g2)))
    # merge's result must itself be a graph loadable by memory.stats.
    _assert_stats_shape(_call(f"{lbl} stats(merge)", seed, merged,
                              lambda: memory.stats(merged)),
                        f"{lbl} stats(merge) seed={seed}")

    for sig in (None, diff.rea_signature):
        name = f"{lbl} diff(sig={'rea' if sig else 'none'})"
        out = _call(name, seed, g, lambda s=sig: diff.diff(g, g2, signature=s))
        _assert_diff_shape(out, f"{name} seed={seed}")

    _assert_summarize_shape(
        _call(f"{lbl} summarize", seed, g, lambda: summarize.summarize(g)),
        f"{lbl} summarize seed={seed}")
    _assert_coverage_shape(
        _call(f"{lbl} coverage", seed, g, lambda: coverage.coverage(g)),
        f"{lbl} coverage seed={seed}")
    _call(f"{lbl} frontier", seed, g, lambda: coverage.frontier(g))
    _call(f"{lbl} gaps_report", seed, g, lambda: coverage.gaps_report(g))

    _assert_align_shape(
        _call(f"{lbl} align", seed, g, lambda: align.align(g, g2)),
        f"{lbl} align seed={seed}")
    _assert_attest_shape(
        _call(f"{lbl} attest", seed, g, lambda: attest.attest(g, g2)),
        f"{lbl} attest seed={seed}")

    # rea_signature / normalize_label on every node must never raise.
    for n in g.get("nodes", []):
        _call(f"{lbl} rea_signature", seed, n, lambda nn=n: diff.rea_signature(nn))
        _call(f"{lbl} normalize_label", seed, n,
              lambda nn=n: diff.normalize_label(nn.get("label")))
    for e in g.get("links", []):
        _call(f"{lbl} edge_key", seed, e, lambda ee=e: memory.edge_key(ee))


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", SMALL_SIZES)
def test_adapters_never_raise_and_shape(size, seed):
    rng = random.Random(seed)
    jag = gen_jag_result(rng, size)
    nr = gen_normalized_result(rng, size)
    _assert_fragment_shape(
        _call("to_fragment(jag)", seed, jag,
              lambda: rea_adapter.to_fragment("analyze_javascript_application", jag)),
        f"to_fragment seed={seed}")
    # Fragment must merge straight into a graph that stats can read.
    frag = rea_adapter.to_fragment("analyze_javascript_application", jag)
    _assert_stats_shape(memory.stats(memory.merge(memory.empty_graph(), frag)),
                        f"stats(merge(fragment)) seed={seed}")
    _assert_report_shape(
        _call("to_report", seed, nr, lambda: compare.to_report(nr)),
        f"to_report seed={seed}")


# --------------------------------------------------------------------------- #
# DETERMINISM: same input twice -> identical output (json canonical equal).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", SMALL_SIZES)
@pytest.mark.parametrize("gen", ["valid", "malformed"])
def test_determinism(gen, size, seed):
    rng = random.Random(seed)
    g = gen_valid(rng, size) if gen == "valid" else gen_malformed(rng, size)
    rng2 = random.Random(seed * 13 + 2)
    g2 = gen_valid(rng2, size) if gen == "valid" else gen_malformed(rng2, size)

    def twice(make):
        return _canon(make()) == _canon(make())

    assert twice(lambda: memory.stats(g)), f"stats nondeterministic seed={seed}"
    assert twice(lambda: memory.merge(copy.deepcopy(g), copy.deepcopy(g2))), \
        f"merge nondeterministic seed={seed}"
    for sig in (None, diff.rea_signature):
        assert twice(lambda s=sig: diff.diff(g, g2, signature=s)), \
            f"diff(sig={sig}) nondeterministic seed={seed}"
    assert twice(lambda: summarize.summarize(g)), f"summarize nondet seed={seed}"
    assert twice(lambda: coverage.coverage(g)), f"coverage nondet seed={seed}"
    assert twice(lambda: coverage.frontier(g)), f"frontier nondet seed={seed}"
    assert twice(lambda: coverage.gaps_report(g)), f"gaps_report nondet seed={seed}"
    assert twice(lambda: align.align(g, g2)), f"align nondet seed={seed}"
    assert twice(lambda: attest.attest(g, g2)), f"attest nondet seed={seed}"


@pytest.mark.parametrize("seed", SEEDS)
def test_adapter_determinism(seed):
    rng = random.Random(seed)
    jag = gen_jag_result(rng, 10)
    nr = gen_normalized_result(rng, 10)
    a = rea_adapter.to_fragment("analyze_javascript_application", jag)
    b = rea_adapter.to_fragment("analyze_javascript_application", jag)
    assert _canon(a) == _canon(b), f"to_fragment nondet seed={seed}"
    assert _canon(compare.to_report(nr)) == _canon(compare.to_report(nr)), \
        f"to_report nondet seed={seed}"


# --------------------------------------------------------------------------- #
# INVARIANTS.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", SMALL_SIZES)
def test_diff_identity(size, seed):
    """diff(g, g) reports no added/removed/rewired -- in both id and signature mode."""
    g = gen_valid(random.Random(seed), size)
    for sig in (None, diff.rea_signature):
        out = diff.diff(g, g, signature=sig)
        s = out["summary"]
        assert s["nodes_added"] == 0 and s["nodes_removed"] == 0, \
            f"diff identity nodes seed={seed} sig={sig}: {s}"
        assert s["edges_added"] == 0 and s["edges_removed"] == 0, \
            f"diff identity edges seed={seed} sig={sig}: {s}"
        assert s["rewired_nodes"] == 0, \
            f"diff identity rewired seed={seed} sig={sig}: {s}"


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", [1, 3, 8, 25])
def test_diff_signature_ignores_id_churn(size, seed):
    """Two graphs with identical (kind, normalized-label, source) multisets but
    fully churned node ids report ZERO node changes under signature=rea_signature."""
    rng = random.Random(seed)
    g = gen_valid(rng, size, unique_ids=True)

    id_map = {}
    for i, n in enumerate(g["nodes"]):
        id_map[n["id"]] = f"CHURN-{seed}-{i}-{rng.randint(0, 10**6)}"
    churned_nodes = []
    for n in g["nodes"]:                      # same order, same fields, new id
        m = dict(n)
        m["id"] = id_map[n["id"]]
        churned_nodes.append(m)
    churned_links = []
    for e in g["links"]:
        m = dict(e)
        if m.get("source") in id_map:
            m["source"] = id_map[m["source"]]
        if m.get("target") in id_map:
            m["target"] = id_map[m["target"]]
        churned_links.append(m)
    g2 = {"directed": True, "multigraph": False,
          "nodes": churned_nodes, "links": churned_links}

    # Sanity: ids really did all change.
    assert not ({n["id"] for n in g["nodes"]} & {n["id"] for n in g2["nodes"]}) \
        or size == 0, f"churn did not change ids seed={seed}"

    out = diff.diff(g, g2, signature=diff.rea_signature)
    assert out["summary"]["nodes_added"] == 0, \
        f"signature diff saw added nodes under pure id churn seed={seed}: {out['summary']}"
    assert out["summary"]["nodes_removed"] == 0, \
        f"signature diff saw removed nodes under pure id churn seed={seed}: {out['summary']}"

    # Plain id-keyed diff, by contrast, sees everything churn (unless empty).
    if size:
        plain = diff.diff(g, g2)
        assert plain["summary"]["nodes_added"] == size, \
            f"id-keyed diff baseline seed={seed}: {plain['summary']}"


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", SMALL_SIZES)
def test_merge_idempotent(size, seed):
    """Merging the same fragment twice does not grow node/edge counts."""
    frag = gen_valid(random.Random(seed), size)
    base = memory.empty_graph()
    memory.merge(base, copy.deepcopy(frag))
    after_one = memory.stats(base)
    memory.merge(base, copy.deepcopy(frag))
    after_two = memory.stats(base)
    assert after_one["nodes"] == after_two["nodes"], \
        f"merge grew nodes on re-merge seed={seed}: {after_one} -> {after_two}"
    assert after_one["edges"] == after_two["edges"], \
        f"merge grew edges on re-merge seed={seed}: {after_one} -> {after_two}"


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("size", SMALL_SIZES)
def test_save_load_roundtrip(size, seed, tmp_path):
    """save then load returns an equal graph (on valid graphs)."""
    g = gen_valid(random.Random(seed), size)
    p = tmp_path / f"g_{seed}_{size}.json"
    memory.save(g, p)
    loaded = memory.load(p)
    assert loaded == g, f"save/load not round-trip seed={seed} size={size}"


def test_load_missing_file_is_empty_graph(tmp_path):
    g = memory.load(tmp_path / "does_not_exist.json")
    assert g == memory.empty_graph()


# --------------------------------------------------------------------------- #
# SCALE smoke: a few-hundred-node graph runs the core pipeline well under 1s.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_scale_smoke(seed):
    rng = random.Random(seed)
    g = gen_valid(rng, SCALE_SIZE)
    g2 = gen_valid(random.Random(seed + 100), SCALE_SIZE)
    t0 = time.perf_counter()
    base = memory.merge(memory.empty_graph(), copy.deepcopy(g))
    memory.merge(base, copy.deepcopy(g2))
    d = diff.diff(g, g2)
    s = summarize.summarize(g)
    c = coverage.coverage(g)
    elapsed = time.perf_counter() - t0
    _assert_diff_shape(d, "scale diff")
    _assert_summarize_shape(s, "scale summarize")
    _assert_coverage_shape(c, "scale coverage")
    # Loose bound: this is ~300 nodes; it should be far under a second, but we
    # allow generous slack so the assertion is about "completes", not microbench.
    assert elapsed < 2.0, f"scale pipeline too slow: {elapsed:.3f}s (seed={seed})"


# --------------------------------------------------------------------------- #
# The genuinely FAIL-OPEN functions survive ALL out-of-contract junk.
# --------------------------------------------------------------------------- #
_HARSH = {
    "None": None,
    "int": 5,
    "str": "junk",
    "list": [1, 2, 3],
    "nodes_not_list": {"nodes": 5, "links": 5},
    "entries_not_dict": {"nodes": [None, 5, "x", {}], "links": [None, 7, {}]},
    "unhashable_id": {"nodes": [{"id": {"deep": [1]}}],
                      "links": [{"source": [1], "target": {}}]},
    "nested_junk": {"nodes": [{"id": "a", "label": {"x": [1, {"y": 2}]}}],
                    "links": [{"source": "a", "target": "a", "relation": [1]}]},
    "empty": {},
}


@pytest.mark.parametrize("key", list(_HARSH))
def test_adapters_are_fully_fail_open(key):
    """compare.to_report and rea_adapter.to_fragment survive every harsh input."""
    harsh = _HARSH[key]
    out = rea_adapter.to_fragment("analyze_javascript_application", harsh)
    _assert_fragment_shape(out, f"to_fragment harsh={key}")
    out = rea_adapter.to_fragment(harsh, harsh)   # harsh tool name too
    _assert_fragment_shape(out, f"to_fragment harsh-tool={key}")
    rep = compare.to_report(harsh)
    _assert_report_shape(rep, f"to_report harsh={key}")


# --------------------------------------------------------------------------- #
# FAIL-OPEN graph-analysis functions: these are now fail-open against
# out-of-contract inputs (non-dict top level / non-dict entries) -- they return
# the documented empty-ish shape instead of raising. (Previously pinned xfail as
# a known gap; the gap is fixed, so these are ordinary passing assertions.)
# --------------------------------------------------------------------------- #
_GRAPH_FUNCS = {
    "memory.merge": lambda g: memory.merge(g, memory.empty_graph()),
    "memory.stats": memory.stats,
    "diff(id)": lambda g: diff.diff(g, g),
    "diff(sig)": lambda g: diff.diff(g, g, signature=diff.rea_signature),
    "summarize": summarize.summarize,
    "coverage": coverage.coverage,
    "frontier": coverage.frontier,
    "gaps_report": coverage.gaps_report,
    "align": lambda g: align.align(g, g),
    "attest": lambda g: attest.attest(g, g),
}
# The documented fail-open surface we assert against: non-dict top level (None)
# and non-dict entries inside nodes/links. id-specific robustness (mixed-type /
# None ids and edge keys) is characterized separately below.
_GAP_INPUTS = ["None", "entries_not_dict"]

# Per-function assertion that the returned value has the documented shape, so we
# prove "does not raise AND returns the documented empty-ish shape", not merely
# "does not raise".
_GAP_SHAPE = {
    "memory.merge": lambda out: _assert_stats_shape(memory.stats(out), "merge->stats"),
    "memory.stats": lambda out: _assert_stats_shape(out, "stats"),
    "diff(id)": lambda out: _assert_diff_shape(out, "diff(id)"),
    "diff(sig)": lambda out: _assert_diff_shape(out, "diff(sig)"),
    "summarize": lambda out: _assert_summarize_shape(out, "summarize"),
    "coverage": lambda out: _assert_coverage_shape(out, "coverage"),
    "frontier": lambda out: _assert_is_list(out, "frontier"),
    "gaps_report": lambda out: _assert_is_str(out, "gaps_report"),
    "align": lambda out: _assert_align_shape(out, "align"),
    "attest": lambda out: _assert_attest_shape(out, "attest"),
}

_GAP_PARAMS = [
    (fname, iname) for fname in _GRAPH_FUNCS for iname in _GAP_INPUTS
]


@pytest.mark.parametrize("fname,iname", _GAP_PARAMS)
def test_graph_funcs_fail_open(fname, iname):
    """The graph-analysis functions are fail-open on out-of-contract input: a
    non-dict top level or non-dict node/edge entries return the documented
    empty-ish shape, never raise."""
    fn = _GRAPH_FUNCS[fname]
    out = fn(_HARSH[iname])   # must NOT raise -- fail-open contract
    _GAP_SHAPE[fname](out)    # ...AND return the documented shape


# A well-shaped graph (dict + list-of-dict nodes/links, hashable ids) whose ids
# and edge fields are of MIXED, non-orderable types.
_MIXED_ID_GRAPH = {
    "directed": True, "multigraph": False,
    "nodes": [{"id": 0, "label": "a"}, {"id": "b", "label": "b"},
              {"id": None, "label": "c"}],
    "links": [{"source": 0, "target": "b", "relation": "calls"},
              {"source": "b", "target": 0, "relation": None},
              {"source": None, "target": "b", "relation": "x"}],
}


def test_diff_id_mode_mixed_type_ids():
    """diff.diff (id mode) is fail-open on mixed-type / None ids and edge keys:
    its node-id and edge-key sorts use key=str, so a graph whose ids/relations are
    not mutually orderable diffs without raising and returns the documented shape."""
    out = diff.diff(_MIXED_ID_GRAPH, _MIXED_ID_GRAPH)
    _assert_diff_shape(out, "diff(id) mixed ids")


def test_diff_signature_mode_survives_mixed_type_ids():
    """Signature mode keys on string signatures, so it IS fail-open on mixed ids."""
    out = diff.diff(_MIXED_ID_GRAPH, _MIXED_ID_GRAPH, signature=diff.rea_signature)
    _assert_diff_shape(out, "diff(sig) mixed ids")


# A graph with edges missing / None endpoints and relations, and nodes missing
# or None ids -- the "silent target" fail-open surface. Every function EXCEPT
# diff's id-mode sort handles this; prove that here.
_MISSING_ENDPOINT_GRAPH = {
    "directed": True, "multigraph": False,
    "nodes": [{"id": "a", "confidence": "EXTRACTED"}, {"label": "no id"},
              {"id": None}],
    "links": [{"relation": "calls"},                       # no source/target
              {"source": "a"}, {"target": "a"},            # half-dangling
              {"source": None, "target": None, "relation": None}],
}


def test_non_diff_funcs_tolerate_missing_endpoints():
    """merge/stats/summarize/coverage/align/attest are fail-open on edges/nodes
    with missing or None ids/endpoints (diff id-mode is the documented exception)."""
    g = _MISSING_ENDPOINT_GRAPH
    _assert_stats_shape(memory.stats(g), "missing-endpoint stats")
    _assert_stats_shape(memory.stats(memory.merge(memory.empty_graph(),
                                                  copy.deepcopy(g))),
                        "missing-endpoint merge")
    _assert_summarize_shape(summarize.summarize(g), "missing-endpoint summarize")
    _assert_coverage_shape(coverage.coverage(g), "missing-endpoint coverage")
    coverage.frontier(g)
    coverage.gaps_report(g)
    _assert_align_shape(align.align(g, g), "missing-endpoint align")
    _assert_attest_shape(attest.attest(g, g), "missing-endpoint attest")
    # diff signature-mode also copes (string signatures).
    _assert_diff_shape(diff.diff(g, g, signature=diff.rea_signature),
                       "missing-endpoint diff(sig)")
