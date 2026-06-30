"""Tests for RunPod direct-SSH endpoint discovery (pure selection logic)."""

from __future__ import annotations

import pytest

from noiseweaver.pod import runpod
from noiseweaver.pod.runpod import RunPodError, _direct_ssh_port, discover_endpoint


def _pod(pid, name, status, ports):
    return {"id": pid, "name": name, "desiredStatus": status, "runtime": {"ports": ports}}


def _port(ip, public, priv, pub):
    return {"ip": ip, "isIpPublic": public, "privatePort": priv, "publicPort": pub, "type": "tcp"}


def test_direct_ssh_port_picks_public_22():
    pod = _pod(
        "p1",
        "demo-pod",
        "RUNNING",
        [
            _port("10.0.0.1", False, 22, 5000),  # private ip — skip
            _port("1.2.3.4", True, 22, 40022),  # the direct sshd
            _port("1.2.3.4", True, 8188, 41888),  # comfyui, not ssh
        ],
    )
    assert _direct_ssh_port(pod) == ("1.2.3.4", 40022)


def test_direct_ssh_port_none_when_no_public_22():
    pod = _pod("p1", "g", "RUNNING", [_port("1.2.3.4", True, 8188, 9)])
    assert _direct_ssh_port(pod) is None


def test_discover_single_running(monkeypatch):
    ports = [_port("1.2.3.4", True, 22, 40022)]
    monkeypatch.setattr(runpod, "list_pods", lambda _k: [_pod("p1", "demo-pod", "RUNNING", ports)])
    ep = discover_endpoint("fake-key")
    assert (ep.host, ep.port, ep.running) == ("1.2.3.4", 40022, True)


def test_discover_ambiguous_raises(monkeypatch):
    ports = [_port("1.2.3.4", True, 22, 40022)]
    monkeypatch.setattr(
        runpod,
        "list_pods",
        lambda _k: [_pod("p1", "a", "RUNNING", ports), _pod("p2", "b", "RUNNING", ports)],
    )
    with pytest.raises(RunPodError):
        discover_endpoint("fake-key")
    # ...but a name filter disambiguates
    ep = discover_endpoint("fake-key", name="b")
    assert ep.pod_id == "p2"


def test_discover_none_raises(monkeypatch):
    monkeypatch.setattr(runpod, "list_pods", lambda _k: [])
    with pytest.raises(RunPodError):
        discover_endpoint("fake-key")


def test_no_api_key_raises():
    with pytest.raises(RunPodError):
        runpod.list_pods("")
