"""Tools — the API layer (axis 1). A tool speaks one protocol to a running service; it does **not**
know or care where that service runs. ``bind(endpoint)`` returns a client for SERVICE mode;
``encode``/``decode`` carry a request through a JOB (the workload's handler runs the same op).

The one built-in tool is ComfyUI (HTTP), which runs anywhere a CUDA host serves it — and there is
exactly ONE client here: local/runpod/k8s differ only in the Endpoint it's handed, never in this
code. A plugin supplies any platform-specific tool of its own (e.g. a local-daemon client) by
implementing the :class:`Tool` protocol and handing instances to the :class:`Scheduler`.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..scheduler import HostCapabilities
from .model import (
    BackendUnavailable,
    Capability,
    ComputeError,
    Endpoint,
    ExecutionMode,
    ImageRequest,
    ImageResult,
    Requirements,
    WorkloadSpec,
)


@runtime_checkable
class BoundTool(Protocol):
    """A tool bound to a live :class:`Endpoint` — the SERVICE-mode client."""

    def text_to_image(self, req: ImageRequest) -> ImageResult: ...


@runtime_checkable
class Tool(Protocol):
    """The API layer. Platform-agnostic; the scheduler matches ``requirements`` against a
    platform's offers, then `bind`s the tool to whatever endpoint the platform provisions."""

    name: str
    requirements: Requirements
    modes: frozenset[ExecutionMode]

    def capabilities(self) -> frozenset[Capability]: ...
    def serves(self, cap: Capability, model: str, variant: str | None) -> bool: ...
    def local_endpoint(self) -> Endpoint | None: ...        # well-known address when run locally
    def workload(self, platform_kind: str) -> WorkloadSpec: ...  # to deploy on a container platform
    def bind(self, endpoint: Endpoint) -> BoundTool: ...
    def host_probe(self, endpoint: Endpoint) -> HostCapabilities | None: ...
    def encode(self, cap: Capability, req: object) -> dict: ...      # JOB: request -> job input
    def decode(self, cap: Capability, out: dict) -> object: ...      # JOB: job output -> result


# --------------------------------------------------------------------------- #
# ComfyUI — HTTP /prompt; runs on any CUDA host (local, runpod, k8s)
# --------------------------------------------------------------------------- #
# FLUX.1 checkpoints ComfyUI can run (the t2i graph below).
COMFY_CHECKPOINTS = {
    "flux1-schnell": "flux1-schnell-fp8.safetensors",
    "flux1-dev": "flux1-dev-fp8.safetensors",
}


def build_flux_graph(req: ImageRequest, ckpt: str, prefix: str = "concept") -> dict:
    """A standard FLUX.1 text→image ComfyUI /prompt graph (not pipeline-specific)."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": req.prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": req.negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": req.width, "height": req.height, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": req.seed, "steps": req.steps, "cfg": req.guidance,
                         "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                         "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": prefix}},
    }


class _BoundComfyUI:
    """SERVICE-mode client — drives the shared :class:`noiseweaver.pod.ComfyUIClient` over the
    endpoint's HTTP URL (one ComfyUI client, whether the server is local, on a pod, or in k8s)."""

    def __init__(self, endpoint: Endpoint, timeout: float = 30.0, prefix: str = "concept"):
        self.base = endpoint.address.rstrip("/")
        self.timeout = timeout
        self.prefix = prefix

    def text_to_image(self, req: ImageRequest) -> ImageResult:
        from ..pod.comfyui import ComfyUIClient, ComfyUIError

        ckpt = COMFY_CHECKPOINTS.get(req.variant or "")
        if ckpt is None:
            raise ComputeError(
                f"ComfyUI has no checkpoint for variant '{req.variant}' "
                f"(serves {sorted(COMFY_CHECKPOINTS)} only).")
        client = ComfyUIClient(self.base, timeout=self.timeout)
        try:
            result = client.run_graph(build_flux_graph(req, ckpt, self.prefix), timeout=1800.0)
        except ComfyUIError as e:
            raise ComputeError(str(e)) from e
        except OSError as e:
            raise BackendUnavailable(f"ComfyUI not reachable at {self.base}: {e}") from e
        pngs = client.find_outputs(result, ".png")
        if not pngs:
            raise ComputeError(f"ComfyUI produced no image (status={result.status})")
        ref = pngs[0]
        data = client.download(ref["filename"], ref["subfolder"], ref["type"])
        Path(req.output).write_bytes(data)
        return ImageResult(path=req.output, tool="comfyui", platform="",
                           meta={"variant": req.variant, "ckpt": ckpt})


class ComfyUITool:
    name = "comfyui"
    # ComfyUI itself runs anywhere; a FLUX workload needs a CUDA host — that lives in the workload's
    # requirements (so local-mac without a CUDA offer won't be scheduled for it), not on the tool.
    requirements = Requirements()
    modes = frozenset({ExecutionMode.SERVICE, ExecutionMode.JOB})

    def __init__(self, local_url: str = "http://127.0.0.1:8188", image: str = "comfyui:latest"):
        self._local_url = local_url
        self._image = image

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TEXT_TO_IMAGE})

    def serves(self, cap: Capability, model: str, variant: str | None) -> bool:
        return cap == Capability.TEXT_TO_IMAGE and (variant in COMFY_CHECKPOINTS)

    def local_endpoint(self) -> Endpoint:
        return Endpoint(kind="http", address=self._local_url)

    def workload(self, platform_kind: str) -> WorkloadSpec:
        return WorkloadSpec(
            tool="comfyui", image=self._image, port=8188, ready_path="/system_stats",
            requirements=Requirements(accelerator=frozenset({"cuda"})))

    def bind(self, endpoint: Endpoint) -> _BoundComfyUI:
        return _BoundComfyUI(endpoint)

    def host_probe(self, endpoint: Endpoint) -> HostCapabilities | None:
        # ComfyUI serves the FLUX.1 checkpoints regardless of memory accounting; the platform's
        # availability gates it. (A live /object_info probe could refine this later.)
        return HostCapabilities(models={"flux": set(COMFY_CHECKPOINTS)}, free_mb=0)

    def encode(self, cap: Capability, req: object) -> dict:
        if cap != Capability.TEXT_TO_IMAGE or not isinstance(req, ImageRequest):
            raise ComputeError(f"comfyui job encode: unsupported {cap}")
        return {"op": cap.value, "request": asdict(req)}

    def decode(self, cap: Capability, out: dict) -> ImageResult:
        return ImageResult(path=out["path"], tool="comfyui", platform="", meta=out.get("meta", {}))
