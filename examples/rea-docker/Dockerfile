# Run REA's MCP server on Linux, where its analyzer does not need the macOS
# Swift/xcrun process-ownership helper (which fails to compile on older Xcode
# Command Line Tools). This is the reproducible way to get a real JavaScript
# Application Graph out of REA from a Mac host.
#
# REA is PINNED (rea-agents 6.7.0) for reproducibility. The source of truth is the
# Graphify-Labs pinned mirror of REA -- https://github.com/Graphify-Labs/rea (MIT,
# forked from morluto/rea @ 39275420) -- so this image does not depend on the
# upstream repo staying available; see ../../NOTICE. Bump REA_VERSION to update.
#
# Build (from this directory):
#   docker build -t reagraph-rea:latest .
#
# If your Docker lacks BuildKit/buildx, use the install+commit fallback instead:
#   docker run --name rea_install node:24-slim \
#     sh -c "apt-get update && apt-get install -y --no-install-recommends procps \
#            && npm install -g rea-agents@6.7.0"
#   docker commit --change 'ENTRYPOINT ["rea-agents","--mcp"]' rea_install reagraph-rea:latest
#   docker rm rea_install
#
# Point reagraph at it (mount the directory holding your artifact; use the path
# INSIDE the container as the pull target):
#   REACMD="docker run --rm -i -v $PWD/out:/out:ro reagraph-rea:latest"
#   reagraph pull /out/app.asar --rea-cmd "$REACMD" --graph app.json --no-cache
#
# REA's analyze_javascript_application wants an ASAR or an extracted JS app. Pack
# a source tree into an ASAR with the bundled packer:
#   docker run --rm -v $PWD/app:/src:ro -v $PWD/out:/out node:24-slim \
#     npx -y @electron/asar pack /src /out/app.asar

FROM node:24-slim

# Pinned REA (rea-agents) version. Kept in lockstep with the Graphify-Labs/rea
# mirror; bump both together when updating.
ARG REA_VERSION=6.7.0

# procps gives REA a procps-compatible `ps`, which its process-ownership provider
# requires before spawning the isolated JS analyzer worker (without it the
# analysis fails with "spawn ps ENOENT"). node:24 satisfies REA's engine
# requirement (^22.19 || ^24.11 || >=26).
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "rea-agents@${REA_VERSION}" \
    && npm cache clean --force

# Start REA as an MCP stdio server. `docker run -i` keeps stdin open for the
# MCP client (reagraph) to speak to it.
ENTRYPOINT ["rea-agents", "--mcp"]
