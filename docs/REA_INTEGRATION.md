# REA integration

This document explains how REAgraph connects to REA. The short version: REAgraph
does not contain REA. It talks to a running REA over the Model Context Protocol
(MCP), reads the findings REA produces, and folds them into a persistent graph on
disk. REA stays REA; REAgraph stays the thin memory and structure layer on top.

The wiring described here is in the tree and tested: `reagraph.rea_client` (the
MCP client), `reagraph.rea_adapter` (the result adapter, grounded against REA's
JavaScript Application Graph schema), `reagraph.bridge` (incremental merge), and
the `reagraph pull <target>` command. The `mcp` package is lazy-imported, so the
rest of REAgraph works without it, and `examples/fake_rea_mcp_server.py` lets you
exercise the whole loop offline (see `examples/rea_live_demo.sh`).

## The integration model: client, not vendor

REAgraph does not vendor REA's source. REA is an MCP server; REAgraph is an MCP
client. At analysis time REAgraph spawns a REA process on demand, speaks MCP to
it, calls REA's analysis tools, and collects the results. The default spawn
command is:

```
npx -y rea-agents@latest
```

That default is overridable. Set the `REAGRAPH_REA_CMD` environment variable to
point at any command that starts a REA MCP server (a pinned version, a local
checkout, a wrapper script, a container entrypoint). REAgraph runs whatever that
variable names and talks MCP to it; it does not care how REA was installed.

Why integrate at the protocol boundary instead of copying REA's code:

- Licensing. REA is MIT, graphify is Apache-2.0, and REAgraph is MIT. Keeping the
  three as separate projects that talk over formats and protocols keeps each
  license clean and avoids mixing code under different terms into one tree.
- Maintenance. Consuming REA's output rather than forking it means REAgraph rides
  REA's upstream. Improvements to REA's analysis arrive for free; REAgraph only
  maintains the thin adapter and client in between. The same logic applies on the
  query side, where REAgraph delegates to graphify rather than embedding it.

If you want REA's source checked out locally for development (to read it, pin it,
or patch it), the clean option is a git submodule. A submodule keeps REA's
history and license separate and under REA's own path, so nothing about
REAgraph's licensing or provenance changes. It is a convenience for developers,
not a dependency: REAgraph still talks to REA over MCP at runtime, submodule or
not.

## Data flow

A REA tool result is turned into a graph fragment, merged into the persistent
graph one finding at a time, and then queried through graphify.

```
  REA (MCP server)
      |
      |  tool result (REA's analysis output)
      v
  rea_client  (MCP client: spawns REA, calls its tools, yields results)
      |
      |  raw result dict
      v
  rea_adapter.to_fragment   (REA result -> node-link / JAG fragment)
      |
      |  fragment dict
      v
  bridge.ingest_stream      (per finding: normalize -> merge -> SAVE)
      |                      crash-safe (saved after every merge)
      |                      fail-open (one bad finding is skipped, not fatal)
      v
  reagraph.json             (persistent memory, graphify node-link JSON)
      |
      |  query / path / explain
      v
  graphify
```

The pieces in detail:

- `rea_client` implements the bridge's `ReaSource` contract. `ReaSource` is a
  Protocol with a single method, `findings() -> Iterable[dict]`. The client's job
  is to spawn REA (per `REAGRAPH_REA_CMD`, default `npx -y rea-agents@latest`),
  drive its analysis tools over MCP, and yield one result dict per finding.
  Because the contract is just "yield dicts", REAgraph never imports REA and is
  never coupled to it at the code level.
- `rea_adapter.to_fragment` maps one REA tool result into the fragment shape the
  bridge expects: either a JAG document (nodes and edges carrying an `epistemic`
  state) or an already node-link document. The adapter is the one place that
  knows REA's result shape; everything downstream is REA-agnostic.
- `bridge.ingest_stream` takes a source (or any iterable of dicts), and for each
  finding: normalizes it to a node-link fragment, merges it into the on-disk
  graph, and saves immediately. JAG epistemic states map to graphify confidence
  (`observed` becomes `EXTRACTED`, `inferred` becomes `INFERRED`, `unavailable`
  edges are dropped), and the original relation is preserved in `context` as
  `rea:<relation>` so provenance is never lost. Saving after every merge is what
  makes the stream crash-safe: if the run dies on finding N, findings 1 through
  N-1 are already durably on disk. Skipping a malformed finding (logged to stderr,
  never raised) is what makes it fail-open: one bad symbol never discards the
  whole analysis.
- `reagraph.json` is the persistent graph, written in graphify's node-link JSON.
  Node identity is the node `id`; edge identity is the tuple
  `(source, target, relation)`. Identity is never a byte offset, so re-observing a
  thing dedups and upgrades its confidence rather than duplicating it.
- graphify reads that same file for query, path, and explain, for example
  `graphify query "where is the license checked" --graph reagraph.json`.

## How to use it

The live path, once the REA client is wired, is: point REAgraph at REA and let it
stream findings into the graph. REA is spawned on demand, so there is nothing to
start by hand.

- Default command: `npx -y rea-agents@latest`. This fetches and runs REA's MCP
  server, so a recent Node toolchain is the only prerequisite for the static
  analyses.
- Override: set `REAGRAPH_REA_CMD` to run a different REA (pinned version, local
  checkout, container). REAgraph runs exactly that command and speaks MCP to it.

Which analyses need a native engine, and which do not:

- No native engine required. Static analysis of JavaScript, TypeScript, and
  Electron applications, and other targets where symbols and strings survive
  (for example .NET and WASM). These are text-like artifacts that REA can reason
  over without a disassembler, so the default `npx` REA is enough.
- Native engine required. Compiled native binaries (stripped or otherwise) that
  need a disassembler or decompiler. REA drives one of Ghidra, Hopper, or IDA for
  these. That engine is REA's own dependency: you install and configure it the way
  REA documents, and REAgraph inherits whatever REA can do once it is set up.
  REAgraph adds no native analysis of its own.

Graceful degradation when REA or MCP is absent. REAgraph is useful without a live
REA at all:

- Offline `reagraph add`. If you already have REA findings on disk (JAG JSON, or
  any node-link graph), fold them into memory directly:
  `reagraph add finding.jag.json`. This path uses only the `ingest` and `memory`
  modules and needs neither REA nor the `mcp` package. `reagraph diff`, `stats`,
  `summary`, and `coverage` are all offline too.
- A fake source for the live loop without REA. The bridge consumes any object
  satisfying the `ReaSource` contract, so a fake in-memory source can exercise the
  whole ingest loop (merge, save, dedup, confidence upgrade, fail-open skip)
  without spawning REA. The test suite drives the bridge this way
  (`tests/test_bridge.py`), and a fake server under `examples/` serves the same
  purpose for trying the integration locally. `bridge.FileReaSource` is the
  built-in version of this: give it a list of finding files and it yields them one
  per file, skipping any that fail to load.
- Missing `mcp`. The `mcp` package is only needed to run or speak to an MCP
  server. It is lazy-imported, so everything above (offline add, diff, the fake
  source) works whether or not `mcp` is installed. Only `reagraph serve` and the
  live REA client actually require it.

## Limitations

This integration is deliberately honest about what it does not guarantee.

- REA's output format moves fast. REA is actively developed and its result shape
  changes. `rea_adapter.to_fragment` is therefore best-effort and fail-open: it
  maps the fields it recognizes and the bridge skips (with a stderr note) any
  finding it cannot normalize, rather than aborting the run. A REA release can
  still change a field the adapter has not caught up to; when that happens you get
  skipped findings and a log line, not a crash, and the adapter is the one place
  to update.
- Native analysis inherits REA's setup requirements. Anything that needs Ghidra,
  Hopper, or IDA is exactly as capable, and exactly as fiddly to install, as REA
  itself. REAgraph does not package, bundle, or simplify those engines; if REA
  cannot analyze a given native binary in your environment, neither can REAgraph.
- Privacy and legal responsibility is yours and unchanged. REA may send analysis
  work to a model provider, so results and artifact details can leave your
  machine; that is a property of running REA, and REAgraph routing REA's output
  into a local graph does not change it. You are responsible for having
  authorization to analyze whatever target you point this at. REAgraph adds
  memory, structure, and diff on top of REA; it adds no new permission to analyze
  anything and makes no legal or privacy guarantee about what you choose to run.

REAgraph is an independent project and is not affiliated with or endorsed by the
REA project.
