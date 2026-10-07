"""Strategy-A protection (Plan 4 spec 2, 9): B must not change anything A's paper, live or go-live
count depends on. These run in the full suite; tests/paper/test_static.py (run by ci.yml) carries the
CI part (Task 13)."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import ewbot
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, is_fingerprinted, tree_files

SRC = Path(ewbot.__file__).resolve().parent
FROZEN_A = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
B_FOLDERS = ("stratb", "paperb")
# Strategy A's files: fingerprinted code, research code, runtimes, lock, workflows, sync script, lockfiles.
PROTECTED = (
    "src/ewbot/types.py", "src/ewbot/config.py", "src/ewbot/indicators.py", "src/ewbot/data/",
    "src/ewbot/waves/", "src/ewbot/strategy/", "src/ewbot/engine/", "src/ewbot/execution/", "src/ewbot/risk/",
    "src/ewbot/backtest/", "src/ewbot/paper/", "src/ewbot/live/", "holdout.lock",
    ".github/workflows/paper.yml", ".github/workflows/ci.yml", "scripts/", "uv.lock", "pyproject.toml",
)
B_PATHS = (
    "src/ewbot/stratb/", "src/ewbot/paperb/", "tests/stratb/", "tests/paperb/", ".github/workflows/paper-b.yml",
    "docs/strategy-b.md", "holdout-b.lock",
)
INCLUDE_TRUE = {"stratb/holdout.py", "paperb/runner.py"}  # the only callers that may read 2024+ candles
INCLUDE_PASS_THROUGH = {"stratb/research.py"}  # backtest_b forwards its own include_holdout argument
A_ARTIFACTS = {"paper.yml", "holdout.lock", "refs/heads/paper", "origin/paper", "BTC-USD_1h.parquet"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules(folders: tuple[str, ...] | None = None):
    roots = [SRC] if folders is None else [SRC / f for f in folders if (SRC / f).is_dir()]
    for root in roots:
        for f in sorted(root.rglob("*.py")):
            yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def test_strategy_a_fingerprint_is_unchanged():
    assert current_fingerprint() == FROZEN_A


def test_strategy_b_code_is_outside_strategy_a_fingerprinted_folders():
    for folder in B_FOLDERS:
        assert not is_fingerprinted(f"{folder}/x.py") and not is_fingerprinted(f"{folder}/sub/y.py")
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/"))]


def test_strategy_b_work_changes_no_strategy_a_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches
    strategy B touches no strategy-A path. A branch of strategy-A maintenance alone is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(B_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


def test_only_the_b_holdout_and_the_paper_b_runner_unlock_2024_candles():
    literal_true, other = set(), set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "include_holdout":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            literal_true.add(rel)
                        elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                            other.add(rel)
    assert literal_true <= INCLUDE_TRUE
    assert other <= INCLUDE_PASS_THROUGH


def test_strategy_b_code_never_names_strategy_a_runtime_artifacts():
    for rel, tree in _modules(B_FOLDERS):
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not names & A_ARTIFACTS, rel
