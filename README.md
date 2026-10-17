# REAgraph

**A persistent, queryable knowledge graph for reverse engineering.**

REAgraph turns one-shot reverse-engineering findings into a durable, structural
graph of a program — so you (and your AI agent) never reverse the same thing
twice, and so you can diff two builds by *meaning* instead of by byte offset.

It is the memory + structure layer that sits alongside a reverse-engineering
engine. It is designed to complement [REA](https://github.com/morluto/rea)
(agent-driven reverse engineering, MIT) and
[graphify](https://github.com/Graphify-Labs/graphify) (source → knowledge graph,
Apache-2.0). It is **not affiliated with or endorsed by the REA project.**

> Status: early. The core pieces below work and are tested. The live REA wiring
> connects to the real REA MCP server (`npx rea-agents --mcp`), discovers its
> tools, and calls `analyze_javascript_application` with the correct arguments;
> a non-empty graph from the real analyzer needs a host meeting REA's own
> Node/Swift requirements (see `docs/LIVE_PULL.md`). See `docs/DESIGN.md`.

## Why

Reverse-engineering tools are brilliant *sensors* but they have three holes:

1. **No memory.** Findings live in a chat session and evaporate. Every session
   re-pays the full (slow, token-heavy) cost and rediscovers the same facts.
2. **No reliable structure/diff.** Version-diffing by byte location reports
   "everything changed" when code merely moved
   ([REA #4520](https://github.com/morluto/rea/issues/4520): a real app diff came
   back *0 unchanged* because bytes shifted).
3. **Unamortized cost.** Every analysis pays full price; nothing compounds.

REAgraph plugs those three holes: **persistent queryable memory**, **structural
(byte-agnostic) identity & diff**, and a store whose value **compounds** as
findings accumulate.

## What works today

- `reagraph add` — merge a reverse-engineering finding (REA JAG JSON, or any
  node-link graph) into a persistent `reagraph.json`, with **dedup** and
  **confidence upgrade** (a later *observed* fact promotes an earlier *inferred*
  one instead of duplicating it).
- `reagraph diff A B` — a **structural** diff of two graphs by node/edge
  identity, immune to byte relocation. This is the fix for the #4520 class of
  bug: a module that only moved in the file reads as *unchanged*.
- `reagraph summary` — a **bounded** map of a graph of any size (god nodes +
  clusters), so a gigabyte-scale analysis becomes a short overview an agent can
  actually read.
- `reagraph coverage` — an **honest** verification report: what is observed vs
  only inferred vs unverified, with the frontier of "probe here next" and the
  explicit reminder that absence of a finding is not proof of absence.
- `reagraph stats` — totals and confidence breakdown of the accumulated graph.
- `reagraph pull <target>` — connect to **REA** (spawned via `npx rea-agents
  --mcp`, overridable with `$REAGRAPH_REA_CMD`), analyze a target, and fold each
  finding into the graph as it arrives. REAgraph is an MCP *client* of REA; it
  vendors no REA code. Degrades gracefully when REA or `mcp` is absent (use
  `reagraph add`), and when REA itself returns an analysis error it reports it
  and records nothing rather than folding a placeholder.
  A **content-addressed cache** (keyed by the artifact's hash + analysis scope)
  serves a repeat analysis with **no REA call** (`--no-cache` / `--refresh` to
  override). The cache is local today and the backend is pluggable so a shared
  tier can drop in later.
- `reagraph cache stats|clear` — inspect or wipe the local analysis cache
  (`$REAGRAPH_CACHE`, default `~/.cache/reagraph`).
- `reagraph attest --source src.json --shipped shipped.json` — **does the shipped
  artifact match the source?** Structurally aligns a graphify source graph with a
  REAgraph shipped-artifact graph (name match where symbols survive, Weisfeiler-
  Lehman fingerprints where they do not, carrying confidence and abstaining when
  unsure), then reports the **shipped-but-unexplained** residual (the supply-chain
  signal) and a confidence-weighted **integrity score**. High-signal triage, not
  an oracle: "unexplained" means "could not confidently match", not "malicious".
  Alignment uses seed-propagation to recover structure on stripped binaries, still
  abstaining on ties.
- `reagraph doctor` — preflight the live `pull`: checks `mcp`, `node`/`npx`, the
  resolved REA command, and `graphify`, with actionable fixes. A clean doctor
  means prerequisites are present, not that a real REA run was verified.

The analysis cache backend is pluggable: `reagraph.remote_cache.RemoteCacheBackend`
is a **shared-tier client** (read-only by default so you benefit from others'
public analyses without uploading; opt-in push is gated so private artifacts
never leak; fail-open). The hosted server it talks to is the next step, not yet
built (see [docs/CACHE.md](docs/CACHE.md)).
- `reagraph serve` — run the REAgraph **MCP server** (`remember` / `recall` /
  `diff` / `stats`) so any agent uses REAgraph as its reverse-engineering memory
  (needs the `mcp` package).
- `reagraph.bridge` — fold a live stream of findings in incrementally, saving
  after each one (crash-safe) and skipping a bad finding instead of aborting the
  run (so one malformed symbol never discards the whole analysis).

## Quickstart

```bash
pip install -e .

# Fold a REA finding (or any node-link graph) into persistent memory
reagraph add finding.jag.json            # writes/updates ./reagraph.json
reagraph add more_findings.jag.json      # merges, dedups, upgrades confidence

reagraph stats                           # nodes/edges/confidence breakdown

# Structural version diff (byte-agnostic)
reagraph diff app-v1.json app-v2.json    # added / removed / rewired, not "0 unchanged"
```

Query/path/explain over the accumulated graph are delegated to graphify when it
is installed (the graph is written in graphify's node-link format):

```bash
graphify query "where is the license checked" --graph reagraph.json
```

## How it fits

```
  agent (Claude Code / Codex / opencode)
        |  asks REA to analyze a region
        v
      REA  --(finding: evidence + epistemic state)-->  reagraph add
        |                                                   |
        |                                           reagraph.json  (persistent memory)
        |                                                   |
        '---- query/path/explain  <----  graphify  <--------'
```

The agent probes with REA, folds each finding into REAgraph, and queries the
accumulated graph *before* paying for another probe. Cost stops evaporating and
starts compounding.

## Benchmarks

Reproducible harnesses in [`bench/`](bench/) (`python bench/bench_*.py`). Numbers
below are from this machine; they are synthetic and machine-dependent, and the
REA-call latency in the cache benchmark is modeled (real REA analyses take seconds
to minutes), so treat them as orders of magnitude, not guarantees.

- **Diff is byte-agnostic (the #4520 win).** On a build where every symbol's
  location moved but the structure is identical: REAgraph reports **0% changed**;
  a naive location-keyed diff reports **100% changed**.
- **Alignment abstains instead of mis-matching.** Measured against generated
  ground truth: **precision 1.00** both when symbols survive and when the binary is
  stripped; recall **1.00** with symbols, **0.95** stripped (structure-only). All
  injected nodes land in the residual (5/5 stripped, 6/6 tampered). Clean vs
  tampered **integrity separation ~0.10** (0.99 vs 0.89).
- **Summary stays bounded.** A 100k-node graph summarizes to **~1.4 KB** (god nodes
  capped, clusters capped) in ~0.2 s.
- **Scale.** merge ~2-4M nodes/s, diff ~1-3M nodes/s, summarize ~0.5-0.9M nodes/s;
  align ~0.1M nodes/s (heavier: Weisfeiler-Lehman + propagation).
- **Cache.** A repeat analysis is served with **no REA call**: a hit costs ~**90µs**
  vs re-running the analysis. At a modeled 30 s/analysis over 10 repeats that is
  **~270 s saved**; the win compounds across sessions, teammates, and CI.

## Roadmap

See [docs/DESIGN.md](docs/DESIGN.md). Near-term: incremental live-ingest from the
REA MCP server, an MCP server exposing the memory to agents, demand-scoped
re-analysis, and a shared analysis cache keyed by artifact hash.

## License

MIT — see [LICENSE](LICENSE). REAgraph is an independent project and is not
affiliated with REA (morluto/rea) or its authors; see [NOTICE](NOTICE).
