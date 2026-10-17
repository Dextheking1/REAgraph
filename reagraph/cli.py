"""reagraph CLI: add findings to persistent memory, diff builds, show stats.

    reagraph add <finding.jag.json> [--graph reagraph.json]
    reagraph diff <a.json> <b.json>
    reagraph stats [--graph reagraph.json]
    reagraph summary [--graph reagraph.json] [--top N]       # bounded map of a big graph
    reagraph coverage [--graph reagraph.json]                # what's verified vs unverified
    reagraph query "<question>" [--graph reagraph.json]      # delegates to graphify
    reagraph serve [--graph reagraph.json]                   # run the MCP server
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys

from . import coverage as coveragemod
from . import diff as diffmod
from . import memory
from . import summarize as summarizemod
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


def _cmd_summary(args) -> int:
    graph = memory.load(args.graph)
    print(summarizemod.format_summary(summarizemod.summarize(graph, top=args.top)))
    return 0


def _cmd_coverage(args) -> int:
    print(coveragemod.gaps_report(memory.load(args.graph)))
    return 0


def _cmd_pull(args) -> int:
    # Connect to a running/spawned REA MCP server, analyze the target, and fold
    # each finding into the graph as it arrives (crash-safe, fail-open). By
    # default a content-addressed cache serves a repeat analysis with no REA call.
    from . import bridge, rea_client
    cmd = shlex.split(args.rea_cmd) if args.rea_cmd else None
    cache = None
    if not args.no_cache:
        from . import cache as cachemod
        cache = cachemod.Cache()
    client = rea_client.ReaClient(target=args.target, server_cmd=cmd, cache=cache, refresh=args.refresh)
    s = bridge.ingest_stream(client, args.graph)
    print(f"pulled from REA into {args.graph}: {s['nodes']} nodes, {s['edges']} edges "
          f"(EXTRACTED {s['edge_confidence']['EXTRACTED']} / INFERRED {s['edge_confidence']['INFERRED']})")
    return 0


def _cmd_attest(args) -> int:
    # Does the shipped artifact match the source? Structural source<->binary diff.
    from . import attest
    src = memory.load(args.source)
    shipped = memory.load(args.shipped)
    print(attest.format_report(attest.attest(src, shipped, min_confidence=args.min_confidence)))
    return 0


def _cmd_cache(args) -> int:
    from . import cache as cachemod
    c = cachemod.Cache()
    if args.action == "clear":
        n = c.clear()
        print(f"cleared {n} cache entries from {c.stats()['dir']}")
    else:
        st = c.stats()
        print(f"cache: {st['entries']} entries, {st['bytes']} bytes at {st['dir']}")
    return 0


def _cmd_doctor(args) -> int:
    from . import doctor
    print(doctor.report())
    return 0 if doctor.ready() else 1


def _cmd_serve(args) -> int:
    # The MCP server resolves its graph via REAGRAPH_GRAPH; forward --graph.
    os.environ["REAGRAPH_GRAPH"] = args.graph
    from . import serve  # lazy: pulls in mcp only when actually serving
    return serve.main()


def _cmd_serve_cache(args) -> int:
    from . import cache_server
    # Tokens are sourced from the environment ONLY (never a CLI arg: argv leaks
    # via `ps`), mirroring cache_server.main's posture.
    read_token = os.environ.get(cache_server.ENV_READ_TOKEN) or None
    write_token = os.environ.get(cache_server.ENV_WRITE_TOKEN) or None
    cache_server.serve(
        host=args.host,
        port=args.port,
        directory=args.dir,
        read_token=read_token,
        write_token=write_token,
        max_value_bytes=int(args.max_value_mb * 1024 * 1024),
        validate_payload=not args.no_validate_payload,
    )
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

    sm = sub.add_parser("summary", help="bounded map (god nodes + clusters) of a big graph")
    sm.add_argument("--graph", default=DEFAULT_GRAPH)
    sm.add_argument("--top", type=int, default=20)
    sm.set_defaults(func=_cmd_summary)

    cv = sub.add_parser("coverage", help="what's observed vs inferred vs unverified (honest gaps)")
    cv.add_argument("--graph", default=DEFAULT_GRAPH)
    cv.set_defaults(func=_cmd_coverage)

    q = sub.add_parser("query", help="query the graph (delegates to graphify)")
    q.add_argument("question")
    q.add_argument("--graph", default=DEFAULT_GRAPH)
    q.set_defaults(func=_cmd_query)

    pl = sub.add_parser("pull", help="connect to REA, analyze a target, fold findings into the graph")
    pl.add_argument("target", help="what REA should analyze (e.g. an unpacked app dir or file)")
    pl.add_argument("--rea-cmd", default=None,
                    help="override the REA MCP server command (else $REAGRAPH_REA_CMD, else npx rea-agents)")
    pl.add_argument("--graph", default=DEFAULT_GRAPH)
    pl.add_argument("--no-cache", action="store_true", help="always call REA, do not use the cache")
    pl.add_argument("--refresh", action="store_true", help="re-analyze and overwrite cached results")
    pl.set_defaults(func=_cmd_pull)

    at = sub.add_parser("attest", help="does the shipped artifact match the source? structural source<->binary diff")
    at.add_argument("--source", required=True, help="graphify source graph (graph.json)")
    at.add_argument("--shipped", required=True, help="REAgraph graph of the shipped artifact")
    at.add_argument("--min-confidence", type=float, default=0.5, help="alignment confidence floor (abstain below)")
    at.set_defaults(func=_cmd_attest)

    ca = sub.add_parser("cache", help="inspect or clear the content-addressed analysis cache")
    ca.add_argument("action", nargs="?", choices=["stats", "clear"], default="stats")
    ca.set_defaults(func=_cmd_cache)

    dc = sub.add_parser("doctor", help="check prerequisites for live `reagraph pull` (mcp / node / REA)")
    dc.set_defaults(func=_cmd_doctor)

    sv = sub.add_parser("serve", help="run the REAgraph MCP server (needs the 'mcp' package)")
    sv.add_argument("--graph", default=DEFAULT_GRAPH)
    sv.set_defaults(func=_cmd_serve)

    sc = sub.add_parser("serve-cache",
                        help="run the shared analysis-cache HTTP server "
                             "(tokens from $REAGRAPH_CACHE_READ_TOKEN / _WRITE_TOKEN)")
    sc.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    sc.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    sc.add_argument("--dir", default=None,
                    help="store directory (default $REAGRAPH_CACHE_SERVER_DIR or the cache default)")
    sc.add_argument("--max-value-mb", type=float, default=5.0,
                    help="reject uploads larger than this many MiB (default 5)")
    sc.add_argument("--no-validate-payload", action="store_true",
                    help="accept any bytes (skip the reagraph cache-entry shape check)")
    sc.set_defaults(func=_cmd_serve_cache)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
