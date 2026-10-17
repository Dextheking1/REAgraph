"""Derive a BUILD-STABLE identity for a REA node so a structural diff stays honest.

REA's JavaScript Application Graph gives every node a `node_id` like
`jag_node_<sha256>` whose hash embeds the BUILD's artifact digest. The SAME
logical module therefore gets a DIFFERENT `node_id` in a different build, so a
naive structural diff of two builds reports nearly everything as changed -- the
churn is an artifact of the id, not of the code. This module re-keys a node from
its `identity` block instead, so an entity that did not actually change keeps the
same id across builds and a diff surfaces only real movement.

Grounding (read-only research, not vendored): REA attaches an `identity` block to
every JAG node describing how that node is identified. Verified real shapes::

    javascript-module: {"strategy":"artifact-local-key","stability":"artifact-version",
                        "artifact_sha256":"<per-build>","namespace":"unresolved-semantic-module","key":"external:fs"}
    asar-entry:        {"strategy":"canonical-path","stability":"artifact-version",
                        "artifact_sha256":"<per-build>","path":"src/main.js"}
    ipc-handler:       {"strategy":"artifact-local-key","stability":"artifact-version",
                        "artifact_sha256":"<per-build>","namespace":"electron-ipc-handler",
                        "key":"main:handle:telemetry:send:24:33-28:1"}
    browser-window:    {"strategy":"artifact-local-key","stability":"artifact-version",
                        "artifact_sha256":"<per-build>","namespace":"electron-browser-window","key":"7:14-9:4"}
    ipc-channel:       {"strategy":"observation-fingerprint","stability":"observation-only",
                        "observation_sha256":"<per-build>","scope":"electron-ipc-channel"}
    artifact (root):   {"strategy":"content-digest","stability":"global-exact","sha256":"<content hash>"}
    javascript-asset:  {"strategy":"content-digest","stability":"global-exact","sha256":"<content hash>"}

Three rules make the derived key build-stable:

  - DROP the volatile per-build fields `artifact_sha256` and `observation_sha256`:
    they change every build and say nothing about whether the entity changed.
  - KEEP `sha256` for `content-digest` / `global-exact` nodes (artifact,
    javascript-asset): there the content hash *is* the identity and only moves
    when the file content moves, so dropping it would merge genuinely different
    assets.
  - NORMALIZE source line:col ranges out of a `key`, because code that merely
    shifts down a file keeps its meaning but changes its range (see
    `normalize_range_tokens`): a pure-range key collapses to just its
    namespace/scope, and a trailing ":<range>" is stripped from a semantic key
    (`main:handle:telemetry:send:24:33-28:1` -> `main:handle:telemetry:send`).

Everything here is deliberately FAIL-OPEN: an identity with no usable stable
component yields None from `stable_key`, and `derive_stable_id` then falls back to
the node's own `node_id`/`id`, so a node is never left identity-less and no shape
of junk ever raises.

Pure stdlib.
"""

from __future__ import annotations

import re

# A source span is a line:col pair, optionally a range to a second line:col pair,
# e.g. "7:14-9:4" or "24:33-28:1". We only ever treat a token as a range when it
# has this exact colon-separated numeric shape, so a real name that merely
# contains digits (e.g. "base64Encode") is never mistaken for one.
_RANGE = r"\d+:\d+(?:-\d+:\d+)?"
# A `key` that is ONLY a span (a node identified purely by where it sits).
_PURE_RANGE_RE = re.compile(r"^" + _RANGE + r"$")
# A ":<span>" suffix on an otherwise semantic key.
_TRAILING_RANGE_RE = re.compile(r":" + _RANGE + r"$")


def normalize_range_tokens(key: str) -> str:
    """Strip source line:col ranges out of an identity `key` so a moved entity is stable.

    The regex is ``\\d+:\\d+(-\\d+:\\d+)?`` (a line:col pair, optionally a range to
    a second pair). Two cases, conservative by design:

      - a *pure* range ("7:14-9:4") -> "" (the caller falls back to namespace/scope);
      - a *trailing* ":<range>" on a semantic key
        ("main:handle:telemetry:send:24:33-28:1") -> the key without it
        ("main:handle:telemetry:send").

    Anything that is not a colon-separated numeric span is left untouched, so a
    real name containing digits ("base64Encode", "external:fs") passes through
    intact. Non-str input is returned unchanged.
    """
    if not isinstance(key, str):
        return key
    k = key.strip()
    if _PURE_RANGE_RE.match(k):
        return ""
    return _TRAILING_RANGE_RE.sub("", k)


def stable_key(identity: dict, kind: str = "") -> str | None:
    """A build-stable, human-readable key for a node's `identity`, or None.

    Composes ``kind`` + ``strategy`` + the stable semantic fields present
    (``namespace`` / ``scope`` / ``path`` / ``key`` normalized via
    `normalize_range_tokens`; ``sha256`` kept ONLY for ``global-exact`` /
    ``content-digest`` nodes). The volatile ``artifact_sha256`` /
    ``observation_sha256`` are always dropped. Fields are emitted in sorted order
    so the result is deterministic and independent of dict ordering.

    Returns None when `identity` is not a dict or carries no usable stable
    component (e.g. only a per-build hash), so the caller can fall back.
    """
    if not isinstance(identity, dict):
        return None

    strategy = identity.get("strategy")
    stability = identity.get("stability")

    fields: dict[str, str] = {}
    for name in ("namespace", "scope", "path"):
        value = identity.get(name)
        if isinstance(value, str) and value:
            fields[name] = value

    raw_key = identity.get("key")
    if isinstance(raw_key, str) and raw_key:
        normalized = normalize_range_tokens(raw_key)
        if normalized:
            fields["key"] = normalized

    # Keep the content digest ONLY where it is itself the stable identity.
    if stability == "global-exact" or strategy == "content-digest":
        sha = identity.get("sha256")
        if isinstance(sha, str) and sha:
            fields["sha256"] = sha

    if not fields:
        return None  # nothing stable to key on

    head = []
    if kind:
        head.append(f"kind={kind}")
    if isinstance(strategy, str) and strategy:
        head.append(f"strategy={strategy}")
    body = [f"{name}={fields[name]}" for name in sorted(fields)]
    return "|".join(head + body)


def derive_stable_id(node: dict) -> str:
    """The id to use for a JAG node: its build-stable key when one exists, else fall back.

    Returns ``f"rea:{stable_key(...)}"`` when the node's `identity` yields a stable
    key, otherwise the node's own ``node_id``/``id`` so nothing is ever left
    identity-less. Never raises on junk (None, {}, a non-dict identity).
    """
    try:
        if isinstance(node, dict):
            kind = node.get("kind")
            key = stable_key(node.get("identity"), kind if isinstance(kind, str) else "")
            if key:
                return f"rea:{key}"
    except Exception:
        pass  # fall through to the fallback id; this never raises
    return _fallback_id(node)


def remap_edges(
    nodes: list,
    edges: list,
    *,
    id_field: str = "node_id",
    src: str = "source_node_id",
    dst: str = "target_node_id",
) -> tuple[dict, list]:
    """Re-key a whole JAG consistently: build raw->stable id_map, rewrite edge endpoints.

    Returns ``(id_map, edges_remapped)`` where ``id_map`` maps each raw node id to
    `derive_stable_id(node)` and every edge is copied with its ``src``/``dst``
    rewritten through the map. An endpoint absent from the map passes through
    unchanged, so a partial graph is never corrupted. Deterministic: the map is
    built in node order and edge order is preserved.

    COLLISION NOTE: if two distinct raw nodes derive the SAME stable id (possible
    when REA's semantic key is genuinely ambiguous, e.g. two anonymous closures at
    the same namespace), both raw ids map to that one stable id and the nodes will
    MERGE on it downstream. That is deterministic and matches REAgraph's dedup
    semantics; `stable_key` is composed to minimize such collisions.
    """
    id_map: dict = {}
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        raw = node.get(id_field)
        if raw is None:
            raw = node.get("id")
        if raw is None:
            continue
        id_map[raw] = derive_stable_id(node)

    remapped = []
    for edge in edges or []:
        if not isinstance(edge, dict):
            remapped.append(edge)
            continue
        new_edge = dict(edge)
        if src in new_edge and new_edge[src] in id_map:
            new_edge[src] = id_map[new_edge[src]]
        if dst in new_edge and new_edge[dst] in id_map:
            new_edge[dst] = id_map[new_edge[dst]]
        remapped.append(new_edge)

    return id_map, remapped


def _fallback_id(node) -> str:
    """The node's own id as a string, or a stringified node as the last resort."""
    if isinstance(node, dict):
        fid = node.get("node_id") or node.get("id")
        if fid is not None:
            return str(fid)
    return str(node)
