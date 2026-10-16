"""Tests for content-addressed artifact identity."""

from __future__ import annotations

import hashlib

import pytest

from reagraph.artifact import artifact_id, hash_dir, hash_file, short


def test_file_hash_matches_hashlib_and_is_stable(tmp_path):
    p = tmp_path / "blob.bin"
    data = b"the quick brown fox" * 100
    p.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert hash_file(p) == expected
    assert hash_file(p) == expected  # stable across calls


def test_identical_content_hashes_equal(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"same bytes")
    b.write_bytes(b"same bytes")
    assert hash_file(a) == hash_file(b)


def test_one_byte_change_differs(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"content-A")
    b.write_bytes(b"content-B")
    assert hash_file(a) != hash_file(b)


def _build_tree(root, files):
    for rel, content in files:
        fp = root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)


def test_hash_dir_is_order_independent(tmp_path):
    files = [
        ("x/one.txt", b"one"),
        ("y/two.txt", b"two"),
        ("z/three.txt", b"three"),
    ]
    one = tmp_path / "tree1"
    two = tmp_path / "tree2"
    one.mkdir()
    two.mkdir()
    _build_tree(one, files)
    _build_tree(two, list(reversed(files)))  # written in a different order
    assert hash_dir(one) == hash_dir(two)


def test_hash_dir_changes_on_content_change(tmp_path):
    base = tmp_path / "base"
    changed = tmp_path / "changed"
    base.mkdir()
    changed.mkdir()
    _build_tree(base, [("a.txt", b"hello"), ("b.txt", b"world")])
    _build_tree(changed, [("a.txt", b"hello"), ("b.txt", b"WORLD!")])
    assert hash_dir(base) != hash_dir(changed)


def test_hash_dir_changes_when_file_added(tmp_path):
    base = tmp_path / "base"
    more = tmp_path / "more"
    base.mkdir()
    more.mkdir()
    _build_tree(base, [("a.txt", b"hello")])
    _build_tree(more, [("a.txt", b"hello"), ("b.txt", b"extra")])
    assert hash_dir(base) != hash_dir(more)


def test_hash_dir_skips_symlinks(tmp_path):
    base = tmp_path / "base"
    linked = tmp_path / "linked"
    base.mkdir()
    linked.mkdir()
    _build_tree(base, [("real.txt", b"real")])
    _build_tree(linked, [("real.txt", b"real")])
    # A dangling + a valid symlink must not change the digest.
    (linked / "ptr.txt").symlink_to(linked / "real.txt")
    (linked / "dangling.txt").symlink_to(linked / "nope.txt")
    assert hash_dir(base) == hash_dir(linked)


def test_artifact_id_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"payload")
    aid = artifact_id(p)
    assert aid.startswith("sha256:")
    assert aid == "sha256:" + hashlib.sha256(b"payload").hexdigest()


def test_artifact_id_dispatches_dir_vs_file(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"data")
    d = tmp_path / "d"
    d.mkdir()
    (d / "f.bin").write_bytes(b"data")
    # Same byte content, different shape -> different ids.
    assert artifact_id(f).startswith("sha256:")
    assert artifact_id(d).startswith("sha256:")
    assert artifact_id(f) != artifact_id(d)


def test_artifact_id_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        artifact_id(tmp_path / "does-not-exist")


def test_short_form():
    aid = "sha256:" + "a" * 64
    assert short(aid) == "sha256:" + "a" * 12
    assert short(aid, 6) == "sha256:aaaaaa"


def test_large_file_streams(tmp_path):
    p = tmp_path / "big.bin"
    block = b"\xde\xad\xbe\xef" * 256 * 1024  # 1 MiB
    h = hashlib.sha256()
    with open(p, "wb") as f:
        for _ in range(4):  # ~4 MiB
            f.write(block)
            h.update(block)
    # Small chunk size forces many streaming reads.
    assert hash_file(p, chunk=64 * 1024) == h.hexdigest()
