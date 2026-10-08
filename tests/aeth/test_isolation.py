"""Strategy-A protection (Plan 5a spec 3, 11): A-ETH must not change anything strategy A's paper, live or go-live
count depends on, nor strategy B's ended code. These run in the full suite; tests/paper/test_static.py (run by
ci.yml) carries the CI part."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import ewbot
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, is_fingerprinted, tree_files

SRC = Path(ewbot.__file__).resolve().parent
FROZEN_A = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
# Strategy A's files (fingerprinted code, research code, runtimes, lock, workflows, scripts, lockfiles) and strategy
# B's (ended, kept as a record).
PROTECTED = (
    "src/ewbot/types.py", "src/ewbot/config.py", "src/ewbot/indicators.py", "src/ewbot/data/",
    "src/ewbot/waves/", "src/ewbot/strategy/", "src/ewbot/engine/", "src/ewbot/execution/", "src/ewbot/risk/",
    "src/ewbot/backtest/", "src/ewbot/paper/", "src/ewbot/live/", "src/ewbot/stratb/", "src/ewbot/paperb/",
    "holdout.lock", ".github/workflows/paper.yml", ".github/workflows/ci.yml", ".github/workflows/paper-b.yml",
    "scripts/", "ops/", "deploy/", "uv.lock", "pyproject.toml",
)
AETH_PATHS = ("src/ewbot/aeth/", "tests/aeth/", ".github/workflows/paper-aeth.yml", "docs/aeth.md",
              "holdout-aeth.lock", "aeth-stage1.json")
UNLOCK_TRUE = {"aeth/holdout.py", "aeth/paper.py"}  # the only callers that may read 2024+ ETH candles
# Runtime artifacts of strategy A and strategy B that A-ETH code must never name.
FOREIGN = {"paper.yml", "paper-b.yml", "holdout.lock", "holdout-b.lock", "refs/heads/paper", "origin/paper",
           "paper-b", "BTC-USD_1h.parquet", "ETH-USD_15m.parquet", "btc-usd-1h-", "stratb-15m-"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules(folder: str | None = None):
    root = SRC if folder is None else SRC / folder
    for f in sorted(root.rglob("*.py")) if root.is_dir() else []:
        yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def test_strategy_a_fingerprint_is_unchanged():
    assert current_fingerprint() == FROZEN_A


def test_aeth_code_is_outside_strategy_a_fingerprinted_folders():
    assert not is_fingerprinted("aeth/x.py") and not is_fingerprinted("aeth/sub/y.py")
    assert [rel for rel, _ in _modules("aeth")]
    assert not [p for p in tree_files() if p.startswith("src/ewbot/aeth/")]


def test_aeth_work_changes_no_protected_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches A-ETH
    touches no strategy-A or strategy-B path. A branch that does not touch A-ETH is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(AETH_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


def test_only_the_aeth_holdout_and_paper_runtime_unlock_2024_candles():
    literal_true, other = set(), set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "unlock_2024":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            literal_true.add(rel)
                        elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                            other.add(rel)
    assert literal_true <= UNLOCK_TRUE
    assert other == set()


def test_aeth_code_never_names_strategy_a_or_b_runtime_artifacts():
    for rel, tree in _modules("aeth"):
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not names & FOREIGN, rel


def test_aeth_never_imports_strategy_b():
    for rel, tree in _modules("aeth"):
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in mods if m.startswith(("ewbot.stratb", "ewbot.paperb"))], rel
