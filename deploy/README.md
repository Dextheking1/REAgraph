# Deploying the REAgraph shared-cache server

This is a **reference deployment** of the REAgraph shared-cache server. It is not
an officially hosted instance. Run your own with the files in this directory.

The server is a pure-stdlib Python HTTP service started by
`python -m reagraph.cache_server`. It serves a content-addressed cache of
reverse-engineering analysis fragments so repeat analyses are served without
re-running REA. See [../docs/CACHE_SERVER.md](../docs/CACHE_SERVER.md) for the
protocol and [../docs/CACHE.md](../docs/CACHE.md) for how the client uses it.

The server ships inside the `reagraph` package, which is stdlib-only with no
runtime dependencies, so the image is tiny.

## The server, in short

- CLI: `python -m reagraph.cache_server --host 0.0.0.0 --port 8787 --dir /data`
  plus `--max-value-mb` and `--no-validate-payload`.
- Auth tokens come from the **environment only**, never from CLI args:
  - `REAGRAPH_CACHE_READ_TOKEN` (optional; gates reads if set)
  - `REAGRAPH_CACHE_WRITE_TOKEN` (gates PUT/DELETE if set)
- Storage dir is `--dir`, or `REAGRAPH_CACHE_SERVER_DIR`.
- It speaks **plain HTTP**. Put TLS in front with a proxy or platform.

## Before you expose it

- **Always set a write token** for any internet-facing instance. Open writes
  make the cache a public dumping ground. Set `REAGRAPH_CACHE_WRITE_TOKEN`.
- Set `REAGRAPH_CACHE_READ_TOKEN` too if the cache should not be world-readable.
- **Terminate TLS in front of the server.** Fly does this for you. On a bare VPS
  use a reverse proxy (Caddy, nginx, Traefik) in front of port 8787.
- The `--dir` is a **cache**. Persist it on a volume so it survives restarts, but
  every entry is reproducible, so back it up lazily or not at all. Losing it only
  means the next analyses re-run and refill it.

## Docker

Build from the **repo root** (the Dockerfile lives in `deploy/` but the build
context is the repo root):

```bash
docker build -f deploy/Dockerfile -t reagraph-cache .
```

Run it with a write token and a persistent volume:

```bash
docker run -d \
  -e REAGRAPH_CACHE_WRITE_TOKEN=your-write-token \
  -v reagraph-cache:/data \
  -p 8787:8787 \
  reagraph-cache
```

Add `-e REAGRAPH_CACHE_READ_TOKEN=your-read-token` to gate reads as well.

The image runs as a non-root user, exposes 8787, and has a HEALTHCHECK that hits
`/health` with stdlib `urllib` (there is no curl in the slim base image).

## Docker Compose (plain VPS)

[`compose.yaml`](compose.yaml) builds from the repo-root context, maps a named
volume to `/data`, maps port 8787, and reads the tokens from the host
environment. Run it from the repo root:

```bash
REAGRAPH_CACHE_WRITE_TOKEN=your-write-token \
  docker compose -f deploy/compose.yaml up -d
```

Export `REAGRAPH_CACHE_READ_TOKEN` in the same way if you want to gate reads.
Keep a reverse proxy in front for TLS.

## Fly.io

[`fly.toml`](fly.toml) is a minimal, current-schema config using the placeholder
app name `reagraph-cache`. It builds from `deploy/Dockerfile`, mounts a volume at
`/data`, forces HTTPS, and health-checks `/health`. Deploy from the repo root so
the build context is the repo root:

```bash
# Create or register the app without deploying yet.
fly launch --no-deploy

# Set the write token as a secret (never a CLI arg or a committed file).
fly secrets set REAGRAPH_CACHE_WRITE_TOKEN=your-write-token
# Optional: gate reads too.
fly secrets set REAGRAPH_CACHE_READ_TOKEN=your-read-token

# Create the volume the [mounts] block expects.
fly volumes create reagraph_cache_data --size 1 --region iad

# Deploy.
fly deploy -c deploy/fly.toml
```

Fly terminates TLS for you, so clients reach it over `https://`.

## Pointing a client at it

```python
Cache(backend=RemoteCacheBackend("https://your-host", token=READ_TOKEN))
```

Use the read token (or the write token, which also reads) depending on whether
you gated reads. See [../docs/CACHE.md](../docs/CACHE.md) for the full client
story, including read-only-by-default and opt-in push.
