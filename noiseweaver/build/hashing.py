"""Content addressing for a staged build DAG.

Each stage's artifact lives at ``staging/<stage>/<hash>`` where::

    hash(stage) = sha256(stage_name + canonical_json(params) + parent_hash + recipe_version)

``parent_hash`` is the upstream stage's hash, so the path hashes *all* upstream params — every
parameter branch coexists on disk and is cheaply cached. ``recipe_version`` is the executor
implementation version (bumping it invalidates that stage + descendants); it is **caller-supplied
data** (the pipeline owns the per-stage versions), defaulting to ``"1"``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Truncated digest used for staging dir names: long enough to avoid collisions in one user's
# library, short enough to keep paths readable. This is the Merkle *action* key, NOT a CAS blob key
# (blob keys are the full 64-char SHA-256).
HASH_LEN = 16


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8. Two semantically equal
    param dicts hash identically regardless of key order or formatting."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stage_hash(
    stage_name: str,
    params: dict[str, Any],
    parent_hash: str | None,
    *,
    recipe_version: str = "1",
) -> str:
    """Merkle hash for one stage. ``parent_hash`` is "" for the root. ``recipe_version`` is folded
    in so bumping an executor invalidates the cache without touching params."""
    h = hashlib.sha256()
    h.update(stage_name.encode("utf-8"))
    h.update(b"\0")
    h.update(canonical_json(params).encode("utf-8"))
    h.update(b"\0")
    h.update((parent_hash or "").encode("utf-8"))
    h.update(b"\0")
    h.update(recipe_version.encode("utf-8"))
    return h.hexdigest()[:HASH_LEN]


def file_sha256(path: str | Path) -> str:
    """Content hash of a file (e.g. a user-supplied external input), folded into the affected
    stage's params so a changed file forces a rebuild. Returns HASH_LEN hex chars."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:HASH_LEN]
