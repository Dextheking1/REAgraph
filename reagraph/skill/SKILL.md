---
name: reagraph
description: "Reverse-engineering memory for agents. Use when reverse engineering a binary or app, diffing two builds, or checking whether a shipped artifact matches its source. REAgraph is the persistent, queryable graph layer that sits between REA (agent-driven RE engine) and graphify (source knowledge graph): it folds one-shot RE findings into a durable graph so you never reverse the same thing twice, diffs two builds by meaning instead of byte offset, and flags shipped-but-unexplained structure. Trigger: /reagraph."
---

# reagraph

REAgraph turns throwaway reverse-engineering findings into a **persistent, queryable
graph** of a program. It is composition, not a monolith: it is an MCP *client* of
[REA](https://github.com/morluto/rea) (the analysis engine) and writes graphs in
[graphify](https://github.com/Graphify-Labs/graphify)'s format (the query engine).
It vendors no code from either.

Use it to plug the three holes a raw RE engine has:
1. **No memory** — findings evaporate with the chat session; REAgraph folds each
   into a durable `reagraph.json` with dedup + confidence upgrade.
2. **No reliable diff** — byte-offset diffs report "everything changed" when code
   merely moved or was rebuilt; REAgraph diffs by stable *signature*.
3. **Unamortized cost** — a content-addressed cache serves a repeat analysis with
   no REA call; a shared tier lets a team/community benefit from each other's runs.

## Agents talk to it directly (MCP)

Run the MCP server and point your agent/CLI at it; the agent then has
`remember` / `recall` / `diff` / `stats` tools backed by the persistent graph:

```bash
pip install "reagraph[live]"              # installs reagraph + mcp
reagraph serve --graph reagraph.json      # MCP stdio server over the memory
```

Claude Code / any MCP client config:

```json
{ "mcpServers": {
    "reagraph": { "command": "reagraph", "args": ["serve", "--graph", "reagraph.json"] }
} }
```

The agent now **remembers** across sessions: it probes with REA, calls `remember`
to fold the finding in, and calls `recall` *before* paying for another probe.

## The loop (what the agent should do)

```
preflight -> probe with REA -> fold findings into memory -> recall before re-probing
          -> version-diff builds -> attest shipped vs source
```

1. **Preflight.** `reagraph doctor` checks `mcp`, `node`/`npx`, the resolved REA
   command, and `graphify`. A clean doctor means prerequisites are present.
2. **Analyze + fold (live).** `reagraph pull <target>` spawns REA, analyzes the
   target, and folds each finding into the graph as it arrives (crash-safe,
   fail-open, cache-aware). Or fold a finding you already have: `reagraph add
   finding.jag.json`.
3. **Recall.** Query the accumulated graph before re-probing — delegated to
   graphify: `graphify query "where is the license checked" --graph reagraph.json`.
4. **Map a huge graph.** `reagraph summary` gives a bounded god-node/cluster
   overview so a gigabyte-scale analysis becomes a short read; `reagraph coverage`
   reports what is observed vs only inferred vs unverified (honest gaps).
5. **Version-diff.** `reagraph diff a.json b.json` — byte-agnostic (see below) —
   or `reagraph compare <left> <right>` for REA's **authoritative** exact-matching
   comparison (it analyzes both builds and runs REA's own
   `compare_application_versions`), reporting added/removed/changed/unchanged.
6. **Attest.** `reagraph attest --source src.json --shipped shipped.json` — does the
   shipped artifact match the source? Reports the shipped-but-unexplained residual
   (the supply-chain signal) and a confidence-weighted integrity score. High-signal
   triage, not an oracle.

## Running REA (the analysis engine)

REA is a Node MCP server. The default command is `npx -y rea-agents@latest --mcp`
(override with `--rea-cmd` or `$REAGRAPH_REA_CMD`). Two things to know:

- **On macOS, run REA in Docker/Linux.** Its analyzer needs a toolchain (Swift)
  that older Xcode CLTs lack. See `examples/rea-docker/` for a ready image. Point
  REAgraph at it: `reagraph pull /out/app.asar --rea-cmd "docker run --rm -i -v
  $PWD/out:/out:ro reagraph-rea:latest"` (use the in-container path as the target).
- **Large apps: use the CLI transport.** REA's MCP stdio response is size-capped;
  a large app overflows it (`resource_constraint`). Use
  `reagraph pull <target> --rea-cli-cmd "<REA CLI base>"` to analyze via REA's
  complete JSON output instead. Verified on a real 14k-node app.

REA wants an **ASAR or an extracted JS application** (its
`analyze_javascript_application` tool takes `input_path`). Pack a source tree with
`npx @electron/asar pack src app.asar`.

- **Demand-scope a re-analysis.** After a small change, don't re-analyze the whole
  app: `reagraph pull <target> --source-path src/main.js --source-path src/auth.js`
  scopes REA to those exact inventory files (repeatable). Scope is part of the cache
  key, so each scope is its own entry.

## Byte-agnostic version diff (the headline)

REA's node ids are per-build (they embed a build hash), so diffing two builds by id
reports nearly the whole app as changed. `reagraph diff` defaults to **signature
matching** when the graphs are REA graphs: it keys nodes on kind + range-normalized
label + source file (kind-aware for cross-file entities like IPC channels), so a
reformatted or rebuilt-but-identical build reads as **unchanged** and only real
structural change surfaces. Force the mode with `--by {auto,id,signature}`.

Measured on real REA output: a raw id-diff reports 73-80 node changes per build-pair
on a 56-node app (and ~13,500 on a 14k-node app); the signature diff reports **0**
for a reformatted-identical build and exactly the true delta otherwise
(precision/recall/F1 = 1.00, zero spurious). Note REA's own analysis has a ~0.2%
run-to-run node-set floor at scale, which the diff reports faithfully rather than
hiding.

## Shared cache (optional, team/community)

`reagraph pull` caches each analysis by artifact hash + scope. Share the cache so a
repeat analysis anyone has already run is served with no REA call:

```bash
reagraph serve-cache --dir /var/lib/reagraph-cache   # host it (token-gate writes)
# consumers: Cache(backend=RemoteCacheBackend("https://your-host", token=READ_TOKEN))
```

Read-only by default (benefit from others, upload nothing); opt-in push is gated so
private artifacts never leak. See `docs/CACHE_SERVER.md` and `deploy/`.

## Honest scope

- "Unexplained" in `attest` means "could not confidently match", not "malicious".
- `reagraph diff` is a fast offline signature heuristic; `reagraph compare` is REA's
  authoritative exact-matching comparison (needs REA, analyzes both builds). Use
  `diff` for a quick offline pass on graphs you already have, `compare` when you want
  REA's own verdict.
- REAgraph degrades gracefully: with no REA/`mcp` installed, `reagraph add` and the
  diff/summary/coverage/attest commands still work on any node-link graph.

See the repo README and `docs/` for details.
