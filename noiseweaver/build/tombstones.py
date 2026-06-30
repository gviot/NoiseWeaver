"""Durable triage: 'tombstoned' (rejected) artifacts the DAG must never recompute.

A rejection is recorded in two synced places: a committed sidecar ``<base>.tombstones.json`` beside
the spec (the durable, git-portable source of truth — survives ``rm -rf staging``; one sidecar per
*base* id covers the base + all its variants), and a local ``.rejected`` marker inside the
artifact's staging dir (a fast co-located mirror for the viewer). Identity is the Merkle action key
``(stage, hash)`` — a param/recipe change yields a new, un-tombstoned hash that WILL rebuild
(inherent to content addressing). ``resolve`` treats a stage as dead if it's in the sidecar, reports
it, and halts the chain — above the ``--force`` gate, so force can never resurrect a rejection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .spec import SpecFormat

SIDECAR_SUFFIX = ".tombstones.json"


def sidecar_for(spec_path: str | Path, fmt: SpecFormat) -> Path:
    """The tombstone sidecar path for a spec (base id + all its named variants share one)."""
    spec_path = Path(spec_path)
    name = spec_path.name
    stem = name[: -len(fmt.suffix)] if name.endswith(fmt.suffix) else spec_path.stem
    base = stem.split(".", 1)[0]
    return spec_path.parent / f"{base}{SIDECAR_SUFFIX}"


class Tombstones:
    """The committed sidecar of rejected ``(stage, hash)`` artifacts for one asset. Construct via
    ``Tombstones(sidecar_path)``; a spec built purely in memory (no path) gets an empty no-op."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._dead: dict[tuple[str, str], dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                return
            for e in data.get("tombstones", []):
                self._dead[(e["stage"], e["hash"])] = e

    def is_dead(self, stage: str, hash_: str) -> bool:
        return (stage, hash_) in self._dead

    def all(self) -> list[dict[str, Any]]:
        return [self._dead[k] for k in sorted(self._dead)]

    def add(self, stage: str, hash_: str, asset_id: str, reason: str, at: str) -> None:
        self._dead[(stage, hash_)] = {
            "stage": stage, "hash": hash_, "asset_id": asset_id, "reason": reason, "at": at,
        }
        self._save()

    def remove(self, stage: str, hash_: str) -> bool:
        existed = self._dead.pop((stage, hash_), None) is not None
        self._save()
        return existed

    def _save(self) -> None:
        if self.path is None:
            return
        if not self._dead:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "tombstones": self.all()}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
