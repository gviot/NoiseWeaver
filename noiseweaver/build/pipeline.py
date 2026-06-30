"""A :class:`Pipeline` is what a plugin injects to define its build: the ordered stages and the
per-stage knowledge the generic engine needs to hash + cache them. The engine knows *how* to chain,
hash, store, and cache; the pipeline knows *what* the stages are. A plugin's concrete pipeline (its
real stage chain) stays in the plugin — only this generic shape is open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A resolve-time param transform: (stage, params, spec, context) -> params. Used for pipeline-
# specific hashing inputs (e.g. folding a referenced prompt fragment's text into the params).
ParamHook = Callable[[str, dict[str, Any], Any, Any], dict[str, Any]]


@dataclass(frozen=True)
class Pipeline:
    stages: tuple[str, ...]                              # the chain, in order
    normalize: Callable[[str, dict], dict]              # per-stage validate + fill defaults
    authoritative: frozenset[str] = frozenset()         # stages whose output is NOT reproducible
    # stage -> param names that are file paths (content folded into the hash)
    external_file_params: dict[str, list[str]] = field(default_factory=dict)
    id_dependent_stages: frozenset[str] = frozenset()   # stages whose hash includes the spec id
    recipe_versions: dict[str, str] = field(default_factory=dict)   # stage -> executor version
    artifact_names: dict[str, str] = field(default_factory=dict)    # stage -> primary filename
    param_hooks: tuple[ParamHook, ...] = ()             # extra resolve-time param transforms

    def recipe_version(self, stage: str) -> str:
        return self.recipe_versions.get(stage, "1")
