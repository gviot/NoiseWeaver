"""A content-addressed store: immutable blobs + JSON manifests, each named by the SHA-256 of its
bytes.

Why CAS: objects named by their content dedup automatically (identical outputs across build variants
are stored once), verify on read, and — being write-once — are the one store you can safely
back up as a plain folder (no live-database torn-write problem). That makes a CAS the natural home
for a build cache you point Backblaze straight at.

This module is generic — no pipeline or build-DAG knowledge. A consumer ingests files with
``put_file``, records what it produced as a manifest with ``put_manifest`` (a dict mapping output
names → blob hashes), and later rebuilds a working tree with ``materialize`` (a hardlink, so no
bytes are copied and nothing is duplicated on disk).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB streaming reads


class CasStore:
    """Blobs + manifests under ``<root>/cas/<ab>/<sha256>`` (sharded by the first byte of the hash).

    Blobs are written once and made read-only (0444) so a stray write can't corrupt content that
    other artifacts are hardlinked to. All writes are atomic (temp file + rename)."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()
        self.blobs = self.root / "cas"

    def _path(self, sha: str) -> Path:
        return self.blobs / sha[:2] / sha

    def has(self, sha: str) -> bool:
        return self._path(sha).exists()

    def blob_path(self, sha: str) -> Path:
        p = self._path(sha)
        if not p.exists():
            raise KeyError(f"blob {sha} not in CAS")
        return p

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self._path(sha)
        if not dest.exists():
            self._commit(dest, lambda tmp: Path(tmp).write_bytes(data))
        return sha

    def put_file(self, src: Path | str) -> str:
        """Ingest a file's content as a blob; returns its sha. A second ingest of identical content
        is a no-op (same hash → already present)."""
        src = Path(src)
        sha = _sha256_file(src)
        dest = self._path(sha)
        if not dest.exists():
            self._commit(dest, lambda tmp: shutil.copyfile(src, tmp))
        return sha

    def get_bytes(self, sha: str) -> bytes:
        return self.blob_path(sha).read_bytes()

    def materialize(self, sha: str, dest: Path | str) -> Path:
        """Place blob ``sha`` at ``dest`` — a hardlink (shares bytes with the blob, zero copy) when
        possible, else a copy (cross-device / no-hardlink filesystem). Overwrites ``dest``."""
        dest = Path(dest)
        src = self.blob_path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        try:
            os.link(src, dest)
        except OSError:
            shutil.copyfile(src, dest)
        return dest

    # manifests are just canonical-JSON blobs, so they dedup + verify like any other content.
    def put_manifest(self, obj: dict) -> str:
        return self.put_bytes(_canonical(obj))

    def get_manifest(self, sha: str) -> dict:
        return json.loads(self.get_bytes(sha))

    def _commit(self, dest: Path, write_tmp) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
        os.close(fd)
        try:
            write_tmp(tmp)
            os.chmod(tmp, 0o444)  # immutable blob
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
