#!/usr/bin/env python3
"""Fail a commit that reintroduces a private/proprietary name into this public repo.

The forbidden names are *not* stored here — that would defeat the purpose. They live one-per-line in
``.ip-denylist`` at the repo root, which is gitignored, so the public tree never spells out what it
scrubs. Each non-comment line is a case-insensitive substring; a match anywhere in a scanned file
blocks the commit.

Usage:
    python scripts/check_ip_free.py [FILE ...]

With file arguments (how pre-commit calls it) only those are scanned; with none, the staged files
are scanned. If ``.ip-denylist`` is missing the guard is inert and says so (exit 0).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DENYLIST_NAME = ".ip-denylist"


def repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(__file__).resolve().parent.parent


def load_needles(root: Path) -> list[str] | None:
    f = root / DENYLIST_NAME
    if not f.exists():
        return None
    needles = []
    for line in f.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            needles.append(s.lower())
    return needles


def staged_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    )
    return [root / p for p in out.stdout.split("\n") if p]


def scan(path: Path, needles: list[str]) -> list[tuple[int, str, str]]:
    """Return (lineno, needle, line) for every denylisted hit in ``path``; [] for binary/missing."""
    if not path.is_file() or path.name == DENYLIST_NAME:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable — nothing to scan
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for needle in needles:
            if needle in low:
                hits.append((lineno, needle, line.strip()))
    return hits


def main(argv: list[str]) -> int:
    root = repo_root()
    needles = load_needles(root)
    if needles is None:
        print(f"check_ip_free: no {DENYLIST_NAME} found at {root} — IP guard inactive.")
        print(f"  create {DENYLIST_NAME} (one forbidden substring per line) to enable it.")
        return 0
    if not needles:
        return 0  # empty denylist = nothing to forbid

    targets = [Path(a) for a in argv] if argv else staged_files(root)
    failures: dict[str, list[tuple[int, str, str]]] = {}
    for path in targets:
        hits = scan(path, needles)
        if hits:
            rel = path.relative_to(root) if path.is_absolute() and root in path.parents else path
            failures[str(rel)] = hits

    if not failures:
        return 0

    print("✗ IP-free guard: proprietary name(s) found — this repo is public.\n")
    for fname, hits in failures.items():
        for lineno, needle, line in hits:
            print(f"  {fname}:{lineno}: matches '{needle}'  →  {line}")
    print(f"\nRemove them (or, if a name is a false positive, narrow {DENYLIST_NAME}).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
