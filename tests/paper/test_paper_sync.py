"""scripts/paper_sync.sh (the git side of .github/workflows/paper.yml) against local bare repositories.

No network and no ewbot: the recompute is a fake command that rewrites the three ledger files and
counts its calls (each real recompute may send Telegram messages, so the count is what matters),
another writer is a second clone pushing to the bare origin, and push failures are simulated with a
pre-push hook."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "paper_sync.sh"
LEDGER_FILES = {"ledger.json", "report.html", "runs.log"}


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    return tuple(int(x) for x in re.findall(r"\d+", out)[:2])


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or _git_version() < (2, 42), reason="needs bash and git >= 2.42 (worktree --orphan)"
)

FAKE_RECOMPUTE = r"""#!/usr/bin/env bash
# fake `ewbot paper run`: count the call, optionally let another writer push first, rewrite the ledger
set -euo pipefail
[ -z "${FAKE_FAIL:-}" ] || { echo "fake recompute failed" >&2; exit 7; }
echo call >> "$FAKE_CALLS"
n=$(wc -l < "$FAKE_CALLS" | tr -d ' ')
if [ -n "${FAKE_OTHER_WRITER:-}" ]; then bash "$OTHER_WRITER"; fi
printf '{"run": %s}\n' "$n" > "$PAPER_DIR/ledger.json"
echo "report $n" > "$PAPER_DIR/report.html"
echo "run $n" >> "$PAPER_DIR/runs.log"
"""

OTHER_WRITER = r"""#!/usr/bin/env bash
# another writer: one commit on top of origin's paper branch (an orphan one if it has none), pushed
# from a separate clone
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
d="$OTHER_CLONE"
if git -C "$d" ls-remote --exit-code origin refs/heads/paper >/dev/null; then
  git -C "$d" fetch -q origin +refs/heads/paper:refs/remotes/origin/paper
  git -C "$d" checkout -q -B paper origin/paper
elif [ "$(git -C "$d" branch --show-current)" != paper ]; then
  git -C "$d" switch -q --orphan paper
fi
echo "other $(date +%s) $RANDOM" >> "$d/runs.log"
echo '{"other": true}' > "$d/ledger.json"
echo "other report" > "$d/report.html"
git -C "$d" add ledger.json report.html runs.log
git -C "$d" commit -q -m "other writer"
git -C "$d" push -q origin HEAD:refs/heads/paper
"""

PRE_PUSH = r"""#!/usr/bin/env bash
# HOOK_MODE=fail: the first HOOK_FAILS pushes fail before anything is sent (a transient error);
# HOOK_MODE=land: push the commit with hooks off, then report a failure (the reply got lost)
n=$(( $(cat "$HOOK_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$HOOK_COUNT"
case "${HOOK_MODE:-}" in
  fail) [ "$n" -gt "$HOOK_FAILS" ] && exit 0; echo "simulated push failure $n" >&2; exit 1 ;;
  land) git -c core.hooksPath=/dev/null push -q "$1" HEAD:refs/heads/paper; echo "simulated lost reply" >&2; exit 1 ;;
esac
"""


class World:
    """A bare origin with master, the workflow's master checkout (`work`), the paper worktree path
    and another writer's clone."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin, self.work, self.other = tmp / "origin.git", tmp / "work", tmp / "other"
        self.paper = tmp / "runner-temp" / "paper"
        self.hooks = tmp / "hooks"
        self.hooks.mkdir()
        gitconfig = tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = master\n"
            "[advice]\n\tdetachedHead = false\n",
            encoding="utf-8",
        )
        self.fake = self._script("fake_recompute.sh", FAKE_RECOMPUTE)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_GLOBAL=str(gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            PAPER_DIR=str(self.paper),
            PAPER_PUSH_BACKOFF="0",
            FAKE_CALLS=str(tmp / "calls"),
            HOOK_COUNT=str(tmp / "hook-count"),
            OTHER_CLONE=str(self.other),
            OTHER_WRITER=str(self._script("other_writer.sh", OTHER_WRITER)),
        )
        self.git(tmp, "init", "-q", "--bare", str(self.origin))
        self.git(tmp, "init", "-q", str(self.work))
        (self.work / "README.md").write_text("code\n", encoding="utf-8")
        self.git(self.work, "add", "README.md")
        self.git(self.work, "commit", "-q", "-m", "code")
        self.git(self.work, "remote", "add", "origin", str(self.origin))
        self.git(self.work, "push", "-q", "origin", "master")
        self.git(self.work, "config", "core.hooksPath", str(self.hooks))  # shared with the paper worktree
        self.git(tmp, "clone", "-q", str(self.origin), str(self.other))

    def _script(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def git(self, repo: Path, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=self.env)
        if check:
            assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def sync(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=self.work, capture_output=True, text=True, env={**self.env, **env}
        )

    def push(self, **env: str) -> subprocess.CompletedProcess:
        return self.sync("push", "bash", str(self.fake), **env)

    def recompute(self) -> None:
        """The workflow's `Paper run` step."""
        subprocess.run(["bash", str(self.fake)], check=True, env=self.env)

    def other_writer(self) -> str:
        subprocess.run(["bash", self.env["OTHER_WRITER"]], check=True, env=self.env, capture_output=True)
        return self.origin_paper()

    def first_run(self, seeded: bool) -> str:
        """Checkout + paper run; returns origin's paper sha before the push step ("" on the first run)."""
        seed = self.other_writer() if seeded else ""
        r = self.sync("checkout")
        assert r.returncode == 0, r.stderr
        self.recompute()
        return seed

    def pre_push(self, mode: str, fails: int = 0) -> None:
        (self.hooks / "pre-push").write_text(PRE_PUSH, encoding="utf-8")
        (self.hooks / "pre-push").chmod(0o755)
        self.env.update(HOOK_MODE=mode, HOOK_FAILS=str(fails))

    def origin_paper(self) -> str:
        return self.git(self.origin, "rev-parse", "-q", "--verify", "refs/heads/paper", check=False)

    def head(self) -> str:
        return self.git(self.paper, "rev-parse", "HEAD")

    def calls(self) -> int:
        path = Path(self.env["FAKE_CALLS"])
        return len(path.read_text().splitlines()) if path.exists() else 0

    def hook_count(self) -> int:
        return int(Path(self.env["HOOK_COUNT"]).read_text())


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


SEEDED = pytest.mark.parametrize("seeded", [False, True], ids=["first-run", "existing-branch"])


# ----------------------------------------------------------------------------- checkout


def test_checkout_starts_an_orphan_paper_branch_when_origin_has_none(w):
    r = w.sync("checkout")
    assert r.returncode == 0, r.stderr
    assert w.git(w.paper, "symbolic-ref", "HEAD") == "refs/heads/paper"
    assert w.git(w.paper, "rev-parse", "-q", "--verify", "HEAD", check=False) == ""  # unborn
    assert [p.name for p in w.paper.iterdir()] == [".git"]  # none of master's files


def test_checkout_uses_origins_paper_branch(w):
    seed = w.other_writer()
    r = w.sync("checkout")
    assert r.returncode == 0, r.stderr
    assert w.head() == seed and w.git(w.paper, "symbolic-ref", "HEAD") == "refs/heads/paper"
    assert {p.name for p in w.paper.iterdir()} == LEDGER_FILES | {".git"}


def test_checkout_fails_instead_of_starting_an_orphan_when_origin_is_unreadable(w):
    w.other_writer()  # origin has a ledger, but this run cannot see it
    w.git(w.work, "remote", "set-url", "origin", str(w.tmp / "unreachable.git"))
    r = w.sync("checkout")
    assert r.returncode != 0 and "ls-remote" in r.stderr
    assert not w.paper.exists() and w.git(w.work, "branch", "--list", "paper") == ""


# ----------------------------------------------------------------------------- commit and push


@SEEDED
def test_push_commits_the_ledger_files_and_pushes_them(w, seeded):
    seed = w.first_run(seeded)
    r = w.push()
    assert r.returncode == 0, r.stderr
    assert w.origin_paper() == w.head()
    assert set(w.git(w.origin, "ls-tree", "--name-only", "paper").split()) == LEDGER_FILES
    assert w.git(w.paper, "rev-parse", "-q", "--verify", "HEAD^", check=False) == seed
    assert w.calls() == 1  # the push step did not recompute


def test_push_without_a_ledger_change_pushes_nothing(w):
    seed = w.other_writer()
    assert w.sync("checkout").returncode == 0
    r = w.push()
    assert r.returncode == 0, r.stderr
    assert "no ledger change" in r.stdout and w.origin_paper() == seed == w.head()


@pytest.mark.parametrize("breakage", ["missing-ledger-file", "commit-fails"])
def test_a_git_error_while_committing_fails_instead_of_reading_as_no_change(w, breakage):
    w.first_run(seeded=False)
    if breakage == "missing-ledger-file":
        (w.paper / "report.html").unlink()
    else:
        (w.hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (w.hooks / "pre-commit").chmod(0o755)
    r = w.push()
    assert r.returncode != 0 and "commit" in r.stderr
    assert "no ledger change" not in r.stdout and w.origin_paper() == ""


@SEEDED
def test_a_failed_push_to_an_unmoved_origin_is_retried_without_a_recompute(w, seeded):
    seed = w.first_run(seeded)
    w.pre_push("fail", fails=2)
    r = w.push()
    assert r.returncode == 0, r.stderr
    assert w.hook_count() == 3 and "did not move" in r.stdout
    assert w.calls() == 1  # no second recompute, so no Telegram message is sent twice
    # the same single commit on top of the seed, pushed on the third try
    assert w.origin_paper() == w.head() and w.git(w.paper, "rev-parse", "-q", "--verify", "HEAD^", check=False) == seed
    assert (w.paper / "runs.log").read_text().splitlines()[-1] == "run 1"


def test_a_push_that_landed_despite_an_error_is_done(w):
    w.first_run(seeded=True)
    w.pre_push("land")
    r = w.push()
    assert r.returncode == 0, r.stderr
    assert "landed" in r.stdout and w.hook_count() == 1 and w.calls() == 1
    assert w.origin_paper() == w.head()


@SEEDED
def test_a_moved_origin_is_recomputed_on_and_pushed_on_top(w, seeded):
    w.first_run(seeded)
    theirs = w.other_writer()  # someone else wrote to paper after this run started
    r = w.push()
    assert r.returncode == 0, r.stderr
    assert w.calls() == 2  # one recompute on top of their ledger
    assert w.origin_paper() == w.head() and w.git(w.paper, "rev-parse", "HEAD^") == theirs
    log = (w.paper / "runs.log").read_text().splitlines()
    assert log[-1] == "run 2" and log[-2].startswith("other ")  # their history kept, ours appended


def test_gives_up_after_three_pushes_to_an_unmoved_origin(w):
    seed = w.first_run(seeded=True)
    w.pre_push("fail", fails=99)
    r = w.push()
    assert r.returncode != 0 and "push failed 3 times" in r.stderr
    assert w.hook_count() == 3 and w.calls() == 1 and w.origin_paper() == seed


def test_recomputes_at_most_twice_and_never_after_the_last_push(w):
    w.first_run(seeded=True)
    w.other_writer()
    r = w.push(FAKE_OTHER_WRITER="1")  # origin moves again during every recompute
    assert r.returncode != 0 and "push failed 3 times" in r.stderr
    assert w.calls() == 3  # the Paper run step + 2 recomputes for 3 pushes
    assert w.origin_paper() != w.head()


def test_a_failing_recompute_fails_the_step(w):
    w.first_run(seeded=True)
    theirs = w.other_writer()
    r = w.push(FAKE_FAIL="1")
    assert r.returncode != 0 and w.origin_paper() == theirs


def test_usage_errors(w):
    assert w.sync().returncode != 0
    assert w.sync("push").returncode != 0  # no recompute command
    assert w.sync("checkout", PAPER_DIR="").returncode != 0
