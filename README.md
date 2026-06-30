# NoiseWeaver

A rebrandable, polyglot **platform for AI asset pipelines** — the open, generic layer you run
*around* a pipeline. It provides the parts every studio needs and none of the parts that are
anyone's secret.

NoiseWeaver contains **no pipeline**: the stages, recipes, model wiring, and prompts are a *plugin*
written by whoever uses it. The dependency arrow points one way — plugins import `noiseweaver`;
`noiseweaver` never imports a plugin, a specific compute daemon, or a storage backend it merely
orchestrates. That keeps the platform free of proprietary IP and able to stand on its own (and to be
shared between studios).

## The four pillars (+ the shell)

| Module | What it is |
|---|---|
| `noiseweaver.build` | A generic **content-addressed staged-build engine** over JSON specs — a mini build system: a spec document → a Merkle DAG → CAS staging → variant ledger + tombstones. |
| `noiseweaver.compute` | A **Tool × Platform compute scheduler** — *what* API you speak (ComfyUI, or a plugin's own client) is orthogonal to *where* it runs (local, RunPod, k8s); placed Kubernetes-style by matching requirements ↔ offers. |
| `noiseweaver.storage` / `.cas` | A **storage/repo abstraction** (local folder \| Perforce stream) + an immutable **content-addressed store**. The pipeline only ever sees a local path. |
| `noiseweaver.pod` | **Pod transport** — discover a RunPod's SSH, hold a persistent tunnel, talk to ComfyUI over HTTP, and run a service on a rented pod (`PodService`). |
| `noiseweaver.shell` / `.plugin` | A **rebrandable Gradio shell** + the small plugin contract that mounts a pipeline into it. |

Everything is pure-Python and **fully unit-tested with no daemon, pod, cluster, or Perforce server**
(70 tests) — the external pieces are injected and faked.

---

## Build engine — `noiseweaver.build`

A spec is a JSON document (`{type, stages: {stage: params}}`). The engine knows how to chain, hash,
store, and cache stages into a Merkle DAG where every parameter branch is content-addressed and
cheaply cached; it knows nothing about what the stages *do*. A plugin injects a **`Pipeline`** (the
stage names + per-stage normalizers, recipe versions, etc.) and the executors.

```python
from noiseweaver.build import Spec, SpecFormat, Pipeline, Staging, Index, build

fmt = SpecFormat(suffix=".demo.json")                  # your on-disk format + a validator
pipe = Pipeline(stages=("gen", "post"), normalize=my_normalize,
                recipe_versions={"gen": "g/1"}, artifact_names={"gen": "gen.png"})

spec = Spec.load("proj/thing.demo.json", fmt, root="…")
build(spec, pipe, {"gen": gen_executor, "post": post_executor},
      staging=Staging("~/.cache/staging"), index=Index("~/.cache/staging"))
# unchanged stages are cache hits; changing one param recomputes only it + its descendants.
```

Subclass `Spec` to bind your format once (so callers keep a clean `MySpec.load(path)`) — e.g. a
plugin binds its own `.<type>.json` format + validator + recipe versions in one place.

## Compute — `noiseweaver.compute`

Two orthogonal axes, composed into a **`Placement = (Tool, Platform, ExecutionMode)`** the scheduler
resolves Kubernetes-style:

- **Tool** — the API you speak (`ComfyUITool` HTTP ships built in; a plugin adds its own, e.g. an
  NDJSON/UDS local-daemon client). Platform-agnostic; one client whether the service is local, on a
  pod, or in a cluster.
- **Platform** — where it's provisioned and reached (`LocalPlatform`, `ExternalPlatform`,
  `RemotePlatform`). Tool-agnostic; RunPod/k8s lifecycle is injected once and shared across tools.
- **Requirements ↔ Offers** — a tool declares `Requirements({os, arch, accelerator, vram})`; a
  platform declares `Offers`; the scheduler only places a tool where the offers satisfy it (an MLX
  tool requires `{macos, mlx}` so it never schedules off a Mac; a CUDA workload never lands on a Mac).
- **Execution modes** — `SERVICE` (a persistent endpoint, many calls) and `JOB` (submit → result).

```python
from noiseweaver.compute import (
    Scheduler, ComfyUITool, LocalPlatform, RemotePlatform, Offers,
    ImageRequest, text_to_image,
)

sched = Scheduler(
    # ComfyUITool ships with NoiseWeaver; MyLocalDaemonTool is your plugin's own Tool.
    tools=[MyLocalDaemonTool(), ComfyUITool()],
    platforms=[LocalPlatform(),
               RemotePlatform("runpod", Offers("linux", "x86_64", frozenset({"cuda"}), 80),
                              acquire=my_pod_acquire)],
)
# picks the local daemon for a Mac-hostable weight, else comfyui@runpod — by matching, not branches.
result = text_to_image(sched, ImageRequest(prompt="…", output="/tmp/out.png",
                                           model="flux", variant="flux1-schnell"))
```

## Storage — `noiseweaver.storage` / `noiseweaver.cas`

Point each logical repo at a local folder or a Perforce stream; the pipeline only sees a local path
(a synced Perforce stream *is* a folder), and NoiseWeaver manages the Perforce workspace (client
spec + login) and `sync`/`submit`.

```python
from noiseweaver.config import NoiseWeaverConfig

cfg = NoiseWeaverConfig.load()        # reads noiseweaver.toml; P4 password from $P4PASSWD
assets = cfg.repo("assets")
assets.ensure_ready()                 # provisions the p4 client + login (idempotent)
assets.sync()                         # `p4 sync` (no-op for a local repo)
root = assets.root()                  # the local path your pipeline reads/writes
assets.submit([root / "new.fbx"], "add new asset")
```

`noiseweaver.cas.CasStore` is the immutable, content-addressed blob+manifest store the build engine's
staging is built on (hardlink materialization, safe to back up as a plain folder).

To back a repo with a local, password-protected, checkpoint-backed Perforce server (and how to
fold-back-up Perforce safely with Backblaze), see
[`docs/perforce-local-server.md`](docs/perforce-local-server.md).

## Pod — `noiseweaver.pod`

The generic pod transport the compute backends provision onto: `discover_endpoint` (RunPod GraphQL
direct-SSH), `PodSSH` (persistent control SSH + local port-forward), `ComfyUIClient` (HTTP + UI→API
workflow conversion), and `PodService` — the reusable lifecycle: discover → connect → optional
deploy hook → start a foreground command → forward its port → wait until ready → tear down on close.
Discovery + the SSH class are injectable, so the whole lifecycle unit-tests with no pod.

## The shell — `noiseweaver.shell` / `noiseweaver.plugin`

```python
from noiseweaver.shell import launch
launch(MyPipelinePlugin(), my_config)    # serves the rebranded UI on the LAN
```

A plugin implements three methods — `branding()`, `build_header(config)`, `tabs()`. The shell owns
the chrome; set `NOISEWEAVER_TITLE` / `NOISEWEAVER_SUBTITLE` to rebrand without code. See
[`noiseweaver/plugin.py`](noiseweaver/plugin.py).

## Configuration

`noiseweaver.toml` (path via `NOISEWEAVER_CONFIG`) — repos + the compute policy; secrets come from
the environment, never the file. See [`noiseweaver.example.toml`](noiseweaver.example.toml).

```toml
[repos.assets]
kind   = "perforce"         # or "local"
stream = "//assets/main"
client = "assets-mac-you"
root   = "~/p4/assets"

[compute]
runpod = true               # offload backends available to the scheduler
k8s    = false
prefer = ["local", "runpod", "k8s"]
```

## Develop

```bash
uv sync --extra dev
uv run pytest        # 70 tests, no external services
uv run ruff check
```

MIT licensed.
