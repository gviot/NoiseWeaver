"""The scheduler — the k8s control plane for compute. Given an operation (capability + model +
variant), it finds every valid **Placement = (Tool, Platform, ExecutionMode)** and picks the best:

  1. the tool must *serve* the requested capability+model+variant;
  2. the platform's **offers** must satisfy BOTH the tool's requirements and the workload's
     (an MLX tool needs ``mlx`` → only a Mac; a ComfyUI FLUX workload needs ``cuda`` → never
     local-mac);
  3. the placement must be *hostable* — a local service is probed live (its advertised models +
     memory budget); a remote/to-be-provisioned one trusts ``tool.serves`` (the image has the
     weights);
  4. prefer order wins (local before rented pod before cluster), SERVICE before JOB.

``execute`` then runs the placement: SERVICE provisions an endpoint, binds the tool, calls the op,
releases; JOB submits the workload + encoded request and decodes the result.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..scheduler import HostCapabilities, estimate_mb
from .model import (
    BackendUnavailable,
    Capability,
    ComputeError,
    ExecutionMode,
    ImageRequest,
    ImageResult,
)
from .platforms import Platform
from .tools import Tool

DEFAULT_PREFER = ("local", "runpod", "k8s", "external")


class NoPlacement(ComputeError):
    """No (tool, platform) can run the requested operation."""


@dataclass(frozen=True)
class Placement:
    tool: Tool
    platform: Platform
    mode: ExecutionMode

    def __str__(self) -> str:  # e.g. "comfyui@runpod/service"
        return f"{self.tool.name}@{self.platform.kind}/{self.mode.value}"


class Scheduler:
    """Holds the registered tools + platforms and resolves operations to placements."""

    def __init__(self, tools: list[Tool], platforms: list[Platform],
                 prefer: tuple[str, ...] = DEFAULT_PREFER):
        self.tools = tools
        self.platforms = platforms
        self.prefer = prefer

    def _platform_rank(self, kind: str) -> int:
        return self.prefer.index(kind) if kind in self.prefer else len(self.prefer)

    def candidates(self, cap: Capability, model: str, variant: str | None) -> list[Placement]:
        """Every valid placement for the op, in preference order (best first)."""
        out: list[Placement] = []
        for tool in self.tools:
            if cap not in tool.capabilities() or not tool.serves(cap, model, variant):
                continue
            workload_reqs = tool.workload("").requirements
            for plat in self.platforms:
                if not plat.available():
                    continue
                offers = plat.offers()
                if not (offers.satisfies(tool.requirements) and offers.satisfies(workload_reqs)):
                    continue
                modes = tool.modes & plat.modes()
                if not modes:
                    continue
                mode = (ExecutionMode.SERVICE if ExecutionMode.SERVICE in modes
                        else next(iter(modes)))
                out.append(Placement(tool, plat, mode))
        out.sort(key=lambda p: (self._platform_rank(p.platform.kind),
                                0 if p.mode == ExecutionMode.SERVICE else 1))
        return out

    def schedule(self, cap: Capability, model: str, variant: str | None,
                 est_mb: int | None = None) -> Placement:
        """The best hostable placement, or raise :class:`NoPlacement`. Local placements are probed
        live for hosting + memory; remote ones trust the tool's static serving."""
        est = estimate_mb(variant or "") if est_mb is None else est_mb
        reasons: list[str] = []
        for cand in self.candidates(cap, model, variant):
            if cand.platform.kind == "local" or cand.platform.kind == "external":
                caps = cand.tool.host_probe(cand.platform.provision(cand.tool).endpoint)
                if caps is None:
                    reasons.append(f"{cand}: unreachable")
                    continue
                if not caps.can_host(model, variant, est):
                    reasons.append(f"{cand}: can't host {model}/{variant}")
                    continue
            return cand
        cands = self.candidates(cap, model, variant)
        if not cands:
            raise NoPlacement(
                f"no tool/platform serves {cap.value} {model}/{variant}")
        raise NoPlacement(
            f"no hostable placement for {cap.value} {model}/{variant} (est {est} MB) — "
            + "; ".join(reasons))


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def _run_bound(bound, cap: Capability, request):
    if cap == Capability.TEXT_TO_IMAGE:
        return bound.text_to_image(request)
    raise ComputeError(f"capability {cap.value} not wired in the bound tool yet")


def execute(placement: Placement, cap: Capability, request):
    """Run an operation on a resolved placement. SERVICE provisions→binds→calls→releases; JOB
    submits the encoded request and decodes the result."""
    tool, plat, mode = placement.tool, placement.platform, placement.mode
    if mode == ExecutionMode.SERVICE:
        lease = plat.provision(tool)
        try:
            result = _run_bound(tool.bind(lease.endpoint), cap, request)
        finally:
            lease.release()
    else:
        out = plat.submit(tool, cap, tool.encode(cap, request))
        result = tool.decode(cap, out)
    if isinstance(result, ImageResult):
        result.platform = plat.kind
    return result


def text_to_image(scheduler: Scheduler, req: ImageRequest) -> ImageResult:
    """Convenience: schedule + execute a text→image op end to end."""
    placement = scheduler.schedule(Capability.TEXT_TO_IMAGE, req.model, req.variant)
    return execute(placement, Capability.TEXT_TO_IMAGE, req)


__all__ = [
    "BackendUnavailable", "Capability", "ExecutionMode", "Placement", "Scheduler",
    "NoPlacement", "execute", "text_to_image", "HostCapabilities",
]
