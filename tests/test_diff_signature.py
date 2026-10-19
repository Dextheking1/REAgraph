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


def test_ipc_channel_source_file_is_not_part_of_identity():
    # An IPC channel spans main (handle) + preload (invoke); REA attributes the
    # channel node to one file or the other from run to run. Its name is the
    # stable identity, so a source-file flip on an untouched channel must NOT
    # register as a change.
    a = _graph([_n("a1", "ipc-channel", "notes:list", "src/preload.js")])
    b = _graph([_n("z1", "ipc-channel", "notes:list", "src/main.js")])

    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_added"] == 0
    assert sig["summary"]["nodes_removed"] == 0
    assert sig["summary"]["unchanged_nodes"] == 1

    # A genuinely different channel name is still a change.
    c = _graph([_n("z1", "ipc-channel", "notes:save", "src/main.js")])
    sig2 = diff.diff(a, c, signature=diff.rea_signature)
    assert sig2["summary"]["nodes_added"] == 1
    assert sig2["summary"]["nodes_removed"] == 1


def test_normalize_label_strips_only_ranges():
    assert diff.normalize_label("BrowserWindow@7:14-9:4") == "BrowserWindow"
    assert diff.normalize_label("handler@24:33-28:1") == "handler"
    assert diff.normalize_label("telemetry:send") == "telemetry:send"  # not a range
    assert diff.normalize_label("base64") == "base64"  # digits, not a range
    assert diff.normalize_label(None) is None


def test_normalize_label_strips_trailing_at_span():
    # The @-prefixed trailing span REA emits is removed (single position or range).
    assert diff.normalize_label("handler@24:33-28:1") == "handler"
    assert diff.normalize_label("BrowserWindow@7:14-9:4") == "BrowserWindow"
    assert diff.normalize_label("loadConfig@3:0") == "loadConfig"  # single position
    # A bare position RANGE that is the whole label is an anonymous node -> dropped.
    assert diff.normalize_label("7:14-9:4") == ""
    # And the @-only form (anonymous node) drops to empty too.
    assert diff.normalize_label("@7:14-9:4") == ""


def test_normalize_label_preserves_interior_colon_numbers():
    # These are NOT trailing @-spans and NOT whole-label ranges: an interior
    # "d:d" that is part of a real name must survive UNCHANGED (the latent trap:
    # the old regex mangled every one of these).
    assert diff.normalize_label("1:1") == "1:1"  # a lone position / verse ref
    assert diff.normalize_label("Genesis 1:1") == "Genesis 1:1"
    assert diff.normalize_label("3:14") == "3:14"
    assert diff.normalize_label("2:0") == "2:0"  # a version string
    assert diff.normalize_label("base64") == "base64"
    assert diff.normalize_label("config:3:14:reload") == "config:3:14:reload"


def test_two_builds_differing_only_by_at_span_match_under_signature():
    # The whole point: a node whose ONLY change is its @line:col source span
    # (code moved) must still match across builds under rea_signature.
    a = _graph([_n("a1", "function", "handler@24:33-28:1", "src/ipc.js")])
    b = _graph([_n("z1", "function", "handler@40:12-44:7", "src/ipc.js")])

    sig = diff.diff(a, b, signature=diff.rea_signature)
    assert sig["summary"]["nodes_added"] == 0
    assert sig["summary"]["nodes_removed"] == 0
    assert sig["summary"]["unchanged_nodes"] == 1


def test_interior_colon_number_is_not_mangled_in_signature():
    # Two builds where a label legitimately contains "1:1": the signature must
    # match them (not drop the "1:1"), and must NOT collide "1:1" with "2:2".
    a = _graph([_n("a1", "verse", "Genesis 1:1", "book.txt")])
    b = _graph([_n("z1", "verse", "Genesis 1:1", "book.txt")])
    same = diff.diff(a, b, signature=diff.rea_signature)
    assert same["summary"]["unchanged_nodes"] == 1
    assert same["summary"]["nodes_added"] == 0

    c = _graph([_n("z1", "verse", "Genesis 2:2", "book.txt")])
    diffed = diff.diff(a, c, signature=diff.rea_signature)
    assert diffed["summary"]["nodes_added"] == 1
    assert diffed["summary"]["nodes_removed"] == 1
