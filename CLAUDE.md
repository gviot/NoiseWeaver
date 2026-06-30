# NoiseWeaver — working rules

NoiseWeaver is the open, generic platform layer you run *around* an AI asset pipeline. **This
repository is public.** Every change is published; treat it that way.

The rules below are enforced by a pre-commit hook (see the end). Read them before editing — most of
them exist because they were broken once and the cleanup was expensive.

## 1. Public & IP-free — the rule that matters most

NoiseWeaver contains **no pipeline and no proprietary integration.** The dependency arrow points one
way: a downstream plugin imports `noiseweaver`; `noiseweaver` imports nothing private and names
nothing private.

Concretely, the following must **never** appear in this repo (code, comments, docstrings, tests,
commit messages, config, fixtures):

- The name of any private downstream consumer, its codenames, or its daemons/binaries.
- A concrete proprietary backend. The only built-in compute tool is **ComfyUI** (genuinely
  open-source). Any platform-specific or private tool (e.g. a local-daemon client) is supplied by
  the plugin and lives in the plugin's repo — it is *injected* into `Scheduler`/`ComputeRouter`,
  never hard-coded here.
- Private filesystem paths, server hostnames, internal infrastructure, or a teammate's machine
  layout. Examples in docstrings must be generic (`/Users/x/...`, `perforce.example.com`).
- A specific model **menu/roadmap** baked into generic code. The router ships no model catalog;
  estimates and served-model lists come from the injected probe or are passed by the caller.

The test for whether something belongs here: *could any studio use it without owning our pipeline?*
If no, it belongs in a plugin.

**How the guard knows the forbidden names without leaking them:** the actual codenames live only in
`.ip-denylist` at the repo root, which is **gitignored** — it is never committed, so the public repo
never spells out what it is scrubbing. `scripts/check_ip_free.py` reads that file and fails the
commit on a case-insensitive substring match. To start guarding a new private name, add one
substring per line to `.ip-denylist` (it is local to your checkout; recreate it after a fresh
clone). If the file is absent the guard is inert and says so.

## 2. Code quality

The bar is "a stranger can depend on this":

- **Lint clean.** `ruff check` passes (config in `pyproject.toml`: line length 100; `E F I UP B`
  plus the docstring floor `D100 D104 D419`). No `# noqa` without a one-line reason.
- **Tests pass and need nothing external.** The whole suite runs with **no daemon, pod, cluster, or
  Perforce server** — external pieces are *injected and faked* (a fake unix socket, a fake P4
  runner, a fake pod `acquire`). New behaviour ships with a test that follows this pattern; never
  add a test that reaches the network or a real service.
- **Pure-Python, typed.** Full type hints on public signatures; `from __future__ import annotations`
  at the top of each module. Prefer dataclasses + small protocols over inheritance.
- **One way to do a thing.** Before adding a backend/tool/platform, check it isn't a special case of
  an existing abstraction (Tool × Platform, the build DAG, the Repo interface). Generalise, don't
  fork.

## 3. Documentation

Code is the source of truth for behaviour; docstrings carry the *why*.

- **Every module starts with a docstring** that says what it is *and why it exists* — the design
  tension it resolves, not a restatement of the name. This floor is linted (`D100`/`D104`); the
  quality of it is on you. The existing modules are the reference for tone.
- **Public classes and non-obvious functions** get a docstring covering intent, the contract, and
  any sharp edge ("do NOT simplify this away", "SERVICE-only", units of a number).
- **Keep the surfaces in sync.** A change to the public API, config keys, or backend ids must update
  `README.md` and `noiseweaver.example.toml` in the same commit. The README test count should match
  `pytest`.
- Comments explain *why*, never *what*. If a comment narrates the code, delete it.

## 4. Before you commit

```bash
uv sync --extra dev          # once: installs ruff, pytest, pre-commit
uv run pre-commit install    # once: enables the git hook
```

Then every commit runs, in order: the **IP-free guard**, **ruff**, and the **test suite**. A commit
that trips any of them is blocked — fix it, don't bypass it (`--no-verify` defeats the point and is
how the last leak shipped). To run the gates by hand: `uv run pre-commit run --all-files`.
