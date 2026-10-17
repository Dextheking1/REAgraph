"""Build-stable node identity, tested against the real REA `identity` block shapes.

No REA/MCP server is needed: the fixtures reproduce the exact `identity` shapes a
REA JavaScript Application Graph attaches to each node kind (verified against the
`rea-agents` server). The core problem these tests pin down is that REA's
`node_id` embeds the BUILD's artifact digest, so we assert that
`reagraph.rea_identity` derives an id that survives a rebuild: volatile per-build
hashes are dropped, a genuine content digest is kept, source line:col ranges are
normalized out, and nothing ever raises on junk.
"""

from reagraph import rea_identity


# --- real identity fixtures (exact shapes) -----------------------------------


def _javascript_module(artifact_sha="a" * 64, key="external:fs"):
    return {
        "strategy": "artifact-local-key",
        "stability": "artifact-version",
        "artifact_sha256": artifact_sha,
        "namespace": "unresolved-semantic-module",
        "key": key,
    }


def _asar_entry(artifact_sha="b" * 64, path="src/main.js"):
    return {
        "strategy": "canonical-path",
        "stability": "artifact-version",
        "artifact_sha256": artifact_sha,
        "path": path,
    }


def _ipc_handler(artifact_sha="c" * 64, key="main:handle:telemetry:send:24:33-28:1"):
    return {
        "strategy": "artifact-local-key",
        "stability": "artifact-version",
        "artifact_sha256": artifact_sha,
        "namespace": "electron-ipc-handler",
        "key": key,
    }


def _browser_window(artifact_sha="d" * 64, key="7:14-9:4"):
    return {
        "strategy": "artifact-local-key",
        "stability": "artifact-version",
        "artifact_sha256": artifact_sha,
        "namespace": "electron-browser-window",
        "key": key,
    }


def _ipc_channel(observation_sha="e" * 64):
    return {
        "strategy": "observation-fingerprint",
        "stability": "observation-only",
        "observation_sha256": observation_sha,
        "scope": "electron-ipc-channel",
    }


def _javascript_asset(sha="f" * 64):
    return {"strategy": "content-digest", "stability": "global-exact", "sha256": sha}


# --- volatile per-build fields are dropped -----------------------------------


def test_artifact_sha256_dropped():
    # Same logical module in two builds: only artifact_sha256 differs.
    k1 = rea_identity.stable_key(_javascript_module(artifact_sha="1" * 64), "javascript-module")
    k2 = rea_identity.stable_key(_javascript_module(artifact_sha="2" * 64), "javascript-module")
    assert k1 == k2
    assert k1 is not None
    assert "1" * 64 not in k1 and "2" * 64 not in k1


def test_observation_sha256_dropped():
    k1 = rea_identity.stable_key(_ipc_channel(observation_sha="1" * 64), "ipc-channel")
    k2 = rea_identity.stable_key(_ipc_channel(observation_sha="2" * 64), "ipc-channel")
    assert k1 == k2
    assert k1 is not None
    assert "electron-ipc-channel" in k1
    assert "1" * 64 not in k1 and "2" * 64 not in k1


# --- global-exact content digest is KEPT -------------------------------------


def test_global_exact_sha256_kept_distinguishes_content():
    # Different content -> different keys.
    k_a = rea_identity.stable_key(_javascript_asset(sha="a" * 64), "javascript-asset")
    k_b = rea_identity.stable_key(_javascript_asset(sha="b" * 64), "javascript-asset")
    assert k_a != k_b
    assert "a" * 64 in k_a


def test_global_exact_sha256_identical_content_identical_key():
    k1 = rea_identity.stable_key(_javascript_asset(sha="c" * 64), "javascript-asset")
    k2 = rea_identity.stable_key(_javascript_asset(sha="c" * 64), "javascript-asset")
    assert k1 == k2


# --- range normalization -----------------------------------------------------


def test_pure_range_collapses_to_empty():
    assert rea_identity.normalize_range_tokens("7:14-9:4") == ""


def test_pure_range_key_falls_back_to_namespace():
    key = rea_identity.stable_key(_browser_window(key="7:14-9:4"), "browser-window")
    assert key is not None
    assert "electron-browser-window" in key
    assert "7:14-9:4" not in key
    # The entity is stable even when its span moves.
    moved = rea_identity.stable_key(_browser_window(key="40:2-42:8"), "browser-window")
    assert key == moved


def test_trailing_range_stripped_from_semantic_key():
    assert (
        rea_identity.normalize_range_tokens("main:handle:telemetry:send:24:33-28:1")
        == "main:handle:telemetry:send"
    )
    key = rea_identity.stable_key(_ipc_handler(key="main:handle:telemetry:send:24:33-28:1"), "ipc-handler")
    moved = rea_identity.stable_key(_ipc_handler(key="main:handle:telemetry:send:99:1-120:4"), "ipc-handler")
    assert "main:handle:telemetry:send" in key
    assert "24:33-28:1" not in key
    assert key == moved


def test_real_name_with_digits_is_left_intact():
    assert rea_identity.normalize_range_tokens("base64Encode") == "base64Encode"
    assert rea_identity.normalize_range_tokens("external:fs") == "external:fs"
    assert rea_identity.normalize_range_tokens("main:handle:telemetry:send") == "main:handle:telemetry:send"


def test_normalize_non_str_passthrough():
    assert rea_identity.normalize_range_tokens(None) is None


# --- stable_key None semantics -----------------------------------------------


def test_stable_key_none_when_no_stable_component():
    # Only a volatile per-build hash -> nothing to key on.
    assert rea_identity.stable_key({"strategy": "x", "stability": "artifact-version", "artifact_sha256": "z" * 64}) is None
    assert rea_identity.stable_key({}) is None
    assert rea_identity.stable_key(None) is None
    assert rea_identity.stable_key("not-a-dict") is None


def test_stable_key_deterministic_order_independent():
    a = {"strategy": "artifact-local-key", "namespace": "ns", "key": "x:y", "stability": "artifact-version"}
    b = {"stability": "artifact-version", "key": "x:y", "namespace": "ns", "strategy": "artifact-local-key"}
    assert rea_identity.stable_key(a, "k") == rea_identity.stable_key(b, "k")


# --- derive_stable_id --------------------------------------------------------


def test_derive_stable_id_uses_stable_key():
    node = {"node_id": "jag_node_" + "9" * 64, "kind": "javascript-module", "identity": _javascript_module()}
    sid = rea_identity.derive_stable_id(node)
    assert sid.startswith("rea:")
    assert "9" * 64 not in sid


def test_derive_stable_id_falls_back_to_node_id():
    node = {"node_id": "jag_node_raw", "kind": "javascript-module", "identity": {}}
    assert rea_identity.derive_stable_id(node) == "jag_node_raw"
    node2 = {"id": "plain_id"}  # no identity at all
    assert rea_identity.derive_stable_id(node2) == "plain_id"


def test_derive_stable_id_never_raises_on_junk():
    for junk in (None, {}, {"identity": None}, {"identity": "nope"}, {"identity": 5}, 42, "str"):
        assert isinstance(rea_identity.derive_stable_id(junk), str)


# --- remap_edges -------------------------------------------------------------


def test_remap_edges_rewrites_endpoints_and_passes_unmapped():
    nodes = [
        {"node_id": "raw_a", "kind": "javascript-module", "identity": _javascript_module(key="external:fs")},
        {"node_id": "raw_b", "kind": "asar-entry", "identity": _asar_entry(path="src/main.js")},
    ]
    edges = [
        {"edge_id": "e1", "source_node_id": "raw_a", "target_node_id": "raw_b", "relation": "imports"},
        {"edge_id": "e2", "source_node_id": "raw_a", "target_node_id": "external_unknown", "relation": "loads"},
    ]
    id_map, remapped = rea_identity.remap_edges(nodes, edges)

    assert id_map["raw_a"] == rea_identity.derive_stable_id(nodes[0])
    assert id_map["raw_b"] == rea_identity.derive_stable_id(nodes[1])

    assert remapped[0]["source_node_id"] == id_map["raw_a"]
    assert remapped[0]["target_node_id"] == id_map["raw_b"]
    # unmapped endpoint passes through unchanged
    assert remapped[1]["source_node_id"] == id_map["raw_a"]
    assert remapped[1]["target_node_id"] == "external_unknown"
    # original edges untouched (copies returned)
    assert edges[0]["source_node_id"] == "raw_a"


def test_remap_edges_deterministic():
    nodes = [
        {"node_id": "raw_a", "kind": "javascript-module", "identity": _javascript_module()},
        {"node_id": "raw_b", "kind": "asar-entry", "identity": _asar_entry()},
    ]
    edges = [{"source_node_id": "raw_a", "target_node_id": "raw_b", "relation": "imports"}]
    first = rea_identity.remap_edges(nodes, edges)
    second = rea_identity.remap_edges(nodes, edges)
    assert first == second


def test_remap_edges_handles_empty_and_junk():
    id_map, remapped = rea_identity.remap_edges([], [])
    assert id_map == {} and remapped == []
    id_map, remapped = rea_identity.remap_edges([None, {"identity": {}}], ["junk", {"source_node_id": "x"}])
    assert "junk" in remapped  # non-dict edge passes through


# --- the two-build scenario (the whole point) --------------------------------


def test_two_build_same_module_same_id_changed_module_differs():
    # Build 1 and build 2 of the SAME module: only artifact_sha256 changes.
    build1 = {
        "node_id": "jag_node_" + "1" * 64,
        "kind": "javascript-module",
        "identity": _javascript_module(artifact_sha="1" * 64, key="external:fs"),
    }
    build2 = {
        "node_id": "jag_node_" + "2" * 64,  # different node_id (embeds build digest)
        "kind": "javascript-module",
        "identity": _javascript_module(artifact_sha="2" * 64, key="external:fs"),
    }
    # A genuinely different module.
    other = {
        "node_id": "jag_node_" + "3" * 64,
        "kind": "javascript-module",
        "identity": _javascript_module(artifact_sha="3" * 64, key="external:path"),
    }

    id1 = rea_identity.derive_stable_id(build1)
    id2 = rea_identity.derive_stable_id(build2)
    id_other = rea_identity.derive_stable_id(other)

    assert id1 == id2  # unchanged entity keeps its id across builds
    assert id1 != id_other  # real change still shows up as a different id
    # and it did not just fall back to the volatile node_id
    assert id1.startswith("rea:")
