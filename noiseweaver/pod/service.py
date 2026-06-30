"""Generic pod-hosted service lifecycle.

The reusable skeleton behind "run a service on a rented pod and reach it locally": discover a
RunPod's direct-SSH endpoint → open the persistent control SSH → (optional deploy hook) → start a
foreground command → forward its port to localhost → wait until a ready URL answers. The endpoint is
the local tunnel; ``close()`` tears down the SSH, which kills the foreground service (RunPod kills
detached processes — that's the model). The *what* (which command, which port, which ready path,
what to deploy) is the caller's; this owns the *how*.

Discovery + the SSH class are injectable so the whole lifecycle unit-tests with no pod.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .runpod import RunPodError, discover_endpoint
from .ssh import PodSSH


class PodService:
    def __init__(
        self,
        api_key: str | None,
        ssh_key: str | Path | None,
        *,
        name: str | None = None,
        pod_id: str | None = None,
        discover: Callable[..., Any] = discover_endpoint,
        ssh_factory: Callable[..., Any] = PodSSH,
    ):
        self._api_key = api_key
        self._ssh_key = ssh_key
        self._name = name
        self._pod_id = pod_id
        self._discover = discover
        self._ssh_factory = ssh_factory
        self.endpoint: Any = None
        self.ssh: Any = None
        self.proc: Any = None
        self.local_port: int | None = None

    def discover(self) -> Any:
        """Resolve the pod's direct-SSH endpoint (no SSH connection). Idempotent."""
        if self.endpoint is not None:
            return self.endpoint
        if not self._api_key:
            raise RunPodError("no RunPod API key — cannot discover the pod endpoint")
        self.endpoint = self._discover(self._api_key, name=self._name, pod_id=self._pod_id)
        return self.endpoint

    def connect(self) -> Any:
        """Discover the pod + open the persistent control SSH (idempotent)."""
        if self.ssh is not None:
            return self.ssh
        self.discover()
        if not self._ssh_key or not Path(self._ssh_key).exists():
            raise RunPodError(f"SSH key not found: {self._ssh_key}")
        self.ssh = self._ssh_factory(self.endpoint, self._ssh_key)
        self.ssh.connect()
        return self.ssh

    def start(
        self,
        command: str,
        port: int,
        *,
        ready_path: str = "/",
        deploy: Callable[[Any], None] | None = None,
        wait: bool = True,
        wait_timeout: float = 180.0,
        poll: float = 3.0,
        log: Callable[[str], None] = print,
    ) -> str:
        """Start ``command`` in the foreground of the SSH session, forward ``port`` to localhost,
        (optionally) wait until ``ready_path`` answers, and return the local base URL. ``deploy``
        (if given) runs on the connection before the service starts."""
        if self.ssh is None:
            self.connect()
        if deploy is not None:
            deploy(self.ssh)
        self.proc = self.ssh.start_background(command)
        self.local_port = self.ssh.forward(port)
        base = f"http://127.0.0.1:{self.local_port}"
        log(f"[pod] service starting; tunneled pod:{port} -> {base}")
        if wait:
            self._wait_ready(base + ready_path, wait_timeout, poll, log)
        return base

    def _wait_ready(self, url: str, timeout: float, poll: float, log) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    if r.status == 200:
                        log("[pod] service is ready.")
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            if self.proc is not None and not self.proc.running:
                raise RunPodError(f"service exited during startup. Log tail:\n{self.log_tail()}")
            time.sleep(poll)
        raise RunPodError(f"service did not become ready within {timeout}s")

    def log_tail(self, nbytes: int = 4000) -> str:
        """Tail the pod-side log of the foreground process."""
        proc = self.proc
        if self.ssh is not None and proc is not None and getattr(proc, "log_path", None):
            try:
                return self.ssh.run(f"tail -c {nbytes} {proc.log_path}").stdout
            except Exception:  # noqa: BLE001
                pass
        return proc.read_available()[-nbytes:] if proc is not None else ""

    def close(self) -> None:
        if self.proc is not None:
            self.proc.stop()
            self.proc = None
        if self.ssh is not None:
            self.ssh.close()  # closing the connection kills the foreground service (by design)
            self.ssh = None
