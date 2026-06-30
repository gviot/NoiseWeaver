"""A generic content-addressed staged-build engine over JSON specs — a mini build system: a
:class:`Spec` document → a Merkle :func:`resolve`/:func:`build` DAG → CAS :class:`Staging` →
variant :class:`Index` + :class:`Tombstones`. Parameterized by a :class:`Pipeline` (the stage
definitions) + injected executors; the engine knows nothing about what the stages *do*.
"""

from .dag import (
    BuildResult,
    ExecResult,
    Executor,
    StageContext,
    StagePlan,
    StageStatus,
    build,
    resolve,
)
from .hashing import HASH_LEN, canonical_json, file_sha256, stage_hash
from .index import Index
from .pipeline import ParamHook, Pipeline
from .spec import Spec, SpecFormat, discover_specs, find_spec
from .staging import Artifact, Staging, utc_now_iso
from .tombstones import Tombstones, sidecar_for

__all__ = [
    "Spec", "SpecFormat", "discover_specs", "find_spec",
    "Pipeline", "ParamHook",
    "canonical_json", "stage_hash", "file_sha256", "HASH_LEN",
    "Staging", "Artifact", "utc_now_iso", "Index", "Tombstones", "sidecar_for",
    "resolve", "build", "StageContext", "ExecResult", "Executor",
    "StagePlan", "StageStatus", "BuildResult",
]
