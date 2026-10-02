from __future__ import annotations

import shutil
import subprocess

import pytest

from ewbot.paper import fingerprint as fpm
from ewbot.paper.fingerprint import (
    HOLDOUT_COMMIT,
    PROJECT_ROOT,
    FingerprintError,
    baseline_fingerprint,
    current_fingerprint,
    fingerprint_of,
    is_fingerprinted,
    tree_files,
)

EXPECTED = {
    "src/ewbot/types.py",
    "src/ewbot/config.py",
    "src/ewbot/indicators.py",
    "src/ewbot/data/candles.py",
    "src/ewbot/waves/__init__.py",
    "src/ewbot/waves/engine.py",
    "src/ewbot/waves/enumerate.py",
    "src/ewbot/waves/patterns.py",
    "src/ewbot/waves/scoring.py",
    "src/ewbot/waves/zigzag.py",
    "src/ewbot/strategy/__init__.py",
    "src/ewbot/strategy/base.py",
    "src/ewbot/strategy/choch.py",
    "src/ewbot/strategy/elliott.py",
    "src/ewbot/strategy/fib_swing.py",
    "src/ewbot/strategy/hybrid.py",
    "src/ewbot/engine/__init__.py",
    "src/ewbot/engine/loop.py",
    "src/ewbot/engine/position_mgr.py",
    "src/ewbot/execution/__init__.py",
    "src/ewbot/execution/costs.py",
    "src/ewbot/execution/paper.py",
    "src/ewbot/risk/__init__.py",
    "src/ewbot/risk/breakers.py",
    "src/ewbot/risk/sizing.py",
}


def _has_commit(commit: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True, timeout=30
        )
    except OSError:
        return False
    return r.returncode == 0


def test_the_file_set_is_exactly_the_spec_list():
    assert set(tree_files()) == EXPECTED


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("types.py", True),
        ("data/candles.py", True),
        ("engine/loop.py", True),
        ("waves/sub/new.py", True),
        ("data/coinbase_history.py", False),
        ("data/__init__.py", False),
        ("backtest/runner.py", False),
        ("paper/runner.py", False),
        ("cli.py", False),
        ("__init__.py", False),
        ("engine/notes.txt", False),
        ("engine.py", False),
    ],
)
def test_inclusion_rules(rel, expected):
    assert is_fingerprinted(rel) is expected


def test_value_is_independent_of_file_order():
    a = {"src/ewbot/b.py": b"2", "src/ewbot/a.py": b"1"}
    b = {"src/ewbot/a.py": b"1", "src/ewbot/b.py": b"2"}
    assert fingerprint_of(a) == fingerprint_of(b)
    assert fingerprint_of(a) != fingerprint_of({"src/ewbot/a.py": b"2", "src/ewbot/b.py": b"1"})


def test_one_comment_line_changes_it_and_excluded_files_do_not(tmp_path):
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot")
    base = current_fingerprint(tmp_path)
    assert base == current_fingerprint(PROJECT_ROOT)
    (tmp_path / "src/ewbot/backtest/runner.py").write_text("# report tweak\n", encoding="utf-8")
    (tmp_path / "src/ewbot/cli.py").write_text("# cli tweak\n", encoding="utf-8")
    assert current_fingerprint(tmp_path) == base
    loop = tmp_path / "src/ewbot/engine/loop.py"
    loop.write_bytes(loop.read_bytes() + b"# one comment line\n")
    assert current_fingerprint(tmp_path) != base


@pytest.mark.skipif(not _has_commit(HOLDOUT_COMMIT), reason="holdout commit 64e628a not in this clone's history")
def test_baseline_recomputes_from_git_and_equals_the_working_tree():
    """Tripwire: if this fails, a fingerprinted file changed after the holdout and the paper go-live
    counter will reset (new epoch) on the next `ewbot paper run`."""
    fp, sha = baseline_fingerprint()
    assert sha == HOLDOUT_COMMIT and len(fp) == 64
    assert fp == current_fingerprint()


def test_baseline_needs_git_history(tmp_path):
    with pytest.raises(FingerprintError):
        baseline_fingerprint(tmp_path, "0" * 40)


def test_git_files_reads_committed_bytes(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src/ewbot/engine").mkdir(parents=True)
    (repo / "src/ewbot/engine/loop.py").write_bytes(b"x = 1\n")
    (repo / "src/ewbot/cli.py").write_bytes(b"y = 1\n")
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c1"], check=True
    )
    (repo / "src/ewbot/engine/loop.py").write_bytes(b"x = 2\n")  # uncommitted edit: not the baseline
    fp, sha = baseline_fingerprint(repo, "HEAD")
    assert fp == fingerprint_of({"src/ewbot/engine/loop.py": b"x = 1\n"})
    assert len(sha) == 40 and fp != fpm.current_fingerprint(repo)
