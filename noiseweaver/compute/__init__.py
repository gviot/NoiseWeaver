"""Backend-agnostic compute: two orthogonal axes — **Tool** (the API: ComfyUI, OpenAI, or a
plugin-supplied client) and **Platform** (where it runs: local, runpod, k8s, external) — composed
into a **Placement** the scheduler resolves by matching a tool's requirements against a platform's
offers (k8s-style). See :mod:`noiseweaver.compute.model` for the vocabulary.
"""

from .model import (
    BackendUnavailable,
    Capability,
    ComputeError,
    Endpoint,
    ExecutionMode,
    ImageRequest,
    ImageResult,
    Lease,
    Offers,
    Requirements,
    WorkloadSpec,
)
from .platforms import ExternalPlatform, LocalPlatform, Platform, RemotePlatform, host_offers
from .scheduler import (
    NoPlacement,
    Placement,
    Scheduler,
    execute,
    text_to_image,
)
from .tools import ComfyUITool, Tool

__all__ = [
    "BackendUnavailable", "Capability", "ComputeError", "Endpoint", "ExecutionMode",
    "ImageRequest", "ImageResult", "Lease", "Offers", "Requirements", "WorkloadSpec",
    "ExternalPlatform", "LocalPlatform", "Platform", "RemotePlatform", "host_offers",
    "NoPlacement", "Placement", "Scheduler", "execute", "text_to_image",
    "ComfyUITool", "Tool",
]
