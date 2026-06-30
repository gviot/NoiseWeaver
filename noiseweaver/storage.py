"""Storage/repo abstraction — point NoiseWeaver at a local folder or a Perforce stream.

The key simplification: **a Perforce stream, once synced, is just a folder.** So every repo resolves
to a local ``root()`` that the pipeline reads/writes exactly as before; local and Perforce differ
only in provisioning (``ensure_ready``), pulling (``sync``), and publishing (``submit``). The
pipeline never learns which backend it's on.

NoiseWeaver **fully manages** the Perforce workspace: ``ensure_ready`` makes sure there's a valid
login ticket and an up-to-date client spec (idempotent, so a fresh machine self-provisions). Every
``p4`` invocation goes through an injectable runner, so all of this is unit-testable without a live
Perforce server. Secrets (the P4 password) come from the environment, never from config files.

This module imports nothing pipeline-, daemon-, or NoiseWeaver-UI-specific — just stdlib + ``p4``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class RepoSpec:
    """Where a logical repo lives. ``kind`` selects the backend; the Perforce fields are unused for
    ``local``. ``root`` is always the local working path everything resolves against."""

    kind: str  # "local" | "perforce"
    root: Path
    # perforce-only
    port: str | None = None      # P4PORT, e.g. "ssl:perforce.studio:1666"
    user: str | None = None      # P4USER
    client: str | None = None    # the workspace name NoiseWeaver creates/updates
    stream: str | None = None    # the depot stream, e.g. "//assets/main"


@dataclass
class SyncResult:
    """Outcome of a sync. ``synced`` is a best-effort file count (-1 = unknown)."""

    synced: int = 0
    detail: str = ""


@runtime_checkable
class Repo(Protocol):
    """A working tree NoiseWeaver reads/writes. The pipeline only ever uses ``root()``."""

    def root(self) -> Path: ...
    def ensure_ready(self) -> None: ...  # provision: mkdir (local) / login + client spec (perforce)
    def sync(self) -> SyncResult: ...    # pull latest (no-op for local)
    def submit(self, paths: Sequence[Path], description: str) -> None: ...  # publish (no-op: local)


# ----------------------------------------------------------------------------- local


class LocalRepo:
    """A plain directory. sync/submit are no-ops — the working copy *is* the source of truth."""

    def __init__(self, root: Path | str):
        self._root = Path(root).expanduser()

    def root(self) -> Path:
        return self._root

    def ensure_ready(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def sync(self) -> SyncResult:
        return SyncResult(synced=0, detail="local (no sync)")

    def submit(self, paths: Sequence[Path], description: str) -> None:
        return None  # nothing to publish for a local folder


# ----------------------------------------------------------------------------- perforce

# A p4 runner runs `p4 <args>` (optionally with stdin) and returns stdout, raising P4Error on
# failure. Injectable so PerforceRepo is testable without a server.
P4Runner = Callable[[list[str], "str | None"], str]


class P4Error(RuntimeError):
    def __init__(self, args: list[str], code: int, message: str):
        self.args_ = args
        self.code = code
        super().__init__(f"`p4 {' '.join(args)}` failed ({code}): {message}")


def run_p4(args: list[str], stdin: str | None = None) -> str:
    """Default runner: shell out to the `p4` CLI. P4PORT/P4USER/P4CLIENT are passed per-call as
    flags by PerforceRepo, so the ambient environment doesn't have to be configured."""
    proc = subprocess.run(
        ["p4", *args], input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise P4Error(args, proc.returncode, (proc.stderr or proc.stdout).strip())
    return proc.stdout


# p4 messages that mean "nothing to do", which we treat as success on submit.
_BENIGN_SUBMIT = ("no file(s) to reconcile", "no files to submit", "no file(s) to submit")


class PerforceRepo:
    """A p4 stream workspace NoiseWeaver provisions and drives. ``ensure_ready`` guarantees a login
    ticket + a client spec rooted at ``root`` and bound to ``stream``; ``sync``/``submit`` then run
    against that client. The password (for the initial login) is read from the environment."""

    def __init__(self, spec: RepoSpec, *, password: str | None = None, runner: P4Runner = run_p4):
        missing = [f for f in ("port", "user", "client", "stream") if not getattr(spec, f)]
        if missing:
            raise ValueError(f"perforce repo needs {', '.join(missing)} (set them in the config)")
        self._spec = spec
        self._root = Path(spec.root).expanduser()
        self._password = password
        self._run = runner

    def root(self) -> Path:
        return self._root

    # connection flags: login is per-user (no client yet); other ops need the client too.
    def _conn(self, *, with_client: bool) -> list[str]:
        args = ["-p", self._spec.port, "-u", self._spec.user]
        if with_client:
            args += ["-c", self._spec.client]
        return args

    def ensure_ready(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_login()
        self._ensure_client()

    def _ensure_login(self) -> None:
        """Make sure there's a valid ticket; if not, log in with the configured password."""
        try:
            self._run([*self._conn(with_client=False), "login", "-s"], None)
            return  # ticket already valid
        except P4Error:
            pass
        if not self._password:
            raise P4Error(
                ["login"], 1,
                "not logged in and no P4 password available — set P4PASSWD in the environment")
        self._run([*self._conn(with_client=False), "login"], self._password + "\n")

    def _client_spec(self) -> str:
        # allwrite (not noallwrite): leave synced files writable so the pipeline's tools can rewrite
        # them in place without `p4 edit` first — a content-build workflow touches many files from
        # many tools. This is consistent with submit(), which `p4 reconcile`s the workspace to open
        # whatever changed before submitting (rather than relying on files being opened for edit).
        return (
            f"Client: {self._spec.client}\n"
            f"Owner: {self._spec.user}\n"
            f"Root: {self._root}\n"
            "Options: allwrite noclobber nocompress unlocked nomodtime normdir\n"
            "SubmitOptions: submitunchanged\n"
            "LineEnd: local\n"
            f"Stream: {self._spec.stream}\n"
        )

    def _ensure_client(self) -> None:
        # `p4 client -i` creates or updates the workspace spec — idempotent, so re-running on a
        # fresh machine just provisions it. A stream client derives its view from the stream.
        self._run([*self._conn(with_client=True), "client", "-i"], self._client_spec())

    def sync(self) -> SyncResult:
        out = self._run([*self._conn(with_client=True), "sync"], None)
        lines = [ln for ln in out.splitlines() if " - " in ln]  # one per file synced
        return SyncResult(synced=len(lines), detail=out.strip())

    def submit(self, paths: Sequence[Path], description: str) -> None:
        targets = [str(Path(p).expanduser()) for p in paths] or [f"{self._root}/..."]
        # reconcile opens add/edit/delete to match the workspace; tolerate "nothing changed".
        self._tolerant([*self._conn(with_client=True), "reconcile", *targets])
        self._tolerant([*self._conn(with_client=True), "submit", "-d", description])

    def _tolerant(self, args: list[str]) -> None:
        try:
            self._run(args, None)
        except P4Error as e:
            if any(b in str(e).lower() for b in _BENIGN_SUBMIT):
                return  # nothing to do — not an error
            raise


# ----------------------------------------------------------------------------- factory


def make_repo(spec: RepoSpec, *, password: str | None = None, runner: P4Runner = run_p4) -> Repo:
    """Build a Repo from its spec. ``password`` (Perforce only) comes from the environment."""
    if spec.kind == "local":
        return LocalRepo(spec.root)
    if spec.kind == "perforce":
        return PerforceRepo(spec, password=password, runner=runner)
    raise ValueError(f"unknown repo kind {spec.kind!r} (expected 'local' or 'perforce')")
