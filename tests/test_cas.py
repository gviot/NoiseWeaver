"""CasStore: content addressing (dedup, verify), atomic immutable blobs, manifests, and hardlink
materialization."""

from __future__ import annotations

import hashlib

import pytest

from noiseweaver.cas import CasStore


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_put_bytes_is_content_addressed_and_dedups(tmp_path):
    cas = CasStore(tmp_path)
    a = cas.put_bytes(b"hello")
    b = cas.put_bytes(b"hello")
    assert a == b == _sha(b"hello")
    assert cas.has(a)
    assert cas.get_bytes(a) == b"hello"
    # sharded path, one physical blob for identical content
    assert cas.blob_path(a).parent.name == a[:2]


def test_put_file_hashes_content(tmp_path):
    cas = CasStore(tmp_path)
    src = tmp_path / "x.bin"
    src.write_bytes(b"some output bytes")
    sha = cas.put_file(src)
    assert sha == _sha(b"some output bytes")
    assert cas.get_bytes(sha) == b"some output bytes"


def test_blobs_are_read_only(tmp_path):
    cas = CasStore(tmp_path)
    sha = cas.put_bytes(b"immutable")
    mode = cas.blob_path(sha).stat().st_mode & 0o777
    assert mode == 0o444


def test_missing_blob_raises(tmp_path):
    cas = CasStore(tmp_path)
    assert not cas.has("0" * 64)
    with pytest.raises(KeyError):
        cas.blob_path("0" * 64)


def test_materialize_hardlinks_no_extra_bytes(tmp_path):
    cas = CasStore(tmp_path)
    sha = cas.put_bytes(b"payload")
    dest = tmp_path / "work" / "out.bin"
    cas.materialize(sha, dest)
    assert dest.read_bytes() == b"payload"
    # hardlink: same inode as the blob (no second copy on disk)
    assert dest.stat().st_ino == cas.blob_path(sha).stat().st_ino


def test_materialize_overwrites_existing(tmp_path):
    cas = CasStore(tmp_path)
    sha = cas.put_bytes(b"v2")
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"v1-stale")
    cas.materialize(sha, dest)
    assert dest.read_bytes() == b"v2"


def test_manifest_roundtrip_and_dedup(tmp_path):
    cas = CasStore(tmp_path)
    manifest = {"outputs": {"concept.png": "ab" * 32}, "stage": "concept"}
    h1 = cas.put_manifest(manifest)
    h2 = cas.put_manifest(dict(reversed(list(manifest.items()))))  # key order irrelevant
    assert h1 == h2  # canonical JSON → same hash
    assert cas.get_manifest(h1) == manifest
