"""The live-ingest bridge guarantees, tested with a fake in-memory source.

No REA/Ghidra needed: a fake source yields three findings (one malformed) and we
assert the two selling points vs REA — crash-safety (graph saved after every
merge) and fail-open (one bad finding is skipped, not fatal) — plus confidence
upgrade across a stream (observed later promotes an earlier inferred fact).
"""

from pathlib import Path

from reagraph import bridge, memory


class FakeReaSource:
    """Yields JAG findings in order; the middle one is malformed/empty."""

    def findings(self):
        # 1. INFERRED: we merely guess check() calls net().
        yield {
            "nodes": [{"id": "check", "epistemic": "inferred"},
                      {"id": "net", "epistemic": "inferred"}],
            "edges": [{"source": "check", "target": "net",
                       "relation": "calls", "epistemic": "inferred"}],
        }
        # 2. Malformed: no nodes/links/edges. Must be skipped, not fatal.
        yield {"garbage": "no graph here"}
        # 3. OBSERVED: we actually see the same node and edge.
        yield {
            "nodes": [{"id": "check", "epistemic": "observed"},
                      {"id": "net", "epistemic": "observed"}],
            "edges": [{"source": "check", "target": "net",
                       "relation": "calls", "epistemic": "observed"}],
        }


def test_stream_merges_good_skips_bad_and_is_crash_safe(tmp_path):
    graph_path = tmp_path / "reagraph.json"

    # Crash-safety probe: after every merge the on-disk file must exist and be
    # loadable. We also record how many times on_merge fired (once per GOOD
    # finding — the bad one never reaches a merge).
    merges = []

    def on_merge(stats):
        assert graph_path.exists(), "graph must be saved after each merge"
        memory.load(graph_path)  # must be valid JSON on disk, not half-written
        merges.append(stats)

    final = bridge.ingest_stream(FakeReaSource(), graph_path=str(graph_path), on_merge=on_merge)

    # The bad finding was skipped (not fatal): only the 2 good findings merged.
    assert len(merges) == 2
    # Both good findings describe the same node/edge -> deduped, not duplicated.
    assert final["nodes"] == 2
    assert final["edges"] == 1
    # Confidence UPGRADED across the stream: observed (finding 3) promoted the
    # inferred fact from finding 1.
    assert final["edge_confidence"]["EXTRACTED"] == 1
    assert final["edge_confidence"]["INFERRED"] == 0

    # The upgrade order is observable in the saved graph too.
    g = memory.load(graph_path)
    check = next(n for n in g["nodes"] if n["id"] == "check")
    assert check["confidence"] == "EXTRACTED"


def test_confidence_only_upgrades_never_downgrades(tmp_path):
    graph_path = tmp_path / "g.json"
    # Observed first, then a later inferred re-observation must NOT downgrade it.
    docs = [
        {"nodes": [{"id": "a", "epistemic": "observed"}, {"id": "b", "epistemic": "observed"}],
         "edges": [{"source": "a", "target": "b", "relation": "calls", "epistemic": "observed"}]},
        {"nodes": [{"id": "a", "epistemic": "inferred"}, {"id": "b", "epistemic": "inferred"}],
         "edges": [{"source": "a", "target": "b", "relation": "calls", "epistemic": "inferred"}]},
    ]
    final = bridge.ingest_many(docs, graph_path=str(graph_path))
    assert final["edges"] == 1
    assert final["edge_confidence"]["EXTRACTED"] == 1


def test_file_source_reads_jag_from_disk(tmp_path, capsys):
    import json

    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "nodes": [{"id": "x", "epistemic": "observed"}, {"id": "y", "epistemic": "observed"}],
        "edges": [{"source": "x", "target": "y", "relation": "calls", "epistemic": "observed"}],
    }), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")

    graph_path = tmp_path / "out.json"
    # The unreadable file is skipped, the good one is ingested.
    final = bridge.ingest_stream(
        bridge.FileReaSource([str(good), str(bad)]),
        graph_path=str(graph_path),
    )
    assert graph_path.exists()
    # x and y nodes from the good file; the bad file contributed nothing.
    assert final["nodes"] == 2
    assert "skipped file" in capsys.readouterr().err


def test_empty_stream_still_creates_graph_file(tmp_path):
    graph_path = tmp_path / "empty.json"
    final = bridge.ingest_stream([], graph_path=str(graph_path))
    assert graph_path.exists()  # crash-safe from the very first step
    assert final == {"nodes": 0, "edges": 0,
                     "edge_confidence": {"EXTRACTED": 0, "INFERRED": 0, "other": 0}}
