"""Build-DAG resolution and execution — the generic engine.

``resolve`` computes every stage's content hash (hashes depend only on params + parent hash, so the
full plan — cached vs would-run — is known without executing anything; this powers ``--dry-run``).
``build`` then executes only uncached stages, dispatching each to its injected executor.

The engine is parameterized by a :class:`Pipeline` (the stage names/order/hashing knowledge) and a
``{stage: Executor}`` map. It knows nothing about what concept/cutout/mesh *are* — that lives in the
plugin (and is faked in tests).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .hashing import file_sha256, stage_hash
from .index import Index
from .pipeline import Pipeline
from .spec import Spec
from .staging import META_NAME, Artifact, Staging
from .tombstones import Tombstones


@dataclass
class StagePlan:
    stage: str
    params: dict[str, Any]
    parent_hash: str | None
    hash: str
    cached: bool
    dir: Path
    tombstoned: bool = False


@dataclass
class StageContext:
    """Everything an executor needs: its params, the upstream artifact, where to write. ``config``
    is the opaque per-build context the caller passes (e.g. the plugin's resolved Config)."""

    spec: Spec
    stage: str
    params: dict[str, Any]
    parent: Artifact | None
    out_dir: Path
    config: Any = None

    @property
    def asset(self) -> Spec:  # back-compat alias for callers that name specs "assets"
        return self.spec


@dataclass
class ExecResult:
    artifacts: list[str] = field(default_factory=list)
    engine_versions: dict[str, str] = field(default_factory=dict)
    # Free-form provenance an executor wants recorded in the node's manifest (NOT hashed — the stage
    # hash is params only). E.g. the concept stage records the assembled prompt it actually sent.
    metadata: dict[str, Any] = field(default_factory=dict)


class Executor(Protocol):
    def __call__(self, ctx: StageContext) -> ExecResult | None: ...


@dataclass
class StageStatus:
    stage: str
    hash: str
    status: str  # "cached" | "built" | "would-run" | "skipped"
    artifact: Artifact | None = None


@dataclass
class BuildResult:
    asset_id: str
    stages: list[StageStatus] = field(default_factory=list)

    def final(self) -> Artifact | None:
        for s in reversed(self.stages):
            if s.artifact is not None:
                return s.artifact
        return None


def resolve(
    spec: Spec,
    pipeline: Pipeline,
    *,
    staging: Staging,
    tombstones: Tombstones | None = None,
    context: Any = None,
    stages: tuple[str, ...] | None = None,
) -> list[StagePlan]:
    """Compute the hash + cache status for each stage in the chain. A stage is ``tombstoned`` if it
    is in the spec's committed sidecar (reported but never executed)."""
    plans: list[StagePlan] = []
    parent_hash: str | None = None
    for stage in (stages or pipeline.stages):
        params = pipeline.normalize(stage, spec.stages.get(stage, {}))
        # Fold external file content into params so a changed file forces a rebuild.
        for param_name in pipeline.external_file_params.get(stage, []):
            val = params.get(param_name)
            if val and Path(val).exists():
                params = {**params, f"{param_name}_sha": file_sha256(val)}
        # Pipeline-specific resolve-time param transforms (e.g. folding referenced prompt text).
        for hook in pipeline.param_hooks:
            params = hook(stage, params, spec, context)
        # Id-dependent stages bake the spec id into their output, so it's a real hash input.
        if stage in pipeline.id_dependent_stages:
            params = {**params, "asset_id": spec.id}
        h = stage_hash(stage, params, parent_hash, recipe_version=pipeline.recipe_version(stage))
        dead = bool(tombstones and tombstones.is_dead(stage, h))
        plans.append(StagePlan(
            stage=stage, params=params, parent_hash=parent_hash, hash=h,
            cached=staging.exists(stage, h), dir=staging.dir_for(stage, h), tombstoned=dead))
        parent_hash = h
    return plans


def build(
    spec: Spec,
    pipeline: Pipeline,
    executors: dict[str, Executor],
    *,
    staging: Staging,
    index: Index,
    tombstones: Tombstones | None = None,
    context: Any = None,
    config: Any = None,
    stages: tuple[str, ...] | None = None,
    until: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> BuildResult:
    """Resolve the DAG and execute uncached stages. ``dry_run`` reports cached vs would-run;
    ``force`` rebuilds cached stages; ``until`` stops after the stage. A tombstoned stage is
    ``skipped`` (never executed, beats ``force``, halts the chain). ``context`` feeds resolve hooks;
    ``config`` is the opaque per-build context handed to executors."""
    plans = resolve(spec, pipeline, staging=staging, tombstones=tombstones,
                    context=context, stages=stages)
    result = BuildResult(asset_id=spec.id)
    parent: Artifact | None = None

    for plan in plans:
        if plan.tombstoned:
            log(f"  {plan.stage:8s} {plan.hash}  skipped (rejected)")
            result.stages.append(StageStatus(plan.stage, plan.hash, "skipped"))
            break

        cached = plan.cached and not force

        if dry_run:
            status = "cached" if cached else "would-run"
            log(f"  {plan.stage:8s} {plan.hash}  {status}")
            result.stages.append(StageStatus(plan.stage, plan.hash, status))
            if until and plan.stage == until:
                break
            continue

        if cached:
            art = staging.load(plan.stage, plan.hash, spec.id)
            log(f"  {plan.stage:8s} {plan.hash}  cached")
            result.stages.append(StageStatus(plan.stage, plan.hash, "cached", art))
            if art is not None:
                index.record(spec.id, plan.stage, plan.hash, plan.params,
                             plan.parent_hash, art.created or "")
            parent = art
        else:
            art = _execute(plan, spec, pipeline, parent, executors, config, staging, index, log)
            result.stages.append(StageStatus(plan.stage, plan.hash, "built", art))
            parent = art

        if until and plan.stage == until:
            break

    return result


def _execute(
    plan: StagePlan,
    spec: Spec,
    pipeline: Pipeline,
    parent: Artifact | None,
    executors: dict[str, Executor],
    config: Any,
    staging: Staging,
    index: Index,
    log: Callable[[str], None],
) -> Artifact:
    if plan.stage not in executors:
        raise RuntimeError(f"no executor registered for stage '{plan.stage}'")
    out_dir = staging.prepare(plan.stage, plan.hash)
    (out_dir / META_NAME).unlink(missing_ok=True)
    ctx = StageContext(spec=spec, stage=plan.stage, params=plan.params, parent=parent,
                       out_dir=out_dir, config=config)
    log(f"  {plan.stage:8s} {plan.hash}  building...")
    res = executors[plan.stage](ctx) or ExecResult()

    artifacts = res.artifacts or _infer_artifacts(out_dir)
    art = Artifact(
        stage=plan.stage, hash=plan.hash, dir=out_dir, params=plan.params,
        input_hash=plan.parent_hash, asset_id=spec.id, artifacts=artifacts,
        engine_versions=res.engine_versions, recipe_version=pipeline.recipe_version(plan.stage),
        authoritative=plan.stage in pipeline.authoritative,
        primary_name=pipeline.artifact_names.get(plan.stage, ""), metadata=res.metadata)
    staging.write_meta(art)
    index.record(spec.id, plan.stage, plan.hash, plan.params, plan.parent_hash, art.created)
    log(f"  {plan.stage:8s} {plan.hash}  built ({', '.join(artifacts) or 'no files'})")
    return art


def _infer_artifacts(out_dir: Path) -> list[str]:
    return sorted(
        p.name for p in out_dir.iterdir() if p.name != META_NAME and not p.name.startswith(".")
    )
