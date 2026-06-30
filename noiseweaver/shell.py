"""The generic Gradio shell: chrome + tab strip + server, driven entirely by a :class:`Plugin`.

This is the rebrandable frontend a studio ships. It knows nothing about any pipeline — it asks the
plugin for branding, a header, and tabs, and wires them up. Swap the plugin (or set the brand env
vars) and the same shell becomes a different studio's tool. Gradio is imported lazily so importing
this module costs nothing when only the (headless) platform pieces are used.
"""

from __future__ import annotations

from typing import Any

from .plugin import Plugin, resolve_branding


def launch(plugin: Plugin, config: Any, *, share: bool = False, server_port: int = 7860) -> None:
    """Render `plugin` into a Gradio app and serve it on the LAN.

    server_name 0.0.0.0 so artists reach it over the network (no host login) — the whole point of a
    web tool. Branding comes from the plugin, with env overrides applied (resolve_branding)."""
    import gradio as gr

    b = resolve_branding(plugin.branding())

    with gr.Blocks(title=b.title) as demo:
        heading = f"# {b.title}"
        if b.subtitle:
            heading += f"\n{b.subtitle}"
        gr.Markdown(heading)

        header = plugin.build_header(config)

        with gr.Tabs():
            for label, builder in plugin.tabs():
                with gr.Tab(label):
                    builder(config, header)

    # A plugin may serve artifacts (image tiles, downloads) from outside the cwd/temp — e.g. a
    # content-addressed staging dir under $HOME. Gradio blocks those by default, so let the plugin
    # whitelist its roots via an optional `allowed_paths()`. Duck-typed so it stays optional.
    allowed = list(getattr(plugin, "allowed_paths", lambda: [])() or [])

    # Gradio 6 moved css to launch() (was a Blocks kwarg).
    demo.queue().launch(share=share, server_port=server_port, server_name="0.0.0.0", css=b.css,
                        allowed_paths=allowed or None)
