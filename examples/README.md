# REAgraph examples

A self-contained demo of the two things REAgraph does today: **persistent
memory** and a **byte-agnostic structural diff**.

## Run it

```bash
bash examples/demo.sh
```

The script uses `./.venv/bin/reagraph` if it exists, otherwise falls back to
`python -m reagraph.cli`. It builds a fresh graph at `/tmp/reagraph_demo.json`
(wiped at the start, so it is safely re-runnable).

## What it shows

- **`add` → persistent memory.** A reverse-engineering finding
  (`sample_findings/login.jag.json`, a small REA JAG doc of a login/auth path)
  is folded into a durable graph. The finding stops living in a chat session and
  starts compounding.
- **`stats` → coverage.** Node/edge counts and the observed (`EXTRACTED`) vs
  inferred (`INFERRED`) confidence split of the accumulated graph.
- **`diff` → byte-agnostic version diff.** `login_v2.jag.json` is the *same*
  program with every symbol relocated (addresses and file layout all shifted),
  plus **one** added exfil edge (`session_create → analytics.evil.example`). A
  byte-offset diff would report "everything changed". REAgraph diffs by
  node/edge identity, so it reports the truth: mostly unchanged, one node added,
  one edge added, `session_create` rewired — the real signal isn't buried.

## Live, end-to-end demo (no real REA or Node required)

```bash
bash examples/rea_live_demo.sh
```

This shows the *whole* pipeline the way it runs against real REA, but offline:

```
fake REA MCP server  ->  MCP client  ->  bridge  ->  persistent graph
```

- **The fake REA server** (`fake_rea_mcp_server.py`) is a tiny MCP stdio server
  that stands in for real REA. It advertises REA-like tools
  (`analyze-javascript-application`, `list-detected-ipc-channels`) and returns a
  small, realistic **canned** JAG finding — an Electron-style JS app with
  modules, imports, an IPC channel, and one injected-looking exfil edge
  (`renderer → collect.evil-metrics.example`, marked `inferred`). The canned
  data lives in the `CANNED_JS_ANALYSIS` module constant so it is easy to read.
- **The live loop** (run when the optional `mcp` package is installed): a small
  inline MCP client launches the server over stdio, lists its tools, calls
  `analyze-javascript-application`, and streams the finding through
  `reagraph.bridge` into a durable graph at `/tmp/reagraph_live_demo.json`
  (saved after every merge — crash-safe). It then prints `reagraph stats` and
  `reagraph summary`.
- **Graceful fallback:** the live loop needs `mcp`. If it is not installed, the
  script says so and falls back to the offline path (feeding
  `sample_findings/*.jag.json` through `reagraph add`), reaching the same end
  state. The script **always exits 0**, with or without `mcp`.

### Point at real REA

To drive the pipeline with real REA instead of the fake server, point
`REAGRAPH_REA_CMD` at the real MCP command and re-run — the inline client will
launch that instead:

```bash
export REAGRAPH_REA_CMD="npx -y rea-agents@latest"
bash examples/rea_live_demo.sh
```
