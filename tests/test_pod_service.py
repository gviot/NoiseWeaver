"""PodService lifecycle with injected fakes — discover → ssh → deploy → start → forward → wait →
teardown, no live pod. The HTTP ready-check is monkeypatched."""

from __future__ import annotations

import pytest

from noiseweaver.pod import PodService
from noiseweaver.pod.runpod import PodEndpoint, RunPodError


class FakeProc:
    def __init__(self):
        self.running = True
        self.stopped = False
        self.log_path = "/tmp/svc.log"

    def stop(self):
        self.stopped = True
        self.running = False

    def read_available(self):
        return ""


class FakeSSH:
    def __init__(self, endpoint, key):
        self.endpoint = endpoint
        self.connected = False
        self.closed = False
        self.started = None
        self.forwarded = None
        self.proc = FakeProc()

    def connect(self):
        self.connected = True

    def start_background(self, cmd):
        self.started = cmd
        return self.proc

    def forward(self, port):
        self.forwarded = port
        return 54321

    def close(self):
        self.closed = True


def _ep(_key, **_kw):
    return PodEndpoint(pod_id="p1", name="pod", host="1.2.3.4", port=2222, status="RUNNING")


def _svc(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("k")
    return PodService("api-key", str(key), discover=_ep, ssh_factory=FakeSSH)


def test_start_runs_deploy_then_service_and_returns_tunnel(tmp_path, monkeypatch):
    monkeypatch.setattr(PodService, "_wait_ready", lambda self, *a, **k: None)
    deployed = []
    svc = _svc(tmp_path)
    base = svc.start("cd /comfy && exec python main.py --port 8188", 8188,
                     ready_path="/object_info", deploy=lambda ssh: deployed.append(ssh),
                     log=lambda *_: None)
    assert base == "http://127.0.0.1:54321"
    assert svc.ssh.connected and svc.ssh.started.endswith("--port 8188")
    assert svc.ssh.forwarded == 8188 and deployed == [svc.ssh]  # deploy ran before start


def test_close_stops_proc_and_ssh(tmp_path, monkeypatch):
    monkeypatch.setattr(PodService, "_wait_ready", lambda self, *a, **k: None)
    svc = _svc(tmp_path)
    svc.start("run", 8188, log=lambda *_: None)
    proc, ssh = svc.proc, svc.ssh
    svc.close()
    assert proc.stopped and ssh.closed and svc.ssh is None and svc.proc is None


def test_connect_requires_api_key_and_ssh_key(tmp_path):
    with pytest.raises(RunPodError, match="API key"):
        PodService(None, str(tmp_path / "k")).connect()
    with pytest.raises(RunPodError, match="SSH key"):
        PodService("k", str(tmp_path / "absent"), discover=_ep).connect()


def test_wait_ready_fails_if_proc_dies(tmp_path):
    svc = _svc(tmp_path)
    svc.connect()
    svc.proc = FakeProc()
    svc.proc.running = False  # service crashed
    svc.local_port = 54321
    with pytest.raises(RunPodError, match="exited during startup"):
        svc._wait_ready("http://127.0.0.1:54321/x", timeout=0.2, poll=0.01, log=lambda *_: None)
