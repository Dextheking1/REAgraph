"""Stable content-addressed identity for an artifact (the thing REA analyzes).

An artifact is a file (a binary, an app bundle) or a directory tree (an unpacked
app, an asar). Its id is derived purely from its *content*, so the same bytes
always yield the same id regardless of path, mtime, or filesystem iteration
order. That id is the key the shared cache is built on: identical input -> cache
hit, one changed byte -> cache miss.

    artifact_id("/bin/thing")   -> "sha256:<64 hex>"
    short(artifact_id(...))     -> "sha256:ab12cd34ef56"

Files are hashed in chunks so a multi-gigabyte binary never loads into memory.
Directories hash the sorted list of (relative_posix_path, file_sha256) pairs, so
the digest is independent of walk order. Symlinks are skipped. Pure stdlib.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def hash_file(path, *, chunk=1 << 20) -> str:
    """Streaming sha256 hex of a file, read `chunk` bytes at a time so an
    arbitrarily large file never loads into memory at once."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def hash_dir(path) -> str:
    """Deterministic digest of a directory tree: sha256 over the sorted list of
    (relative_posix_path, file_sha256) pairs. Sorting by path makes the id stable
    regardless of filesystem iteration order. Symlinks are skipped (we hash the
    tree's real file content, not where links point); nothing is ignored."""
    root = Path(path)
    entries = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Drop symlinked directories so os.walk never follows them.
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            fp = Path(dirpath) / name
            if fp.is_symlink():
                continue  # skip symlinks safely
            rel = fp.relative_to(root).as_posix()
            entries.append((rel, hash_file(fp)))
    entries.sort()
    h = hashlib.sha256()
    for rel, digest in entries:
        # Length-prefix-free but unambiguous: NUL separators, newline per entry.
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def artifact_id(path) -> str:
    """Content id of a file or directory as "sha256:<hex>". Dispatches on isdir.
    Raises FileNotFoundError for a missing path — the one acceptable raise."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    digest = hash_dir(p) if p.is_dir() else hash_file(p)
    return f"sha256:{digest}"


def short(artifact_id, n=12) -> str:
    """Short human-readable form, e.g. "sha256:ab12cd34ef56". Keeps the algorithm
    prefix and the first `n` hex chars of the digest."""
    prefix, _, digest = artifact_id.partition(":")
    return f"{prefix}:{digest[:n]}"
