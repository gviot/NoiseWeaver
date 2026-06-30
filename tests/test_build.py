"""The generic build engine: spec doc model, Merkle hashing, resolve/build caching, variants,
tombstones — exercised with a tiny 2-stage fake pipeline (no plugin, no real executors)."""

from __future__ import annotations

import json

from noiseweaver.build import (
    Index,
    Pipeline,
    Spec,
    SpecFormat,
    Staging,
    Tombstones,
    build,
    canonical_json,
    discover_specs,
    find_spec,
    resolve,
    sidecar_for,
    stage_hash,
)

FMT = SpecFormat(suffix=".demo.json")


def _pipeline():
    def normalize(stage, params):
        # fill a default + validate-ish; "gen" takes a seed, "post" takes a scale
        out = dict(params)
        out.setdefault("seed", 0) if stage == "gen" else out.setdefault("scale", 1)
        return out

    return Pipeline(
        stages=("gen", "post"),
        normalize=normalize,
        authoritative=frozenset({"gen"}),
        recipe_versions={"gen": "g/1", "post": "p/1"},
        artifact_names={"gen": "gen.txt", "post": "post.txt"},
        id_dependent_stages=frozenset({"post"}),
    )


def _spec():
    return Spec.from_dict({"type": "demo", "stages": {"gen": {"seed": 7}, "post": {"scale": 2}}},
                         FMT, spec_id="proj/thing")


# --------------------------------------------------------------------------- #
# hashing + spec doc model
# --------------------------------------------------------------------------- #
def test_canonical_json_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_stage_hash_sensitive_to_recipe_version():
    a = stage_hash("gen", {"seed": 1}, None, recipe_version="g/1")
    b = stage_hash("gen", {"seed": 1}, None, recipe_version="g/2")
    assert a != b  # bumping the executor version invalidates the cache


def test_spec_load_save_roundtrip_and_id_from_path(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    p = d / "thing.demo.json"
    p.write_text(json.dumps({"type": "demo", "stages": {"gen": {"seed": 1}}}))
    s = Spec.load(p, FMT, root=tmp_path)
    assert s.id == "proj/thing" and s.name == "thing" and s.variant is None
    # a variant filename sets .variant
    pv = d / "thing.hi.demo.json"
    pv.write_text(p.read_text())
    assert Spec.load(pv, FMT, root=tmp_path).variant == "hi"
    # discover + find
    assert set(discover_specs(tmp_path, FMT)) == {p, pv}
    assert find_spec(tmp_path, "proj/thing", FMT) == p


def test_fork_is_anonymous_and_distinct():
    s = _spec()
    f = s.fork(FMT, None, "gen", seed=99)
    assert f.variant is None and f.path is None
    assert f.stages["gen"]["seed"] == 99 and s.stages["gen"]["seed"] == 7  # original untouched


# --------------------------------------------------------------------------- #
# resolve / build
# --------------------------------------------------------------------------- #
def test_resolve_chains_hashes_and_id_dependent_stage():
    plans = resolve(_spec(), _pipeline(), staging=Staging(_tmp()))
    assert [p.stage for p in plans] == ["gen", "post"]
    assert plans[1].parent_hash == plans[0].hash       # Merkle chain
    assert plans[1].params["asset_id"] == "proj/thing"  # id folded into the id-dependent stage
    assert plans[0].params["asset_id"] != "proj/thing" if "asset_id" in plans[0].params else True


def test_build_executes_then_caches(tmp_path):
    pipe, spec = _pipeline(), _spec()
    staging, index = Staging(tmp_path), Index(tmp_path)
    runs = []

    def gen(ctx):
        runs.append(ctx.stage)
        (ctx.out_dir / "gen.txt").write_text(f"seed={ctx.params['seed']}")

    def post(ctx):
        runs.append(ctx.stage)
        (ctx.out_dir / "post.txt").write_text(f"parent={ctx.parent.primary.read_text()}")

    ex = {"gen": gen, "post": post}
    quiet = {"staging": staging, "index": index, "log": lambda *_: None}
    r1 = build(spec, pipe, ex, **quiet)
    assert [s.status for s in r1.stages] == ["built", "built"]
    assert r1.final().primary.read_text() == "parent=seed=7"   # parent.primary via artifact_names
    # second build: all cached, no executor runs
    runs.clear()
    r2 = build(spec, pipe, ex, **quiet)
    assert [s.status for s in r2.stages] == ["cached", "cached"] and runs == []


def _write(ctx, name, text):
    (ctx.out_dir / name).write_text(text)  # returns None → a valid empty ExecResult


def test_changing_a_param_rebuilds_only_descendants(tmp_path):
    pipe = _pipeline()
    staging, index = Staging(tmp_path), Index(tmp_path)
    ex = {"gen": lambda c: _write(c, "gen.txt", "x"),
          "post": lambda c: _write(c, "post.txt", "y")}
    build(_spec(), pipe, ex, staging=staging, index=index, log=lambda *_: None)
    # nudge post's param only → gen stays cached, post rebuilds
    spec2 = Spec.from_dict({"type": "demo", "stages": {"gen": {"seed": 7}, "post": {"scale": 9}}},
                          FMT, spec_id="proj/thing")
    r = build(spec2, pipe, ex, staging=staging, index=index, log=lambda *_: None)
    assert [s.status for s in r.stages] == ["cached", "built"]


def test_dry_run_reports_without_executing(tmp_path):
    ran = []
    noop = {"gen": lambda c: ran.append(1), "post": lambda c: ran.append(1)}
    r = build(_spec(), _pipeline(), noop, staging=Staging(tmp_path), index=Index(tmp_path),
              dry_run=True, log=lambda *_: None)
    assert [s.status for s in r.stages] == ["would-run", "would-run"] and ran == []


def test_tombstone_skips_and_halts_chain(tmp_path):
    pipe, spec = _pipeline(), _spec()
    staging, index = Staging(tmp_path), Index(tmp_path)
    plans = resolve(spec, pipe, staging=staging)
    tombs = Tombstones(tmp_path / "t.tombstones.json")
    tombs.add("gen", plans[0].hash, spec.id, "bad", "now")
    ran = []
    r = build(spec, pipe, {"gen": lambda c: ran.append(1), "post": lambda c: ran.append(1)},
              staging=staging, index=index, tombstones=tombs, force=True, log=lambda *_: None)
    # force can't resurrect; the dead ancestor halts the chain before post
    assert [s.status for s in r.stages] == ["skipped"] and ran == []


def test_param_hook_feeds_the_hash(tmp_path):
    # a hook that folds extra text into the hash → two specs with the same params but different
    # hook context produce different hashes
    def hook(stage, params, spec, context):
        return {**params, "_folded": context} if stage == "gen" else params

    pipe = Pipeline(stages=("gen",), normalize=lambda s, p: dict(p), param_hooks=(hook,))
    a = resolve(_spec(), pipe, staging=Staging(tmp_path), context="A")[0].hash
    b = resolve(_spec(), pipe, staging=Staging(tmp_path), context="B")[0].hash
    assert a != b


def test_sidecar_for_strips_suffix_and_variant(tmp_path):
    base = sidecar_for(tmp_path / "thing.demo.json", FMT)
    var = sidecar_for(tmp_path / "thing.hi.demo.json", FMT)
    assert base.name == "thing.tombstones.json" and var == base  # one sidecar per base id


def _tmp():
    import pathlib
    import tempfile
    return pathlib.Path(tempfile.mkdtemp())
