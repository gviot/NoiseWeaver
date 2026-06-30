"""The compute vocabulary: two orthogonal axes + how they're matched.

  * **Tool** — the API/interface you speak (ComfyUI HTTP, OpenAI /v1, or a plugin's own NDJSON/UDS
    client). Platform-agnostic: the same client code runs wherever the service is reachable.
  * **Platform** — where/how it's provisioned and reached (local, runpod, k8s, external). Tool-
    agnostic: the same provisioning code serves any workload.

A runnable unit is a **Placement = (Tool, Platform, ExecutionMode)**. Modeled on Kubernetes: a
``WorkloadSpec`` is the pod spec, a ``Platform`` is the kubelet/runtime, an ``Endpoint`` is the
Service, and **requirements ↔ offers** is nodeSelector/affinity — an MLX tool *requires* ``{os:
macos, accel: mlx}`` so it only ever schedules on a Mac, while ComfyUI requires nothing and runs
anywhere.

Text ops use the OpenAI schema (the lingua franca) so an Ollama/LiteLLM tool needs no bespoke wire
format; image generation has no standard, so :class:`ImageRequest` is our own minimal shape.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Capability(StrEnum):
    """An operation a tool can perform. The unit a caller asks the scheduler for."""

    TEXT_TO_IMAGE = "text_to_image"
    CHAT = "chat"
    EMBED = "embed"


class ExecutionMode(StrEnum):
    """How a tool is invoked on a platform.

    SERVICE — provision a persistent endpoint, make many calls, release it (a local daemon, a warm
    ComfyUI server). JOB — submit a workload + input, run to completion, get the result back (a
    RunPod-serverless invocation, a k8s Job); no idle warm cost.
    """

    SERVICE = "service"
    JOB = "job"


class ComputeError(RuntimeError):
    """A tool/platform op failed (the service answered with an error)."""


class BackendUnavailable(ComputeError):
    """A tool/platform could not be reached at all — the caller may fall back or offload."""


# --------------------------------------------------------------------------- #
# placement constraints — requirements (what a tool needs) ↔ offers (what a platform provides)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Requirements:
    """What a tool needs from a host (k8s nodeSelector). Empty fields mean "don't care"."""

    os: frozenset[str] = frozenset()           # e.g. {"macos"}, {"linux"}
    arch: frozenset[str] = frozenset()          # {"arm64"}, {"x86_64"}
    accelerator: frozenset[str] = frozenset()   # {"mlx"}, {"cuda"} — ANY one must be offered
    min_vram_gb: int = 0


@dataclass(frozen=True)
class Offers:
    """What a platform can provide (k8s node labels + capacity)."""

    os: str
    arch: str
    accelerators: frozenset[str] = frozenset()
    vram_gb: int = 0

    def satisfies(self, req: Requirements) -> bool:
        if req.os and self.os not in req.os:
            return False
        if req.arch and self.arch not in req.arch:
            return False
        if req.accelerator and not (req.accelerator & self.accelerators):
            return False
        if req.min_vram_gb and self.vram_gb < req.min_vram_gb:
            return False
        return True


# --------------------------------------------------------------------------- #
# connection + deployment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Endpoint:
    """A reachable address for a running service. ``kind`` selects how a tool connects."""

    kind: str          # "http" | "uds" | "openai" | ...
    address: str       # "http://127.0.0.1:8188" | "/Users/x/.local/daemon.sock"


@dataclass(frozen=True)
class WorkloadSpec:
    """The deployable definition of a tool on a *container* platform (runpod/k8s) — the pod spec.

    For local/external platforms the address is well-known, so ``image``/``command`` are None and
    the platform uses the tool's local endpoint instead. The same spec is what makes RunPod/K8s
    provisioning shared across tools: only ``image``/``command``/``port`` differ per tool.
    """

    tool: str
    image: str | None = None
    command: tuple[str, ...] | None = None
    port: int | None = None
    ready_path: str | None = None               # HTTP path that returns 200 when ready
    env: dict[str, str] = field(default_factory=dict)
    requirements: Requirements = Requirements()


@dataclass
class Lease:
    """A provisioned, reachable service. ``release`` tears it down (no-op for local/external)."""

    endpoint: Endpoint
    release: Callable[[], None] = lambda: None


# --------------------------------------------------------------------------- #
# operation request/result types
# --------------------------------------------------------------------------- #
@dataclass
class ImageRequest:
    """A text→image generation. The image never crosses the wire — the backend writes ``output``
    (an absolute path the caller owns) and returns it."""

    prompt: str
    output: str
    width: int = 1024
    height: int = 1024
    steps: int = 4
    seed: int = 0
    guidance: float = 1.0
    negative: str = ""
    model: str = "flux"            # family
    variant: str | None = None     # specific weight
    quantize: int | None = None
    lora: str | None = None
    lora_scale: float = 1.0


@dataclass
class ImageResult:
    path: str
    tool: str
    platform: str
    meta: dict = field(default_factory=dict)
