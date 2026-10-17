# REAgraph shared-cache server.
# Build from the repo root (the build context is the repo root, not deploy/):
#   docker build -f deploy/Dockerfile -t reagraph-cache .
FROM python:3.12-slim

WORKDIR /app

# The server ships inside the `reagraph` package; copy the whole project and
# install it. The package is stdlib-only (no runtime deps), so this stays tiny.
COPY . /app
RUN pip install --no-cache-dir .

# Run as a non-root user and let it own the data dir.
RUN useradd --create-home --uid 10001 reagraph \
    && mkdir -p /data \
    && chown -R reagraph:reagraph /data
USER reagraph

# Content-addressed cache storage. Also read from REAGRAPH_CACHE_SERVER_DIR.
ENV REAGRAPH_CACHE_SERVER_DIR=/data
VOLUME /data

EXPOSE 8787

# No curl in slim; probe /health with stdlib urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3)" || exit 1

# Auth tokens come from the environment only, never CLI args:
#   REAGRAPH_CACHE_READ_TOKEN  (optional; gates reads if set)
#   REAGRAPH_CACHE_WRITE_TOKEN (gates PUT/DELETE if set)
CMD ["python", "-m", "reagraph.cache_server", "--host", "0.0.0.0", "--port", "8787", "--dir", "/data"]
