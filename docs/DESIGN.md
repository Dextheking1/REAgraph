# REAgraph design

## Thesis

Reverse-engineering engines are excellent *sensors* but leave three gaps:

1. **No memory** — findings evaporate with the session; cost never compounds.
2. **No reliable structure/diff** — byte-offset diffs report "everything changed"
   when code merely moved (REA #4520).
3. **Unamortized cost** — every analysis pays full price.

REAgraph is the layer that fixes those: a persistent, structural, confidence-aware
graph that makes a sensor's output *compound*.

## What exists now (v0.0.1)

- `reagraph.memory` — node-link graph store; `merge()` dedups nodes by id and
  edges by `(source, target, relation)`, and **upgrades confidence** on
  re-observation (EXTRACTED > INFERRED). Identity is never a byte offset.
- `reagraph.diff` — structural diff over the same identities; a moved-but-unchanged
  symbol reads as unchanged; only added/removed/rewired structure shows up.
- `reagraph.ingest` — REA JAG (`epistemic`: observed/inferred/unavailable) ->
  graphify confidence; drops `unavailable`; preserves the original relation in
  `context` as `rea:<relation>`.
- `reagraph` CLI — `add` / `diff` / `stats` / `query` (query delegates to graphify).

The graph is written in graphify's node-link JSON, so `graphify query/path/explain
--graph reagraph.json` works out of the box.

## Roadmap (ordered)

1. **Live incremental ingest from the REA MCP server.** Both REA and graphify are
   MCP servers; REAgraph sits in the loop so each REA finding is merged into
   `reagraph.json` the moment it is produced, and the agent queries memory before
   paying for the next probe. (This is the "help the agent *during* RE" wedge; the
   persistent memory falls out for free because the graph is a file on disk.)
2. **REAgraph MCP server.** Expose `remember` / `recall` / `diff` so any agent can
   use REAgraph as its long-term RE memory directly.
3. **Demand-scoped re-analysis.** Only re-reverse the region a query needs; never
   re-reverse what the graph already covers (`graph_stats` drives coverage).
4. **Shared analysis cache, keyed by artifact hash.** The compounding asset and
   the moat: every artifact anyone analyzes makes the next lookup instant.
5. **Structural alignment to source.** Overlay a graphify *source* graph with a
   REA *binary* graph of the same program (god-node anchoring + community + WL
   fingerprints, confidence-weighted) to answer "does shipped == source" — the
   residual (shipped-but-unexplained) is the supply-chain signal.

## Non-goals / honesty

- REAgraph does **not** decompile or reverse-engineer anything itself — it depends
  on a sensor (REA) for that. It adds memory, structure, and diff on top.
- Source<->stripped-binary alignment (roadmap item 5) is a known-hard research
  problem. It is accurate where symbols/strings survive (JS/Electron/.NET/WASM)
  and fuzzier on stripped native binaries; confidence is always carried so the
  tool ranks and abstains rather than asserting.
