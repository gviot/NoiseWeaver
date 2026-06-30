"""NoiseWeaverConfig.load: parse a noiseweaver.toml into ready repos, secrets from env, missing
file tolerated. No live Perforce needed — building a PerforceRepo doesn't touch `p4`."""

from __future__ import annotations

import pytest

from noiseweaver.config import NoiseWeaverConfig
from noiseweaver.storage import LocalRepo, PerforceRepo

_TOML = """
[repos.assets]
kind   = "perforce"
port   = "ssl:p:1666"
user   = "gv"
stream = "//assets/main"
client = "assets-ws"
root   = "~/p4/assets"

[repos.staging]
kind = "local"
root = "./staging"
"""


def test_load_missing_file_yields_no_repos(tmp_path):
    cfg = NoiseWeaverConfig.load(tmp_path / "absent.toml", env={})
    assert cfg.repos == {}


def test_load_builds_local_and_perforce(tmp_path):
    toml = tmp_path / "noiseweaver.toml"
    toml.write_text(_TOML)
    cfg = NoiseWeaverConfig.load(toml, env={"P4PASSWD": "secret"})
    assert isinstance(cfg.repo("assets"), PerforceRepo)
    assert isinstance(cfg.repo("staging"), LocalRepo)
    assert cfg.repo("assets").root().name == "assets"  # ~ expanded to a real path


def test_repo_lookup_missing_raises(tmp_path):
    cfg = NoiseWeaverConfig.load(tmp_path / "absent.toml", env={})
    with pytest.raises(KeyError, match="no repo"):
        cfg.repo("assets")


def test_load_path_from_env(tmp_path, monkeypatch):
    toml = tmp_path / "custom.toml"
    toml.write_text(_TOML)
    cfg = NoiseWeaverConfig.load(env={"NOISEWEAVER_CONFIG": str(toml), "P4PASSWD": "x"})
    assert set(cfg.repos) == {"assets", "staging"}


def test_compute_defaults_to_local_only(tmp_path):
    cfg = NoiseWeaverConfig.load(tmp_path / "absent.toml", env={})
    assert cfg.compute.runpod is False and cfg.compute.k8s is False
    router = cfg.compute.router(probe=lambda: None)  # daemon down + no offload
    from noiseweaver.scheduler import NoBackendAvailable

    with pytest.raises(NoBackendAvailable):
        router.route("flux", "flux2-klein-4b")


def test_compute_section_parsed_into_router(tmp_path):
    toml = tmp_path / "nw.toml"
    toml.write_text(
        '[compute]\nrunpod = true\nk8s = true\nprefer = ["local", "k8s", "runpod"]\n')
    cfg = NoiseWeaverConfig.load(toml, env={})
    assert cfg.compute.runpod and cfg.compute.k8s
    from noiseweaver.scheduler import K8S

    # daemon down → preference order sends it to k8s before runpod
    assert cfg.compute.router(probe=lambda: None).route("flux", "flux2-klein-4b") == K8S


def test_compute_rejects_unknown_backend(tmp_path):
    toml = tmp_path / "nw.toml"
    toml.write_text('[compute]\nprefer = ["local", "azure"]\n')
    with pytest.raises(ValueError, match="unknown backend"):
        NoiseWeaverConfig.load(toml, env={})
