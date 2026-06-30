"""The compute framework: requirements↔offers matching, scheduling (local-first, constraint
exclusion, both modes), execution, and the built-in ComfyUI tool. No live daemon/pod — scheduler
logic runs on fake tools/platforms; a plugin-supplied tool (an MLX local daemon) is modelled by a
fake here."""

from __future__ import annotations

import pytest

from noiseweaver.compute import (
    Capability,
    ComfyUITool,
    Endpoint,
    ExecutionMode,
    ImageRequest,
    ImageResult,
    Lease,
    LocalPlatform,
    NoPlacement,
    Offers,
    RemotePlatform,
    Requirements,
    Scheduler,
    WorkloadSpec,
    execute,
)
from noiseweaver.compute.tools import COMFY_CHECKPOINTS, build_flux_graph
from noiseweaver.scheduler import HostCapabilities

MAC = Offers(os="macos", arch="arm64", accelerators=frozenset({"mlx"}))
CUDA = Offers(os="linux", arch="x86_64", accelerators=frozenset({"cuda"}), vram_gb=48)


# --------------------------------------------------------------------------- #
# requirements ↔ offers
# --------------------------------------------------------------------------- #
def test_offers_satisfies_requirements():
    assert MAC.satisfies(Requirements(os=frozenset({"macos"}), accelerator=frozenset({"mlx"})))
    assert not MAC.satisfies(Requirements(accelerator=frozenset({"cuda"})))  # mac has no cuda
    assert CUDA.satisfies(Requirements(accelerator=frozenset({"cuda"}), min_vram_gb=24))
    assert not CUDA.satisfies(Requirements(min_vram_gb=80))  # not enough vram
    assert CUDA.satisfies(Requirements())  # empty = don't care


# --------------------------------------------------------------------------- #
# fakes for scheduler logic
# --------------------------------------------------------------------------- #
class _FakeBound:
    def text_to_image(self, req):
        return ImageResult(path=req.output, tool="fake", platform="")


class FakeTool:
    def __init__(self, name, requirements, serves, *, modes=None, probe=None):
        self.name = name
        self.requirements = requirements
        self.modes = modes or frozenset({ExecutionMode.SERVICE})
        self._serves = serves
        self._probe = probe

    def capabilities(self):
        return frozenset({Capability.TEXT_TO_IMAGE})

    def serves(self, cap, model, variant):
        return cap == Capability.TEXT_TO_IMAGE and variant in self._serves

    def local_endpoint(self):
        return Endpoint("http", "http://fake")

    def workload(self, kind):
        return WorkloadSpec(tool=self.name, requirements=self.requirements)

    def bind(self, ep):
        return _FakeBound()

    def host_probe(self, ep):
        return self._probe

    def encode(self, cap, req):
        return {"path": req.output}

    def decode(self, cap, out):
        return ImageResult(path=out["path"], tool=self.name, platform="")


def _mlx_like(probe_variants=("flux2-klein-4b",)):
    # An MLX-only local tool (a plugin would supply the real one); mlx pins it to a Mac.
    return FakeTool("mlx", Requirements(accelerator=frozenset({"mlx"})),
                    serves={"flux2-klein-4b", "flux2-klein-9b", "flux1-schnell"},
                    probe=HostCapabilities(models={"flux": set(probe_variants)}))


def _comfy_like():
    # the FLUX workload needs a CUDA host (the real tool puts this on its WorkloadSpec; the fake
    # carries it on requirements, which gates scheduling the same way).
    return FakeTool("comfyui", Requirements(accelerator=frozenset({"cuda"})),
                    serves={"flux1-schnell", "flux1-dev"},
                    probe=HostCapabilities(models={"flux": {"flux1-schnell", "flux1-dev"}}))


def test_schedule_prefers_local_when_hostable():
    local = LocalPlatform(MAC)
    runpod = RemotePlatform("runpod", CUDA, acquire=lambda w: Lease(Endpoint("http", "http://pod")))
    sched = Scheduler([_mlx_like(), _comfy_like()], [local, runpod])
    p = sched.schedule(Capability.TEXT_TO_IMAGE, "flux", "flux2-klein-4b")
    assert (p.tool.name, p.platform.kind, p.mode) == ("mlx", "local", ExecutionMode.SERVICE)


def test_unhostable_local_variant_falls_to_runpod_comfyui():
    # flux1-schnell: the mlx tool serves it but doesn't advertise it locally; comfyui can't run on
    # the mac (needs cuda) → only comfyui@runpod is viable.
    local = LocalPlatform(MAC)
    runpod = RemotePlatform("runpod", CUDA, acquire=lambda w: Lease(Endpoint("http", "http://pod")))
    sched = Scheduler([_mlx_like(), _comfy_like()], [local, runpod])
    p = sched.schedule(Capability.TEXT_TO_IMAGE, "flux", "flux1-schnell")
    assert (p.tool.name, p.platform.kind) == ("comfyui", "runpod")


def test_mlx_tool_never_schedules_on_non_mac():
    # a CUDA-only world: the mlx requirement excludes it everywhere; klein-4b has no home.
    linux_local = LocalPlatform(CUDA)
    sched = Scheduler([_mlx_like(), _comfy_like()], [linux_local])
    with pytest.raises(NoPlacement):
        sched.schedule(Capability.TEXT_TO_IMAGE, "flux", "flux2-klein-4b")
    # but comfyui's flux1 runs fine on the cuda box
    p = sched.schedule(Capability.TEXT_TO_IMAGE, "flux", "flux1-dev")
    assert p.tool.name == "comfyui" and p.platform.kind == "local"


def test_candidates_respect_prefer_order():
    runpod = RemotePlatform("runpod", CUDA, acquire=lambda w: Lease(Endpoint("http", "http://r")))
    k8s = RemotePlatform("k8s", CUDA, acquire=lambda w: Lease(Endpoint("http", "http://k")))
    sched = Scheduler([_comfy_like()], [k8s, runpod], prefer=("local", "runpod", "k8s"))
    cands = sched.candidates(Capability.TEXT_TO_IMAGE, "flux", "flux1-dev")
    assert [c.platform.kind for c in cands] == ["runpod", "k8s"]


# --------------------------------------------------------------------------- #
# execution — service + job
# --------------------------------------------------------------------------- #
def test_execute_service_stamps_platform(tmp_path):
    out = str(tmp_path / "o.png")
    placement = Scheduler([_comfy_like()], [LocalPlatform(CUDA)]).schedule(
        Capability.TEXT_TO_IMAGE, "flux", "flux1-dev")
    res = execute(placement, Capability.TEXT_TO_IMAGE, ImageRequest(prompt="x", output=out,
                                                                    variant="flux1-dev"))
    assert res.path == out and res.platform == "local"


def test_execute_job_mode_submits_and_decodes(tmp_path):
    seen = {}

    def run_job(workload, payload):
        seen["workload"] = workload.tool
        seen["payload"] = payload
        return {"path": payload["path"]}

    tool = FakeTool("comfyui", Requirements(), {"flux1-dev"},
                    modes=frozenset({ExecutionMode.JOB}),
                    probe=HostCapabilities(models={"flux": {"flux1-dev"}}))
    runpod = RemotePlatform("runpod", CUDA, run_job=run_job)  # job-only (no acquire)
    placement = Scheduler([tool], [runpod]).schedule(Capability.TEXT_TO_IMAGE, "flux", "flux1-dev")
    assert placement.mode == ExecutionMode.JOB
    out = str(tmp_path / "j.png")
    res = execute(placement, Capability.TEXT_TO_IMAGE, ImageRequest(prompt="x", output=out,
                                                                    variant="flux1-dev"))
    assert res.path == out and res.platform == "runpod"
    assert seen["workload"] == "comfyui" and seen["payload"]["path"] == out


# --------------------------------------------------------------------------- #
# the built-in ComfyUI tool — pure surface
# --------------------------------------------------------------------------- #
def test_comfyui_tool_serves_flux1_and_builds_graph():
    t = ComfyUITool()
    assert t.serves(Capability.TEXT_TO_IMAGE, "flux", "flux1-schnell")
    assert not t.serves(Capability.TEXT_TO_IMAGE, "flux", "flux2-klein-4b")  # FLUX.2 not a ckpt
    assert t.workload("runpod").requirements.accelerator == frozenset({"cuda"})
    g = build_flux_graph(ImageRequest(prompt="a goblin", output="/o.png", variant="flux1-schnell",
                                      width=768, height=512, steps=4, seed=7, guidance=1.0),
                         COMFY_CHECKPOINTS["flux1-schnell"])
    assert g["1"]["inputs"]["ckpt_name"] == "flux1-schnell-fp8.safetensors"
    assert g["2"]["inputs"]["text"] == "a goblin"
    assert (g["4"]["inputs"]["width"], g["5"]["inputs"]["seed"]) == (768, 7)
