"""Persistent control SSH + local port-forward to the pod.

RunPod kills detached processes on SSH disconnect (nohup/tmux/setsid all die), so
this keeps a **persistent control connection** that holds ComfyUI alive for as
long as we need it, and forwards the pod's ComfyUI port (8188) to a local port for
the HTTP API. When the connection drops, ComfyUI dies with it — that is the model.

Implemented over paramiko so it is fully scriptable (no PTY, unlike the proxy).
"""

from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

from .runpod import PodEndpoint


@dataclass
class SSHResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class PodSSH:
    """A single, reused SSH connection to the pod's direct sshd."""

    def __init__(self, endpoint: PodEndpoint, key_path: str | Path, user: str = "root"):
        self.endpoint = endpoint
        self.key_path = str(Path(key_path).expanduser())
        self.user = user
        self._client: paramiko.SSHClient | None = None
        self._forwards: list[_ForwardServer] = []

    # -- connection lifecycle ------------------------------------------------ #
    def connect(self, timeout: float = 30.0) -> None:
        if self._client is not None:
            return
        client = paramiko.SSHClient()
        # Accepted risk: RunPod pods are ephemeral and their host keys + ip:port churn per pod, so
        # known_hosts pinning is impractical here. We auto-add; the control connection only tunnels
        # to the pod we discovered via the authenticated RunPod API for this user's own GPU jobs.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.endpoint.host,
            port=self.endpoint.port,
            username=self.user,
            key_filename=self.key_path,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        # Keepalive so the network FS / idle NAT doesn't drop our control channel.
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        self._client = client

    def close(self) -> None:
        for fwd in self._forwards:
            fwd.stop()
        self._forwards.clear()
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> PodSSH:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise RuntimeError("PodSSH not connected; call connect() first")
        return self._client

    # -- commands ------------------------------------------------------------ #
    def run(self, cmd: str, timeout: float | None = None, check: bool = False) -> SSHResult:
        """Run a command to completion, draining stdout+stderr concurrently.

        We must read both streams as they arrive: reading stdout to EOF first can deadlock if the
        command fills its (separate) stderr window in the meantime, and vice versa.
        """
        transport = self.client.get_transport()
        assert transport is not None
        chan = transport.open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        out, err = bytearray(), bytearray()
        deadline = (time.monotonic() + timeout) if timeout else None
        while True:
            while chan.recv_ready():
                out += chan.recv(65536)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if deadline and time.monotonic() > deadline:
                chan.close()
                raise TimeoutError(f"remote command timed out after {timeout}s: {cmd}")
            time.sleep(0.05)
        code = chan.recv_exit_status()
        res = SSHResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
        if check and not res.ok:
            raise RuntimeError(f"remote command failed ({code}): {cmd}\n{res.stderr}")
        return res

    def start_background(
        self, cmd: str, log_path: str = "/tmp/comfyui.log"
    ) -> RemoteProcess:
        """Start a long-running process tied to THIS connection.

        We do NOT detach (nohup/tmux would be killed by RunPod). The returned channel stays open;
        closing the SSH connection terminates the process. Output is redirected to ``log_path`` on
        the pod (NOT streamed back over the channel) so the SSH per-channel flow-control window
        can't fill and stall ComfyUI; ``RemoteProcess`` still spawns a drain thread as a backstop.
        """
        transport = self.client.get_transport()
        assert transport is not None
        channel = transport.open_session()
        # Request a PTY so the remote process gets SIGHUP when the connection closes — this is what
        # makes ComfyUI die WITH our session. WITHOUT a PTY it reparents to init and ORPHANS (leaks
        # GPU/RAM). Output still goes to the pod-side log so nothing streams back over the channel
        # (no flow-control window fill). The caller must `exec` the real process inside `cmd` so it
        # (not the wrapper shell) is the PTY session leader that receives the SIGHUP.
        channel.get_pty()
        channel.exec_command(f"{cmd} > {log_path} 2>&1")
        return RemoteProcess(channel, log_path)

    # -- file transfer ------------------------------------------------------- #
    def put(self, local: str | Path, remote: str) -> None:
        sftp = self.client.open_sftp()
        try:
            _sftp_makedirs(sftp, str(Path(remote).parent))
            sftp.put(str(local), remote)
        finally:
            sftp.close()

    def get(self, remote: str, local: str | Path) -> None:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        sftp = self.client.open_sftp()
        try:
            sftp.get(remote, str(local))
        finally:
            sftp.close()

    # -- port forwarding ----------------------------------------------------- #
    def forward(self, remote_port: int, local_port: int = 0, remote_host: str = "127.0.0.1") -> int:
        """Forward local_port -> pod:remote_port. Returns the bound local port.

        Pass local_port=0 to let the OS pick a free port.
        """
        transport = self.client.get_transport()
        assert transport is not None
        fwd = _ForwardServer(local_port, remote_host, remote_port, transport)
        fwd.start()
        self._forwards.append(fwd)
        return fwd.local_port


class RemoteProcess:
    """Handle to a process kept alive by the SSH connection.

    A daemon thread continuously drains the channel so the per-channel SSH window can never fill
    (which would block the remote writer and stall the process). ``log_path`` is the pod-side file
    the process's output was redirected to — fetch it with ``ssh.run("tail ...")`` for diagnostics.
    """

    _BUF_CAP = 65536  # keep only the most recent bytes seen on the channel itself

    def __init__(self, channel: paramiko.Channel, log_path: str = ""):
        self.channel = channel
        self.log_path = log_path
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        try:
            while True:
                drained = False
                while self.channel.recv_ready():
                    data = self.channel.recv(65536)
                    if data:
                        with self._lock:
                            self._buf += data
                            del self._buf[: -self._BUF_CAP]
                        drained = True
                while self.channel.recv_stderr_ready():
                    self.channel.recv_stderr(65536)
                    drained = True
                if self.channel.exit_status_ready() and not self.channel.recv_ready():
                    break
                if not drained:
                    time.sleep(0.2)
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return not self.channel.exit_status_ready()

    def read_available(self) -> str:
        with self._lock:
            return bytes(self._buf).decode("utf-8", "replace")

    def stop(self) -> None:
        try:
            self.channel.close()
        except Exception:
            pass


def _sftp_makedirs(sftp: paramiko.SFTPClient, path: str) -> None:
    parts = [p for p in path.split("/") if p]
    cur = "/" if path.startswith("/") else ""
    for part in parts:
        cur = f"{cur}{part}" if cur in ("", "/") else f"{cur}/{part}"
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


class _ForwardServer(threading.Thread):
    """A tiny local TCP server that tunnels each accepted connection over SSH."""

    def __init__(self, local_port: int, remote_host: str, remote_port: int, transport):
        super().__init__(daemon=True)
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.transport = transport
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", local_port))
        self._sock.listen(16)
        self.local_port = self._sock.getsockname()[1]
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._sock], [], [], 0.5)
            except OSError:
                break  # socket closed by stop() during teardown
            if not r:
                continue
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            chan = self.transport.open_channel(
                "direct-tcpip",
                (self.remote_host, self.remote_port),
                conn.getpeername(),
            )
        except Exception:
            conn.close()
            return
        _pipe(conn, chan)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def _pipe(sock: socket.socket, chan) -> None:
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [])
            if sock in r:
                data = sock.recv(65536)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(65536)
                if not data:
                    break
                sock.sendall(data)
    except OSError:
        pass  # normal: peer reset a keep-alive socket / channel closed on teardown
    finally:
        try:
            chan.close()
        finally:
            sock.close()
