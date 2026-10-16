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
- `reagraph.summarize` — bounded god-node + cluster overview for graphs of any
  size (the fix for gigabyte-scale, undigestible output).
- `reagraph.coverage` — honest observed/inferred/unverified report plus the
  exploration frontier ("probe here next"); absence is never reported as proof.
- `reagraph.bridge` — incremental live ingest: merge each finding as it lands,
  save after every one (crash-safe), skip a bad finding instead of aborting.
- `reagraph.serve` — MCP server (`remember` / `recall` / `diff` / `stats`); the
  `mcp` package is lazy-imported so the rest of REAgraph works without it.
- `reagraph.rea_client` / `reagraph.rea_adapter` — MCP client that connects to a
  live REA and maps its JavaScript Application Graph output into fragments.
- `reagraph.artifact` / `reagraph.cache` / `reagraph.cached` — content-addressed
  analysis cache: a repeat analysis of the same artifact is served with no REA
  call. Local backend today; the backend is pluggable for a future shared tier.
- `reagraph` CLI — `add` / `diff` / `stats` / `summary` / `coverage` / `query`
  (query delegates to graphify) / `pull` (cache-aware) / `cache` / `serve`.

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
4. **Shared analysis cache, keyed by artifact hash.** The LOCAL cache ships (see
   `reagraph.cache` and docs/CACHE.md): a repeat analysis of the same artifact is
   served with no REA call. The SHARED/remote tier (pooling results across people
   and machines) is the compounding moat and is still ahead; the backend is
   pluggable for exactly this, and it must be opt-in to protect private artifacts.
5. **Structural alignment to source.** Overlay a graphify *source* graph with a
   REA *binary* graph of the same program (god-node anchoring + community + WL
   fingerprints, confidence-weighted) to answer "does shipped == source" — the
   residual (shipped-but-unexplained) is the supply-chain signal.

## Future: optional acceleration (not now)

The one compute-heavy step on the horizon is structural alignment / community
detection / fingerprinting over million-node fused graphs (roadmap item 5). If
that becomes the bottleneck, acceleration would be an OPTIONAL, subprocess-isolated
backend, never a core dependency, so `pip install reagraph` stays stdlib-simple.
Preferred order: mature graph-algorithm libraries first (rustworkx, igraph, or
cuGraph on GPU); a massively-parallel runtime like Bend/HVM only if its model
earns its keep, given its early stage and the JSON<->tiny-types interop cost.

## Non-goals / honesty

- REAgraph does **not** decompile or reverse-engineer anything itself — it depends
  on a sensor (REA) for that. It adds memory, structure, and diff on top.
- Source<->stripped-binary alignment (roadmap item 5) is a known-hard research
  problem. It is accurate where symbols/strings survive (JS/Electron/.NET/WASM)
  and fuzzier on stripped native binaries; confidence is always carried so the
  tool ranks and abstains rather than asserting.
