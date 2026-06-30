"""Content-addressed staging, backed by :class:`noiseweaver.cas.CasStore`.

Each output file is stored once as a blob keyed by its SHA-256 (identical outputs across variants
dedup); a per-stage **manifest** (``{stage, hash, …, outputs: {filename: blob_sha}}``) is itself a
blob; an action pointer ``actions/<stage>/<hash>`` records the manifest hash and, written last,
means "this build is complete". ``work/<stage>/<hash>/`` is the materialized (hardlinked) working
tree handed to / produced by executors — disposable; ``cas/`` is the durable, backup-safe store.

An optional ``ddc`` handle (duck-typed: ``put``/``has``/``materialize``/``action_get``/
``action_put``) is a parallel *shared* store (cross-machine); it is fail-open — any error never
blocks a build. ``artifact_names`` maps a stage to its primary output filename (the pipeline's
knowledge) so :attr:`Artifact.primary` resolves without importing anything pipeline-specific.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..cas import CasStore

META_NAME = "meta.json"          # back-compat sentinel; no meta.json is written into work dirs now
REJECTED_MARKER = ".rejected"    # local mirror of a committed tombstone


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Artifact:
    """A materialized (or to-be-materialized) stage output."""

    stage: str
    hash: str
    dir: Path
    params: dict[str, Any]
    input_hash: str | None
    asset_id: str
    artifacts: list[str] = field(default_factory=list)
    engine_versions: dict[str, str] = field(default_factory=dict)
    created: str | None = None
    recipe_version: str = ""
    authoritative: bool = False
    primary_name: str = ""       # the stage's canonical primary filename (from the pipeline)
    metadata: dict[str, Any] = field(default_factory=dict)  # executor provenance (not hashed)

    @property
    def primary(self) -> Path:
        """The primary artifact file (the parent input to the next stage)."""
        if self.primary_name:
            if (self.dir / self.primary_name).exists():
                return self.dir / self.primary_name
            for a in self.artifacts:  # suffix fallback: "<id>.mesh.fb" matches primary "mesh.fb"
                if a.endswith(self.primary_name):
                    return self.dir / a
        if self.artifacts:
            return self.dir / self.artifacts[0]
        return self.dir

    @property
    def exists(self) -> bool:
        if self.artifacts:
            return all((self.dir / a).exists() for a in self.artifacts)
        return self.dir.exists()


class Staging:
    """Maps ``(stage, hash)`` to content-addressed storage under ``root``."""

    def __init__(self, root: Path, ddc: Any | None = None, *,
                 artifact_names: dict[str, str] | None = None, action_prefix: str = "build"):
        self.root = Path(root)
        self.cas = CasStore(self.root)
        self.work = self.root / "work"
        self.actions = self.root / "actions"
        self._ddc = ddc
        self._artifact_names = artifact_names or {}
        self._action_prefix = action_prefix

    def _action_key(self, stage: str, hash_: str) -> str:
        return f"{self._action_prefix}:{stage}:{hash_}"

    def _action_file(self, stage: str, hash_: str) -> Path:
        return self.actions / stage / hash_

    def _local_manifest_sha(self, stage: str, hash_: str) -> str | None:
        f = self._action_file(stage, hash_)
        if not f.exists():
            return None
        return f.read_text().strip() or None

    def _read_manifest(self, stage: str, hash_: str) -> dict[str, Any] | None:
        sha = self._local_manifest_sha(stage, hash_)
        if sha is None or not self.cas.has(sha):
            return None
        try:
            return self.cas.get_manifest(sha)
        except (OSError, ValueError):
            return None

    def dir_for(self, stage: str, hash_: str) -> Path:
        return self.work / stage / hash_

    def _local_complete(self, stage: str, hash_: str) -> bool:
        man = self._read_manifest(stage, hash_)
        if man is None:
            return False
        return all(self.cas.has(b) for b in man.get("outputs", {}).values())

    def exists(self, stage: str, hash_: str) -> bool:
        if self._local_complete(stage, hash_):
            return True
        if self._ddc is None:
            return False
        try:
            return self._ddc.action_get(self._action_key(stage, hash_)) is not None
        except Exception:  # noqa: BLE001
            return False

    def is_rejected(self, stage: str, hash_: str) -> bool:
        return (self.dir_for(stage, hash_) / REJECTED_MARKER).exists()

    def mark_rejected(self, stage: str, hash_: str, record: dict[str, Any]) -> None:
        d = self.dir_for(stage, hash_)
        if d.exists():
            _atomic_write(d / REJECTED_MARKER, json.dumps(record, indent=2, sort_keys=True))

    def unmark_rejected(self, stage: str, hash_: str) -> None:
        (self.dir_for(stage, hash_) / REJECTED_MARKER).unlink(missing_ok=True)

    def read_meta(self, stage: str, hash_: str) -> dict[str, Any] | None:
        return self._read_manifest(stage, hash_)

    def prepare(self, stage: str, hash_: str) -> Path:
        """A clean work dir for an executor (stale hardlinks removed; ``.rejected`` kept)."""
        d = self.dir_for(stage, hash_)
        if d.exists():
            for p in d.iterdir():
                if p.name == REJECTED_MARKER:
                    continue
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_meta(self, art: Artifact) -> None:
        art.created = art.created or utc_now_iso()
        outputs = self._ingest_outputs(art)
        self._record_manifest(art, outputs)
        self._push_to_ddc(art)

    def load(self, stage: str, hash_: str, asset_id: str = "") -> Artifact | None:
        man = self._read_manifest(stage, hash_)
        if man is None:
            if self._ddc is not None:
                return self._restore_from_ddc(stage, hash_, asset_id)
            return None
        outputs: dict[str, str] = man.get("outputs", {})
        d = self.dir_for(stage, hash_)
        d.mkdir(parents=True, exist_ok=True)
        for filename, blob in outputs.items():
            target = d / filename
            if target.exists():
                continue
            if not self.cas.has(blob):
                return self._restore_from_ddc(stage, hash_, asset_id) if self._ddc else None
            self.cas.materialize(blob, target)
        return self._artifact_from_manifest(man, stage, hash_, d, asset_id)

    def _ingest_outputs(self, art: Artifact) -> dict[str, str]:
        outputs: dict[str, str] = {}
        for filename in art.artifacts:
            path = art.dir / filename
            if path.is_file():
                outputs[filename] = self.cas.put_file(path)
        return outputs

    def _record_manifest(self, art: Artifact, outputs: dict[str, str]) -> str:
        manifest = {
            "stage": art.stage, "hash": art.hash, "asset_id": art.asset_id, "params": art.params,
            "input_hash": art.input_hash, "engine_versions": art.engine_versions,
            "artifacts": art.artifacts, "created": art.created,
            "recipe_version": art.recipe_version, "authoritative": art.authoritative,
            "outputs": outputs,
        }
        if art.metadata:  # additive — absent when none, so manifests without it are unchanged
            manifest["metadata"] = art.metadata
        manifest_sha = self.cas.put_manifest(manifest)
        _atomic_write(self._action_file(art.stage, art.hash), manifest_sha)  # last == complete
        return manifest_sha

    def _push_to_ddc(self, art: Artifact) -> None:
        if self._ddc is None:
            return
        try:
            outputs: dict[str, str] = {}
            for filename in art.artifacts:
                path = art.dir / filename
                if path.is_file():
                    outputs[filename] = self._ddc.put(str(path))
            if not outputs:
                return
            record = {
                "stage": art.stage, "hash": art.hash, "asset_id": art.asset_id,
                "params": art.params, "input_hash": art.input_hash,
                "engine_versions": art.engine_versions, "created": art.created,
                "recipe_version": art.recipe_version, "authoritative": art.authoritative,
                "outputs": outputs,
            }
            self._ddc.action_put(
                self._action_key(art.stage, art.hash), json.dumps(record, sort_keys=True))
        except Exception:  # noqa: BLE001
            pass

    def _restore_from_ddc(self, stage: str, hash_: str, asset_id: str) -> Artifact | None:
        if self._ddc is None:
            return None
        try:
            rec_json = self._ddc.action_get(self._action_key(stage, hash_))
            if rec_json is None:
                return None
            rec = json.loads(rec_json)
            outputs: dict[str, str] = rec.get("outputs", {})
            if not outputs:
                return None
            out_dir = self.prepare(stage, hash_)
            for filename, cas_hash in outputs.items():
                if not self._ddc.has(cas_hash):
                    return None
                self._ddc.materialize(cas_hash, str(out_dir / filename))
            art = self._artifact_from_manifest(rec, stage, hash_, out_dir, asset_id)
            self._record_manifest(art, self._ingest_outputs(art))
            return art
        except Exception:  # noqa: BLE001
            return None

    def _artifact_from_manifest(
        self, man: dict[str, Any], stage: str, hash_: str, d: Path, asset_id: str
    ) -> Artifact:
        outputs = man.get("outputs", {})
        return Artifact(
            stage=stage, hash=hash_, dir=d,
            params=man.get("params", {}), input_hash=man.get("input_hash"),
            asset_id=man.get("asset_id", asset_id),
            artifacts=man.get("artifacts", list(outputs.keys())),
            engine_versions=man.get("engine_versions", {}), created=man.get("created"),
            recipe_version=man.get("recipe_version", ""),
            authoritative=man.get("authoritative", False),
            primary_name=self._artifact_names.get(stage, ""),
            metadata=man.get("metadata", {}),
        )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
