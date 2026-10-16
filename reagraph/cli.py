"""reagraph CLI: add findings to persistent memory, diff builds, show stats.

    reagraph add <finding.jag.json> [--graph reagraph.json]
    reagraph diff <a.json> <b.json>
    reagraph stats [--graph reagraph.json]
    reagraph query "<question>" [--graph reagraph.json]   # delegates to graphify
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from . import diff as diffmod
from . import memory
from .ingest import load_any

DEFAULT_GRAPH = "reagraph.json"


def _cmd_add(args) -> int:
    before = memory.stats(memory.load(args.graph))
    graph = memory.load(args.graph)
    for f in args.files:
        memory.merge(graph, load_any(f))
    memory.save(graph, args.graph)
    after = memory.stats(graph)
    print(
        f"merged {len(args.files)} finding(s) into {args.graph}: "
        f"nodes {before['nodes']}->{after['nodes']}, edges {before['edges']}->{after['edges']}"
    )
    return 0


def _cmd_diff(args) -> int:
    a = load_any(args.a)
    b = load_any(args.b)
    print(diffmod.format_report(diffmod.diff(a, b)))
    return 0


def _cmd_stats(args) -> int:
    s = memory.stats(memory.load(args.graph))
    print(f"{args.graph}: {s['nodes']} nodes, {s['edges']} edges")
    print(f"  edge confidence: {s['edge_confidence']}")
    return 0


def _cmd_query(args) -> int:
    if not shutil.which("graphify"):
        print("graphify is not installed; install it to query the graph:\n"
              "  uv tool install graphifyy   # then: graphify query ... --graph " + args.graph,
              file=sys.stderr)
        return 1
    return subprocess.call(["graphify", "query", args.question, "--graph", args.graph])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reagraph", description="Persistent graph memory for reverse engineering.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="fold finding(s) into persistent memory")
    a.add_argument("files", nargs="+", help="REA JAG JSON or node-link graph file(s)")
    a.add_argument("--graph", default=DEFAULT_GRAPH)
    a.set_defaults(func=_cmd_add)

    d = sub.add_parser("diff", help="structural (byte-agnostic) diff of two graphs")
    d.add_argument("a")
    d.add_argument("b")
    d.set_defaults(func=_cmd_diff)

    s = sub.add_parser("stats", help="show accumulated-graph coverage")
    s.add_argument("--graph", default=DEFAULT_GRAPH)
    s.set_defaults(func=_cmd_stats)

    q = sub.add_parser("query", help="query the graph (delegates to graphify)")
    q.add_argument("question")
    q.add_argument("--graph", default=DEFAULT_GRAPH)
    q.set_defaults(func=_cmd_query)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
