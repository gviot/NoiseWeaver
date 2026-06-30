"""NoiseWeaver's platform config — repos today, branding/compute later. Self-standing: it depends
on nothing but stdlib + :mod:`noiseweaver.storage`.

Config is a ``noiseweaver.toml`` (path via ``NOISEWEAVER_CONFIG``, else ``./noiseweaver.toml``); a
``[repos.<name>]`` table per logical repo. Secrets — the Perforce password — come from the
environment (``P4PASSWD``), never the file. A missing file is fine (no repos configured), so the
platform still imports and runs without one.

```toml
[repos.assets]
kind   = "perforce"
port   = "ssl:perforce.studio:1666"
user   = "guillaume"
stream = "//assets/main"
client = "assets-mac-gv"
root   = "~/p4/assets"

[repos.staging]
kind = "local"
root = "./staging"
```
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .scheduler import K8S, LOCAL, RUNPOD, ComputeRouter, HostCapabilities
from .storage import P4Runner, Repo, RepoSpec, make_repo, run_p4

DEFAULT_CONFIG = "noiseweaver.toml"
P4_PASSWORD_ENV = "P4PASSWD"


@dataclass(frozen=True)
class ComputeConfig:
    """Which offload backends are available + the routing preference order. Parsed from
    ``[compute]`` in ``noiseweaver.toml`` (all optional; the default is local-only)."""

    runpod: bool = False
    k8s: bool = False
    prefer: tuple[str, ...] = (LOCAL, RUNPOD, K8S)

    def router(
        self, probe: Callable[[], HostCapabilities | None] | None = None
    ) -> ComputeRouter:
        """A :class:`ComputeRouter` from this config. ``probe`` is the local-host capability
        probe the caller injects (a plugin wires its own daemon client; tests pass a fake)."""
        return ComputeRouter(
            probe=probe,
            runpod_available=self.runpod,
            k8s_available=self.k8s,
            prefer=self.prefer,
        )


def _spec_from_table(name: str, t: dict[str, Any]) -> RepoSpec:
    root = t.get("root")
    if not root:
        raise ValueError(f"repo '{name}' needs a 'root'")
    return RepoSpec(
        kind=t.get("kind", "local"),
        root=Path(os.path.expanduser(str(root))),
        port=t.get("port"),
        user=t.get("user"),
        client=t.get("client"),
        stream=t.get("stream"),
    )


def _compute_from_table(t: dict[str, Any]) -> ComputeConfig:
    prefer = t.get("prefer")
    valid = {LOCAL, RUNPOD, K8S}
    if prefer is not None:
        bad = [b for b in prefer if b not in valid]
        if bad:
            raise ValueError(
                f"[compute] prefer has unknown backend(s) {bad}; pick from {sorted(valid)}")
    return ComputeConfig(
        runpod=bool(t.get("runpod", False)),
        k8s=bool(t.get("k8s", False)),
        prefer=tuple(prefer) if prefer else (LOCAL, RUNPOD, K8S),
    )


@dataclass
class NoiseWeaverConfig:
    """The resolved platform config: logical repo name → a ready-to-use :class:`Repo`, plus the
    compute-routing policy (:class:`ComputeConfig`)."""

    repos: dict[str, Repo]
    compute: ComputeConfig = field(default_factory=ComputeConfig)

    def repo(self, name: str) -> Repo:
        if name not in self.repos:
            raise KeyError(f"no repo '{name}' configured (have: {sorted(self.repos)})")
        return self.repos[name]

    @classmethod
    def load(
        cls,
        path: str | os.PathLike | None = None,
        *,
        runner: P4Runner = run_p4,
        env: dict[str, str] | None = None,
    ) -> NoiseWeaverConfig:
        """Parse the toml into repos + compute policy. ``runner``/``env`` are injectable for
        testing; the Perforce password is read from ``env[P4PASSWD]``. A missing config file yields
        zero repos and a local-only compute policy."""
        env = env if env is not None else dict(os.environ)
        cfg_path = Path(path or env.get("NOISEWEAVER_CONFIG", DEFAULT_CONFIG)).expanduser()
        data: dict[str, Any] = {}
        if cfg_path.exists():
            data = tomllib.loads(cfg_path.read_text())
        password = env.get(P4_PASSWORD_ENV) or None
        repos = {
            name: make_repo(_spec_from_table(name, table), password=password, runner=runner)
            for name, table in (data.get("repos") or {}).items()
        }
        return cls(repos=repos, compute=_compute_from_table(data.get("compute") or {}))
