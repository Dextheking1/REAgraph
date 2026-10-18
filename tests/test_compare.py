"""Authoritative comparison: mapping REA's compare_application_versions result, and
driving analyze+compare over a (fake) MCP session.

No REA/mcp/node needed: the fake session returns canned analyze Evidence and a
canned comparison result in REA's real shape (verified against rea-agents).
"""

import asyncio

from reagraph import compare, rea_client


# -- the mapper (reagraph.compare.to_report / format_report) -------------------

def _rea_comparison(normalized):
    return {"normalized_result": normalized}


def _normalized(items, summary=None, matching=None):
    return {
        "comparison_id": "javc_abc",
        "left": {"evidence_id": "ev_l"}, "right": {"evidence_id": "ev_r"},
        "summary": summary or {},
        "matching": matching or {"exact_node_identity": 3, "structural_fingerprint": 1},
        "items": items,
        "limitations": ["no fuzzy pairing"],
    }


def test_to_report_buckets_items_by_status():
    items = [
        {"node_kind": "javascript-module", "status": "unchanged",
         "left_node_id": "L1", "right_node_id": "R1"},
        {"node_kind": "endpoint", "status": "added",
         "left_node_id": None, "right_node_id": "R9"},
        {"node_kind": "ipc-handler", "status": "removed",
         "left_node_id": "L8", "right_node_id": None},
        {"node_kind": "javascript-module", "status": "changed",
         "left_node_id": "L2", "right_node_id": "R2"},
    ]
    rep = compare.to_report(_normalized(items))
    assert rep["summary"] == {"unchanged": 1, "added": 1, "removed": 1,
                              "changed": 1, "unknown": 0}
    assert rep["items"]["added"][0]["kind"] == "endpoint"
    assert rep["items"]["added"][0]["right"] == "R9"
    assert rep["items"]["removed"][0]["left"] == "L8"
    assert rep["comparison_id"] == "javc_abc"


def test_to_report_prefers_reas_summary_counts():
    # REA's own summary counts win over counting items (items may be truncated).
    rep = compare.to_report(_normalized([], summary={"unchanged": 100, "added": 2,
                                                     "removed": 0, "changed": 1,
                                                     "unknown": 0}))
    assert rep["summary"]["unchanged"] == 100
    assert rep["summary"]["added"] == 2


def test_to_report_is_fail_open_on_junk():
    for bad in (None, {}, {"items": "nope"}, 42, {"items": [1, 2, "x"]}):
        rep = compare.to_report(bad)
        assert set(rep["summary"]) == {"unchanged", "added", "removed", "changed", "unknown"}
        assert isinstance(rep["items"], dict)


def test_format_report_is_bounded_and_readable():
    items = [{"node_kind": "endpoint", "status": "added",
              "left_node_id": None, "right_node_id": f"R{i}"} for i in range(50)]
    rep = compare.to_report(_normalized(items, summary={"added": 50, "unchanged": 0,
                                                        "removed": 0, "changed": 0,
                                                        "unknown": 0}))
    text = compare.format_report(rep, top=5)
    assert "added     : 50" in text
    assert "+45 more" in text  # bounded output
    assert "matched by:" in text


# -- the client orchestration (analyze both + compare on a session) -----------

class _FakeSession:
    """Returns canned analyze Evidence for each target, then a canned comparison."""

    def __init__(self, comparison_normalized, left_err=None, right_err=None):
        self.comparison = {"normalized_result": comparison_normalized}
        self.left_err = left_err
        self.right_err = right_err
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "analyze_javascript_application":
            path = args.get("input_path")
            err = self.left_err if "left" in str(path) else self.right_err
            if err:
                return {"error": {"code": "execution_failure", "message": err}}
            return {"evidence_id": f"ev_{path}", "normalized_result": {"graph": {}}}
        if name == "compare_application_versions":
            # left/right must be the analyze Evidence envelopes.
            assert "left" in args and "right" in args
            return self.comparison
        raise AssertionError(f"unexpected tool {name}")


def test_compare_on_session_analyzes_both_then_compares():
    normalized = _normalized([
        {"node_kind": "endpoint", "status": "added",
         "left_node_id": None, "right_node_id": "R9"},
    ])
    session = _FakeSession(normalized)
    client = rea_client.ReaClient()
    out = asyncio.run(client.compare_on_session(session, "build-left", "build-right"))

    # It analyzed both targets (input_path) then compared with both Evidence.
    assert [c[0] for c in session.calls] == [
        "analyze_javascript_application", "analyze_javascript_application",
        "compare_application_versions",
    ]
    assert session.calls[0][1] == {"input_path": "build-left"}
    assert session.calls[2][1]["left"]["evidence_id"] == "ev_build-left"
    assert out["comparison_id"] == "javc_abc"

    rep = compare.to_report(out)
    assert rep["summary"]["added"] == 1


def test_compare_on_session_surfaces_analysis_error():
    session = _FakeSession(_normalized([]), left_err="could not complete")
    client = rea_client.ReaClient()
    try:
        asyncio.run(client.compare_on_session(session, "build-left", "build-right"))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "left build" in str(exc) and "could not complete" in str(exc)
    # It must not have reached the comparison call.
    assert all(c[0] != "compare_application_versions" for c in session.calls)
