"""Compute routing: pick WHERE a model runs (local | runpod | k8s).

The model *identity* — the family + ``variant`` (the specific weight) — is the caller's concern.
WHERE that compute happens is ours: a resident local daemon hosts what it can fit, and anything it
can't (a bigger weight, or no daemon at all) spills to a rented RunPod or a Kubernetes GPU job.
Because the backend never changes the pixels, it is NOT part of the asset's content hash — the same
family+variant+params is one artifact wherever it ran.

NoiseWeaver imports nothing proprietary, so the local-host capability *probe* is **injected**: the
caller passes a zero-arg callable returning :class:`HostCapabilities` (or ``None`` when down). A
plugin wires its own daemon client as that probe; tests pass a fake. Everything here is pure
logic + dataclasses → fully unit-testable with no daemon, no pod, no cluster.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# Backend ids (also the strings recorded in build metadata / shown in the UI).
LOCAL = "local"
RUNPOD = "runpod"
K8S = "k8s"
ALL_BACKENDS = (LOCAL, RUNPOD, K8S)

# Optional resident-footprint estimates (MB) per variant — the admission estimate the router
# compares against the local daemon's free budget. NoiseWeaver ships none (a generic platform has
# no model menu); a plugin populates this, or passes ``est_mb`` directly. Unknown weights → 0
# (= "ask the daemon, don't pre-reject"), so a new variant still gets a chance at the local daemon.
ESTIMATE_MB: dict[str, int] = {}


def estimate_mb(variant: str) -> int:
    """Estimated resident footprint (MB) for ``variant``; 0 when unknown (don't pre-reject)."""
    return ESTIMATE_MB.get(variant, 0)


@dataclass(frozen=True)
class HostCapabilities:
    """What a local daemon can host right now — the probe's return value.

    ``models`` maps a family to the variant set the daemon actually implements today. ``free_mb``
    is the admission headroom (memory limit minus active); 0 means "no limit / unknown" → memory
    never blocks. The probe constructs this from the daemon's ``status`` reply.
    """

    models: dict[str, set[str]] = field(default_factory=dict)
    free_mb: int = 0

    def can_host(self, engine: str, variant: str, est_mb: int = 0) -> bool:
        """True if this daemon implements ``engine``/``variant`` AND has room for ``est_mb``."""
        if variant not in self.models.get(engine, set()):
            return False
        if self.free_mb and est_mb and est_mb > self.free_mb:
            return False
        return True


class NoBackendAvailable(RuntimeError):
    """No configured backend can run the requested model (daemon can't host it, no pod, no k8s)."""


@dataclass
class ComputeRouter:
    """Routes a (family, variant) to the first backend that can run it, in preference order.

    ``probe`` returns the local daemon's :class:`HostCapabilities` (or ``None`` if it's down);
    ``runpod_available`` / ``k8s_available`` say whether those offload backends are configured.
    ``prefer`` is the order to try (default: local first, then pod, then cluster).
    """

    probe: Callable[[], HostCapabilities | None] | None = None
    runpod_available: bool = False
    k8s_available: bool = False
    prefer: tuple[str, ...] = (LOCAL, RUNPOD, K8S)

    def route(self, engine: str, variant: str, est_mb: int | None = None) -> str:
        """Return the backend id to run ``engine``/``variant`` on. Raises
        :class:`NoBackendAvailable` if nothing fits."""
        est = estimate_mb(variant) if est_mb is None else est_mb
        reasons: list[str] = []
        for backend in self.prefer:
            if backend == LOCAL:
                caps = self.probe() if self.probe is not None else None
                if caps is None:
                    reasons.append("local: daemon down")
                elif caps.can_host(engine, variant, est):
                    return LOCAL
                else:
                    reasons.append(f"local: can't host {engine}/{variant}")
            elif backend == RUNPOD:
                if self.runpod_available:
                    return RUNPOD
                reasons.append("runpod: not configured")
            elif backend == K8S:
                if self.k8s_available:
                    return K8S
                reasons.append("k8s: not configured")
        raise NoBackendAvailable(
            f"no backend can run {engine}/{variant} (est {est} MB) — " + "; ".join(reasons))

    def is_local(self, engine: str, variant: str, est_mb: int | None = None) -> bool:
        """True if this would route to the local daemon — used by build planning to decide whether
        to spin up a pod. Fail-soft: a routing failure (e.g. no backend) reads as 'not local'."""
        try:
            return self.route(engine, variant, est_mb) == LOCAL
        except NoBackendAvailable:
            return False
