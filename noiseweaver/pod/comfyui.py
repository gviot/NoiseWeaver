"""ComfyUI HTTP client: UI→API workflow runner + direct-graph runner.

The load-bearing piece is :func:`ui_to_api`. ComfyUI's exported *UI* workflows feed ``resolution``
/ ``target_face_num`` / ``texture_size`` through PrimitiveNode/Reroute nodes; a naive UI→API
conversion leaves those at 0 and ComfyUI rejects the prompt ("must be a positive integer" /
"resolution must be [>0,>0]"). The ``resolve()`` recursion that chases reroute chains and
substitutes primitive widget values is the whole point — do NOT "simplify" it away.

On top of that core, this client adds a configurable server URL, image upload, real output
download via ``/view``, and a poll loop with a timeout and error surfacing.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Primitive node class_types treated as value sources (verbatim from run_wf.py).
PRIMS = (
    "PrimitiveNode",
    "PrimitiveInt",
    "PrimitiveFloat",
    "PrimitiveString",
    "PrimitiveBoolean",
    "Int",
    "Float",
    "String",
    "Boolean",
)
# Default image-input node class for the TRELLIS workflows.
IMAGE_NODE_CLASS = "Trellis2LoadImageWithTransparency"


class ComfyUIError(RuntimeError):
    pass


def ui_to_api(wf: dict, obj_info: dict) -> dict:
    """Convert an exported ComfyUI *UI* workflow to the ``/prompt`` API graph.

    Ported verbatim from remote/run_wf.py (the PrimitiveNode/Reroute resolution is the whole
    point). ``obj_info`` is the parsed ``/object_info`` (node schemas) from the live server.
    """
    links = {link[0]: (str(link[1]), link[2]) for link in wf["links"]}
    prim_val: dict[str, object] = {}
    reroute_src: dict[str, int] = {}
    for n in wf["nodes"]:
        t, nid = n["type"], str(n["id"])
        if t in PRIMS:
            wv = n.get("widgets_values", []) or []
            if wv:
                prim_val[nid] = wv[0]
        if t == "Reroute":
            ins = n.get("inputs", []) or []
            if ins and ins[0].get("link") is not None:
                reroute_src[nid] = ins[0]["link"]

    def resolve(link_id, depth=0):
        src, slot = links[link_id]
        if src in reroute_src and depth < 10:
            return resolve(reroute_src[src], depth + 1)
        if src in prim_val:
            return ("value", prim_val[src])
        return ("link", [src, slot])

    prompt: dict[str, dict] = {}
    for n in wf["nodes"]:
        ct = n["type"]
        if ct in PRIMS + ("Note", "MarkdownNote", "Reroute"):
            continue
        nid = str(n["id"])
        info = obj_info.get(ct)
        if info is None:
            continue
        inputs: dict[str, object] = {}
        linked: set[str] = set()
        for s in n.get("inputs", []) or []:
            if s.get("link") is not None:
                _, val = resolve(s["link"])
                inputs[s["name"]] = val
                linked.add(s["name"])
        for name, spec in list(info["input"].get("required", {}).items()) + list(
            info["input"].get("optional", {}).items()
        ):
            if name in linked:
                continue
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(t, list):
                inputs[name] = cfg.get("default", t[0] if t else None)
            elif t in ("INT", "FLOAT", "STRING", "BOOLEAN"):
                inputs[name] = cfg.get(
                    "default", 0 if t in ("INT", "FLOAT") else ("" if t == "STRING" else False)
                )
        prompt[nid] = {"class_type": ct, "inputs": inputs}
    return prompt


def inject(prompt: dict, image: str | None, overrides: dict, image_node_class: str) -> dict:
    """Inject the input image (by class_type) and per-class overrides (overrides win)."""
    for _nid, nd in prompt.items():
        if image is not None and nd["class_type"] == image_node_class:
            nd["inputs"]["image"] = image
        for k, v in overrides.get(nd["class_type"], {}).items():
            nd["inputs"][k] = v
    return prompt


@dataclass
class JobResult:
    prompt_id: str
    status: str | None
    outputs: dict  # node_id -> output dict from /history


class ComfyUIClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # -- HTTP (post() ports run_wf.py's HTTPError->_http_error capture) ------- #
    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return json.load(r)

    def post(self, path: str, data: dict):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return {"_http_error": e.code, "body": e.read().decode()[:2000]}

    def object_info(self) -> dict:
        return self.get("/object_info")

    # -- image upload (multipart/form-data) ---------------------------------- #
    def upload_image(self, local_path: str, name: str | None = None, overwrite: bool = True) -> str:
        """Upload an RGBA PNG to ComfyUI's input dir; returns the server-side filename."""
        import mimetypes
        import os
        import uuid

        name = name or os.path.basename(local_path)
        boundary = f"----noiseweaver{uuid.uuid4().hex}"
        with open(local_path, "rb") as f:
            file_bytes = f.read()
        mime = mimetypes.guess_type(local_path)[0] or "image/png"
        parts = [
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode()
            + f"Content-Type: {mime}\r\n\r\n".encode()
            + file_bytes
            + b"\r\n",
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
            + (b"true" if overwrite else b"false")
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base + "/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            raise ComfyUIError(f"image upload failed ({e.code}): {e.read().decode()[:500]}") from e
        # response: {"name": "...", "subfolder": "", "type": "input"}
        sub = resp.get("subfolder") or ""
        return f"{sub}/{resp['name']}" if sub else resp["name"]

    # -- run + wait ---------------------------------------------------------- #
    def submit(self, graph: dict) -> str:
        r = self.post("/prompt", {"prompt": graph})
        if "_http_error" in r:
            raise ComfyUIError(f"ComfyUI validation error: {r['body'][:1500]}")
        return r["prompt_id"]

    def wait(self, prompt_id: str, timeout: float = 1800.0, poll: float = 3.0) -> JobResult:
        """Poll /history until the job appears; raise on timeout or error status."""
        deadline = time.monotonic() + timeout
        while True:
            h = self.get("/history/" + prompt_id)
            if prompt_id in h:
                entry = h[prompt_id]
                status = (entry.get("status") or {}).get("status_str")
                if status == "error":
                    msgs = (entry.get("status") or {}).get("messages")
                    raise ComfyUIError(f"ComfyUI job {prompt_id} errored: {msgs}")
                return JobResult(prompt_id, status, entry.get("outputs", {}))
            if time.monotonic() > deadline:
                raise ComfyUIError(f"ComfyUI job {prompt_id} timed out after {timeout}s")
            time.sleep(poll)

    def run_ui_workflow(
        self,
        ui_workflow: dict,
        *,
        image: str | None = None,
        overrides: dict | None = None,
        image_node_class: str = IMAGE_NODE_CLASS,
        timeout: float = 1800.0,
    ) -> JobResult:
        """Full path: /object_info → ui_to_api → inject → submit → wait."""
        graph = ui_to_api(ui_workflow, self.object_info())
        inject(graph, image, overrides or {}, image_node_class)
        return self.wait(self.submit(graph), timeout=timeout)

    def run_graph(self, graph: dict, *, timeout: float = 1800.0) -> JobResult:
        """Run a graph already in API format (e.g. the concept FLUX graph)."""
        return self.wait(self.submit(graph), timeout=timeout)

    # -- output retrieval ---------------------------------------------------- #
    def download(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        q = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": folder_type}
        )
        with urllib.request.urlopen(self.base + "/view?" + q, timeout=self.timeout) as r:
            return r.read()

    @staticmethod
    def find_outputs(result: JobResult, ext: str) -> list[dict]:
        """Find output file refs ({filename, subfolder, type}) whose name ends with ext.

        ComfyUI keys outputs by category (images/gltf/result/...) and Trellis save nodes vary, so
        scan every output value's list entries for ones with a matching 'filename'.
        """
        hits: list[dict] = []
        for node_id, out in result.outputs.items():
            for _key, val in out.items():
                if not isinstance(val, list):
                    continue
                for entry in val:
                    if isinstance(entry, dict) and str(entry.get("filename", "")).endswith(ext):
                        hits.append(
                            {
                                "node_id": str(node_id),
                                "filename": entry["filename"],
                                "subfolder": entry.get("subfolder", ""),
                                "type": entry.get("type", "output"),
                            }
                        )
        return hits

    @staticmethod
    def find_output_paths(result: JobResult, ext: str) -> list[str]:
        """Find output *path strings* ending with ext anywhere in /history outputs.

        Trellis2 reports GLBs this way: ``Preview3D`` returns a ``result`` list of absolute pod
        paths, and ``Trellis2ExportMesh`` writes the file but reports nothing — neither uses the
        SaveImage ``{filename,subfolder}`` dict shape. The returned paths are absolute (fetch via
        SFTP, not ``/view``).
        """
        paths: list[str] = []
        for out in result.outputs.values():
            for val in out.values():
                items = val if isinstance(val, list) else [val]
                for entry in items:
                    if isinstance(entry, str) and entry.endswith(ext):
                        paths.append(entry)
        return list(dict.fromkeys(paths))  # de-dup, preserve order
