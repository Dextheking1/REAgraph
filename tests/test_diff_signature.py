"""Signature-mode diff: byte-agnostic version diffing across unstable node ids.

REA's node ids are per-build (they embed a build hash, and some identities are
context-dependent), so an id-keyed diff of two builds reports nearly everything as
changed -- the #4520 shape, reintroduced by the id. `diff(a, b, signature=...)`
matches nodes by a build-stable signature (kind + range-normalized label + source
file), with duplicates disambiguated by a stable ordinal, so a reformatted but
semantically identical build reads as UNCHANGED and only real movement shows up.
"""

from reagraph import diff, memory


def _n(nid, kind, label, source):
    return {"id": nid, "rea_kind": kind, "label": label, "source_file": source}


def _churn(nodes, prefix):
    """Copy nodes with brand-new ids (per-build churn) but identical descriptors."""
    return [dict(n, id=f"{prefix}{i}") for i, n in enumerate(nodes, 1)]


def _graph(nodes, links=None):
    g = memory.empty_graph()
    g["nodes"] = nodes
    g["links"] = links or []
    return g


def test_identical_builds_with_churned_ids_read_as_unchanged():
    base_nodes = [
        _n("a1", "module", "main", "src/main.js"),
        _n("a2", "endpoint", "api/login", "src/auth.js"),
        _n("a3", "function", "validate", "src/auth.js"),
    ]
    a = _graph(base_nodes)
    b = _graph(_churn(base_nodes, "z"))  # every id different, same meaning

    # id-keyed diff: everything churned -> all added + all removed (the bug).
    id_diff = diff.diff(a, b)
    assert id_diff["summary"]["nodes_added"] == 3
    assert id_diff["summary"]["nodes_removed"] == 3

    # signature diff: identical meaning -> zero change.
    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_added"] == 0
    assert sig["summary"]["nodes_removed"] == 0
    assert sig["summary"]["rewired_nodes"] == 0
    assert sig["summary"]["unchanged_nodes"] == 3


def test_signature_detects_a_real_addition_under_churn():
    base_nodes = [
        _n("a1", "module", "main", "src/main.js"),
        _n("a2", "endpoint", "api/login", "src/auth.js"),
    ]
    a = _graph(base_nodes)
    b_nodes = _churn(base_nodes, "z") + [
        _n("z99", "endpoint", "evil.example/exfil", "src/main.js")
    ]
    b = _graph(b_nodes)

    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_added"] == 1
    assert sig["summary"]["nodes_removed"] == 0
    # The reported id is the signature key of the added node.
    assert any("evil.example/exfil" in nid for nid in sig["nodes_added"])


def test_signature_detects_a_real_removal_under_churn():
    base_nodes = [
        _n("a1", "module", "main", "src/main.js"),
        _n("a2", "module", "util", "src/util.js"),
    ]
    a = _graph(base_nodes)
    b = _graph(_churn([base_nodes[0]], "z"))  # util removed

    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_removed"] == 1
    assert sig["summary"]["nodes_added"] == 0
    assert any("util" in nid for nid in sig["nodes_removed"])


def test_label_source_range_is_normalized_for_matching():
    # The same entity whose label embeds a source line:col range that SHIFTED
    # (code moved) must still match.
    a = _graph([_n("a1", "browser-window", "BrowserWindow@7:14-9:4", "src/main.js")])
    b = _graph([_n("z1", "browser-window", "BrowserWindow@10:14-14:4", "src/main.js")])

    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_added"] == 0
    assert sig["summary"]["nodes_removed"] == 0
    assert sig["summary"]["unchanged_nodes"] == 1


def test_multiset_matching_counts_duplicates():
    # Two nodes that share a signature in A, one in B -> exactly one removed.
    a = _graph([
        _n("a1", "module", "https", "src/auth.js"),
        _n("a2", "module", "https", "src/auth.js"),
    ])
    b = _graph([_n("z1", "module", "https", "src/auth.js")])

    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_removed"] == 1
    assert sig["summary"]["nodes_added"] == 0
    assert sig["summary"]["unchanged_nodes"] == 1


def test_default_diff_is_unchanged_backcompat():
    # Without signature=, behavior is exactly the id-keyed diff (no regression).
    a = _graph([_n("a1", "module", "main", "src/main.js")])
    b = _graph([_n("a1", "module", "main", "src/main.js")])
    d = diff.diff(a, b)
    assert d["summary"]["unchanged_nodes"] == 1
    assert d["summary"]["nodes_added"] == 0


def test_normalize_label_strips_only_ranges():
    assert diff.normalize_label("BrowserWindow@7:14-9:4") == "BrowserWindow"
    assert diff.normalize_label("handler@24:33-28:1") == "handler"
    assert diff.normalize_label("telemetry:send") == "telemetry:send"  # not a range
    assert diff.normalize_label("base64") == "base64"  # digits, not a range
    assert diff.normalize_label(None) is None
