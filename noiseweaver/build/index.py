"""Variant ledger: ``<staging>/index.json`` — maps ``(asset_id, stage, hash) -> meta`` so a UI/CLI
can list and compare every parameter branch that has been built. A plain diffable JSON ledger (the
volumes are hundreds of variants, not millions)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INDEX_NAME = "index.json"


class Index:
    def __init__(self, staging_root: Path):
        self.path = Path(staging_root) / INDEX_NAME
        self._entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text()).get("entries", [])
            except (json.JSONDecodeError, OSError):
                self._entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": self._entries}, indent=2, sort_keys=True))

    def record(self, asset_id: str, stage: str, hash_: str, params: dict[str, Any],
               input_hash: str | None, created: str) -> None:
        """Upsert one (asset_id, stage, hash) entry."""
        key = (asset_id, stage, hash_)
        self._entries = [
            e for e in self._entries if (e["asset_id"], e["stage"], e["hash"]) != key
        ]
        self._entries.append({
            "asset_id": asset_id, "stage": stage, "hash": hash_, "params": params,
            "input_hash": input_hash, "created": created,
        })
        self._save()

    def variants(self, asset_id: str | None = None, stage: str | None = None) -> list[dict]:
        return [
            e for e in self._entries
            if (asset_id is None or e["asset_id"] == asset_id)
            and (stage is None or e["stage"] == stage)
        ]

    def all(self) -> list[dict]:
        return list(self._entries)
