"""The plugin contract + branding resolution — NoiseWeaver standing alone, no pipeline needed."""

from __future__ import annotations

from noiseweaver.plugin import Branding, Plugin, resolve_branding


def test_resolve_branding_passthrough_without_env(monkeypatch):
    monkeypatch.delenv("NOISEWEAVER_TITLE", raising=False)
    monkeypatch.delenv("NOISEWEAVER_SUBTITLE", raising=False)
    b = Branding(title="X", subtitle="y", css="z")
    assert resolve_branding(b) == Branding("X", "y", "z")


def test_resolve_branding_env_overrides_title_and_subtitle(monkeypatch):
    monkeypatch.setenv("NOISEWEAVER_TITLE", "StudioOne")
    monkeypatch.setenv("NOISEWEAVER_SUBTITLE", "our pipeline")
    b = resolve_branding(Branding(title="Default", subtitle="...", css="c"))
    assert b.title == "StudioOne"
    assert b.subtitle == "our pipeline"
    assert b.css == "c"  # CSS stays plugin-owned, not env-overridable


class _FakePlugin:
    """A minimal stand-in proving the contract is satisfiable without any real pipeline."""

    def branding(self):
        return Branding(title="T", subtitle="s")

    def build_header(self, config):
        return None

    def tabs(self):
        return [("Tab", lambda config, header: None)]


def test_minimal_plugin_conforms_to_contract():
    p = _FakePlugin()
    assert isinstance(p, Plugin)  # runtime_checkable: has branding / build_header / tabs
    assert p.branding().title == "T"
    assert p.tabs()[0][0] == "Tab"
