"""CLI-level tests: command dispatch, argument parsing, and exit codes.

The individual modules (memory, diff, attest, coverage, ...) are tested on their
own elsewhere; this file closes the gap around ``reagraph.cli`` itself -- the
subcommand dispatch, argparse wiring, and the int exit code of every command.

The CLI is driven IN-PROCESS: ``cli.main([...])`` is called with an argv list and
its returned int is asserted, with output captured via capsys. Everything that
would touch the real world is kept out: graphs live under ``tmp_path``, the cache
is redirected to a tmp dir via ``REAGRAPH_CACHE``, and the commands that need
REA/mcp/node (pull/serve/serve-cache/compare) are never executed -- only their
argument wiring is checked through ``build_parser()``.
"""

from __future__ import annotations

import argparse
import json

import pytest

from reagraph import cli


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write(path, graph: dict) -> str:
    path.write_text(json.dumps(graph), encoding="utf-8")
    return str(path)


def _run(capsys, *argv):
    """Call cli.main in-process; return (exit_code, stdout, stderr)."""
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _node(nid, **kw):
    n = {"id": nid, "label": kw.pop("label", nid)}
    n.update(kw)
    return n


def _rich_graph() -> dict:
    """A small graph with a hub, EXTRACTED + INFERRED edges, and a ghost target."""
    return {
        "directed": True,
        "multigraph": False,
        "nodes": [
            _node("h", label="hub"),
            _node("a", label="alpha"),
            _node("b", label="beta"),
            _node("c", label="gamma"),
        ],
        "links": [
            {"source": "h", "target": "a", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "h", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "h", "target": "c", "relation": "calls", "confidence": "INFERRED"},
            # points at a node with no entry of its own -> a ghost/unresolved target
            {"source": "c", "target": "ghost1", "relation": "uses", "confidence": "INFERRED"},
        ],
    }


# --------------------------------------------------------------------------- #
# add  (+ dedup on re-observation)
# --------------------------------------------------------------------------- #
def test_add_folds_finding_and_stats_reports_counts(tmp_path, capsys):
    graph = tmp_path / "mem.json"
    finding = _write(
        tmp_path / "f1.json",
        {
            "nodes": [_node("a"), _node("b")],
            "links": [{"source": "a", "target": "b", "relation": "calls"}],
        },
    )

    code, out, _ = _run(capsys, "add", finding, "--graph", str(graph))
    assert code == 0
    assert "merged 1 finding(s)" in out
    assert graph.exists()

    code, out, _ = _run(capsys, "stats", "--graph", str(graph))
    assert code == 0
    assert "2 nodes, 1 edges" in out


def test_add_overlapping_finding_dedups(tmp_path, capsys):
    graph = tmp_path / "mem.json"
    first = _write(
        tmp_path / "f1.json",
        {
            "nodes": [_node("a"), _node("b")],
            "links": [{"source": "a", "target": "b", "relation": "calls"}],
        },
    )
    # Fully overlapping finding: same two nodes, same edge -> must NOT double.
    second = _write(
        tmp_path / "f2.json",
        {
            "nodes": [_node("a"), _node("b")],
            "links": [{"source": "a", "target": "b", "relation": "calls"}],
        },
    )

    assert _run(capsys, "add", first, "--graph", str(graph))[0] == 0
    assert _run(capsys, "add", second, "--graph", str(graph))[0] == 0

    code, out, _ = _run(capsys, "stats", "--graph", str(graph))
    assert code == 0
    assert "2 nodes, 1 edges" in out  # counts did not double


# --------------------------------------------------------------------------- #
# stats / summary / coverage
# --------------------------------------------------------------------------- #
def test_stats_reports_counts_and_confidence(tmp_path, capsys):
    graph = _write(tmp_path / "g.json", _rich_graph())
    code, out, _ = _run(capsys, "stats", "--graph", graph)
    assert code == 0
    assert "4 nodes, 4 edges" in out
    assert "edge confidence" in out


def test_summary_shows_god_nodes_and_clusters(tmp_path, capsys):
    graph = _write(tmp_path / "g.json", _rich_graph())
    code, out, _ = _run(capsys, "summary", "--graph", graph)
    assert code == 0
    assert "Graph overview" in out
    assert "god nodes" in out
    assert "clusters" in out
    # The hub is the highest-degree node -> appears in the god-node list.
    assert "hub" in out


def test_coverage_shows_observed_and_inferred(tmp_path, capsys):
    graph = _write(tmp_path / "g.json", _rich_graph())
    code, out, _ = _run(capsys, "coverage", "--graph", graph)
    assert code == 0
    assert "OBSERVED" in out
    assert "INFERRED" in out
    assert "nodes observed" in out
    assert "edges inferred" in out
    # the ghost endpoint we point at but never analyzed is surfaced
    assert "ghost1" in out


# --------------------------------------------------------------------------- #
# diff: id regime vs auto-detected signature regime
# --------------------------------------------------------------------------- #
def test_diff_stable_ids_reports_real_change(tmp_path, capsys):
    a = _write(
        tmp_path / "a.json",
        {
            "nodes": [_node("x1"), _node("x2")],
            "links": [{"source": "x1", "target": "x2", "relation": "calls"}],
        },
    )
    b = _write(
        tmp_path / "b.json",
        {
            "nodes": [_node("x1"), _node("x2"), _node("x3")],
            "links": [
                {"source": "x1", "target": "x2", "relation": "calls"},
                {"source": "x2", "target": "x3", "relation": "calls"},
            ],
        },
    )
    code, out, _ = _run(capsys, "diff", a, b)
    assert code == 0
    # auto on stable-id (no rea_kind) graphs stays in id mode -> no banner
    assert "signature" not in out
    assert "added nodes     : 1" in out
    assert "+ x3" in out


def _rea_graph(prefix):
    """A REA-style graph: nodes carry rea_kind; ids are per-build (churned)."""
    descriptors = [
        ("module", "main", "src/main.js"),
        ("endpoint", "api/login", "src/auth.js"),
        ("function", "validate", "src/auth.js"),
    ]
    nodes = [
        {"id": f"{prefix}{i}", "rea_kind": kind, "label": label, "source_file": src}
        for i, (kind, label, src) in enumerate(descriptors, 1)
    ]
    return {"directed": True, "multigraph": False, "nodes": nodes, "links": []}


def test_diff_auto_picks_signature_for_rea_graphs(tmp_path, capsys):
    # Same meaning, totally different ids -> auto must pick the signature regime
    # and report ZERO change (the #4520 fix).
    a = _write(tmp_path / "a.json", _rea_graph("a"))
    b = _write(tmp_path / "b.json", _rea_graph("z"))

    code, out, _ = _run(capsys, "diff", a, b)
    assert code == 0
    assert "signature" in out  # the auto-detection banner
    assert "added nodes     : 0" in out
    assert "removed nodes   : 0" in out


def test_diff_by_id_on_rea_graphs_shows_churn(tmp_path, capsys):
    # Forcing --by id on the SAME churned pair reports full add/remove churn.
    a = _write(tmp_path / "a.json", _rea_graph("a"))
    b = _write(tmp_path / "b.json", _rea_graph("z"))

    code, out, _ = _run(capsys, "diff", a, b, "--by", "id")
    assert code == 0
    assert "signature" not in out  # id mode has no banner
    assert "added nodes     : 3" in out
    assert "removed nodes   : 3" in out


# --------------------------------------------------------------------------- #
# attest: clean vs tampered
# --------------------------------------------------------------------------- #
def _src_graph() -> dict:
    return {
        "nodes": [
            _node("s1", label="authenticate"),
            _node("s2", label="checkPassword"),
            _node("s3", label="loadUser"),
        ],
        "links": [
            {"source": "s1", "target": "s2", "relation": "calls"},
            {"source": "s1", "target": "s3", "relation": "calls"},
        ],
    }


def _shipped_clean() -> dict:
    return {
        "nodes": [
            _node("b17", label="authenticate"),
            _node("b42", label="checkPassword"),
            _node("b99", label="loadUser"),
        ],
        "links": [
            {"source": "b17", "target": "b42", "relation": "calls"},
            {"source": "b17", "target": "b99", "relation": "calls"},
        ],
    }


def _parse_integrity(text: str) -> float:
    for line in text.splitlines():
        if line.startswith("Integrity:"):
            return float(line.split()[1])
    raise AssertionError(f"no Integrity line in report:\n{text}")


def test_attest_clean_pair_high_integrity(tmp_path, capsys):
    src = _write(tmp_path / "src.json", _src_graph())
    shipped = _write(tmp_path / "shipped.json", _shipped_clean())

    code, out, _ = _run(capsys, "attest", "--source", src, "--shipped", shipped)
    assert code == 0
    assert _parse_integrity(out) > 0.9
    assert "Shipped-but-unexplained: 0 node(s)" in out


def test_attest_tampered_pair_surfaces_residual(tmp_path, capsys):
    tampered = _shipped_clean()
    tampered["nodes"].append(_node("bX", label="collect.evil-metrics"))
    tampered["links"].append({"source": "b17", "target": "bX", "relation": "sends"})
    tampered["links"].append({"source": "b42", "target": "b99", "relation": "sends"})

    src = _write(tmp_path / "src.json", _src_graph())
    shipped = _write(tmp_path / "tampered.json", tampered)

    code, out, _ = _run(capsys, "attest", "--source", src, "--shipped", shipped)
    assert code == 0
    assert _parse_integrity(out) < 1.0
    assert "bX" in out  # the injected node surfaces in the residual
    assert "Residual clusters" in out


# --------------------------------------------------------------------------- #
# cache  (redirected to a tmp dir)
# --------------------------------------------------------------------------- #
def test_cache_stats_and_clear(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("REAGRAPH_CACHE", str(tmp_path / "cache"))

    code, out, _ = _run(capsys, "cache", "stats")
    assert code == 0
    assert "cache:" in out and "entries" in out

    code, out, _ = _run(capsys, "cache", "clear")
    assert code == 0
    assert "cleared" in out


# --------------------------------------------------------------------------- #
# install-skill
# --------------------------------------------------------------------------- #
def test_install_skill_writes_skill_md(tmp_path, capsys):
    dest = tmp_path / "skills"
    code, out, _ = _run(capsys, "install-skill", "--dir", str(dest))
    assert code == 0

    skill = dest / "reagraph" / "SKILL.md"
    assert skill.exists()
    assert skill.read_text(encoding="utf-8").strip()  # non-empty content
    assert "installed the reagraph skill" in out


# --------------------------------------------------------------------------- #
# doctor: a report that never crashes; return is the 0/1 readiness signal
# --------------------------------------------------------------------------- #
def test_doctor_reports_without_crashing(capsys):
    code, out, _ = _run(capsys, "doctor")
    # 1 is the documented "not ready" signal (e.g. mcp absent), not a failure.
    assert code in (0, 1)
    assert "REAgraph doctor" in out
    assert "verdict" in out


# --------------------------------------------------------------------------- #
# query: must fail gracefully when graphify is not on PATH
# --------------------------------------------------------------------------- #
def test_query_without_graphify_fails_gracefully(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    graph = _write(tmp_path / "g.json", _rich_graph())

    code, out, err = _run(capsys, "query", "what calls hub?", "--graph", graph)
    assert code == 1  # non-zero, actionable, no traceback
    assert "graphify is not installed" in err


# --------------------------------------------------------------------------- #
# arg wiring only (no spawning): pull / compare / serve / serve-cache
# --------------------------------------------------------------------------- #
def test_parse_pull_arguments():
    ns = cli.build_parser().parse_args(
        ["pull", "x", "--source-path", "a", "--source-path", "b", "--rea-cli-cmd", "c"]
    )
    assert ns.cmd == "pull"
    assert ns.target == "x"
    assert ns.source_path == ["a", "b"]
    assert ns.rea_cli_cmd == "c"
    assert ns.func is cli._cmd_pull


def test_parse_compare_arguments():
    ns = cli.build_parser().parse_args(["compare", "left", "right", "--save", "f"])
    assert ns.cmd == "compare"
    assert ns.left == "left"
    assert ns.right == "right"
    assert ns.save == "f"
    assert ns.func is cli._cmd_compare


def test_parse_serve_cache_arguments():
    ns = cli.build_parser().parse_args(["serve-cache", "--port", "0"])
    assert ns.cmd == "serve-cache"
    assert ns.port == 0
    assert ns.func is cli._cmd_serve_cache


def test_parse_serve_arguments():
    ns = cli.build_parser().parse_args(["serve", "--graph", "g"])
    assert ns.cmd == "serve"
    assert ns.graph == "g"
    assert ns.func is cli._cmd_serve


# --------------------------------------------------------------------------- #
# argparse error paths
# --------------------------------------------------------------------------- #
def test_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        cli.main(["definitely-not-a-command"])
    assert exc.value.code != 0


def test_missing_required_subcommand_exits():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code != 0


def test_missing_positional_for_add_exits():
    with pytest.raises(SystemExit):
        cli.main(["add"])  # requires at least one file


def test_missing_required_flags_for_attest_exits():
    with pytest.raises(SystemExit):
        cli.main(["attest"])  # --source / --shipped are required


# --------------------------------------------------------------------------- #
# --help and the full subcommand roster
# --------------------------------------------------------------------------- #
def test_help_exits_zero_and_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "reagraph" in out


def test_build_parser_lists_every_expected_subcommand():
    parser = cli.build_parser()
    subactions = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    assert subactions, "parser has no subparsers"
    choices = set(subactions[0].choices)
    expected = {
        "add", "diff", "compare", "stats", "summary", "coverage", "query",
        "pull", "attest", "cache", "doctor", "serve", "serve-cache",
        "install-skill",
    }
    assert choices == expected


# --------------------------------------------------------------------------- #
# setup-rea: builds a local REA image via docker; we never actually run docker.
# --------------------------------------------------------------------------- #

def test_setup_rea_errors_cleanly_without_docker(monkeypatch, capsys):
    # No docker on PATH -> exit 1 with actionable guidance, no crash.
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    rc = cli.main(["setup-rea"])
    assert rc == 1
    assert "docker" in capsys.readouterr().err.lower()


def test_setup_rea_errors_when_daemon_down(monkeypatch, capsys):
    # docker present but daemon down (`docker info` nonzero) -> exit 1, no build.
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/docker")
    calls = []

    def fake_call(cmd, **kw):
        calls.append(cmd)
        return 1 if cmd[:2] == ["docker", "info"] else 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    rc = cli.main(["setup-rea"])
    assert rc == 1
    assert "daemon" in capsys.readouterr().err.lower()
    # It must not have attempted an image build (no `docker run ... node:24-slim`).
    assert not any("node:24-slim" in " ".join(map(str, c)) for c in calls)


def test_setup_rea_prints_ready_command_when_image_exists(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_call(cmd, **kw):
        if cmd[:2] == ["docker", "info"]:
            return 0
        if cmd[:3] == ["docker", "image", "inspect"]:
            return 0  # image already built
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    rc = cli.main(["setup-rea"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already built" in out
    assert "REAGRAPH_REA_CMD" in out and cli.REA_IMAGE in out


def test_setup_rea_is_registered():
    p = cli.build_parser()
    assert "setup-rea" in p._subparsers._group_actions[0].choices
