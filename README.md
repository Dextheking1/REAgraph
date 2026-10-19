# REAgraph

**Tell what actually changed between two builds — not what just moved.**

Rebuild an app and a reverse-engineering engine reports almost *everything*
changed, because per-build ids churn. REAgraph reports the one thing that actually
changed:

```
$ reagraph demo
  REA raw id-diff :  59 of 56 nodes "changed"   <- the #4520 problem
  reagraph diff   :  1 changed
    + endpoint  "network endpoint"  src/main.js
```

That's two **real** REA analyses one build apart, diffed by *meaning* instead of by
byte offset — precision/recall/**F1 = 1.00, zero spurious**, validated to 14k
nodes. Try it with zero setup:

```bash
uv tool install reagraph && reagraph demo
```

<!-- Record the GIF with `vhs examples/demo.tape` -> examples/reagraph-demo.gif -->

REAgraph is the persistent-memory + structural-diff layer that sits alongside a
reverse-engineering engine. It folds one-shot findings into a durable graph (so you
and your agent never reverse the same thing twice), diffs two builds by structure,
and attests a shipped artifact against its source. It composes
[REA](https://github.com/morluto/rea) (agent-driven analysis, MIT) and
[graphify](https://github.com/Graphify-Labs/graphify) (source → knowledge graph,
Apache-2.0), and vendors no code from either — it is **not affiliated with or
endorsed by the REA project.**

> Status: early but real. 1300+ tests, works on real REA output, CI on Python
> 3.9–3.13. Live analysis runs REA as a separate process — `reagraph setup-rea`
> gets it in one command (see `docs/LIVE_PULL.md`, `docs/DESIGN.md`).

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
never leak; fail-open). Compose it local-first with `TieredBackend` (warms the
local tier on a remote hit). See [docs/CACHE.md](docs/CACHE.md).
- `reagraph serve-cache` — run the **shared-cache server** the client above talks
  to: a stdlib HTTP service over the same content-addressed store, with public
  read / token-gated write (tokens from the environment, never argv), a sha256
  key allowlist (its own path-traversal defense), an upload size cap, and
  cache-entry-shape validation. See [docs/CACHE_SERVER.md](docs/CACHE_SERVER.md)
  and a reference Docker/Fly deployment in [deploy/](deploy/).
- `reagraph serve` — run the REAgraph **MCP server** (`remember` / `recall` /
  `diff` / `stats`) so any agent uses REAgraph as its reverse-engineering memory
  (needs the `mcp` package).
- `reagraph.bridge` — fold a live stream of findings in incrementally, saving
  after each one (crash-safe) and skipping a bad finding instead of aborting the
  run (so one malformed symbol never discards the whole analysis).

## Install

```bash
uv tool install reagraph              # the reagraph CLI, isolated, on your PATH
uv tool install "reagraph[live]"      # + mcp, for the live pull / serve / compare paths

reagraph install-skill                # install the /reagraph agent skill into ~/.claude/skills
reagraph setup-rea                    # build a local pinned REA image (no external REA repo needed)
```

REAgraph runs REA as a separate process and vendors no REA code; `reagraph
setup-rea` builds a pinned REA Docker image locally (from a Graphify-Labs mirror of
REA, MIT) and prints the exact `REAGRAPH_REA_CMD` — so you never visit the REA repo
or hand-assemble a docker command.

No install needed to try it: `uvx reagraph diff a.json b.json`. Prefer pip? `pip
install "reagraph[live]"`. Development from a checkout: `uv pip install -e ".[live]"`.

## Quickstart

```bash
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

## Use it as a skill (agents talk to it directly)

A ready-made agent skill ships in [`skills/reagraph/`](skills/reagraph/SKILL.md).
Drop it into a Claude Code / agent setup (or copy to `~/.claude/skills/reagraph/`)
and the agent learns the reverse-engineering loop: probe with REA, fold findings
into memory, recall before re-probing, diff builds by meaning, and attest shipped
vs source. Pair it with the MCP server so the agent has live `remember` / `recall`
/ `diff` / `stats` tools over a persistent graph:

```bash
reagraph serve --graph reagraph.json
# MCP client: { "mcpServers": { "reagraph": { "command": "reagraph",
#              "args": ["serve", "--graph", "reagraph.json"] } } }
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

- **Diff is byte-agnostic (the #4520 win) -- on REAL REA output.** Measured on
  real `rea-agents` graphs of an Electron app (`bench/bench_diff_quality.py`,
  fixtures in `bench/fixtures/diff/real`, generated via `examples/rea-docker`):
  REA's node ids are per-build, so a naive id-keyed diff reports **73-80 of 56
  nodes changed** on every build-pair. Matching by build-stable signature,
  `reagraph diff` reports a reformatted-but-identical build as **0 changed** and
  each real mutation (added endpoint / renamed function / removed handler) as
  **exactly its true delta** -- precision/recall/**F1 = 1.00, noise floor 0.00**,
  zero spurious changes, zero data loss.
- **Scales to a real large app.** Analyzed a real 14,344-node / 14,593-edge
  application (webpack + its full dependency tree, ~28 MB ASAR) end to end through
  `reagraph pull`. REA's MCP stdio transport caps the response, so large apps use
  the CLI-JSON transport (`--rea-cli-cmd`); the adapter maps the 14k-node graph in
  well under a second, `reagraph summary` renders it as a bounded overview, and the
  signature diff recovers an injected change from full node-id churn at that scale
  in ~60 ms.
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

See [docs/DESIGN.md](docs/DESIGN.md). The MCP server, live-ingest from REA, and the
shared analysis cache (client + `reagraph serve-cache` server, keyed by artifact
hash) are in the tree. Near-term: demand-scoped re-analysis, a hosted community
cache instance, and harder attestation on stripped native binaries.

## Diff your releases in CI

A reusable GitHub Action runs the byte-agnostic diff on two build graphs and
surfaces the result — a job summary plus, on pull requests, a bot comment that
updates in place: **a bot comment on every release: the real change, not the
thousands of phantom ones.** It works on pre-exported graphs (no REA needed) or
on graphs pulled live from REA in CI.

- Action: [`.github/actions/reagraph-diff`](.github/actions/reagraph-diff/action.yml)
- Example consumer workflow: [`.github/workflows/release-diff.yml`](.github/workflows/release-diff.yml)
- How to use it in your own repo: [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md)

## License

MIT — see [LICENSE](LICENSE). REAgraph is an independent project and is not
affiliated with REA (morluto/rea) or its authors; see [NOTICE](NOTICE).
