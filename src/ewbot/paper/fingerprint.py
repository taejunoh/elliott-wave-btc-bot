"""Strategy fingerprint (Plan 2 spec 5.1): sha256 over the source files that decide trades.

Included (paths relative to src/ewbot): types.py, config.py, indicators.py, data/candles.py and every
*.py below waves/, strategy/, engine/, execution/, risk/. Everything else (data/coinbase_history.py,
backtest/, cli.py, paper/, __init__.py at the package root) is excluded. Each file contributes
<repo-relative path> NUL <bytes> NUL, in sorted path order; any byte change (comments, whitespace)
changes the value - the strict policy of the spec.

The baseline is the same file set at the holdout commit 64e628a, read from git (`git cat-file blob`,
i.e. the plain `git show <commit>:<path>` bytes)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # src/ewbot/paper/fingerprint.py -> repository root
PKG_PREFIX = "src/ewbot/"
HOLDOUT_COMMIT = "64e628ad4865b3b04571a7cba2886881eae19176"  # chore(holdout): record holdout run d1be04baa06c
INCLUDED_FILES = frozenset({"types.py", "config.py", "indicators.py", "data/candles.py"})
INCLUDED_DIRS = frozenset({"waves", "strategy", "engine", "execution", "risk"})


class FingerprintError(RuntimeError):
    """The fingerprint baseline cannot be computed (git missing or the commit is not in the history)."""


def is_fingerprinted(rel: str) -> bool:
    """rel: posix path relative to src/ewbot (e.g. "engine/loop.py")."""
    if not rel.endswith(".py"):
        return False
    if rel in INCLUDED_FILES:
        return True
    head, sep, _ = rel.partition("/")
    return bool(sep) and head in INCLUDED_DIRS


def fingerprint_of(files: Mapping[str, bytes]) -> str:
    """sha256( for path in sorted(paths): path + NUL + bytes + NUL ), independent of mapping order."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8") + b"\0" + files[path] + b"\0")
    return h.hexdigest()


def tree_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/<rel>": bytes} for every fingerprinted file in the working tree."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot"
    out: dict[str, bytes] = {}
    for f in pkg.rglob("*.py"):
        rel = f.relative_to(pkg).as_posix()
        if f.is_file() and is_fingerprinted(rel):
            out[PKG_PREFIX + rel] = f.read_bytes()
    return out


def current_fingerprint(repo_root: Path | None = None) -> str:
    return fingerprint_of(tree_files(repo_root))


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        r = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FingerprintError(f"git {' '.join(args)} failed: {exc}") from exc
    if r.returncode != 0:
        raise FingerprintError(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def git_files(commit: str, repo_root: Path | None = None) -> tuple[str, dict[str, bytes]]:
    """(full commit sha, {"src/ewbot/<rel>": bytes}) of the fingerprinted files at `commit`."""
    root = PROJECT_ROOT if repo_root is None else Path(repo_root)
    sha = _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    names = _git(root, "ls-tree", "-r", "--name-only", sha, "--", "src/ewbot").decode("utf-8").splitlines()
    out: dict[str, bytes] = {}
    for path in names:
        if path.startswith(PKG_PREFIX) and is_fingerprinted(path[len(PKG_PREFIX):]):
            out[path] = _git(root, "cat-file", "blob", f"{sha}:{path}")
    return sha, out


def baseline_fingerprint(repo_root: Path | None = None, commit: str = HOLDOUT_COMMIT) -> tuple[str, str]:
    """(fingerprint, full commit sha) of the fingerprinted file set at the holdout commit."""
    sha, files = git_files(commit, repo_root)
    if not files:
        raise FingerprintError(f"no fingerprinted files under src/ewbot at {sha}")
    return fingerprint_of(files), sha
