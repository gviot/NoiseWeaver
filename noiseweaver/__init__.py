"""NoiseWeaver — a rebrandable, polyglot platform for AI asset pipelines.

The open, generic layer a studio runs *around* a pipeline, in four pillars + a shell:

  * ``noiseweaver.build``   — a content-addressed staged-build engine over JSON specs (spec doc →
    Merkle DAG → CAS staging → variants/tombstones), parameterized by a plugin's ``Pipeline``.
  * ``noiseweaver.compute`` — a Tool × Platform scheduler: *what* API (ComfyUI, or a plugin's own
    client) is orthogonal to *where* it runs (local | RunPod | k8s), matched requirements↔offers.
  * ``noiseweaver.storage`` / ``.cas`` — a repo abstraction (local | Perforce stream) + an immutable
    content-addressed store.
  * ``noiseweaver.pod``     — pod transport (RunPod discovery, SSH tunnel, ComfyUI, PodService).
  * ``noiseweaver.shell`` / ``.plugin`` — a rebrandable Gradio shell + the plugin contract.

It contains **no pipeline** — the stages/recipes/models are a *plugin* on top. Dependency direction
is one-way: plugins import ``noiseweaver``; ``noiseweaver`` never imports a plugin (nor any specific
compute daemon or storage backend it merely orchestrates). That keeps the platform free of
proprietary IP and able to stand on its own.
"""
