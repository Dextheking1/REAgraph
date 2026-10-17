# Running REA in Docker (a real graph from a Mac host)

REA's JavaScript analyzer runs on Linux without the macOS Swift/xcrun helper that
fails to compile on older Xcode Command Line Tools, so a Linux container is the
reproducible way to get a real JavaScript Application Graph out of REA from a Mac.
This was verified end to end: `reagraph pull` through this image produced a real
56-node / 68-edge graph of a small Electron app.

## 1. Build the image

```bash
docker build -t reagraph-rea:latest examples/rea-docker
```

If your Docker has no BuildKit/buildx, use the install+commit fallback in the
Dockerfile header comment (same result, no builder needed).

## 2. Pack your app into an ASAR (if it is a source tree)

`analyze_javascript_application` wants an ASAR or an extracted JS application, not
an arbitrary source directory. The `@electron/asar` packer ships in the image:

```bash
docker run --rm -v "$PWD/app:/src:ro" -v "$PWD/out:/out" node:24-slim \
  npx -y @electron/asar pack /src /out/app.asar
```

## 3. Pull it into REAgraph

Point `--rea-cmd` at a `docker run` that mounts the artifact directory and starts
the image (its ENTRYPOINT already appends `--mcp`). The pull **target is the path
inside the container**:

```bash
REACMD="docker run --rm -i -v $PWD/out:/out:ro reagraph-rea:latest"
reagraph pull /out/app.asar --rea-cmd "$REACMD" --graph app.json --no-cache
reagraph summary --graph app.json
```

## Notes and caveats

- **macOS file sharing.** Docker Desktop / colima only share some host paths into
  the VM (typically your home directory, not `/tmp`). Keep the app and `out/`
  under `$HOME` or the container will see an empty mount.
- **Cold-start flake.** The first tool call against a freshly built image can
  return `invalid_request` once; `reagraph pull` fails open (reports 0 nodes, no
  crash). Re-run and it succeeds. A persistent (non-`--rm`) REA server avoids it.
- **Version diffing.** REA's node identities are *artifact-version scoped* (they
  embed the build's `artifact_sha256`, and some keys embed source line ranges), so
  two different builds produce different node ids for the same logical module.
  `reagraph diff` keys on identity, so diffing two raw REA builds overstates the
  change. Diff is byte-agnostic for stable-identity graphs (a graphify source
  graph, or the same build); for cross-build comparison REA provides its own
  `compare_application_versions`. See `docs/LIVE_PULL.md`.
