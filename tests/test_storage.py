"""Storage backends: LocalRepo (trivial) and PerforceRepo driven against a fake `p4` runner, so the
whole login → client-spec → sync → submit lifecycle is covered with no live Perforce server."""

from __future__ import annotations

import pytest

from noiseweaver.storage import (
    LocalRepo,
    P4Error,
    PerforceRepo,
    RepoSpec,
    make_repo,
)

# --- LocalRepo ---------------------------------------------------------------


def test_local_repo_root_ensure_and_noops(tmp_path):
    r = LocalRepo(tmp_path / "assets")
    assert r.root() == tmp_path / "assets"
    r.ensure_ready()
    assert (tmp_path / "assets").is_dir()
    assert r.sync().synced == 0
    r.submit([], "noop")  # no-op, must not raise


# --- a fake `p4` ------------------------------------------------------------


def _subcmd(args: list[str]) -> str:
    """The p4 subcommand: first token after the global -p/-u/-c flags (+ their values)."""
    it = iter(args)
    for a in it:
        if a in ("-p", "-u", "-c"):
            next(it, None)
            continue
        return a
    return ""


class FakeP4:
    def __init__(self, *, logged_in=True, sync_output="", fail_on=None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.logged_in = logged_in
        self.sync_output = sync_output
        self.fail_on = fail_on or {}

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        cmd = _subcmd(args)
        if cmd == "login" and "-s" in args:
            if not self.logged_in:
                raise P4Error(args, 1, "Perforce password (P4PASSWD) invalid or unset.")
            return "ticket valid\n"
        if cmd in self.fail_on:
            raise P4Error(args, 1, self.fail_on[cmd])
        if cmd == "sync":
            return self.sync_output
        return ""

    def subcommands(self) -> list[str]:
        return [_subcmd(a) for a, _ in self.calls]


def _spec(tmp_path) -> RepoSpec:
    return RepoSpec(
        kind="perforce", root=tmp_path / "p4assets", port="ssl:p:1666",
        user="gv", client="assets-ws", stream="//assets/main")


# --- PerforceRepo ------------------------------------------------------------


def test_perforce_ensure_ready_provisions_when_logged_in(tmp_path):
    fake = FakeP4(logged_in=True)
    r = PerforceRepo(_spec(tmp_path), password="pw", runner=fake)
    r.ensure_ready()
    assert (tmp_path / "p4assets").is_dir()
    assert fake.subcommands() == ["login", "client"]  # login -s ok → client -i (no login write)
    client_args, client_spec = next((a, s) for a, s in fake.calls if _subcmd(a) == "client")
    assert "Stream: //assets/main" in client_spec
    assert str(tmp_path / "p4assets") in client_spec  # Root points at the local workspace
    assert "-c" in client_args and "assets-ws" in client_args


def test_perforce_logs_in_when_no_ticket(tmp_path):
    fake = FakeP4(logged_in=False)
    r = PerforceRepo(_spec(tmp_path), password="secret", runner=fake)
    r.ensure_ready()
    # login -s (fails) → login (pipes the password) → client -i
    assert fake.subcommands() == ["login", "login", "client"]
    assert fake.calls[1][1] == "secret\n"  # password fed to stdin, never on the command line


def test_perforce_login_without_password_errors(tmp_path):
    fake = FakeP4(logged_in=False)
    r = PerforceRepo(_spec(tmp_path), password=None, runner=fake)
    with pytest.raises(P4Error, match="no P4 password"):
        r.ensure_ready()


def test_perforce_sync_counts_files_against_client(tmp_path):
    out = "//assets/a#1 - updated /x/a\n//assets/b#2 - added /x/b\n"
    fake = FakeP4(sync_output=out)
    r = PerforceRepo(_spec(tmp_path), password="pw", runner=fake)
    res = r.sync()
    assert res.synced == 2
    assert fake.subcommands() == ["sync"]
    assert "-c" in fake.calls[-1][0]  # synced against the managed client


def test_perforce_submit_reconciles_then_submits(tmp_path):
    fake = FakeP4()
    r = PerforceRepo(_spec(tmp_path), password="pw", runner=fake)
    r.submit([tmp_path / "p4assets/new.png"], "add new asset")
    assert fake.subcommands() == ["reconcile", "submit"]
    submit_args = fake.calls[-1][0]
    assert "-d" in submit_args and "add new asset" in submit_args


def test_perforce_submit_tolerates_nothing_to_do(tmp_path):
    fake = FakeP4(fail_on={
        "reconcile": "No file(s) to reconcile.",
        "submit": "No files to submit.",
    })
    r = PerforceRepo(_spec(tmp_path), password="pw", runner=fake)
    r.submit([], "noop")  # benign "nothing changed" must not raise


def test_perforce_requires_connection_fields(tmp_path):
    with pytest.raises(ValueError, match="needs"):
        PerforceRepo(RepoSpec(kind="perforce", root=tmp_path), runner=FakeP4())


def test_make_repo_factory(tmp_path):
    assert isinstance(make_repo(RepoSpec(kind="local", root=tmp_path)), LocalRepo)
    assert isinstance(make_repo(_spec(tmp_path), password="pw", runner=FakeP4()), PerforceRepo)
    with pytest.raises(ValueError, match="unknown repo kind"):
        make_repo(RepoSpec(kind="svn", root=tmp_path))
