"""The plugin contract: how a domain pipeline mounts itself into the platform shell.

A plugin supplies three things and nothing pipeline-specific leaks into this module:
- **branding** — the display name/subtitle/CSS (so the shell is agnostic of any one pipeline),
- **a header** — the shared selection widget every tab keys off (e.g. an asset picker),
- **tabs** — named UI builders the shell mounts.

Whoever runs NoiseWeaver writes a plugin against this contract. Keep this module free of Gradio and
pipeline imports so it stays cheap to import and truly generic.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class Branding:
    """What the shell shows in the title bar + header. A plugin sets these (and, at runtime, env
    overrides them) so the UI shows the studio's name, never the platform's or a pipeline's."""

    title: str
    subtitle: str = ""
    css: str = ""


# Env prefix for runtime branding overrides.
ENV_PREFIX = "NOISEWEAVER"


def resolve_branding(b: Branding) -> Branding:
    """Apply runtime overrides: ``{PREFIX}_TITLE`` / ``{PREFIX}_SUBTITLE`` win over the plugin's
    defaults, so the same build can be rebranded without code changes. CSS stays plugin-owned."""
    return Branding(
        title=os.environ.get(f"{ENV_PREFIX}_TITLE", b.title),
        subtitle=os.environ.get(f"{ENV_PREFIX}_SUBTITLE", b.subtitle),
        css=b.css,
    )


# A tab is a (label, builder) pair. The builder receives (config, header) — `header` is whatever
# `build_header` returned (the shared selection widget) — and mounts its UI into the current Tab.
TabBuilder = Callable[[Any, Any], None]


@runtime_checkable
class Plugin(Protocol):
    """A platform plugin. The shell renders the chrome (title, tab strip, server) and calls these;
    the plugin owns the domain (its asset model, its pipeline tabs)."""

    def branding(self) -> Branding:
        """Display name/subtitle/CSS for the shell (before env overrides)."""
        ...

    def build_header(self, config: Any) -> Any:
        """Mount the shared header (e.g. an asset picker + a status banner) and return the widget
        the tabs key off. Runs inside the shell's `gr.Blocks` context."""
        ...

    def tabs(self) -> list[tuple[str, TabBuilder]]:
        """Ordered ``(label, builder)`` tabs to mount. Each builder gets ``(config, header)``."""
        ...

    # Optional (duck-typed — the shell calls it via getattr, so a plugin need not implement it):
    #   def allowed_paths(self) -> list[str]:
    #       """Filesystem roots the shell may serve files from, beyond the cwd + temp dir (Gradio
    #       blocks the rest). Return e.g. the staging root when artifacts live under $HOME."""
