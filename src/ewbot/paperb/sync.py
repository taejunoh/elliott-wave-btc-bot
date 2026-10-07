"""Git side of .github/workflows/paper-b.yml (Plan 4 spec 7): strategy A's scripts/paper_sync.sh rules
for the orphan branch `paper-b` (A's script and its `paper` branch are never touched). Run from the master
checkout:

  python -m ewbot.paperb.sync checkout
      worktree of origin's paper-b at $PAPER_B_DIR (shallow), or an orphan paper-b when origin has none
      (ls-remote exit 2); any other ls-remote failure fails: a network or auth error must not start a
      fresh ledger.
  python -m ewbot.paperb.sync push RECOMPUTE...
      commit ledger.json and runs.log in $PAPER_B_DIR and push to origin's paper-b, 3 pushes at most.
      After a failed push: origin unmoved or unreadable -> the same commit again after a wait (no
      recompute, so no Telegram message twice); origin already at our commit -> done; origin moved ->
      reset to it, run RECOMPUTE (`uv run ewbot stratb paper run ...`), commit and push on top. Never
      recomputes after the last push; fails when every push failed.
Environment: PAPER_B_DIR (required); PAPER_B_PUSH_BACKOFF = seconds, retry n waits n times it (default 5)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

BRANCH = "paper-b"
LEDGER_FILES = ("ledger.json", "runs.log")
ATTEMPTS = 3


class SyncError(RuntimeError):
    """A git step of the paper-B sync failed."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def origin_branch(cwd: Path) -> str:
    """The sha of origin's paper-b, "" when origin has none; SyncError on any other ls-remote failure."""
    r = _git(cwd, "ls-remote", "--exit-code", "origin", f"refs/heads/{BRANCH}", check=False)
    if r.returncode == 0:
        return r.stdout.split()[0]
    if r.returncode == 2:
        return ""
    raise SyncError(f"git ls-remote origin failed (exit {r.returncode}): not starting a new {BRANCH} branch")


def checkout(repo: Path, paper_dir: Path) -> None:
    if origin_branch(repo):
        _git(repo, "fetch", "--no-tags", "--depth=1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        _git(repo, "worktree", "add", "-B", BRANCH, str(paper_dir), f"origin/{BRANCH}")
    else:
        print(f"origin has no {BRANCH} branch: starting an orphan one")
        _git(repo, "worktree", "add", "--orphan", "-b", BRANCH, str(paper_dir))


def commit_ledger(paper_dir: Path) -> bool:
    """Commit the ledger files; False when nothing changed."""
    _git(paper_dir, "add", "--", *[f for f in LEDGER_FILES if (paper_dir / f).exists()])
    if _git(paper_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(paper_dir, "commit", "-q", "-m", f"paper-b: run {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}")
    return True


def push(
    paper_dir: Path,
    recompute: Callable[[], int],
    *,
    attempts: int = ATTEMPTS,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    if not commit_ledger(paper_dir):
        return "no ledger change: nothing to push"
    for attempt in range(1, attempts + 1):
        if _git(paper_dir, "push", "origin", f"HEAD:refs/heads/{BRANCH}", check=False).returncode == 0:
            return f"pushed {_git(paper_dir, 'rev-parse', '--short', 'HEAD').stdout.strip()} (attempt {attempt})"
        head = _git(paper_dir, "rev-parse", "HEAD").stdout.strip()
        parent = _git(paper_dir, "rev-parse", "-q", "--verify", "HEAD^", check=False).stdout.strip()
        try:
            remote: str | None = origin_branch(paper_dir)
        except SyncError:
            remote = None
        if remote == head:
            return f"origin/{BRANCH} is at our commit: the push landed despite the error"
        if attempt >= attempts:
            raise SyncError(f"push failed {attempts} times")
        if remote is None or remote == parent:
            sleep(backoff * attempt)
            continue
        if not remote:
            raise SyncError(f"origin's {BRANCH} branch disappeared")
        print(f"push rejected (attempt {attempt}): origin/{BRANCH} moved to {remote[:12]}, recompute on top of it")
        _git(paper_dir, "fetch", "--no-tags", "--depth=1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        _git(paper_dir, "reset", "-q", "--hard", f"origin/{BRANCH}")
        rc = recompute()
        if rc != 0:
            raise SyncError(f"recompute failed (exit {rc})")
        if not commit_ledger(paper_dir):
            return "no ledger change after the recompute: nothing to push"
    raise SyncError(f"push failed {attempts} times")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paper_dir = os.environ.get("PAPER_B_DIR", "")
    try:
        if not paper_dir:
            raise SyncError("PAPER_B_DIR is not set")
        if args == ["checkout"]:
            checkout(Path.cwd(), Path(paper_dir))
            return 0
        if len(args) > 1 and args[0] == "push":
            backoff = float(os.environ.get("PAPER_B_PUSH_BACKOFF", "5"))
            print(push(Path(paper_dir), lambda: subprocess.run(args[1:]).returncode, backoff=backoff))
            return 0
    except SyncError as exc:
        print(f"paper-b sync: {exc}", file=sys.stderr)
        return 1
    print("usage: python -m ewbot.paperb.sync checkout | push RECOMPUTE...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
