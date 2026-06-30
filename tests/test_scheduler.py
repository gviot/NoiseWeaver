"""ComputeRouter: pick local | runpod | k8s from injected capabilities + availability. Pure logic
— no daemon, no pod, no cluster. The local-host probe is a plain callable returning
HostCapabilities (or None when down)."""

from __future__ import annotations

import pytest

from noiseweaver.scheduler import (
    K8S,
    LOCAL,
    RUNPOD,
    ComputeRouter,
    HostCapabilities,
    NoBackendAvailable,
    estimate_mb,
)

# A local daemon that hosts FLUX.2 Klein-4B with plenty of headroom.
KLEIN = HostCapabilities(models={"flux": {"flux2-klein-4b"}}, free_mb=0)


def test_routes_to_local_when_daemon_can_host():
    r = ComputeRouter(probe=lambda: KLEIN, runpod_available=True, k8s_available=True)
    assert r.route("flux", "flux2-klein-4b") == LOCAL


def test_unsupported_variant_falls_to_runpod():
    # daemon up but doesn't implement the 9B weight → spill to the pod
    r = ComputeRouter(probe=lambda: KLEIN, runpod_available=True, k8s_available=True)
    assert r.route("flux", "flux2-klein-9b") == RUNPOD


def test_daemon_down_falls_to_runpod_then_k8s():
    down = lambda: None  # noqa: E731
    v = "flux2-klein-4b"
    assert ComputeRouter(probe=down, runpod_available=True).route("flux", v) == RUNPOD
    assert ComputeRouter(probe=down, k8s_available=True).route("flux", v) == K8S


def test_no_probe_means_no_local():
    r = ComputeRouter(probe=None, k8s_available=True)
    assert r.route("flux", "flux2-klein-4b") == K8S


def test_memory_headroom_blocks_local():
    # NoiseWeaver ships no footprint catalog, so the caller passes the estimate explicitly.
    tight = HostCapabilities(models={"flux": {"flux2-klein-4b"}}, free_mb=4_000)
    r = ComputeRouter(probe=lambda: tight, runpod_available=True)
    # a 16 GB estimate exceeds the 4 GB budget → spill
    assert r.route("flux", "flux2-klein-4b", est_mb=16_000) == RUNPOD
    # but a tiny explicit estimate fits
    assert r.route("flux", "flux2-klein-4b", est_mb=1_000) == LOCAL


def test_prefer_order_is_honored():
    r = ComputeRouter(
        probe=lambda: None, runpod_available=True, k8s_available=True, prefer=(K8S, RUNPOD, LOCAL)
    )
    assert r.route("flux", "flux2-klein-4b") == K8S


def test_nothing_available_raises_with_reasons():
    r = ComputeRouter(probe=lambda: None)
    with pytest.raises(NoBackendAvailable) as e:
        r.route("flux", "flux2-klein-4b")
    msg = str(e.value)
    assert "local: daemon down" in msg and "runpod: not configured" in msg


def test_is_local_is_fail_soft():
    assert ComputeRouter(probe=lambda: KLEIN).is_local("flux", "flux2-klein-4b") is True
    # no backend at all → not local (and no raise)
    assert ComputeRouter(probe=lambda: None).is_local("flux", "flux2-klein-4b") is False


def test_can_host_checks_family_and_variant():
    caps = HostCapabilities(models={"flux": {"flux2-klein-4b"}})
    assert caps.can_host("flux", "flux2-klein-4b")
    assert not caps.can_host("flux", "flux1-dev")  # variant not implemented
    assert not caps.can_host("other", "x")  # family not hosted


def test_estimate_mb_is_zero_by_default():
    # the platform ships no model menu; everything is "unknown" → 0 (don't pre-reject)
    assert estimate_mb("flux2-klein-4b") == 0
    assert estimate_mb("nonexistent-weight") == 0
