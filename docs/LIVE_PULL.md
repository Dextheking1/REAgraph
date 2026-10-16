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
export REAGRAPH_REA_CMD="npx -y rea-agents@latest"   # or your REA MCP command
reagraph pull ./path/to/app            # folds findings into ./reagraph.json
reagraph stats
reagraph summary
```

Static JavaScript/Electron analysis needs no native engine. Deep native analysis
inherits REA's own requirements (Ghidra/Hopper/IDA) and setup.

## Honest status

The client, the result adapter (grounded on REA's JavaScript Application Graph
schema), the cache-aware pull, and the `doctor` preflight are in the tree and
tested, and the full loop is exercised offline against a fake REA MCP server
(`examples/fake_rea_mcp_server.py`, `examples/rea_live_demo.sh`). An end-to-end
run against the real `npx rea-agents` has NOT been verified in this repo's CI (it
needs Node, REA, and in the native case a decompiler). If you run it against real
REA and hit a tool-name or output-shape mismatch, that is expected surface area:
`rea_adapter` is best-effort and fail-open, and the default tool set is overridable
with `tools=` / and the adapter's `SUPPORTED_TOOLS` is the source of truth.
