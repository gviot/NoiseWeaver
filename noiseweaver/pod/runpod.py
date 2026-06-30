"""RunPod GraphQL API: discover a pod's *direct* SSH endpoint.

The pod's ip:port changes per pod, so we query the RunPod GraphQL API and read the
runtime port mapping to find the direct sshd (``privatePort == 22`` &&
``isIpPublic``). We use the direct sshd rather than the ``ssh.runpod.io`` proxy
because the proxy needs a PTY and is bad for scripting; the direct sshd is clean.

The caller passes the API key (read from its own environment, never from here).
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

GRAPHQL_URL = "https://api.runpod.io/graphql"

# Query the caller's pods + their runtime port mapping.
_PODS_QUERY = """
query {
  myself {
    pods {
      id
      name
      desiredStatus
      runtime {
        ports { ip isIpPublic privatePort publicPort type }
      }
    }
  }
}
""".strip()


class RunPodError(RuntimeError):
    pass


@dataclass(frozen=True)
class PodEndpoint:
    pod_id: str
    name: str
    host: str  # public ip of the direct sshd
    port: int  # public port mapped to the pod's 22
    status: str  # desiredStatus, e.g. "RUNNING"

    @property
    def running(self) -> bool:
        return self.status.upper() == "RUNNING"


def _post(api_key: str, query: str, timeout: float = 20.0) -> dict:
    if not api_key:
        raise RunPodError(
            "No RunPod API key. Pass the RunPod API key (e.g. from RUNPOD_API_KEY) "
            "(https://www.runpod.io/console/user/settings -> API Keys)."
        )
    # Auth via the documented ?api_key= query param. We deliberately avoid resp.raise_for_status()
    # and never put the URL in an error/log line, because the full URL embeds the key — leaking it
    # into tracebacks/logs is the real exposure here (the request itself is over TLS).
    resp = requests.post(
        GRAPHQL_URL,
        params={"api_key": api_key},
        json={"query": query},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RunPodError(f"RunPod API HTTP {resp.status_code} (key redacted)")
    data = resp.json()
    if "errors" in data:
        raise RunPodError(f"RunPod GraphQL error: {data['errors']}")
    return data["data"]


def list_pods(api_key: str) -> list[dict]:
    """Raw pod dicts from the API (id, name, desiredStatus, runtime.ports)."""
    return _post(api_key, _PODS_QUERY)["myself"]["pods"] or []


def _direct_ssh_port(pod: dict) -> tuple[str, int] | None:
    """Return (ip, publicPort) for the public sshd (privatePort 22), or None."""
    runtime = pod.get("runtime") or {}
    for p in runtime.get("ports") or []:
        is_public_ssh = p.get("privatePort") == 22 and p.get("isIpPublic")
        if is_public_ssh and p.get("ip") and p.get("publicPort"):
            return str(p["ip"]), int(p["publicPort"])
    return None


def discover_endpoint(
    api_key: str, *, name: str | None = None, pod_id: str | None = None
) -> PodEndpoint:
    """Find one running pod's direct SSH endpoint.

    Filters by ``pod_id`` or ``name`` if given; otherwise picks the single running
    pod (erroring if there are zero or several so the choice is never ambiguous).
    """
    pods = list_pods(api_key)
    if pod_id:
        pods = [p for p in pods if p.get("id") == pod_id]
    elif name:
        pods = [p for p in pods if p.get("name") == name]

    candidates: list[PodEndpoint] = []
    for p in pods:
        ep = _direct_ssh_port(p)
        if ep is None:
            continue
        host, port = ep
        candidates.append(
            PodEndpoint(
                pod_id=p.get("id", ""),
                name=p.get("name", ""),
                host=host,
                port=port,
                status=p.get("desiredStatus", ""),
            )
        )

    running = [c for c in candidates if c.running] or candidates
    if not running:
        raise RunPodError(
            "No pod with a public direct-SSH port found. Is the pod running, and does "
            "its template expose SSH over a public TCP port (privatePort 22)?"
        )
    if len(running) > 1:
        names = ", ".join(f"{c.name}({c.pod_id})" for c in running)
        raise RunPodError(f"Multiple pods match; pass name= or pod_id= to disambiguate: {names}")
    return running[0]
