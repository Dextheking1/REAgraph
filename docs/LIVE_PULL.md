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

## Running REA in Docker (a real graph from a Mac)

REA's analyzer runs on Linux without the macOS Swift/xcrun helper that blocks it
on an older Xcode toolchain, so a Linux container is the reproducible way to get a
real graph from a Mac host. Verified end to end: `reagraph pull` through a REA
container produced a real 56-node / 68-edge graph of a small Electron app. See
[examples/rea-docker/](../examples/rea-docker/) for the image and the exact steps:
build the image (node >= 24.11 plus `procps`, which REA needs to spawn its
analyzer), pack your app into an ASAR, then point `--rea-cmd` at a `docker run`
that mounts the artifact and starts the server, using the in-container path as the
target:

```bash
REACMD="docker run --rm -i -v $PWD/out:/out:ro reagraph-rea:latest"
reagraph pull /out/app.asar --rea-cmd "$REACMD" --graph app.json --no-cache
```

## Large apps: the CLI-JSON transport

REA's MCP stdio response has a size budget. A large application's full graph
overflows it: the analysis completes but the tool returns
`resource_constraint ... exceeds the stdio response budget` and `pull` records
nothing. REA's CLI emits the complete graph as JSON instead, so for large apps use
the CLI transport:

```bash
# CLI base = the REA CLI (NOT the --mcp server). For Docker, override the entrypoint:
CLI="docker run --rm -v $PWD/out:/out:ro --entrypoint rea-agents reagraph-rea:latest"
reagraph pull /out/big.asar --rea-cli-cmd "$CLI" --graph big.json --no-cache
```

`reagraph pull --rea-cli-cmd <base>` runs `<base> analyze <target> --format json
--full-output`, parses the `{ok, data, meta}` envelope, and folds the same graph
the MCP tool would -- no stdio size cap. Verified on a real 14k-node / 15k-edge
application (webpack + its dependency tree, ~28 MB ASAR, ~200 MB of analysis JSON);
the adapter maps it in well under a second and `reagraph diff` recovers an injected
change from full node-id churn at that scale in ~60 ms. The content-addressed cache
applies to the CLI transport too (keyed with a `transport=cli` scope).

## Version diffing is not raw-build diffing

REA's node identities are deliberately *artifact-version scoped*: a node id embeds
the build's `artifact_sha256`, and some identity keys embed source line ranges, so
the same logical module gets a different id in a different build. `reagraph diff`
keys on identity, so diffing two raw REA builds **overstates** the change (it
reports unchanged modules as removed+added). Diff is byte-agnostic for
stable-identity graphs -- a graphify source graph, or the same build re-folded --
which is the `attest` and dedup path it was built for. For authoritative
cross-build comparison REA provides its own `compare_application_versions` tool;
wiring a `reagraph compare` that delegates to it (as `query` delegates to graphify)
is the honest fix and is tracked in the roadmap.

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
