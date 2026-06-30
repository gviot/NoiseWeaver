"""Pod transport + provider primitives: discover a RunPod's direct SSH endpoint, hold a persistent
control SSH + local port-forward, and talk to a ComfyUI server over HTTP. Generic infrastructure
the platform's compute backends provision onto; nothing here knows about any specific pipeline.
"""

from .comfyui import ComfyUIClient, ComfyUIError, JobResult, inject, ui_to_api
from .runpod import PodEndpoint, RunPodError, discover_endpoint, list_pods
from .service import PodService
from .ssh import PodSSH, RemoteProcess, SSHResult

__all__ = [
    "ComfyUIClient", "ComfyUIError", "JobResult", "inject", "ui_to_api",
    "PodEndpoint", "RunPodError", "discover_endpoint", "list_pods",
    "PodSSH", "RemoteProcess", "SSHResult", "PodService",
]
