"""Platforms — the placement layer (axis 2). A platform provisions a running service (SERVICE) or
runs a workload to completion (JOB), and knows nothing about which tool it's hosting. It advertises
``offers`` (k8s node labels + capacity); the scheduler matches those against a tool's requirements.

``RemotePlatform`` is the seam that stops RunPod/k8s provisioning from being duplicated per tool:
the pod/cluster lifecycle is an injected callable, and the per-tool difference is only the
:class:`WorkloadSpec` (image/command/port). The same RunPod provisioner serves ComfyUI and a
pure-python workload alike.
"""

from __future__ import annotations

import platform as _platform
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .model import Endpoint, ExecutionMode, Lease, Offers, WorkloadSpec
from .tools import Tool


def host_offers() -> Offers:
    """What this machine provides. Apple Silicon offers the ``mlx`` accelerator (what an MLX tool
    requires); a Linux/CUDA box would offer ``cuda`` (detection left to the deployer)."""
    sysname = "macos" if _platform.system() == "Darwin" else _platform.system().lower()
    arch = "arm64" if _platform.machine() in ("arm64", "aarch64") else _platform.machine()
    accel: frozenset[str] = frozenset()
    if sysname == "macos" and arch == "arm64":
        accel = frozenset({"mlx"})
    return Offers(os=sysname, arch=arch, accelerators=accel)


@runtime_checkable
class Platform(Protocol):
    kind: str

    def offers(self) -> Offers: ...
    def available(self) -> bool: ...
    def modes(self) -> frozenset[ExecutionMode]: ...
    def provision(self, tool: Tool) -> Lease: ...                       # SERVICE
    def submit(self, tool: Tool, cap, payload: dict) -> dict: ...       # JOB


class LocalPlatform:
    """This machine. Provisions a tool by handing back its well-known local endpoint (the daemon
    socket, the local server URL) — no container, no teardown."""

    kind = "local"

    def __init__(self, offers: Offers | None = None):
        self._offers = offers or host_offers()

    def offers(self) -> Offers:
        return self._offers

    def available(self) -> bool:
        return True

    def modes(self) -> frozenset[ExecutionMode]:
        return frozenset({ExecutionMode.SERVICE})

    def provision(self, tool: Tool) -> Lease:
        ep = tool.local_endpoint()
        if ep is None:
            raise RuntimeError(f"{tool.name} has no local endpoint")
        return Lease(endpoint=ep)

    def submit(self, tool: Tool, cap, payload: dict) -> dict:
        raise RuntimeError("local platform is SERVICE-only")


class ExternalPlatform:
    """A service someone else already runs (a fixed URL: a shared ComfyUI, an OpenAI endpoint). No
    provisioning; ``offers`` is declared by whoever registers it."""

    kind = "external"

    def __init__(self, endpoint: Endpoint, offers: Offers, name: str = "external"):
        self.kind = name
        self._endpoint = endpoint
        self._offers = offers

    def offers(self) -> Offers:
        return self._offers

    def available(self) -> bool:
        return True

    def modes(self) -> frozenset[ExecutionMode]:
        return frozenset({ExecutionMode.SERVICE})

    def provision(self, tool: Tool) -> Lease:
        return Lease(endpoint=self._endpoint)

    def submit(self, tool: Tool, cap, payload: dict) -> dict:
        raise RuntimeError("external platform is SERVICE-only")


class RemotePlatform:
    """RunPod or k8s. The lifecycle is INJECTED so the heavy infra (paramiko/SSH tunnel, the RunPod
    API, the kubernetes client) lives in the deployer, not here, and is shared across every tool:

      * ``acquire(workload) -> Lease`` provisions a warm service (a pod + tunnel) → SERVICE mode.
      * ``run_job(workload, payload) -> dict`` runs the workload to completion → JOB mode.

    A platform supports whichever modes it was given a runner for. Tests pass fakes; a plugin wires
    its pod provisioner as ``acquire`` and (later) a serverless/k8s submitter as ``run_job``.
    """

    def __init__(
        self,
        kind: str,
        offers: Offers,
        *,
        acquire: Callable[[WorkloadSpec], Lease] | None = None,
        run_job: Callable[[WorkloadSpec, dict], dict] | None = None,
        available: bool | Callable[[], bool] = True,
    ):
        self.kind = kind
        self._offers = offers
        self._acquire = acquire
        self._run_job = run_job
        self._available = available

    def offers(self) -> Offers:
        return self._offers

    def available(self) -> bool:
        return self._available() if callable(self._available) else bool(self._available)

    def modes(self) -> frozenset[ExecutionMode]:
        m = set()
        if self._acquire is not None:
            m.add(ExecutionMode.SERVICE)
        if self._run_job is not None:
            m.add(ExecutionMode.JOB)
        return frozenset(m)

    def provision(self, tool: Tool) -> Lease:
        if self._acquire is None:
            raise RuntimeError(f"{self.kind} has no service provisioner")
        return self._acquire(tool.workload(self.kind))

    def submit(self, tool: Tool, cap, payload: dict) -> dict:
        if self._run_job is None:
            raise RuntimeError(f"{self.kind} has no job runner")
        return self._run_job(tool.workload(self.kind), payload)
