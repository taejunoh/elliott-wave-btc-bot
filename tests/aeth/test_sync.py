"""ewbot.aeth.sync against local bare repositories (no network): a bare origin that already has strategy A's
`paper` and strategy B's `paper-b` branches, the workflow's master checkout, the paper-aeth worktree and another
writer."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from ewbot.aeth import sync
from ewbot.aeth.sync import BRANCH, SyncError, checkout, commit_ledger, push


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    return tuple(int(x) for x in re.findall(r"\d+", out)[:2])


pytestmark = pytest.mark.skipif(_git_version() < (2, 42), reason="needs git >= 2.42 (worktree --orphan)")


def g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = master\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    g(tmp_path, "init", "-q", "--bare", str(origin))
    g(tmp_path, "init", "-q", str(work))
    (work / "README.md").write_text("code\n")
    g(work, "add", "README.md")
    g(work, "commit", "-q", "-m", "code")
    g(work, "remote", "add", "origin", str(origin))
    g(work, "push", "-q", "origin", "master")
    for branch in ("paper", "paper-b"):  # strategy A's and strategy B's ledger branches exist on origin
        g(work, "switch", "-q", "--orphan", branch)
        (work / "ledger.json").write_text(f'{{"{branch}": 1}}\n')
        g(work, "add", "ledger.json")
        g(work, "commit", "-q", "-m", f"{branch} ledger")
        g(work, "push", "-q", "origin", branch)
    g(work, "switch", "-q", "master")
    g(tmp_path, "clone", "-q", str(origin), str(other))
    return {"origin": origin, "work": work, "other": other, "paper": tmp_path / "rt" / "paper-aeth", "tmp": tmp_path}


def _write(d, n):
    (d / "ledger.json").write_text(f'{{"run": {n}}}\n')
    with open(d / "runs.log", "a") as fh:
        fh.write(f"run {n}\n")


def _other_writer(w):
    d = w["other"]
    g(d, "fetch", "-q", "origin")
    g(d, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
    _write(d, 99)
    g(d, "add", "ledger.json", "runs.log")
    g(d, "commit", "-q", "-m", "other")
    g(d, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")


def test_checkout_starts_an_orphan_paper_aeth_then_tracks_it_and_leaves_paper_and_paper_b_alone(world):
    before = {b: g(world["origin"], "rev-parse", f"refs/heads/{b}") for b in ("paper", "paper-b")}
    assert BRANCH == "paper-aeth"
    checkout(world["work"], world["paper"])
    assert g(world["paper"], "branch", "--show-current") == BRANCH
    _write(world["paper"], 1)
    assert push(world["paper"], lambda: 0, backoff=0).startswith("pushed")
    files = g(world["origin"], "ls-tree", "--name-only", f"refs/heads/{BRANCH}").split()
    assert sorted(files) == ["ledger.json", "runs.log"]
    assert {b: g(world["origin"], "rev-parse", f"refs/heads/{b}") for b in ("paper", "paper-b")} == before
    g(world["work"], "worktree", "remove", "--force", str(world["paper"]))
    checkout(world["work"], world["paper"])
    assert (world["paper"] / "runs.log").read_text() == "run 1\n"


def test_an_unreadable_origin_never_starts_a_new_branch(world):
    g(world["work"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    with pytest.raises(SyncError, match="not starting a new paper-aeth"):
        checkout(world["work"], world["paper"])


def test_nothing_to_commit_is_a_success(world):
    checkout(world["work"], world["paper"])
    _write(world["paper"], 1)
    push(world["paper"], lambda: 0, backoff=0)
    assert push(world["paper"], lambda: 0, backoff=0) == "no ledger change: nothing to push"
    assert commit_ledger(world["paper"]) is False


def test_a_moved_origin_means_one_recompute_on_top_of_it(world):
    checkout(world["work"], world["paper"])
    _write(world["paper"], 1)
    push(world["paper"], lambda: 0, backoff=0)
    _write(world["paper"], 2)
    _other_writer(world)
    calls = []

    def recompute():
        calls.append(1)
        _write(world["paper"], 3)
        return 0

    assert push(world["paper"], recompute, backoff=0).startswith("pushed") and len(calls) == 1
    assert g(world["origin"], "show", f"refs/heads/{BRANCH}:runs.log").split("\n") == ["run 1", "run 99", "run 3"]


def test_failed_pushes_retry_the_same_commit_and_give_up_after_three(world):
    checkout(world["work"], world["paper"])
    hooks = world["tmp"] / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    (hooks / "pre-push").chmod(0o755)
    g(world["paper"], "config", "core.hooksPath", str(hooks))
    _write(world["paper"], 1)
    waits = []
    with pytest.raises(SyncError, match="push failed 3 times"):
        push(world["paper"], lambda: pytest.fail("no recompute when origin did not move"), backoff=2.0,
             sleep=waits.append)
    assert waits == [2.0, 4.0]


def test_main_needs_paper_aeth_dir_and_a_known_command(world, monkeypatch, capsys):
    monkeypatch.delenv("PAPER_AETH_DIR", raising=False)
    assert sync.main(["checkout"]) == 1 and "PAPER_AETH_DIR is not set" in capsys.readouterr().err
    monkeypatch.setenv("PAPER_AETH_DIR", str(world["paper"]))
    monkeypatch.chdir(world["work"])
    assert sync.main(["checkout"]) == 0
    _write(world["paper"], 1)
    monkeypatch.setenv("PAPER_AETH_PUSH_BACKOFF", "0")
    assert sync.main(["push", sys.executable, "-c", "pass"]) == 0
    assert sync.main(["status"]) == 1
