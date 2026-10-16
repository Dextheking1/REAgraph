# REAgraph architecture

REAgraph is a thin memory and structure layer. It vendors no code from REA and no
code from graphify. It composes with both at the protocol and format level: it
consumes REA's output format (JAG), it writes graphify's node-link JSON, and it
delegates query, path, and explain to graphify installed as a dependency. REAgraph
itself does not decompile or reverse-engineer anything; it depends on a sensor
(REA) for that and adds memory, structural identity, and diff on top.

## Data flow

```
  agent (Claude Code / Codex / opencode)
      |
      |  1. asks REA to analyze a region
      v
    REA  (sensor: decompile / reverse-engineer)
      |
      |  2. finding as JAG (evidence + epistemic state)
      v
  reagraph  (memory / diff / coverage)
      |      - ingest: JAG epistemic -> graphify confidence
      |      - memory: merge into reagraph.json, dedup + confidence upgrade
      |      - diff:   structural, byte-agnostic version diff
      |
      |  3. reagraph.json written in graphify node-link JSON
      v
  graphify  (query / path / explain over the accumulated graph)
      |
      |  4. answer
      v
    agent
```

The agent probes with REA, folds each finding into REAgraph, and queries the
accumulated graph before paying for another probe. Cost stops evaporating and
starts compounding, because the graph is a plain file on disk.

## Why composition, not copying

- Licensing. REA is MIT, graphify is Apache-2.0, and REAgraph is MIT. Keeping the
  three as separate projects that talk over formats and protocols avoids mixing
  code under different licenses and keeps REAgraph's own license clean. REAgraph is
  an independent project and is not affiliated with or endorsed by REA.
- Maintenance. By consuming REA's output and delegating to graphify rather than
  forking either, REAgraph rides both upstreams. Improvements to REA's analysis or
  graphify's query engine arrive for free; REAgraph only maintains the thin layer
  in between.
- Protocol-level composition. Both REA and graphify are MCP servers, so REAgraph
  composes at the protocol level rather than by embedding either codebase. The
  live-ingest and server pieces (see roadmap) sit in that MCP loop.

## Data model

The graph is graphify's node-link JSON:

```json
{"directed": true, "multigraph": false,
 "nodes": [{"id": "...", "label": "...", "confidence": "EXTRACTED"}],
 "links": [{"source": "...", "target": "...", "relation": "calls",
            "confidence": "INFERRED", "context": "rea:invokes"}]}
```

- Confidence. graphify's vocabulary is EXTRACTED (observed) above INFERRED
  (inferred). REA's JAG epistemic states map in: `observed` becomes EXTRACTED,
  `inferred` becomes INFERRED, and `unavailable` edges are dropped because there is
  nothing to assert. The original relation is preserved in `context` as
  `rea:<relation>` so provenance is never lost.
- Identity. A node's identity is its `id`. An edge's identity is the tuple
  `(source, target, relation)`. Identity is never a byte offset or file location.
  This is the rule that makes a memory out of a stream of findings and the rule
  that makes the diff byte-agnostic: a thing that merely moved in the file is still
  the same node and the same edge.
- Memory rule. When the same node or edge is seen again, it is never duplicated.
  REAgraph keeps the union of attributes and upgrades confidence to the stronger of
  the two, so a later observed fact promotes an earlier inferred one. Re-observing a
  thing makes the graph more certain, not larger.

## Module map

Existing modules (present and tested in this repo):

- `memory` — the node-link graph store. `merge()` dedups nodes by id and edges by
  `(source, target, relation)` and upgrades confidence on re-observation. `stats()`
  reports node, edge, and confidence counts.
- `diff` — structural, byte-agnostic diff over the same identities. A moved but
  otherwise unchanged symbol reads as unchanged; only added, removed, or rewired
  structure shows up.
- `ingest` — maps a REA JAG document to a node-link fragment, auto-detecting JAG
  versus plain node-link input. Pure and tolerant of missing fields.
- `summarize` — a bounded god-node + cluster map of a graph of any size, so a
  gigabyte-scale analysis stays digestible instead of being dumped in full.
- `coverage` — an honest verification report (observed vs inferred vs unverified)
  plus the exploration frontier; absence is never reported as proof of absence.
- `bridge` — incremental live ingest: merge each finding as it is produced, save
  after every one (crash-safe), and skip a bad finding instead of aborting.
- `serve` — the REAgraph MCP server exposing `remember` / `recall` / `diff` /
  `stats`; the `mcp` package is lazy-imported so the rest of REAgraph works
  without it.
- `cli` — the `reagraph` command: `add`, `diff`, `stats`, `summary`, `coverage`,
  `query` (delegates to the installed graphify binary), and `serve`.

Planned (described in the roadmap, not yet in this repo): demand-scoped
re-analysis (only re-reverse the region a query needs; roadmap item 3).

Two further roadmap items are not modules but cross-cutting work: a shared analysis
cache keyed by artifact hash (roadmap item 4), and structural alignment of a
graphify source graph to a REA binary graph of the same program (roadmap item 5),
which is a known-hard research problem and is carried with confidence so the tool
ranks and abstains rather than asserting.
