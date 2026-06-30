"""The build *spec* — a JSON document for a staged build: ``{type, stages: {stage: params}}``.

The spec is the only source; everything else is derived + content-addressed. This is generic format
handling — load/save/fork-variant/discover — parameterized by a :class:`SpecFormat` (the on-disk
suffix + a validator the pipeline supplies). A named *variant* is a sibling file
``<id>.<variant><suffix>`` (one file per branch, diffable in git).

A consumer subclasses ``Spec`` to bind its format once (so callers keep a clean
``MySpec.load(path)`` API) — e.g. a plugin's ``Asset`` binds its own ``.<type>.json`` suffix + a
stage validator.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SpecFormat:
    """The on-disk format of a spec: its filename ``suffix`` and a ``validate`` hook that checks the
    document structure + per-stage params (raising on invalid). The default validator is a no-op."""

    suffix: str = ".spec.json"
    validate: Callable[[dict], Any] = staticmethod(lambda d: d)


@dataclass
class Spec:
    id: str          # identity = path relative to the library root (globally unique)
    name: str        # filename stem only — used in output filenames, temp files, promote targets
    type: str
    stages: dict[str, dict[str, Any]]
    path: Path | None = None
    variant: str | None = None
    raw: dict[str, Any] | None = None

    @classmethod
    def _build(cls, data: dict[str, Any], fmt: SpecFormat, path: Path | None,
               spec_id: str | None, name: str | None) -> Spec:
        """Validate + construct — the single, NON-overridable constructor that ``from_dict``/
        ``load``/``fork`` all route through, so a subclass that translates ``from_dict`` arg names
        (e.g. a plugin's ``Asset``) never breaks the internal calls."""
        fmt.validate(data)
        resolved_id = spec_id or data.get("id") or ""
        if not resolved_id:
            raise ValueError("spec id required: pass spec_id=<name> or include 'id' in data")
        return cls(
            id=resolved_id,
            name=name or Path(resolved_id).name,
            type=data["type"],
            stages=data["stages"],
            path=path,
            raw=data,
        )

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        fmt: SpecFormat,
        path: Path | None = None,
        *,
        spec_id: str | None = None,
        name: str | None = None,
    ) -> Spec:
        """Construct from a parsed dict. ``spec_id`` is the canonical identity; ``name`` defaults to
        its stem. Falls back to ``data['id']`` for in-memory construction (tests/fork), but on-disk
        files must NOT carry an ``id`` field."""
        return cls._build(data, fmt, path, spec_id, name)

    @classmethod
    def load(cls, path: str | Path, fmt: SpecFormat, *, root: str | Path | None = None) -> Spec:
        """Load a spec file. When ``root`` (the library root) is given, ``id`` is the path relative
        to it; else it's the bare filename stem. A ``stem.variant`` filename sets ``variant``."""
        path = Path(path)
        data = json.loads(path.read_text())
        stem = path.name[: -len(fmt.suffix)] if path.name.endswith(fmt.suffix) else path.stem
        base_stem = stem.split(".", 1)[0]
        if root:
            try:
                rel = path.resolve().relative_to(Path(root).expanduser().resolve())
                spec_id = str(rel.parent / base_stem)
            except ValueError:
                spec_id = base_stem
        else:
            spec_id = base_stem
        spec = cls._build(data, fmt, path, spec_id, base_stem)
        if "." in stem:
            spec.variant = stem.split(".", 1)[1]
        return spec

    def save(self, fmt: SpecFormat, path: str | Path | None = None) -> Path:
        path = Path(path) if path else self.path
        if path is None:
            raise ValueError("Spec.save needs a path")
        data = {"type": self.type, "stages": self.stages}  # id is the filename, never the content
        fmt.validate(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        self.path = path
        self.raw = data
        return path

    def fork(self, fmt: SpecFormat, variant: str | None, stage: str, **overrides: Any) -> Spec:
        """Branch a stage's params into a new variant spec (not saved). ``variant`` names a saveable
        ``<id>.<variant><suffix>``; ``None`` is an anonymous in-memory fork (a sweep tile, identity
        = its content hash). ``path`` is dropped so a stray save can't clobber the base."""
        new_stages = copy.deepcopy(self.stages)
        new_stages.setdefault(stage, {}).update(overrides)
        data = {"type": self.type, "stages": new_stages}
        forked = type(self)._build(data, fmt, None, self.id, self.name)
        forked.variant = variant
        return forked

    def vary(self, fmt: SpecFormat, stage: str, **overrides: Any) -> Spec:
        """An anonymous in-memory fork (never saved) — for seed/param sweeps."""
        # Call the generic fork explicitly (not self.fork) so a subclass that translates fork's
        # arg names (a plugin's Asset) doesn't intercept this internal call.
        return Spec.fork(self, fmt, None, stage, **overrides)


def discover_specs(root: str | Path, fmt: SpecFormat) -> list[Path]:
    """All spec files anywhere under ``root`` (base + variants), sorted, recursive."""
    return sorted(Path(root).rglob(f"*{fmt.suffix}"))


def find_spec(root: str | Path, ref: str, fmt: SpecFormat) -> Path | None:
    """Locate the spec for ``ref`` (an id, or ``id.variant``). The relative-path id resolves
    directly; a bare stem resolves only when unambiguous (else ``ValueError``). None if no match."""
    root = Path(root)
    direct = root / f"{ref}{fmt.suffix}"
    if direct.is_file():
        return direct
    target = f"{ref}{fmt.suffix}"
    matches = [p for p in discover_specs(root, fmt) if p.name == target]
    if len(matches) > 1:
        ids = ", ".join(str(p.relative_to(root))[: -len(fmt.suffix)] for p in matches)
        raise ValueError(f"ambiguous spec ref {ref!r}: matches {ids} — use the full id")
    return matches[0] if matches else None
