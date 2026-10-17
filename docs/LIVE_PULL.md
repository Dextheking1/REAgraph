# Live pull against REA

`reagraph pull <target>` connects to a running REA (an MCP server), analyzes the
target, and folds each finding into the graph as it arrives. REAgraph is an MCP
client of REA; it vendors no REA code (see docs/REA_INTEGRATION.md).

## Preflight: `reagraph doctor`

Run it before a live pull. It inspects prerequisites and prints actionable fixes;
it does not spawn REA or run any analysis.

```
reagraph doctor
```

Checks:
- Python >= 3.9.
- `mcp` importable (required only for the live pull; the rest of REAgraph works
  without it). Fix: `pip install mcp`.
- `node`/`npx` on PATH (needed only to spawn REA via the npx default).
- The resolved REA command (explicit `--rea-cmd` > `$REAGRAPH_REA_CMD` > the npx
  default), and whether its first token resolves on PATH. Shown, never executed.
- `graphify` on PATH (optional, used by `reagraph query`).

A clean doctor means the prerequisites are present. It does NOT mean a real REA
run has been verified.

## Running it

```
pip install mcp
# The default command already starts REA's MCP server (npx -y rea-agents@latest
# --mcp). Override only if your REA lives elsewhere:
export REAGRAPH_REA_CMD="npx -y rea-agents@latest --mcp"
reagraph pull ./path/to/app            # folds findings into ./reagraph.json
reagraph stats
reagraph summary
```

The `--mcp` flag is what makes REA start as an MCP *stdio server*; the bare CLI
prints help and exits, so without it the handshake closes on connect. The default
command includes it.

Static JavaScript/Electron analysis needs no decompiler, but REA runs its own
analyzer process and so inherits REA's runtime requirements: a recent **Node**
(REA requires `^22.19 || ^24.11 || >=26`) and, on macOS, a **Swift 5.7+**
toolchain (REA compiles a small process-ownership helper at analysis startup; an
older Xcode Command Line Tools fails that compile). Deep native analysis
additionally inherits REA's decompiler requirements (Ghidra/Hopper/IDA).

## What a live pull does

Against the real REA MCP server, `pull`:
- spawns REA with `--mcp`, completes the MCP handshake, and lists its tools
  (REA advertises ~200);
- calls `analyze_javascript_application` with **`input_path`** (REA's required
  argument name) pointing at an ASAR or an extracted JS/Electron app, and folds
  the returned JavaScript Application Graph into `reagraph.json`;
- if REA returns its structured `{"error": {...}}` body (e.g. the analyzer could
  not start in your environment), prints the error and records **nothing** —
  the failure is never folded as an evidence node and never cached, so a later
  run in a fixed environment re-attempts cleanly.

## Honest status

The client, the result adapter (grounded on REA's JavaScript Application Graph
schema), the cache-aware pull, and the `doctor` preflight are in the tree and
tested, and the full pipeline is exercised over a real MCP stdio transport
against a stand-in REA server (`bench/fake_rea_server_e2e.py`) that returns a
realistic JAG. The integration has been run against the real `npx rea-agents
--mcp`: connect, tool discovery, and the `analyze_javascript_application` call
with `input_path` are verified; a non-empty graph from the real analyzer needs a
host that meets REA's Node/Swift requirements above (where those were unmet, REA
returned its startup error and `pull` correctly reported 0 nodes). The adapter is
best-effort and fail-open; the default tool set is overridable with `tools=`, and
the adapter's `SUPPORTED_TOOLS` is the source of truth for what maps to a JAG.
