# ewbot Plan 2 — Paper-Trading Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the frozen holdout strategy (A with K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5 from `holdout.lock`) as an hourly, deterministic paper-trading runtime on GitHub Actions that keeps accumulating out-of-sample trades toward the 50-trade go-live minimum, proves for at least 4 weeks that the decisions recorded in real time equal the decisions recomputed later, and reports through a ledger and HTML report on an orphan `paper` branch and through Telegram.

**Architecture:** Every run recomputes the whole engine from the first cached bar with `run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)` (spec approach A), inside a read-only trace that records fills, stop moves and order sizes. Trades entering after the holdout's last bar are the paper trades. A JSON ledger stores epochs (strategy fingerprint), trades, decisions (with run id, the hash of the bars they depended on and the recording run's runtime-environment digest), discrepancies, runs (each with its runtime environment) and Telegram delivery state; every file write is temp file + `os.replace`. No strategy-fingerprinted module is edited: new code lives in `src/ewbot/paper/`, plus a `paper` sub-command in `cli.py`, `.github/workflows/paper.yml` with its git helper `scripts/paper_sync.sh`, the read-only `.github/workflows/ci.yml` fingerprint guard on pull requests and `master` pushes, and an ops guide.

**Tech Stack:** Python 3.12 (uv), numpy, pandas, pyarrow, requests, plotly (existing dependencies only); pytest; GitHub Actions (`actions/checkout@v7`, `astral-sh/setup-uv` pinned to the v10.2.0 commit `c18668ad3cf93ea998bef934396af7bb5c839dc7`, `actions/cache@v6`); Telegram Bot API `sendMessage`.

**Spec:** `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md` (parent: `docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md`).

## Global Constraints

- Work on branch `feat/plan2-paper`, from the repository root; Python 3.12 via uv (the workflows pin the exact patch `3.12.13`); tests with `uv run pytest`. No new dependencies (`pyproject.toml` and `uv.lock` unchanged).
- Never modify a strategy-fingerprinted file (spec §5.1): `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. Do not edit `backtest/**`, `data/coinbase_history.py` or `holdout.lock` either. Allowed: create `src/ewbot/paper/*`, `tests/paper/*`, `.github/workflows/paper.yml`, `.github/workflows/ci.yml`, `scripts/paper_sync.sh`, `docs/paper-trading.md`; modify `src/ewbot/cli.py`.
- Fingerprint = `sha256( for path in sorted(paths): path + "\0" + bytes + "\0" )` over the repo-relative paths (`src/ewbot/<path>`) of that file set (25 files today). Baseline = the same set at holdout commit `64e628ad4865b3b04571a7cba2886881eae19176` (`64e628a`), read with `git cat-file blob` (the plain `git show 64e628a:<path>` bytes). Today both are `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`.
- Frozen inputs: params only from `holdout.lock` (`[2.0, 0.7, 0.5]`); config = `holdout_config(BotConfig(), params)` (strategy A, conservative 1% / 3x, breakers on, fee 0.05%, slippage 0.05%, funding 10% APR long); recompute with `start_ts = HOLDOUT_START = 1704067200`; no CLI option changes any of it. The precheck refuses a lock whose `sha256` is not `lock_digest(BotConfig(), params, random_runs, code_sha256)` (true for the committed lock: `d1be04baa06c…`).
- Holdout boundary: `last_bar_ts = 1790938800` (2026-10-02 11:00 UTC); paper trades have `entry_ts > last_bar_ts`; holdout `n_trades.A = 36`. A later epoch counts `entry_ts > epoch.started_ts`.
- Go-live count (criterion 3) = holdout 36 (only while the ledger never left epoch 1) + closed paper trades of the current epoch; required 50; eligible only while the current epoch's fingerprint equals the baseline.
- Decisions: `SIGNAL, ENTRY, TP1, TP2, STOP, EXIT, BREAKEVEN, TRAIL` as `(kind, ts, price, contracts, reason)` + `trade_key, levels, setup, direction, run_id, data_sha256, epoch_id, env_sha256`; id `kind:ts:trade_key`; prices and levels compared with relative tolerance `1e-9`; causes `DATA_REVISION` > `FINGERPRINT_CHANGE` > `ENVIRONMENT_CHANGE` > `UNEXPLAINED`.
- Criterion 4: at least 28 days since the epoch's first successful run, 0 `UNEXPLAINED` discrepancies in the epoch (`ENVIRONMENT_CHANGE` ones are warned and shown, not counted), at least 95% of hours with a successful run.
- Telegram: secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; without them nothing is sent and the run succeeds; each event id `kind:ts:trade_key` is sent at most once per pushed ledger (a run killed after sending, or whose pushes all fail, may send it again in the next run; never lost); 3 tries, then the event stays pending for the next run; an error note keeps a `SendError`'s message and only the type name of any other exception. Daily summary: first run at/after 09:00 America/New_York per New York date; criteria 1–2 (`compare`, RANDOM 100 runs, injectable) are recomputed only in that run; a failure is retried by the next run once (`CRITERIA_ATTEMPTS = 2` per New York date), then the summary goes out marked `기준 미갱신(stale)` with the previous result. Gap warning: more than 3 h since the last successful run.
- Workflow: `cron: "52 */3 * * *"` (sparse backup; the primary hourly trigger is an external Cloudflare Workers cron calling `workflow_dispatch` at :37, `ops/cloudflare-trigger/worker.js`, added 2026-10-03) + `workflow_dispatch` (no inputs); `runs-on: ubuntu-24.04` (pinned, part of the recorded environment); `concurrency: {group: paper, cancel-in-progress: false}`; `permissions: contents: write`; `paper` branch worktree created as an orphan on first use; every fetch shallow (master, the `64e628a` commit and `paper` at depth 1); orphan only when `ls-remote` exits 2, any other `ls-remote` failure fails the job; 3 pushes at most: the same commit again when `origin/paper` did not move (no recompute, no repeated Telegram message), refresh + recompute only when it moved, never after the last push (`scripts/paper_sync.sh`); 35-minute step timeouts on the `ewbot` steps; the fallback alert's `curl --retry 3 --max-time 20` fits its 2-minute step timeout; Telegram secrets only on the steps that send. `.github/workflows/ci.yml`: on `pull_request` and `push` to `master`, `permissions: contents: read`, no secrets, `ubuntu-24.04`, `uv sync --frozen`, then `uv run pytest -q tests/paper/test_fingerprint.py tests/paper/test_static.py` (the fingerprint test pins the baseline value, so it never skips without git history).
- Tests never touch the network, the real `data/cache`, or `<repo>/holdout.lock`: synthetic candles (`tests/synth.random_bars`), temp git repos holding test locks, a fake sender and a fake fetcher. Never compute metrics on real data at/after 2024-01-01 except in the manual pre-deploy step (Task 11).
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Spec Interpretations (decided here, binding for the tasks)

1. **Recompute window.** The engine runs from the first cached bar (2017-01-01, warm-up) with `start_ts = HOLDOUT_START`, exactly like the holdout's A run. The paper recompute therefore *continues* the holdout run: trades with `HOLDOUT_START <= entry_ts <= last_bar_ts` must number 36 again (recorded per run as `holdout_trades_recomputed`, checked by hand in Task 11).
2. **Stop moves, TP1 partials and order sizes** are not in `EngineResult`. `paper/trace.py` swaps the names `PaperBroker` / `WaveEngine` that `ewbot.engine.loop` looks up at run time for pass-through subclasses that only log (no file is edited; a test proves the traced run is bit-identical to `run_backtest`).
3. **When a decision is known.** A run whose last closed bar is T knows every fill and stop move with `ts <= T` and every signal with `ts <= T - 1h` (the engine never evaluates entries on the last bar); the END close of a still-open position is not a decision. Decisions are recorded by the first run that knows them (`known_at > epoch.through_ts`).
4. **Epochs.** `started_ts` of epoch 1 is the holdout's last bar; a new epoch starts at the last bar of the run that saw the new fingerprint. Earlier epochs' trades are frozen; the current epoch's closed trades are recomputed every run. Reverting the code opens a new epoch with the baseline fingerprint: eligible again, but the holdout's 36 never count again (spec §8).
5. **Discrepancies** are `CHANGED` / `VANISHED` (a recorded decision differs / is gone) and `APPEARED` (the current epoch recomputes a decision an earlier run should have recorded). `DATA_REVISION` = the hash of all bars with `ts <=` the decision's ts changed (for `APPEARED`: the hash of the bars seen by the first run that should have known it). `ENVIRONMENT_CHANGE` (spec §5.3 as amended 2026-10-02) = the run that recorded the decision (for `APPEARED`: the first run that should have known it) has another runtime-environment digest than the current run, and so has the previous successful run (the current run is the first in its environment: every run rechecks every recorded decision, so once a run in the new environment has reproduced a decision, a later difference is `UNEXPLAINED`); the environment (`paper/environment.py`) is the Python version, `platform.system()`, `platform.machine()`, the numpy, pandas and pyarrow versions, the sha256 of `uv.lock` and `ImageOS` (None off Actions), all compared; `ImageVersion` is recorded in every run but excluded from the digest (GitHub refreshes the image weekly, comparing it would mask real `UNEXPLAINED` discrepancies); and an unknown recording or previous environment never counts as a change. Each discrepancy is stored and alerted once.
6. **Telegram without secrets** drops events instead of queueing them (a token added later must not flood the chat). A failed send keeps that event and every later one pending, in order.
7. **Criteria 1–2** = `backtest.holdout.evaluate_criteria(compare(cfg, df, HOLDOUT_START, last_bar + 1h, random_runs, workers, allow_holdout=True))`, i.e. over `[HOLDOUT_START, last bar + 1h)`, only while the current epoch is eligible; the stored result is ignored once the epoch changes. The go-live candidate is A, the only strategy the runtime trades and counts (criteria 3–4 are about A's trades), so — like `evaluate_criteria`'s `passing` — one candidate must meet all four: criterion 1 = `"A" in winners`, criterion 2 = criterion 1 and A's max drawdown <= 15%. HYBRID is still compared and shown. A failed recompute is tried at most `CRITERIA_ATTEMPTS = 2` times per New York date (the daily run and the next run, counted from `runs[].criteria_recompute`); after the second failure the daily summary is sent marked `기준 미갱신(stale)` with the previous result and `daily_summary_date` is set, so the next try is on the next New York date.
8. **Failure alerts** are sent by `ewbot paper run` (stage + `EWBOT_RUN_URL`); it touches `EWBOT_FAILURE_MARKER`, and the workflow's `if: failure() || cancelled()` step only sends its own alert when that marker is missing (checkout / setup / sync / push failures, step timeouts, cancellations).
9. **Ledger fields beyond the spec list:** `pending_events`, `daily_summary_date`, `criteria`, `through_ts` in each epoch, `env` / `env_sha256` / `criteria_recompute` in each run and `env_sha256` in each decision (no schema-version change: no `paper` ledger existed yet).
10. **Lock binding.** Besides the git checks, the grid combo and the holdout start, the precheck requires `lock["sha256"] == lock_digest(BotConfig(), params, int(lock["random_runs"]), lock["code_sha256"])`: the whole configuration the run uses (BotConfig defaults with the lock's params), the grid and the holdout start must be the ones the holdout ran with. A changed `BotConfig` default or grid therefore stops the runs at `precheck` instead of opening a new epoch.

## File Map

| File | Task | Responsibility |
|---|---|---|
| `src/ewbot/paper/__init__.py` | 1 | package marker |
| `src/ewbot/paper/fingerprint.py` | 1 | strategy fingerprint, git baseline at `64e628a` |
| `src/ewbot/paper/ledger.py` | 2 | ledger dataclass, atomic IO, epochs, trade records, go-live counter |
| `src/ewbot/paper/trace.py` | 3 | read-only trace of fills / stop moves / orders / last WaveState |
| `src/ewbot/paper/decisions.py` | 4 | out-of-sample split, decisions, replay check, open position, top count |
| `src/ewbot/paper/notify.py` | 5 | Telegram sender, delivery with retry, message formats, timing, events |
| `src/ewbot/paper/criteria.py` | 6 | criteria 1–4 status block |
| `src/ewbot/paper/report.py` | 6 | `report.html` on top of `backtest.report.write_report` |
| `src/ewbot/paper/environment.py` | 7 | runtime environment record and digest (`ENVIRONMENT_CHANGE`) |
| `src/ewbot/paper/runner.py` | 7 | one `paper run`: precheck, data, epoch, recompute, ledger, notify |
| `src/ewbot/cli.py` (modify) | 8 | `ewbot paper run` and `ewbot paper status` |
| `tests/paper/helpers.py` | 3 | synthetic candles, test lock in a temp git repo, fakes |
| `tests/paper/test_*.py` | 1–10 | unit, integration (time walk) and static tests |
| `.github/workflows/paper.yml` | 10 | hourly Actions workflow |
| `.github/workflows/ci.yml` | 10 | fingerprint and static guards on pull requests and `master` pushes |
| `scripts/paper_sync.sh` | 10 | the workflow's git side: paper worktree, commit, push and retry |
| `docs/paper-trading.md` | 10 | ops guide, Telegram setup for the user |

## Task Overview

| Task | Component | New tests |
|---|---|---|
| 1 | Strategy fingerprint | 19 |
| 2 | Ledger model, atomic IO, epochs | 9 |
| 3 | Engine trace + shared test helpers | 6 |
| 4 | Out-of-sample extraction, decisions, replay check | 14 |
| 5 | Telegram notifier, formats, timing, events | 27 |
| 6 | Go-live criteria + paper HTML report | 7 |
| 7 | Runtime environment + paper runner orchestration | 39 |
| 8 | CLI `paper run` / `paper status` | 6 |
| 9 | Time-walk integration test | 8 |
| 10 | Static `allow_holdout` guard, paper and CI workflows, git sync script, ops guide | 42 |
| 11 | Manual pre-deploy checklist (controller) | — |

Baseline before Task 1: `uv run pytest -q` → `443 passed, 1 skipped` without a candle cache (`444 passed` when `data/cache/BTC-USD_1h.parquet` exists and the real-data smoke test runs). After Task 10: 177 more tests.

---

### Task 1: Strategy fingerprint

**Files:**
- Create: `src/ewbot/paper/__init__.py`, `src/ewbot/paper/fingerprint.py`
- Create: `tests/paper/__init__.py`
- Test: `tests/paper/test_fingerprint.py`

**Interfaces:**
- Consumes: the `git` CLI only.
- Produces (`ewbot.paper.fingerprint`): `PROJECT_ROOT: Path`, `PKG_PREFIX = "src/ewbot/"`, `HOLDOUT_COMMIT = "64e628ad4865b3b04571a7cba2886881eae19176"`, `INCLUDED_FILES`, `INCLUDED_DIRS`, `class FingerprintError(RuntimeError)`, `is_fingerprinted(rel: str) -> bool`, `fingerprint_of(files: Mapping[str, bytes]) -> str`, `tree_files(repo_root: Path | None = None) -> dict[str, bytes]`, `current_fingerprint(repo_root: Path | None = None) -> str`, `git_files(commit: str, repo_root: Path | None = None) -> tuple[str, dict[str, bytes]]`, `baseline_fingerprint(repo_root: Path | None = None, commit: str = HOLDOUT_COMMIT) -> tuple[str, str]` (fingerprint, full commit sha).

- [ ] **Step 1: Write the failing test**

Create the test package and the fingerprint tests. Two tripwires: `test_the_working_tree_fingerprint_is_the_frozen_baseline` compares the working tree with the pinned baseline value `FROZEN` (no git needed, never skipped: `.github/workflows/ci.yml` runs it, Task 10), and `test_baseline_recomputes_from_git_and_equals_the_working_tree` recomputes the `64e628a` baseline from git and requires it to equal both (true today: Plan 1 merged without later edits to these files; it is skipped only in a clone without that commit).

```bash
mkdir -p src/ewbot/paper tests/paper
```

**File: `tests/paper/__init__.py`**

```python
"""Tests for ewbot.paper (Plan 2)."""
```

**File: `tests/paper/test_fingerprint.py`**

```python
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

# The baseline value at the holdout commit 64e628a (spec 5.1). Pinned here so the guard below needs no
# git history: .github/workflows/ci.yml runs it on every pull request to master and every push to master.
FROZEN = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"

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


def test_the_working_tree_fingerprint_is_the_frozen_baseline():
    """Tripwire that never skips: if this fails, a fingerprinted file changed after the holdout. Merged
    and run, that change opens a new epoch and resets the go-live counter; reverting it later does not
    bring the holdout's 36 trades back (spec 8)."""
    assert current_fingerprint() == FROZEN


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
    assert sha == HOLDOUT_COMMIT and fp == FROZEN
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_fingerprint.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/paper/__init__.py`**

```python
"""Paper-trading runtime (Plan 2): hourly deterministic recompute, ledger, Telegram, report."""
```

**File: `src/ewbot/paper/fingerprint.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_fingerprint.py -q
uv run python -c "from ewbot.paper.fingerprint import *; print(current_fingerprint()); print(baseline_fingerprint())"
```

Expected: `19 passed`; both lines print `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1` (the second with `'64e628ad4865b3b04571a7cba2886881eae19176'`).

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/__init__.py src/ewbot/paper/fingerprint.py tests/paper/__init__.py tests/paper/test_fingerprint.py
git commit -m "$(cat <<'EOF'
feat(paper): strategy fingerprint with the 64e628a git baseline

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Ledger model, atomic IO, epochs and go-live counter

**Files:**
- Create: `src/ewbot/paper/ledger.py`
- Test: `tests/paper/test_ledger.py`

**Interfaces:**
- Consumes: `ewbot.backtest.holdout.GO_LIVE_MIN_TRADES` (= 50), `ewbot.backtest.report.to_jsonable(o)`, `ewbot.engine.loop.Trade`.
- Produces (`ewbot.paper.ledger`): `SCHEMA_VERSION = 1`, `LEDGER_FILE = "ledger.json"`, `REPORT_FILE = "report.html"`, `RUNS_LOG = "runs.log"`, `GO_LIVE_STRATEGY = "A"`, `class LedgerError(RuntimeError)`, `@dataclass Ledger(baseline, holdout, epochs, trades, open_position, decisions, discrepancies, runs, sent_event_ids, pending_events, daily_summary_date, criteria, schema_version)` with `to_json() -> str` and `Ledger.from_dict(data) -> Ledger`; `holdout_summary(lock) -> dict` (`lock_sha256, params, holdout_start, last_bar_ts, data_sha256, n_trades`); `new_ledger(baseline_fp: str, baseline_commit: str, lock: dict) -> Ledger`; `atomic_write_text(path, text)`; `load_ledger(ledger_dir) -> Ledger | None`; `save_ledger(ledger_dir, ledger) -> Path`; `append_log_line(path, line)`; `current_epoch(ledger) -> dict`; `ensure_epoch(ledger, fingerprint, started_ts) -> tuple[dict, bool]`; `trade_record(t: Trade, epoch_id) -> dict`; `trade_from_record(rec) -> Trade`; `replace_epoch_trades(ledger, epoch_id, trades)`; `golive_count(ledger) -> dict` with keys `epoch_id, eligible, holdout, paper, total, required`.
- Epoch record: `{"id", "fingerprint", "started_ts", "reason", "through_ts"}`.

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_ledger.py`**

```python
from __future__ import annotations

import json
import os

import pytest

from ewbot.engine.loop import Trade
from ewbot.paper import ledger as lg
from ewbot.paper.ledger import (
    LEDGER_FILE,
    SCHEMA_VERSION,
    Ledger,
    LedgerError,
    append_log_line,
    atomic_write_text,
    ensure_epoch,
    golive_count,
    load_ledger,
    new_ledger,
    replace_epoch_trades,
    save_ledger,
    trade_from_record,
    trade_record,
)

LOCK = {
    "sha256": "a" * 64,
    "params": [2.0, 0.7, 0.5],
    "holdout_start": 1_704_067_200,
    "data": {"last_bar_ts": 1_790_938_800, "sha256": "b" * 64},
    "first_run": {"criteria": {"n_trades": {"A": 36, "HYBRID": 40}}},
}


def _trade(entry_ts: int, exit_ts: int, pnl: float = 5.0) -> Trade:
    return Trade(
        entry_ts=entry_ts, exit_ts=exit_ts, setup="W3", direction=1, contracts=2, entry_price=100.0,
        exit_price_avg=101.0, stop0=95.0, tp1=105.0, tp2=None, pnl=pnl, fees=0.1, funding=0.0, r_multiple=0.5,
        exit_reason="STOP", snapshot={"q_top": 0.8, "counts": [{"pattern": "IMP", "points": [(1, 2.0)]}]},
    )


def test_new_ledger_binds_baseline_and_holdout_summary():
    led = new_ledger("f" * 64, "64e628ad4865b3b04571a7cba2886881eae19176", LOCK)
    assert led.schema_version == SCHEMA_VERSION
    assert led.baseline == {"fingerprint": "f" * 64, "commit": "64e628ad4865b3b04571a7cba2886881eae19176"}
    assert led.holdout == {
        "lock_sha256": "a" * 64, "params": [2.0, 0.7, 0.5], "holdout_start": 1_704_067_200,
        "last_bar_ts": 1_790_938_800, "data_sha256": "b" * 64, "n_trades": {"A": 36, "HYBRID": 40},
    }
    assert led.epochs == [{"id": 1, "fingerprint": "f" * 64, "started_ts": 1_790_938_800,
                           "reason": "baseline: holdout commit 64e628a", "through_ts": 1_790_938_800}]


def test_schema_round_trip(tmp_path):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    replace_epoch_trades(led, 1, [_trade(1_790_942_400, 1_790_956_800)])
    led.decisions.append({"id": "ENTRY:1:1", "kind": "ENTRY", "levels": [None, 2.5]})
    led.sent_event_ids.append("ENTRY:1:1")
    led.daily_summary_date = "2026-10-03"
    save_ledger(tmp_path, led)
    again = load_ledger(tmp_path)
    assert again == led
    assert trade_from_record(again.trades[0]).entry_ts == 1_790_942_400
    assert load_ledger(tmp_path / "missing") is None


def test_trade_record_is_json_and_round_trips():
    t = _trade(10, 20)
    rec = trade_record(t, 3)
    assert rec["epoch_id"] == 3 and json.loads(json.dumps(rec)) == rec
    back = trade_from_record(rec)
    assert (back.entry_ts, back.pnl, back.tp2, back.exit_reason) == (10, 5.0, None, "STOP")


def test_schema_version_and_unknown_keys_are_refused(tmp_path):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    save_ledger(tmp_path, led)
    path = tmp_path / LEDGER_FILE
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "schema_version": 99}))
    with pytest.raises(LedgerError, match="schema_version"):
        load_ledger(tmp_path)
    path.write_text(json.dumps({**data, "surprise": 1}))
    with pytest.raises(LedgerError, match="unknown"):
        load_ledger(tmp_path)
    path.write_text("{not json")
    with pytest.raises(LedgerError, match="unreadable"):
        load_ledger(tmp_path)


def test_atomic_write_keeps_the_original_when_replace_fails(tmp_path, monkeypatch):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    save_ledger(tmp_path, led)
    before = (tmp_path / LEDGER_FILE).read_text()
    led.daily_summary_date = "2030-01-01"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(lg.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_ledger(tmp_path, led)
    assert (tmp_path / LEDGER_FILE).read_text() == before
    assert sorted(os.listdir(tmp_path)) == [LEDGER_FILE]  # no temp file left behind


def test_unserialisable_ledger_never_touches_the_file(tmp_path):
    led = new_ledger("f" * 64, "c" * 40, LOCK)
    save_ledger(tmp_path, led)
    before = (tmp_path / LEDGER_FILE).read_text()
    led.criteria = {"sharpe": float("nan")}
    with pytest.raises(ValueError):
        save_ledger(tmp_path, led)
    assert (tmp_path / LEDGER_FILE).read_text() == before


def test_atomic_write_and_log_append(tmp_path):
    p = tmp_path / "sub" / "runs.log"
    append_log_line(p, "one")
    append_log_line(p, "two\n")
    assert p.read_text() == "one\ntwo\n"
    atomic_write_text(p, "x")
    assert p.read_text() == "x"


def test_new_fingerprint_opens_an_epoch_resets_the_count_and_keeps_old_trades():
    led = new_ledger("base", "c" * 40, LOCK)
    ep, created = ensure_epoch(led, "base", 1_791_000_000)
    assert not created and ep["id"] == 1
    replace_epoch_trades(led, 1, [_trade(1_790_942_400, 1_790_956_800), _trade(1_790_960_400, 1_790_964_000)])
    assert golive_count(led) == {"epoch_id": 1, "eligible": True, "holdout": 36, "paper": 2, "total": 38, "required": 50}

    ep2, created = ensure_epoch(led, "edited", 1_791_000_000)
    assert created and ep2 == {"id": 2, "fingerprint": "edited", "started_ts": 1_791_000_000,
                               "reason": "fingerprint changed: base -> edited", "through_ts": 1_791_000_000}
    replace_epoch_trades(led, 2, [])
    assert golive_count(led) == {"epoch_id": 2, "eligible": False, "holdout": 0, "paper": 0, "total": 0, "required": 50}
    assert [t["epoch_id"] for t in led.trades] == [1, 1]  # epoch 1 trades preserved

    replace_epoch_trades(led, 2, [_trade(1_791_003_600, 1_791_007_200)])
    assert golive_count(led)["paper"] == 1 and len(led.trades) == 3

    ep3, created = ensure_epoch(led, "base", 1_791_100_000)  # reverting the code: baseline fingerprint again
    assert created and ep3["id"] == 3
    count = golive_count(led)
    assert count["eligible"] and count["holdout"] == 0 and count["total"] == 0  # the holdout chain stays broken


def test_ledger_from_dict_requires_core_keys():
    with pytest.raises(LedgerError, match="missing"):
        Ledger.from_dict({"schema_version": SCHEMA_VERSION})
    with pytest.raises(LedgerError, match="not a JSON object"):
        Ledger.from_dict([1, 2])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_ledger.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper.ledger'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/paper/ledger.py`**

```python
"""Paper-trading ledger (Plan 2 spec 6.2): JSON model, atomic file IO, epochs and the go-live counter.

ledger.json lives on the `paper` branch next to report.html and runs.log. Every write goes to a temp
file in the same directory that then replaces the target (os.replace), so a crash mid-write leaves
the previous version intact."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ewbot.backtest.holdout import GO_LIVE_MIN_TRADES
from ewbot.backtest.report import to_jsonable
from ewbot.engine.loop import Trade

SCHEMA_VERSION = 1
LEDGER_FILE = "ledger.json"
REPORT_FILE = "report.html"
RUNS_LOG = "runs.log"
GO_LIVE_STRATEGY = "A"  # the paper runtime trades the locked config (strategy.mode "A")


class LedgerError(RuntimeError):
    """ledger.json is unreadable, has another schema version, or does not belong to this holdout."""


@dataclass
class Ledger:
    baseline: dict[str, Any]  # {"fingerprint", "commit"}
    holdout: dict[str, Any]  # holdout_summary(lock)
    epochs: list[dict[str, Any]] = field(default_factory=list)  # {"id","fingerprint","started_ts","reason","through_ts"}
    trades: list[dict[str, Any]] = field(default_factory=list)  # closed paper trades as trade_record dicts
    open_position: dict[str, Any] | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    sent_event_ids: list[str] = field(default_factory=list)
    pending_events: list[dict[str, Any]] = field(default_factory=list)  # {"id","kind","text","attempts"}
    daily_summary_date: str | None = None  # New York date of the last daily summary
    criteria: dict[str, Any] | None = None  # criteria 1-2 from the last daily recompute
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        """Strict JSON (NaN/inf refused with ValueError)."""
        return json.dumps(dataclasses.asdict(self), indent=1, allow_nan=False)

    @classmethod
    def from_dict(cls, data: Any) -> Ledger:
        if not isinstance(data, dict):
            raise LedgerError("ledger root is not a JSON object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise LedgerError(f"ledger schema_version {version!r} is not {SCHEMA_VERSION}; refusing to touch it")
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - names)
        missing = sorted(n for n in ("baseline", "holdout", "epochs") if n not in data)
        if unknown or missing:
            raise LedgerError(f"ledger keys do not match schema {SCHEMA_VERSION}: unknown {unknown}, missing {missing}")
        return cls(**data)


def holdout_summary(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout.lock the paper ledger is bound to (precheck has validated the keys)."""
    return {
        "lock_sha256": lock["sha256"],
        "params": [float(p) for p in lock["params"]],
        "holdout_start": int(lock["holdout_start"]),
        "last_bar_ts": int(lock["data"]["last_bar_ts"]),
        "data_sha256": lock["data"]["sha256"],
        "n_trades": {k: int(v) for k, v in lock["first_run"]["criteria"]["n_trades"].items()},
    }


def new_ledger(baseline_fp: str, baseline_commit: str, lock: dict[str, Any]) -> Ledger:
    """Fresh ledger: epoch 1 carries the baseline fingerprint and starts at the holdout's last bar."""
    hs = holdout_summary(lock)
    start = hs["last_bar_ts"]
    epoch = {
        "id": 1,
        "fingerprint": baseline_fp,
        "started_ts": start,
        "reason": f"baseline: holdout commit {baseline_commit[:7]}",
        "through_ts": start,
    }
    return Ledger(baseline={"fingerprint": baseline_fp, "commit": baseline_commit}, holdout=hs, epochs=[epoch])


# ----------------------------------------------------------------------------- atomic IO


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to a temp file next to `path`, fsync it, then os.replace it over `path`.
    On any error the temp file is removed and `path` keeps its previous content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_ledger(ledger_dir: Path) -> Ledger | None:
    """None if <ledger_dir>/ledger.json does not exist yet."""
    path = Path(ledger_dir) / LEDGER_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{path} is unreadable: {exc}") from exc
    return Ledger.from_dict(data)


def save_ledger(ledger_dir: Path, ledger: Ledger) -> Path:
    path = Path(ledger_dir) / LEDGER_FILE
    text = ledger.to_json()  # serialise fully before touching the file
    atomic_write_text(path, text + "\n")
    return path


def append_log_line(path: Path, line: str) -> None:
    """runs.log: append one line, atomically (read + rewrite through atomic_write_text)."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write_text(path, old + line.rstrip("\n") + "\n")


# ----------------------------------------------------------------------------- epochs and trades


def current_epoch(ledger: Ledger) -> dict[str, Any]:
    return ledger.epochs[-1]


def ensure_epoch(ledger: Ledger, fingerprint: str, started_ts: int) -> tuple[dict[str, Any], bool]:
    """Return (current epoch, created). A fingerprint different from the last epoch's opens a new
    epoch starting at `started_ts` (the last closed bar of this run)."""
    last = ledger.epochs[-1]
    if last["fingerprint"] == fingerprint:
        return last, False
    epoch = {
        "id": int(last["id"]) + 1,
        "fingerprint": fingerprint,
        "started_ts": int(started_ts),
        "reason": f"fingerprint changed: {last['fingerprint'][:12]} -> {fingerprint[:12]}",
        "through_ts": int(started_ts),
    }
    ledger.epochs.append(epoch)
    return epoch, True


def trade_record(t: Trade, epoch_id: int) -> dict[str, Any]:
    rec = to_jsonable(dataclasses.asdict(t))
    rec["epoch_id"] = int(epoch_id)
    return rec


def trade_from_record(rec: dict[str, Any]) -> Trade:
    names = {f.name for f in dataclasses.fields(Trade)}
    return Trade(**{k: v for k, v in rec.items() if k in names})


def replace_epoch_trades(ledger: Ledger, epoch_id: int, trades: Sequence[Trade]) -> None:
    """The current epoch's closed trades are recomputed every run; earlier epochs stay frozen."""
    kept = [t for t in ledger.trades if t["epoch_id"] != epoch_id]
    ledger.trades = kept + [trade_record(t, epoch_id) for t in trades]


def golive_count(ledger: Ledger) -> dict[str, Any]:
    """Spec 9.6 criterion 3 counter. The holdout's A trades count only while the ledger has never
    left the baseline epoch; every fingerprint change resets the count to the new epoch's trades.
    `eligible` is False when the current epoch's fingerprint is not the baseline's."""
    cur = current_epoch(ledger)
    holdout_n = int(ledger.holdout["n_trades"][GO_LIVE_STRATEGY]) if len(ledger.epochs) == 1 else 0
    paper_n = sum(1 for t in ledger.trades if t["epoch_id"] == cur["id"])
    return {
        "epoch_id": int(cur["id"]),
        "eligible": cur["fingerprint"] == ledger.baseline["fingerprint"],
        "holdout": holdout_n,
        "paper": paper_n,
        "total": holdout_n + paper_n,
        "required": GO_LIVE_MIN_TRADES,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_ledger.py -q
```

Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/ledger.py tests/paper/test_ledger.py
git commit -m "$(cat <<'EOF'
feat(paper): ledger model, atomic IO, epochs and go-live counter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Read-only engine trace and shared synthetic test helpers

**Files:**
- Create: `src/ewbot/paper/trace.py`
- Create: `tests/paper/helpers.py`
- Test: `tests/paper/test_trace.py`

**Interfaces:**
- Consumes: module globals `ewbot.engine.loop.PaperBroker` / `ewbot.engine.loop.WaveEngine` (looked up by `_Run.__init__` and `TradingEngine.run`); `PaperBroker.process_bar(bar) -> list[Fill]`, `queue_entry(direction, contracts, stop, tp1, tp2, tp1_contracts)`, `set_stop(price)`, `close_now(ts, price) -> Fill | None`; `WaveEngine.on_1h_close(bar1h, bar4h, sub_bars) -> WaveState`; `run_backtest(cfg, df1h, start_ts=None, end_ts=None, *, allow_holdout=False) -> EngineResult`; `holdout_config(cfg, params) -> BotConfig`; `data_fingerprint(df1h, start_ts, last_ts) -> str`; `lock_digest(cfg, params, random_runs=100, code_sha256=None) -> str`; `tests.synth.random_bars(seed, n, vol, start)`.
- Produces (`ewbot.paper.trace`): `OrderRec(ts, direction, contracts, stop, tp1, tp2)`, `FillRec(ts, kind, contracts, price, trade_key)`, `StopRec(ts, trade_key, old, new, kind)` (`kind` `"BREAKEVEN"` / `"TRAIL"`), `EngineTrace(orders, fills, stops, end_fill, last_state, last_ts, brokers)`, `traced_engine() -> ContextManager[EngineTrace]`.
- Produces (`tests.paper.helpers`): `PARAMS = (2.0, 0.5, 0.0)`, `SEED = 2`, `N_BARS = 3000`, `BOUNDARY = 1704844800` (2024-01-10 00:00 UTC), `HOLDOUT_A_TRADES = 2`, `ts(text) -> int`, `synth_frame(n=N_BARS, seed=SEED) -> DataFrame`, `CODE_SHA` (stand-in holdout code fingerprint), `make_lock(df, boundary=BOUNDARY, params=PARAMS, n_a=2, **overrides) -> dict` (its `sha256` is the real `lock_digest(BotConfig(), params, random_runs, code_sha256)` of the final lock unless `sha256` itself is overridden), `git(repo, *args)`, `commit_lock(repo, lock) -> Path`, `FakeSender(fail=0)` (`.texts`, `.calls`, `.fail`), `FakeFetch(source, clock, fail=0)` (`.calls`, `.fail`), `Clock(now)` (`.now`).
- Synthetic path facts used by Tasks 4–9 (seed 2, 3000 bars from 2023-11-15 00:00 UTC, `PARAMS`): A trades enter 2024-01-01 20:00 and 01-06 20:00 (holdout side of `BOUNDARY`), 01-12 15:00 (TIME exit 01-22 12:00), 02-07 21:00 (STOP 02-08 22:00), 02-16 02:00 short (STOP 02-18 17:00) and 03-13 23:00 C_END long (TRAIL 03-14 07:00, TP1 + BREAKEVEN 03-14 23:00, TP2 03-16 18:00).

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/helpers.py`**

```python
"""Shared helpers for tests/paper: synthetic candles around the 2024-01-01 boundary, a committed
test holdout lock in a temp git repo, a fake Telegram sender and a fake candle updater.

Everything is synthetic (tests/synth.random_bars): no test reads the real cache, the real
<repo>/holdout.lock, or the network."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint, holdout_config, lock_digest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.types import H1
from tests.synth import random_bars

PARAMS = (2.0, 0.5, 0.0)  # a grid combo that trades often on the synthetic path
SEED = 2
N_BARS = 3000  # 2023-11-15 00:00 .. 2024-03-18 23:00 UTC
BOUNDARY = 1_704_844_800  # 2024-01-10 00:00 UTC: the synthetic holdout's last bar
HOLDOUT_A_TRADES = 2  # synthetic A trades entering in [HOLDOUT_START, BOUNDARY]
CODE_SHA = "c0de" * 16  # stands in for the holdout's code_fingerprint (bound through lock_digest)


def ts(text: str) -> int:
    """'YYYY-MM-DD HH:MM' (UTC) -> epoch seconds."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp())


def synth_frame(n: int = N_BARS, seed: int = SEED) -> pd.DataFrame:
    bars = random_bars(seed=seed, n=n, vol=30.0, start=20_000.0)
    return pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    ).astype({"ts": "int64"})


def make_lock(df: pd.DataFrame, boundary: int = BOUNDARY, params: tuple[float, ...] = PARAMS,
              n_a: int = HOLDOUT_A_TRADES, **overrides: Any) -> dict[str, Any]:
    """A completed-holdout lock with the real schema (v3) for synthetic data. Its sha256 is the real
    lock_digest(BotConfig(), params, random_runs, code_sha256) of the final lock (after `overrides`),
    unless `sha256` itself is overridden."""
    cfg = holdout_config(BotConfig(), params)
    lock = {
        "version": 3,
        "keys": ["wave.K4", "strategy.Q_MIN", "strategy.S_MIN"],
        "params": list(params),
        "sha256": None,
        "code_sha256": CODE_SHA,
        "config": json.loads(json.dumps(dataclasses.asdict(cfg))),
        "holdout_start": HOLDOUT_START,
        "random_runs": 100,
        "data": {
            "start_ts": HOLDOUT_START,
            "last_bar_ts": boundary,
            "n_bars": int(((df["ts"] >= HOLDOUT_START) & (df["ts"] <= boundary)).sum()),
            "sha256": data_fingerprint(df, HOLDOUT_START, boundary),
        },
        "first_run": {"criteria": {"n_trades": {"A": n_a, "HYBRID": n_a + 1}}},
        "runs": [],
    }
    lock.update(overrides)
    if lock["sha256"] is None:
        lock["sha256"] = lock_digest(BotConfig(), lock["params"], lock["random_runs"], lock["code_sha256"])
    return lock


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def commit_lock(repo: Path, lock: dict[str, Any]) -> Path:
    """Write <repo>/holdout.lock and commit it (initialising the repo if needed)."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
    path = repo / "holdout.lock"
    path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    git(repo, "add", "holdout.lock")
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], capture_output=True)
    if staged.returncode != 0:
        git(repo, "commit", "-q", "-m", "lock")
    return path


class FakeSender:
    """Records every text; raises for the next `fail` calls."""

    def __init__(self, fail: int = 0) -> None:
        self.texts: list[str] = []
        self.fail = fail
        self.calls = 0

    def __call__(self, text: str) -> None:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("telegram down")
        self.texts.append(text)


class FakeFetch:
    """Stands in for update_cache: writes <cache>/BTC-USD_1h.parquet with the bars of `source`
    (a frame, or a callable returning one) that have closed by clock() - ts < clock() // 1h * 1h."""

    def __init__(self, source: pd.DataFrame | Callable[[], pd.DataFrame], clock: Callable[[], int], fail: int = 0) -> None:
        self.source = source
        self.clock = clock
        self.fail = fail
        self.calls = 0

    def __call__(self, cache_dir: Path) -> Path:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        df = self.source() if callable(self.source) else self.source
        end = int(self.clock()) // H1 * H1
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "BTC-USD_1h.parquet"
        df[df["ts"] < end].reset_index(drop=True).to_parquet(path, index=False)
        return path


class Clock:
    def __init__(self, now: int) -> None:
        self.now = int(now)

    def __call__(self) -> int:
        return self.now
```

**File: `tests/paper/test_trace.py`**

```python
from __future__ import annotations

import pytest

import ewbot.engine.loop as loop_mod
from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.execution.paper import PaperBroker
from ewbot.paper.trace import traced_engine
from ewbot.waves.engine import WaveEngine
from tests.paper.helpers import PARAMS, synth_frame, ts


@pytest.fixture(scope="module")
def runs():
    df = synth_frame()
    cfg = holdout_config(BotConfig(), PARAMS)
    plain = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    with traced_engine() as trace:
        traced = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    cut = df[df["ts"] <= ts("2024-03-15 06:00")]
    with traced_engine() as open_trace:
        open_res = run_backtest(cfg, cut, HOLDOUT_START, None, allow_holdout=True)
    return plain, traced, trace, open_res, open_trace


def test_tracing_does_not_change_the_run(runs):
    plain, traced, trace, _, _ = runs
    assert traced == plain  # trades, equity, signals and events bit-identical
    assert trace.brokers == 1 and len(plain.trades) >= 5


def test_original_classes_are_restored_even_after_an_error():
    with pytest.raises(RuntimeError):
        with traced_engine():
            assert loop_mod.PaperBroker is not PaperBroker
            raise RuntimeError("boom")
    assert loop_mod.PaperBroker is PaperBroker and loop_mod.WaveEngine is WaveEngine


def test_orders_match_signals(runs):
    _, res, trace, _, _ = runs
    assert [o.ts for o in trace.orders] == [s.ts for s in res.signals]
    assert [(o.stop, o.tp1, o.tp2, o.direction) for o in trace.orders] == [
        (s.stop, s.tp1, s.tp2, s.direction) for s in res.signals
    ]


def test_fills_reconstruct_every_trade(runs):
    _, res, trace, _, _ = runs
    assert trace.end_fill is None  # the last trade closed before the data ends
    for t in res.trades:
        mine = [f for f in trace.fills if f.trade_key == t.entry_ts]
        assert mine[0].kind == "ENTRY" and mine[0].ts == t.entry_ts and mine[0].contracts == t.contracts
        assert mine[0].price == t.entry_price
        exits = mine[1:]
        assert sum(f.contracts for f in exits) == t.contracts and exits[-1].ts == t.exit_ts
        avg = sum(f.price * f.contracts for f in exits) / t.contracts
        assert avg == pytest.approx(t.exit_price_avg, rel=1e-12)


def test_stop_moves_breakeven_after_tp1_and_trailing_only_tightens(runs):
    _, res, trace, _, _ = runs
    by_entry = {t.entry_ts: t for t in res.trades}
    kinds = {s.kind for s in trace.stops}
    assert kinds == {"BREAKEVEN", "TRAIL"}
    for s in trace.stops:
        t = by_entry[s.trade_key]
        assert t.direction * (s.new - s.old) > 0  # stops only move in the trade's favour
        assert t.entry_ts <= s.ts <= t.exit_ts
        if s.kind == "BREAKEVEN":
            assert s.new == t.entry_price
            assert any(f.kind == "TP1" and f.ts == s.ts and f.trade_key == s.trade_key for f in trace.fills)
    assert trace.last_state is not None and trace.last_state.ts == int(synth_frame()["ts"].iloc[-1])


def test_a_position_open_at_the_last_bar_is_the_end_fill(runs):
    _, _, _, res, trace = runs
    last = res.trades[-1]
    assert last.exit_reason == "END" and trace.end_fill is not None
    assert trace.end_fill.trade_key == last.entry_ts == ts("2024-03-13 23:00")
    assert trace.end_fill.ts == ts("2024-03-15 06:00")
    assert all(f.kind != "END" for f in trace.fills)  # the END close is not a broker fill
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_trace.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper.trace'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/paper/trace.py`**

```python
"""Read-only instrumentation of one TradingEngine run (Plan 2 spec 5.3 decisions).

EngineResult has trades, equity and signals but not the individual fills (TP1 partials), the stop
moves (breakeven, structural trailing) or the order sizes - all of which the paper ledger records as
decisions. `traced_engine()` swaps the PaperBroker and WaveEngine names that ewbot.engine.loop looks
up at run time for subclasses that call the original methods unchanged and only log what passed
through them. No fingerprinted file is edited and the run's results are bit-identical (tested).
The swap is process-global for the duration of the `with` block: run nothing else concurrently."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

import ewbot.engine.loop as loop_mod
from ewbot.execution.paper import Fill, PaperBroker
from ewbot.types import Bar
from ewbot.waves.engine import WaveEngine, WaveState


@dataclass(frozen=True, slots=True)
class OrderRec:
    ts: int  # bar whose close queued the entry (= Signal.ts)
    direction: int
    contracts: int
    stop: float
    tp1: float
    tp2: float | None


@dataclass(frozen=True, slots=True)
class FillRec:
    ts: int  # bar open time of the fill
    kind: str  # "ENTRY","STOP","TP1","TP2","EXIT" (and "END" for the end-of-data close)
    contracts: int
    price: float
    trade_key: int  # entry_ts of the position the fill belongs to


@dataclass(frozen=True, slots=True)
class StopRec:
    ts: int  # bar whose processing moved the stop (effective from the next bar)
    trade_key: int
    old: float
    new: float
    kind: str  # "BREAKEVEN" (stop moved to the entry price after TP1) | "TRAIL"


@dataclass
class EngineTrace:
    orders: list[OrderRec] = field(default_factory=list)
    fills: list[FillRec] = field(default_factory=list)  # every process_bar fill, in order (END excluded)
    stops: list[StopRec] = field(default_factory=list)
    end_fill: FillRec | None = None  # the END close of a position still open at the last bar
    last_state: WaveState | None = None  # WaveState after the last bar
    last_ts: int | None = None  # last bar processed by the broker
    brokers: int = 0  # PaperBroker instances created inside the block (1 per engine run)


def _classes(trace: EngineTrace) -> tuple[type[PaperBroker], type[WaveEngine]]:
    class TracingBroker(PaperBroker):
        def __init__(self, costs, equity) -> None:  # type: ignore[no-untyped-def]
            super().__init__(costs, equity)
            trace.brokers += 1

        def process_bar(self, bar: Bar) -> list[Fill]:
            key = self.position.entry_ts if self.position is not None else None
            fills = super().process_bar(bar)
            trace.last_ts = bar.ts
            for f in fills:
                if f.kind == "ENTRY":
                    key = f.ts
                assert key is not None, f"{f.kind} fill without a position"
                trace.fills.append(FillRec(f.ts, f.kind, f.contracts, f.price, int(key)))
            return fills

        def queue_entry(self, direction, contracts, stop, tp1, tp2, tp1_contracts) -> None:  # type: ignore[no-untyped-def]
            super().queue_entry(direction, contracts, stop, tp1, tp2, tp1_contracts)
            assert trace.last_ts is not None
            trace.orders.append(OrderRec(trace.last_ts, direction, contracts, stop, tp1, tp2))

        def set_stop(self, price: float) -> None:
            pos = self.position
            if pos is not None and price != pos.stop:
                assert trace.last_ts is not None
                kind = "BREAKEVEN" if price == pos.entry_price else "TRAIL"
                trace.stops.append(StopRec(trace.last_ts, pos.entry_ts, pos.stop, price, kind))
            super().set_stop(price)

        def close_now(self, ts: int, price: float) -> Fill | None:
            pos = self.position
            f = super().close_now(ts, price)
            if f is not None and pos is not None:
                trace.end_fill = FillRec(f.ts, "END", f.contracts, f.price, pos.entry_ts)
            return f

    class TracingWave(WaveEngine):
        def on_1h_close(self, bar1h: Bar, bar4h: Bar | None, sub_bars: Sequence[Bar] | None) -> WaveState:
            ws = super().on_1h_close(bar1h, bar4h, sub_bars)
            trace.last_state = ws
            return ws

    return TracingBroker, TracingWave


@contextmanager
def traced_engine() -> Iterator[EngineTrace]:
    """Every TradingEngine.run() inside the block records into the yielded EngineTrace (use one
    run per block). The original classes are restored on exit, also after an exception."""
    trace = EngineTrace()
    broker_cls, wave_cls = _classes(trace)
    orig_broker, orig_wave = loop_mod.PaperBroker, loop_mod.WaveEngine
    loop_mod.PaperBroker, loop_mod.WaveEngine = broker_cls, wave_cls  # type: ignore[misc]
    try:
        yield trace
    finally:
        loop_mod.PaperBroker, loop_mod.WaveEngine = orig_broker, orig_wave  # type: ignore[misc]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_trace.py -q
```

Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/trace.py tests/paper/helpers.py tests/paper/test_trace.py
git commit -m "$(cat <<'EOF'
feat(paper): read-only engine trace of fills, stop moves and orders

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Out-of-sample extraction, decision records and the replay check

**Files:**
- Create: `src/ewbot/paper/decisions.py`
- Test: `tests/paper/test_decisions.py`

**Interfaces:**
- Consumes: `EngineResult(trades, equity, signals, events)`, `Trade`, `Signal(ts, setup, direction, entry_ref, stop, tp1, tp2, risk_mult, time_stop_4h, instance, lineage, reason)`, `EngineTrace` / `traced_engine` (Task 3), `H1`, `WaveState`, `tests.paper.helpers` (Task 3).
- Produces (`ewbot.paper.decisions`): `REL_TOL = 1e-9`, `DECISION_KINDS`, `CAUSES`, `split_result(res, trace) -> tuple[list[Trade], Trade | None]`, `paper_trades(closed, boundary_ts) -> list[Trade]`, `decision_id(kind, ts, trade_key) -> str`, `known_at(d) -> int`, `build_decisions(res, trace, trades) -> list[dict]` (keys `id, kind, ts, price, contracts, reason, trade_key, levels, setup, direction`), `same_decision(a, b) -> bool`, `classify(recorded, data_revised, current_epoch_id, env_changed=False) -> str` (`CAUSES` order: `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`), `verify_decisions(recorded, recomputed, epoch, revised_at, appeared_revised, *, appeared_after=None, env_sha256=None, appeared_env=None, previous_env_sha256=None) -> list[dict]` (keys `id, type, decision_id, cause, recorded, recomputed`; the APPEARED window is known_at in `(appeared_after or epoch started_ts, epoch through_ts]`; `ENVIRONMENT_CHANGE` when a recorded decision's `env_sha256`, or `appeared_env(recomputed)` for APPEARED, and the previous successful run's `previous_env_sha256` both differ from the current `env_sha256`, None never counting as a change), `decisions_to_record(recomputed, epoch, recorded_ids) -> list[dict]`, `open_position_record(open_trade, trace, epoch_start) -> dict`, `top_count_summary(ws) -> dict | None`.

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_decisions.py`**

```python
from __future__ import annotations

import pytest

from ewbot.backtest.holdout import data_fingerprint, holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.engine.loop import Trade
from ewbot.paper.decisions import (
    build_decisions,
    classify,
    decision_id,
    decisions_to_record,
    known_at,
    open_position_record,
    paper_trades,
    same_decision,
    split_result,
    top_count_summary,
    verify_decisions,
)
from ewbot.paper.trace import traced_engine
from ewbot.types import H1
from tests.paper.helpers import BOUNDARY, PARAMS, synth_frame, ts

CFG = holdout_config(BotConfig(), PARAMS)


def _traced(df):
    with traced_engine() as trace:
        res = run_backtest(CFG, df, HOLDOUT_START, None, allow_holdout=True)
    closed, open_trade = split_result(res, trace)
    trades = closed + ([open_trade] if open_trade is not None else [])
    return res, trace, closed, open_trade, build_decisions(res, trace, trades)


@pytest.fixture(scope="module")
def full():
    return _traced(synth_frame())


@pytest.fixture(scope="module")
def cut():
    df = synth_frame()
    return _traced(df[df["ts"] <= ts("2024-03-15 06:00")])


def _t(entry_ts: int, exit_ts: int) -> Trade:
    return Trade(entry_ts, exit_ts, "W3", 1, 1, 100.0, 101.0, 95.0, 105.0, None, 1.0, 0.0, 0.0, 0.2, "STOP", {})


def test_out_of_sample_boundary():
    trades = [_t(BOUNDARY - H1, BOUNDARY + 5 * H1), _t(BOUNDARY, BOUNDARY + 2 * H1), _t(BOUNDARY + H1, BOUNDARY + 3 * H1)]
    # open at the boundary (entered before) and entered exactly on the boundary bar: holdout trades
    assert paper_trades(trades, BOUNDARY) == [trades[2]]


def test_split_result_separates_the_position_open_at_the_last_bar(full, cut):
    _, _, closed, open_trade, _ = full
    assert open_trade is None and all(t.exit_reason != "END" for t in closed)
    res, _, closed, open_trade, _ = cut
    assert open_trade is res.trades[-1] and open_trade.exit_reason == "END"
    assert closed == list(res.trades[:-1])


def test_decision_kinds_ids_and_fields(full):
    res, trace, closed, _, decs = full
    ids = [d["id"] for d in decs]
    assert len(ids) == len(set(ids))
    assert {d["kind"] for d in decs} == {"SIGNAL", "ENTRY", "TP1", "TP2", "STOP", "EXIT", "BREAKEVEN", "TRAIL"}
    assert [d["ts"] for d in decs] == sorted(d["ts"] for d in decs)
    by_entry = {t.entry_ts: t for t in closed}
    for d in decs:
        assert d["id"] == decision_id(d["kind"], d["ts"], d["trade_key"])
        if d["kind"] == "SIGNAL":
            entry = next(x for x in decs if x["kind"] == "ENTRY" and x["ts"] == d["ts"] + H1)
            assert d["contracts"] == entry["contracts"] and d["trade_key"] == d["ts"]
            assert len(d["levels"]) == 3
        elif d["kind"] == "ENTRY":
            assert d["reason"] == by_entry[d["trade_key"]].setup and d["price"] == by_entry[d["ts"]].entry_price
        elif d["kind"] == "EXIT":
            assert d["reason"] == by_entry[d["trade_key"]].exit_reason
    be = next(d for d in decs if d["kind"] == "BREAKEVEN")
    t = by_entry[be["trade_key"]]
    tp1 = next(d for d in decs if d["kind"] == "TP1" and d["trade_key"] == t.entry_ts)
    assert be["price"] == t.entry_price and be["contracts"] == t.contracts - tp1["contracts"]
    assert be["ts"] == tp1["ts"] and be["levels"][0] != be["price"]


def test_known_at_signals_lag_one_bar():
    assert known_at({"kind": "SIGNAL", "ts": 100 * H1}) == 101 * H1
    for kind in ("ENTRY", "TP1", "TP2", "STOP", "EXIT", "BREAKEVEN", "TRAIL"):
        assert known_at({"kind": kind, "ts": 100 * H1}) == 100 * H1


def test_a_prefix_run_knows_exactly_the_decisions_up_to_its_last_bar(full, cut):
    """Determinism of the recompute: the run cut at T knows every decision of the full run with
    known_at <= T, and nothing else (the END close is not a decision)."""
    full_decs, cut_decs = full[4], cut[4]
    last = ts("2024-03-15 06:00")
    expected = [d for d in full_decs if known_at(d) <= last]
    assert [d["id"] for d in cut_decs] == [d["id"] for d in expected]
    assert all(same_decision(a, b) for a, b in zip(cut_decs, expected, strict=True))


def test_same_decision_tolerance():
    a = {"kind": "ENTRY", "ts": 1, "trade_key": 1, "contracts": 2, "reason": "W3", "setup": "W3", "direction": 1,
         "price": 20_000.0, "levels": [None, 1.0]}
    assert same_decision(a, {**a, "price": 20_000.0 * (1 + 5e-10)})
    assert not same_decision(a, {**a, "price": 20_000.0 * (1 + 5e-9)})
    assert not same_decision(a, {**a, "contracts": 3})
    assert not same_decision(a, {**a, "levels": [1.0, 1.0]})
    assert not same_decision(a, {**a, "reason": "W5"})


def test_classify_order():
    rec = {"epoch_id": 1}
    assert classify(rec, True, 2) == "DATA_REVISION"
    assert classify(rec, True, 2, env_changed=True) == "DATA_REVISION"
    assert classify(rec, False, 2) == "FINGERPRINT_CHANGE"
    assert classify(rec, False, 2, env_changed=True) == "FINGERPRINT_CHANGE"
    assert classify(rec, False, 1, env_changed=True) == "ENVIRONMENT_CHANGE"
    assert classify(None, False, 2, env_changed=True) == "ENVIRONMENT_CHANGE"
    assert classify(rec, False, 1) == "UNEXPLAINED"
    assert classify(None, False, 2) == "UNEXPLAINED"


def _record(decs, df, epoch_id=1, env_sha256="env-a"):
    return [{**d, "run_id": "r1", "epoch_id": epoch_id, "data_sha256": data_fingerprint(df, 0, d["ts"]),
             "env_sha256": env_sha256} for d in decs]


def test_replay_check_classifies_a_revised_bar_as_data_revision(full):
    df = synth_frame()
    decs = full[4]
    entry = next(d for d in decs if d["kind"] == "ENTRY" and d["ts"] > BOUNDARY)
    recorded = _record([d for d in decs if d["ts"] > BOUNDARY], df)
    epoch = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    revised = df.copy()
    i = int(revised.index[revised["ts"] == entry["ts"]][0])
    revised.loc[i, "open"] += 7.0  # one past bar changes: the entry fills at a different open
    revised.loc[i, "high"] = max(revised.loc[i, "high"], revised.loc[i, "open"])
    recomputed = _traced(revised)[4]

    def revised_at(d):
        return data_fingerprint(revised, 0, d["ts"]) != d["data_sha256"]

    found = verify_decisions(recorded, recomputed, epoch, revised_at, lambda r: False)
    hit = next(f for f in found if f["decision_id"] == entry["id"])
    assert hit["type"] == "CHANGED" and hit["cause"] == "DATA_REVISION"
    assert all(f["cause"] == "DATA_REVISION" for f in found)
    assert all(f["recorded"]["ts"] >= entry["ts"] for f in found if f["recorded"])  # nothing before the bar


def test_replay_check_fingerprint_change_and_unexplained(full):
    df = synth_frame()
    decs = [d for d in full[4] if d["ts"] > BOUNDARY]
    epoch2 = {"id": 2, "started_ts": int(df["ts"].iloc[-1]), "through_ts": int(df["ts"].iloc[-1])}
    tampered = _record(decs, df, epoch_id=1)
    tampered[0] = {**tampered[0], "price": tampered[0]["price"] + 1.0}
    found = verify_decisions(tampered, full[4], epoch2, lambda d: False, lambda r: False)
    assert [(f["type"], f["cause"]) for f in found] == [("CHANGED", "FINGERPRINT_CHANGE")]

    epoch1 = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    found = verify_decisions(tampered, full[4], epoch1, lambda d: False, lambda r: False)
    assert [(f["type"], f["cause"]) for f in found] == [("CHANGED", "UNEXPLAINED")]

    gone = _record(decs, df) + [{**_record(decs[:1], df)[0], "id": "ENTRY:1:1", "ts": 1, "trade_key": 1}]
    found = verify_decisions(gone, full[4], epoch1, lambda d: False, lambda r: False)
    assert [(f["type"], f["decision_id"], f["cause"]) for f in found] == [("VANISHED", "ENTRY:1:1", "UNEXPLAINED")]


def test_replay_check_environment_change_against_the_recording_run(full):
    df = synth_frame()
    decs = [d for d in full[4] if known_at(d) > BOUNDARY]
    epoch1 = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    tampered = _record(decs[1:], df)  # recorded under environment "env-a"; decs[0] was never recorded
    tampered[0] = {**tampered[0], "price": tampered[0]["price"] + 1.0}

    def check(current, appeared_env):  # the previous successful run ran in "env-a"
        found = verify_decisions(tampered, full[4], epoch1, lambda d: False, lambda r: False,
                                 env_sha256=current, appeared_env=appeared_env, previous_env_sha256="env-a")
        return [(f["type"], f["cause"]) for f in found]

    assert check("env-b", lambda r: "env-a") == [("CHANGED", "ENVIRONMENT_CHANGE"), ("APPEARED", "ENVIRONMENT_CHANGE")]
    assert check("env-a", lambda r: "env-a") == [("CHANGED", "UNEXPLAINED"), ("APPEARED", "UNEXPLAINED")]
    # unknown recording environment (no run should have known it yet / not recorded): not an environment change
    assert check("env-b", lambda r: None) == [("CHANGED", "ENVIRONMENT_CHANGE"), ("APPEARED", "UNEXPLAINED")]
    unstamped = [{k: v for k, v in d.items() if k != "env_sha256"} for d in tampered]
    found = verify_decisions(unstamped, full[4], epoch1, lambda d: False, lambda r: False, env_sha256="env-b",
                             previous_env_sha256="env-a")
    assert [(f["type"], f["cause"]) for f in found] == [("CHANGED", "UNEXPLAINED"), ("APPEARED", "UNEXPLAINED")]


def test_environment_change_explains_only_the_first_run_in_the_new_environment(full):
    df = synth_frame()
    decs = [d for d in full[4] if known_at(d) > BOUNDARY]
    epoch1 = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    tampered = _record(decs[1:], df)  # recorded under "env-a"; decs[0] was never recorded (APPEARED)
    tampered[0] = {**tampered[0], "price": tampered[0]["price"] + 1.0}  # CHANGED
    tampered.append({**tampered[1], "id": "ENTRY:1:1", "ts": 1, "trade_key": 1})  # VANISHED

    def check(previous):  # the current run is in "env-b"
        found = verify_decisions(tampered, full[4], epoch1, lambda d: False, lambda r: False, env_sha256="env-b",
                                 appeared_env=lambda r: "env-a", previous_env_sha256=previous)
        return [(f["type"], f["cause"]) for f in found]

    types = ["CHANGED", "VANISHED", "APPEARED"]
    assert check("env-a") == [(t, "ENVIRONMENT_CHANGE") for t in types]  # the first run in env-b
    assert check("env-b") == [(t, "UNEXPLAINED") for t in types]  # an env-b run already reproduced them
    assert check("env-c") == [(t, "ENVIRONMENT_CHANGE") for t in types]  # the previous run had yet another one
    assert check(None) == [(t, "UNEXPLAINED") for t in types]  # no previous run, or one without a digest


def test_appeared_only_inside_the_epochs_recorded_window(full):
    df = synth_frame()
    decs = [d for d in full[4] if known_at(d) > BOUNDARY]
    recorded = _record(decs[1:], df)  # the first one was never recorded
    epoch = {"id": 1, "started_ts": BOUNDARY, "through_ts": int(df["ts"].iloc[-1])}
    found = verify_decisions(recorded, full[4], epoch, lambda d: False, lambda r: True)
    assert [(f["type"], f["decision_id"], f["cause"]) for f in found] == [("APPEARED", decs[0]["id"], "DATA_REVISION")]
    early = {**epoch, "through_ts": known_at(decs[0]) - H1}  # not yet known by any recording run
    assert verify_decisions(recorded, full[4], early, lambda d: False, lambda r: True) == []


def test_decisions_to_record_takes_only_newly_known_ones(full):
    decs = full[4]
    through = ts("2024-02-10 00:00")
    epoch = {"id": 1, "started_ts": BOUNDARY, "through_ts": through}
    fresh = decisions_to_record(decs, epoch, set())
    assert fresh and all(known_at(d) > through for d in fresh)
    assert decisions_to_record(decs, epoch, {d["id"] for d in fresh}) == []


def test_open_position_record_and_top_count(cut):
    _, trace, _, open_trade, _ = cut
    rec = open_position_record(open_trade, trace, BOUNDARY)
    assert rec["entry_ts"] == ts("2024-03-13 23:00") and rec["counted"] is True
    assert rec["tp1_done"] is True and rec["remaining"] == rec["contracts"] - 27
    assert rec["stop"] == open_trade.entry_price  # breakeven after TP1
    assert open_position_record(open_trade, trace, ts("2024-03-14 00:00"))["counted"] is False
    top = top_count_summary(trace.last_state)
    assert top is not None and top["ts"] == ts("2024-03-15 06:00") and top["pattern"] in ("IMP", "ABC")
    assert top_count_summary(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_decisions.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper.decisions'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/paper/decisions.py`**

```python
"""Out-of-sample extraction, decision records and the replay check (Plan 2 spec 4.6-4.7, 5.3).

A decision is (kind, ts, price, contracts, reason) plus its trade key (the position's entry_ts; the
signal's own ts for SIGNAL) and, for SIGNAL / stop moves, the levels it set:
  SIGNAL     ts = signal bar, price = entry_ref, contracts = queued size, reason = Signal.reason,
             levels = [stop, tp1, tp2]
  ENTRY      ts = fill bar, price = fill price, reason = setup
  TP1 / TP2 / STOP / EXIT   fills (EXIT reason = the trade's exit reason: TIME, LINEAGE, FLIP, BREAKER)
  BREAKEVEN / TRAIL         stop moves: price = new stop, contracts = contracts still open, levels = [old stop]
The END close of a position still open at the last bar is NOT a decision (it is an artefact of the
data ending). A run whose last closed bar is T knows every fill / stop move with ts <= T, but only
the signals with ts <= T - 1h (the engine never evaluates entries on the last bar): known_at()."""

from __future__ import annotations

from typing import Any, Callable, Sequence

from ewbot.engine.loop import EngineResult, Trade
from ewbot.paper.trace import EngineTrace
from ewbot.types import H1
from ewbot.waves.engine import WaveState

REL_TOL = 1e-9
DECISION_KINDS = ("SIGNAL", "ENTRY", "TP1", "TP2", "STOP", "EXIT", "BREAKEVEN", "TRAIL")
CAUSES = ("DATA_REVISION", "FINGERPRINT_CHANGE", "ENVIRONMENT_CHANGE", "UNEXPLAINED")  # in priority order
_ORDER = {k: i for i, k in enumerate(("EXIT", "ENTRY", "STOP", "TP1", "TP2", "BREAKEVEN", "TRAIL", "SIGNAL"))}
_COMPARED = ("kind", "ts", "trade_key", "contracts", "reason", "setup", "direction")


def split_result(res: EngineResult, trace: EngineTrace) -> tuple[list[Trade], Trade | None]:
    """(closed trades, the position still open at the last bar). The engine force-closes an open
    position at the last bar (exit_reason "END"); the trace's end_fill identifies it."""
    trades = list(res.trades)
    if trace.end_fill is None:
        return trades, None
    last = trades[-1]
    if last.exit_reason != "END" or last.entry_ts != trace.end_fill.trade_key:
        raise ValueError("the END-closed trade is not the last trade of the run")
    return trades[:-1], last


def paper_trades(closed: Sequence[Trade], boundary_ts: int) -> list[Trade]:
    """Closed trades that ENTERED after the boundary (holdout last bar or epoch start): a position
    already open at the boundary belongs to the earlier period and is never counted twice."""
    return [t for t in closed if t.entry_ts > boundary_ts]


def decision_id(kind: str, ts: int, trade_key: int) -> str:
    return f"{kind}:{int(ts)}:{int(trade_key)}"


def known_at(d: dict[str, Any]) -> int:
    """The last-bar ts from which a run can know decision d."""
    return int(d["ts"]) + (H1 if d["kind"] == "SIGNAL" else 0)


def _decision(
    kind: str, ts: int, price: float, contracts: int, reason: str, trade_key: int,
    levels: Sequence[float | None], setup: str, direction: int,
) -> dict[str, Any]:
    return {
        "id": decision_id(kind, ts, trade_key),
        "kind": kind,
        "ts": int(ts),
        "price": float(price),
        "contracts": int(contracts),
        "reason": reason,
        "trade_key": int(trade_key),
        "levels": [None if v is None else float(v) for v in levels],
        "setup": setup,
        "direction": int(direction),
    }


def build_decisions(res: EngineResult, trace: EngineTrace, trades: Sequence[Trade]) -> list[dict[str, Any]]:
    """Every decision of one traced run, ordered by (ts, kind). `trades` = closed + open trades."""
    if len(res.signals) != len(trace.orders):
        raise ValueError(f"{len(res.signals)} signals but {len(trace.orders)} queued entries in the trace")
    by_entry = {t.entry_ts: t for t in trades}
    out: list[dict[str, Any]] = []
    for sig, od in zip(res.signals, trace.orders, strict=True):
        if od.ts != sig.ts:
            raise ValueError(f"queued entry at {od.ts} does not match signal at {sig.ts}")
        out.append(
            _decision("SIGNAL", sig.ts, sig.entry_ref, od.contracts, sig.reason, sig.ts,
                      [sig.stop, sig.tp1, sig.tp2], sig.setup, sig.direction)
        )
    tp1_done: list[tuple[int, int, int]] = []  # (ts, trade_key, contracts)
    for f in trace.fills:
        t = by_entry[f.trade_key]
        reason = t.setup if f.kind == "ENTRY" else (t.exit_reason if f.kind == "EXIT" else f.kind)
        out.append(_decision(f.kind, f.ts, f.price, f.contracts, reason, f.trade_key, [], t.setup, t.direction))
        if f.kind == "TP1":
            tp1_done.append((f.ts, f.trade_key, f.contracts))
    for s in trace.stops:
        t = by_entry[s.trade_key]
        left = t.contracts - sum(n for ts, key, n in tp1_done if key == s.trade_key and ts <= s.ts)
        out.append(_decision(s.kind, s.ts, s.new, left, s.kind, s.trade_key, [s.old], t.setup, t.direction))
    out.sort(key=lambda d: (d["ts"], _ORDER[d["kind"]]))
    return out


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a == b or abs(a - b) <= REL_TOL * max(abs(a), abs(b))


def same_decision(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equal kind / ts / key / size / reason / setup / direction; prices and levels within 1e-9 relative."""
    if any(a[k] != b[k] for k in _COMPARED):
        return False
    if not _close(a["price"], b["price"]) or len(a["levels"]) != len(b["levels"]):
        return False
    return all(_close(x, y) for x, y in zip(a["levels"], b["levels"], strict=True))


def classify(
    recorded: dict[str, Any] | None, data_revised: bool, current_epoch_id: int, env_changed: bool = False
) -> str:
    """DATA_REVISION if a bar up to the decision changed; else FINGERPRINT_CHANGE if the decision
    was recorded under another epoch; else ENVIRONMENT_CHANGE if env_changed (see _env_changed: this
    is the first run in a new runtime environment and the run that recorded the decision, or should
    have, ran in another one); else UNEXPLAINED."""
    if data_revised:
        return "DATA_REVISION"
    if recorded is not None and int(recorded["epoch_id"]) != int(current_epoch_id):
        return "FINGERPRINT_CHANGE"
    if env_changed:
        return "ENVIRONMENT_CHANGE"
    return "UNEXPLAINED"


def _env_changed(recording: str | None, previous: str | None, current: str | None) -> bool:
    """The environment changed under this decision: the previous successful run ran in another
    environment than the current one (this is the first run in it) and so did the run that recorded
    the decision. Once a run in the current environment has rechecked a decision (every run rechecks
    them all), a later difference is not explained by the change. Unknown digests never count."""
    if recording is None or previous is None or current is None:
        return False
    return previous != current and recording != current


def verify_decisions(
    recorded: Sequence[dict[str, Any]],
    recomputed: Sequence[dict[str, Any]],
    epoch: dict[str, Any],
    revised_at: Callable[[dict[str, Any]], bool],
    appeared_revised: Callable[[dict[str, Any]], bool],
    *,
    appeared_after: int | None = None,
    env_sha256: str | None = None,
    appeared_env: Callable[[dict[str, Any]], str | None] | None = None,
    previous_env_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Replay check. Every recorded decision must reappear unchanged (else CHANGED / VANISHED); a
    recomputed decision of the current epoch that a previous run should already have known but did
    not record is APPEARED. revised_at(recorded) / appeared_revised(recomputed) say whether the bars
    up to that decision changed since it was (or should have been) recorded. The APPEARED window is
    known_at in (appeared_after, epoch through_ts]; appeared_after defaults to the epoch's started_ts
    (the runner passes where the epoch began recording, see runner._recording_floor).
    env_sha256 is the current run's environment digest; it is compared with a recorded decision's
    env_sha256 and, for APPEARED, with appeared_env(recomputed) - the environment digest of the run
    that should have recorded it - and with previous_env_sha256, the digest of the previous successful
    run: ENVIRONMENT_CHANGE needs both to differ from it (None: unknown, never an environment change)."""
    by_id = {d["id"]: d for d in recomputed}
    rec_ids = {d["id"] for d in recorded}
    found: list[dict[str, Any]] = []
    for d in recorded:
        r = by_id.get(d["id"])
        if r is not None and same_decision(d, r):
            continue
        kind = "VANISHED" if r is None else "CHANGED"
        moved = _env_changed(d.get("env_sha256"), previous_env_sha256, env_sha256)
        cause = classify(d, revised_at(d), epoch["id"], moved)
        found.append({"id": f"{kind}:{d['id']}", "type": kind, "decision_id": d["id"], "cause": cause,
                      "recorded": d, "recomputed": r})
    lo = int(epoch["started_ts"]) if appeared_after is None else int(appeared_after)
    hi = int(epoch["through_ts"])
    for r in recomputed:
        if r["id"] in rec_ids or not lo < known_at(r) <= hi:
            continue
        recording_env = None if appeared_env is None else appeared_env(r)
        moved = _env_changed(recording_env, previous_env_sha256, env_sha256)
        cause = classify(None, appeared_revised(r), epoch["id"], moved)
        found.append({"id": f"APPEARED:{r['id']}", "type": "APPEARED", "decision_id": r["id"], "cause": cause,
                      "recorded": None, "recomputed": r})
    return found


def decisions_to_record(
    recomputed: Sequence[dict[str, Any]], epoch: dict[str, Any], recorded_ids: set[str]
) -> list[dict[str, Any]]:
    """Decisions first known in this run: known_at after the epoch's through_ts, not yet recorded."""
    through = int(epoch["through_ts"])
    return [d for d in recomputed if known_at(d) > through and d["id"] not in recorded_ids]


def open_position_record(open_trade: Trade, trace: EngineTrace, epoch_start: int) -> dict[str, Any]:
    """The position the engine holds after the last bar (marked at the last close, exit costs included)."""
    key = open_trade.entry_ts
    tp1 = sum(f.contracts for f in trace.fills if f.trade_key == key and f.kind == "TP1")
    stops = [s for s in trace.stops if s.trade_key == key]
    snap = open_trade.snapshot or {}
    end = trace.end_fill
    return {
        "entry_ts": key,
        "setup": open_trade.setup,
        "direction": open_trade.direction,
        "contracts": open_trade.contracts,
        "remaining": open_trade.contracts - tp1,
        "entry_price": open_trade.entry_price,
        "stop0": open_trade.stop0,
        "stop": stops[-1].new if stops else open_trade.stop0,
        "tp1": open_trade.tp1,
        "tp2": open_trade.tp2,
        "tp1_done": tp1 > 0,
        "mark_ts": None if end is None else end.ts,
        "pnl_if_closed": open_trade.pnl,
        "r_if_closed": open_trade.r_multiple,
        "counted": key > epoch_start,
        "q_top": snap.get("q_top"),
        "s_rev": snap.get("s_rev"),
    }


def top_count_summary(ws: WaveState | None) -> dict[str, Any] | None:
    """The current top wave count after the last bar (report: "현재 1등 카운트")."""
    if ws is None or ws.top is None:
        return None
    c = ws.top
    return {
        "ts": ws.ts,
        "pattern": c.pattern,
        "d": c.d,
        "phase": c.phase,
        "q": c.q,
        "p": ws.probs[0] if ws.probs else None,
        "q_top": ws.q_top,
        "s_rev": ws.s_rev,
        "E_live": ws.E_live,
        "ready": ws.ready,
        "points": [[p.sub_ts, p.price] for p in c.points],
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_decisions.py -q
```

Expected: `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/decisions.py tests/paper/test_decisions.py
git commit -m "$(cat <<'EOF'
feat(paper): out-of-sample extraction, decision records and replay check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Telegram notifier, message formats, daily-summary timing and events

**Files:**
- Create: `src/ewbot/paper/notify.py`
- Test: `tests/paper/test_notify.py`

**Interfaces:**
- Consumes: `Ledger`, `new_ledger` (Task 2), `Trade`, `ewbot.strategy.base.blended_r(direction, entry, stop, tp1, tp2, tp1_frac) -> float`, `H1`, `requests`, `tests.paper.helpers.FakeSender`, `ts` (Task 3).
- Produces (`ewbot.paper.notify`): `NY = ZoneInfo("America/New_York")`, `DAILY_HOUR_NY = 9`, `CRITERIA_ATTEMPTS = 2`, `GAP_WARN_S = 10800`, `SEND_ATTEMPTS = 3`, `TELEGRAM_API`, `HEAD = "[ewbot paper]"`, `class SendError`, `Sender` protocol `(text) -> None`, `TelegramSender(token, chat_id, session=None, timeout=15.0)` (any failure is a `SendError`: `telegram HTTP <status>: <reply>` for a status other than 200 or a reply that is not a JSON object with `ok: true`, `telegram request failed: <type name>` for any exception while posting or reading the reply), `sender_from_env(env=None) -> TelegramSender | None`, `make_event(kind, ts, trade_key, text) -> dict` (`id, kind, text, attempts`), `deliver(pending, sent_ids, sender, attempts=3, sleep=time.sleep) -> tuple[list[dict], list[str], list[str]]` (still pending, newly sent ids, notes; a note keeps a `SendError`'s message, only the type name of other exceptions), `daily_summary_due(now_ts, last_date) -> str | None`, `last_ok_run_ts(runs) -> int | None`, `gap_seconds(now_ts, runs) -> int | None`, formats `entry_text(t, tp1_frac)`, `stop_move_text(dec, t)`, `close_text(t, count, counted)`, `daily_text(ny_date, position, equity, count, status, stale=None)` (with `stale`: header marked `기준 미갱신(stale)` and a line with the error), `epoch_text(epoch)`, `discrepancy_text(items)`, `data_hash_text(expected, got)`, `holdout_trades_text(expected, got)`, `criteria_error_text(ny_date, error)`, `gap_text(seconds, last_ts)`, `failure_text(stage, message, run_url)`, `position_line(pos)`, `decision_events(fresh, trades, open_key, epoch_start, count, cur_closed, tp1_frac) -> list[dict]`, `queue_events(ledger, events) -> list[str]`.

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_notify.py`**

```python
from __future__ import annotations

import pytest
import requests

from ewbot.engine.loop import Trade
from ewbot.paper.ledger import new_ledger
from ewbot.paper.notify import (
    GAP_WARN_S,
    SendError,
    TelegramSender,
    close_text,
    criteria_error_text,
    daily_summary_due,
    daily_text,
    decision_events,
    deliver,
    entry_text,
    failure_text,
    gap_seconds,
    make_event,
    position_line,
    queue_events,
    sender_from_env,
    stop_move_text,
)
from ewbot.types import H1
from tests.paper.helpers import FakeSender, ts

TOKEN = "123456:SECRET"


class _Resp:
    """`body` is the parsed JSON reply, or an exception that json() raises (a non-JSON reply)."""

    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, resp: _Resp | Exception) -> None:
        self.resp = resp
        self.posts: list[tuple[str, dict, float]] = []

    def post(self, url: str, json: dict, timeout: float) -> _Resp:
        self.posts.append((url, json, timeout))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _trade(**kw) -> Trade:
    base = dict(
        entry_ts=ts("2024-02-07 21:00"), exit_ts=ts("2024-02-08 22:00"), setup="W3", direction=1, contracts=19,
        entry_price=23_516.46, exit_price_avg=23_010.75, stop0=23_022.26, tp1=24_219.14, tp2=24_751.39,
        pnl=-123.4, fees=4.5, funding=0.2, r_multiple=-1.08, exit_reason="STOP",
        snapshot={"q_top": 0.6, "s_rev": 0.662, "counts": [{"pattern": "IMP", "phase": "W2"}]},
    )
    base.update(kw)
    return Trade(**base)


def test_telegram_sender_posts_plain_text():
    sess = _Session(_Resp(200, {"ok": True}))
    TelegramSender(TOKEN, "42", session=sess)("hello")
    url, payload, timeout = sess.posts[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True} and timeout > 0


@pytest.mark.parametrize("resp", [_Resp(429, {"ok": False}), _Resp(200, {"ok": False}), requests.ConnectionError(f"x {TOKEN}")])
def test_telegram_sender_raises_without_leaking_the_token(resp):
    with pytest.raises(SendError) as exc:
        TelegramSender(TOKEN, "42", session=_Session(resp))("hello")
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "resp, expected",
    [
        (_Resp(502, ValueError("<html>bad gateway</html>")), "telegram HTTP 502"),  # non-JSON reply
        (_Resp(200, ValueError("not json")), "telegram HTTP 200"),
        (_Resp(200, ["ok"]), "telegram HTTP 200"),  # JSON, but not an object
        (_Resp(200, None), "telegram HTTP 200"),
        (_Resp(200, RuntimeError(f"decoder broke on {TOKEN}")), "RuntimeError"),  # any other failure: type only
    ],
)
def test_telegram_sender_turns_odd_replies_into_send_errors(resp, expected):
    with pytest.raises(SendError) as exc:
        TelegramSender(TOKEN, "42", session=_Session(resp))("hello")
    assert expected in str(exc.value) and TOKEN not in str(exc.value) and exc.value.__cause__ is None


def test_deliver_notes_only_the_error_type_unless_it_is_a_send_error():
    def leaky(text: str) -> None:
        raise ConnectionError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed")

    _, _, notes = deliver(_events(1), [], leaky, sleep=lambda s: None)
    assert notes == ["send failed for ENTRY:1000:0: ConnectionError"]

    def rejected(text: str) -> None:
        raise SendError("telegram HTTP 429: too many requests")

    _, _, notes = deliver(_events(1), [], rejected, sleep=lambda s: None)
    assert notes == ["send failed for ENTRY:1000:0: telegram HTTP 429: too many requests"]


def test_sender_from_env():
    assert sender_from_env({}) is None
    assert sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN}) is None
    assert sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": " "}) is None
    assert isinstance(sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}), TelegramSender)


def _events(n: int) -> list[dict]:
    return [make_event("ENTRY", 1000 + i, i, f"msg {i}") for i in range(n)]


def test_deliver_without_a_token_skips_and_drops():
    pending, sent, notes = deliver(_events(2), [], None)
    assert pending == [] and sent == [] and "not configured" in notes[0]
    assert deliver([], [], None) == ([], [], [])


def test_deliver_retries_three_times_then_succeeds():
    sender, sleeps = FakeSender(fail=2), []
    pending, sent, notes = deliver(_events(2), [], sender, sleep=sleeps.append)
    assert pending == [] and sent == ["ENTRY:1000:0", "ENTRY:1001:1"] and notes == []
    assert sender.texts == ["msg 0", "msg 1"] and sleeps == [1.0, 2.0]


def test_deliver_keeps_the_failed_event_and_everything_after_it_pending():
    evs = _events(3)
    sender = FakeSender(fail=0)
    first = deliver(evs[:1], [], sender, sleep=lambda s: None)
    assert first[1] == ["ENTRY:1000:0"]
    sender.fail = 3
    pending, sent, notes = deliver(evs, ["ENTRY:1000:0"], sender, sleep=lambda s: None)
    assert sent == [] and [e["id"] for e in pending] == ["ENTRY:1001:1", "ENTRY:1002:2"]
    assert pending[0]["attempts"] == 1 and "send failed" in notes[0]
    pending, sent, _ = deliver(pending, ["ENTRY:1000:0"], sender, sleep=lambda s: None)  # next run: retried
    assert pending == [] and sent == ["ENTRY:1001:1", "ENTRY:1002:2"]
    assert sender.texts == ["msg 0", "msg 1", "msg 2"]  # each exactly once


@pytest.mark.parametrize(
    "now, last, expected",
    [
        ("2024-03-09 13:59", None, None),  # 08:59 EST
        ("2024-03-09 14:00", None, "2024-03-09"),  # 09:00 EST
        ("2024-03-10 12:59", "2024-03-09", None),  # DST starts: 08:59 EDT
        ("2024-03-10 13:00", "2024-03-09", "2024-03-10"),  # 09:00 EDT
        ("2024-03-10 15:00", "2024-03-10", None),  # once per New York day
        ("2024-03-11 03:00", "2024-03-10", None),  # 23:00 EDT is still 2024-03-10 in New York
        ("2024-11-03 13:30", "2024-11-02", None),  # DST ends: 08:30 EST
        ("2024-11-03 14:07", "2024-11-02", "2024-11-03"),  # 09:07 EST
        ("2024-07-01 13:07", "2024-06-30", "2024-07-01"),  # summer: 09:07 EDT
    ],
)
def test_daily_summary_first_run_after_nine_new_york(now, last, expected):
    assert daily_summary_due(ts(now), last) == expected


def test_gap_seconds_since_the_last_successful_run():
    runs = [{"ts": 1000, "status": "ok"}, {"ts": 5000, "status": "ok"}]
    assert gap_seconds(5000 + 3 * H1, runs) == 3 * H1 and not gap_seconds(5000 + 3 * H1, runs) > GAP_WARN_S
    assert gap_seconds(5000 + 3 * H1 + 1, runs) > GAP_WARN_S
    assert gap_seconds(9999, []) is None


def test_entry_close_stop_and_failure_formats():
    t = _trade()
    text = entry_text(t, 0.5)
    for part in ("신규 진입", "W3 롱", "2024-02-07 21:00 UTC", "23,516.46", "23,022.26", "24,219.14", "24,751.39",
                 "blended R", "IMP W2", "q 0.600", "S_rev 0.662", "19계약"):
        assert part in text
    text = close_text(t, {"total": 37, "required": 50}, counted=True)
    assert "청산 · STOP" in text and "-123.40" in text and "R -1.08" in text and "37/50" in text
    assert "카운트 제외" in close_text(t, {"total": 36, "required": 50}, counted=False)
    dec = {"kind": "BREAKEVEN", "ts": ts("2024-02-08 03:00"), "price": 23_516.46, "contracts": 9, "levels": [23_022.26]}
    text = stop_move_text(dec, t)
    assert "손절 이동 (본전)" in text and "23,022.26 → 23,516.46" in text and "잔여 9계약" in text
    assert "(추적)" in stop_move_text({**dec, "kind": "TRAIL"}, t)
    text = failure_text("data", "coinbase down", "https://github.com/o/r/actions/runs/1")
    assert "단계 data" in text and "coinbase down" in text and "actions/runs/1" in text
    assert "로그" not in failure_text("data", "x", None)
    assert daily_text("2024-02-08", "p", "e", "c", ["s1", "s2"]).splitlines()[1:] == ["p", "e", "c", "s1", "s2"]
    stale = daily_text("2024-02-08", "p", "e", "c", ["s1"], stale="RuntimeError: boom").splitlines()
    assert stale[0].endswith("일일 요약 2024-02-08 (뉴욕) · 기준 미갱신(stale)") and stale[1:4] == ["p", "e", "c"]
    assert "재계산 2회 실패" in stale[4] and "RuntimeError: boom" in stale[4] and stale[5] == "s1"
    text = criteria_error_text("2024-02-08", "RuntimeError: boom")
    assert "2024-02-08" in text and "다음 실행에서 1회 다시 계산" in text and "기준 미갱신(stale)" in text
    assert position_line(None) == "포지션: 없음"


def _dec(kind: str, ts_: int, key: int, **kw) -> dict:
    return {"id": f"{kind}:{ts_}:{key}", "kind": kind, "ts": ts_, "trade_key": key, "price": 1.0, "contracts": 1,
            "levels": [0.5], **kw}


def test_decision_events_entry_stop_moves_and_closes():
    a = _trade(entry_ts=100 * H1, exit_ts=110 * H1, contracts=4, exit_reason="TP2")
    b = _trade(entry_ts=120 * H1, exit_ts=125 * H1, exit_reason="END")  # still open
    fresh = [
        _dec("SIGNAL", 99 * H1, 99 * H1),
        _dec("ENTRY", 100 * H1, 100 * H1),
        _dec("TRAIL", 103 * H1, 100 * H1),
        _dec("TP1", 105 * H1, 100 * H1),
        _dec("BREAKEVEN", 105 * H1, 100 * H1),
        _dec("TP2", 110 * H1, 100 * H1),
        _dec("ENTRY", 120 * H1, 120 * H1),
    ]
    count = {"holdout": 36, "required": 50}
    evs = decision_events(fresh, {a.entry_ts: a, b.entry_ts: b}, b.entry_ts, 50 * H1, count, [a], 0.5)
    assert [e["id"] for e in evs] == [
        f"ENTRY:{100 * H1}:{100 * H1}", f"TRAIL:{103 * H1}:{100 * H1}", f"BREAKEVEN:{105 * H1}:{100 * H1}",
        f"CLOSE:{110 * H1}:{100 * H1}", f"ENTRY:{120 * H1}:{120 * H1}",
    ]
    assert "37/50" in evs[3]["text"]  # 36 holdout + this paper trade


def test_queue_events_never_duplicates():
    led = new_ledger("f", "c" * 40, {
        "sha256": "a", "params": [2.0, 0.7, 0.5], "holdout_start": 1, "data": {"last_bar_ts": 2, "sha256": "b"},
        "first_run": {"criteria": {"n_trades": {"A": 36}}},
    })
    evs = _events(3)
    led.sent_event_ids.append(evs[0]["id"])
    assert queue_events(led, evs + [evs[1]]) == [evs[1]["id"], evs[2]["id"]]
    assert queue_events(led, evs) == []
    assert [e["id"] for e in led.pending_events] == [evs[1]["id"], evs[2]["id"]]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_notify.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper.notify'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/paper/notify.py`**

```python
"""Telegram delivery and message formats (Plan 2 spec 6.3).

Secrets come from the environment (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; GitHub Secrets in the
workflow). Without them nothing is sent and the run still succeeds. Every message is an event with
id "kind:ts:trade_key"; an id already in the ledger's sent_event_ids is never sent again. A send is
tried 3 times; an event that still fails stays pending (with every event after it) for the next run."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import requests

from ewbot.engine.loop import Trade
from ewbot.paper.ledger import Ledger
from ewbot.strategy.base import blended_r
from ewbot.types import H1

NY = ZoneInfo("America/New_York")
DAILY_HOUR_NY = 9
CRITERIA_ATTEMPTS = 2  # criteria 1-2 recomputes per New York date: the daily-summary run and one retry
GAP_WARN_S = 3 * H1
SEND_ATTEMPTS = 3
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
HEAD = "[ewbot paper]"


class SendError(RuntimeError):
    """Telegram rejected or did not answer a sendMessage call."""


class Sender(Protocol):
    def __call__(self, text: str) -> None:
        """Send one plain-text message; raise on any failure."""


class TelegramSender:
    """POST sendMessage (plain text, no parse mode). Raises SendError on any failure: an HTTP status
    other than 200 or a reply that is not a JSON object with "ok": true (message: the status and the
    start of Telegram's reply), or any exception while posting or reading the reply (message: its
    type name only - the API URL holds the token)."""

    def __init__(self, token: str, chat_id: str, session: Any | None = None, timeout: float = 15.0) -> None:
        self._url = TELEGRAM_API.format(token=token)
        self._chat_id = chat_id
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    def __call__(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            resp = self._session.post(self._url, json=payload, timeout=self._timeout)
            status = int(resp.status_code)
            try:
                body = resp.json()
            except ValueError:  # not JSON (requests' JSONDecodeError is a ValueError)
                body = None
            ok = status == 200 and isinstance(body, dict) and bool(body.get("ok"))
            reply = "" if ok else str(resp.text)[:200]
        except Exception as exc:  # noqa: BLE001 - transport or reply errors: type name only, no token
            raise SendError(f"telegram request failed: {type(exc).__name__}") from None
        if not ok:
            raise SendError(f"telegram HTTP {status}: {reply}")


def sender_from_env(env: Mapping[str, str] | None = None) -> TelegramSender | None:
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("TELEGRAM_CHAT_ID", "").strip()
    return TelegramSender(token, chat) if token and chat else None


def make_event(kind: str, ts: int, trade_key: int | str, text: str) -> dict[str, Any]:
    return {"id": f"{kind}:{int(ts)}:{trade_key}", "kind": kind, "text": text, "attempts": 0}


def deliver(
    pending: Sequence[dict[str, Any]],
    sent_ids: Sequence[str],
    sender: Sender | None,
    attempts: int = SEND_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Send pending events in order. Returns (still pending, newly sent ids, error notes).
    No sender: the events are dropped unsent (not kept, so a later token does not flood the chat).
    Ids already sent are dropped silently. The first event that fails `attempts` times stops the
    batch: it and every later event stay pending, in order. The error note keeps a SendError's
    message (token-free by construction) and only the type name of any other exception."""
    if sender is None:
        return [], [], [f"telegram not configured: {len(pending)} event(s) not sent"] if pending else []
    done = set(sent_ids)
    newly: list[str] = []
    for i, ev in enumerate(pending):
        if ev["id"] in done:
            continue
        err: Exception | None = None
        for a in range(attempts):
            try:
                sender(ev["text"])
                err = None
                break
            except Exception as exc:  # noqa: BLE001 - any transport error is retried, then recorded
                err = exc
                if a < attempts - 1:
                    sleep(2.0**a)
        if err is not None:
            rest = [dict(e) for e in pending[i:] if e["id"] not in done]
            rest[0]["attempts"] = int(rest[0].get("attempts", 0)) + 1
            why = str(err) if isinstance(err, SendError) else type(err).__name__
            return rest, newly, [f"send failed for {ev['id']}: {why}"]
        newly.append(ev["id"])
        done.add(ev["id"])
    return [], newly, []


# ----------------------------------------------------------------------------- timing


def daily_summary_due(now_ts: int, last_date: str | None) -> str | None:
    """The New York date if this is the first run at/after 09:00 New York time that day, else None."""
    local = datetime.fromtimestamp(now_ts, NY)
    if local.hour < DAILY_HOUR_NY:
        return None
    day = local.date().isoformat()
    return None if day == last_date else day


def last_ok_run_ts(runs: Sequence[dict[str, Any]]) -> int | None:
    ok = [int(r["ts"]) for r in runs if r.get("status") == "ok"]
    return max(ok) if ok else None


def gap_seconds(now_ts: int, runs: Sequence[dict[str, Any]]) -> int | None:
    """Seconds since the last successful run, or None before the first one."""
    last = last_ok_run_ts(runs)
    return None if last is None else int(now_ts) - last


# ----------------------------------------------------------------------------- formats


def _px(x: float | None) -> str:
    return "-" if x is None else f"{x:,.2f}"


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _side(direction: int) -> str:
    return "롱" if direction > 0 else "숏"


def _num(x: Any) -> str:
    return "-" if x is None else f"{float(x):.3f}"


def position_line(pos: Mapping[str, Any] | None) -> str:
    """One line for ledger.open_position (daily summary and report)."""
    if pos is None:
        return "포지션: 없음"
    counted = "" if pos["counted"] else " (구간 시작 전 진입: 카운트 제외)"
    return (
        f"포지션: {pos['setup']} {_side(pos['direction'])} {pos['remaining']}/{pos['contracts']}계약 · "
        f"진입 {_px(pos['entry_price'])} ({_utc(pos['entry_ts'])}) · 손절 {_px(pos['stop'])} · "
        f"청산 시 손익 {pos['pnl_if_closed']:+,.2f}{counted}"
    )


def entry_text(t: Trade, tp1_frac: float) -> str:
    snap = t.snapshot or {}
    counts = snap.get("counts") or []
    top = counts[0] if counts else {}
    br = blended_r(t.direction, t.entry_price, t.stop0, t.tp1, t.tp2, tp1_frac)
    q = snap.get("q_top")
    s_rev = snap.get("s_rev")
    return "\n".join(
        [
            f"{HEAD} 신규 진입",
            f"{t.setup} {_side(t.direction)} · {_utc(t.entry_ts)}",
            f"진입 {_px(t.entry_price)} · 손절 {_px(t.stop0)} · {t.contracts}계약",
            f"TP1 {_px(t.tp1)} · TP2 {_px(t.tp2)} · blended R {br:.2f}",
            f"1등 카운트 {top.get('pattern', '?')} {top.get('phase', '?')} · q {_num(q)} · S_rev {_num(s_rev)}",
        ]
    )


def stop_move_text(dec: dict[str, Any], t: Trade) -> str:
    label = "본전" if dec["kind"] == "BREAKEVEN" else "추적"
    old = dec["levels"][0] if dec["levels"] else None
    return "\n".join(
        [
            f"{HEAD} 손절 이동 ({label})",
            f"{t.setup} {_side(t.direction)} · 진입 {_utc(t.entry_ts)}",
            f"손절 {_px(old)} → {_px(dec['price'])} · {_utc(dec['ts'])} 봉 마감 · 잔여 {dec['contracts']}계약",
        ]
    )


def close_text(t: Trade, count: Mapping[str, Any], counted: bool) -> str:
    tail = "" if counted else " (이 거래는 구간 시작 전 진입: 카운트 제외)"
    return "\n".join(
        [
            f"{HEAD} 청산 · {t.exit_reason}",
            f"{t.setup} {_side(t.direction)} · 진입 {_utc(t.entry_ts)} → 청산 {_utc(t.exit_ts)}",
            f"손익 {t.pnl:+,.2f} USD · R {t.r_multiple:+.2f}",
            f"표본 외 누적 {count['total']}/{count['required']}{tail}",
        ]
    )


def daily_text(
    ny_date: str, position: str, equity: str, count: str, status: Sequence[str], stale: str | None = None
) -> str:
    """stale: why criteria 1-2 could not be recomputed on this date (the status block then shows the
    previous result and the summary is marked 기준 미갱신(stale))."""
    head = f"{HEAD} 일일 요약 {ny_date} (뉴욕)"
    if stale is None:
        return "\n".join([head, position, equity, count, *status])
    note = f"실거래 전환 판정 1·2번 재계산 {CRITERIA_ATTEMPTS}회 실패: 이전 결과 표시 ({stale[:200]})"
    return "\n".join([f"{head} · 기준 미갱신(stale)", position, equity, count, note, *status])


def epoch_text(epoch: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 전략 지문 변경 · 구간 {epoch['id']} 시작 ({_utc(epoch['started_ts'])})",
            f"{epoch['reason']}",
            "표본 외 카운터를 0으로 리셋 (기준 지문과 다르면 실거래 전환 판정 대상 아님)",
        ]
    )


def discrepancy_text(items: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"{HEAD} 경고: 결정 불일치 {len(items)}건"]
    for d in items[:10]:
        lines.append(f"{d['type']} {d['decision_id']} · {d['cause']}")
    if len(items) > 10:
        lines.append(f"외 {len(items) - 10}건 더 (report.html 참고)")
    if any(d["cause"] == "ENVIRONMENT_CHANGE" for d in items):
        lines.append(
            "ENVIRONMENT_CHANGE: 실행 환경(Python·OS·numpy·pandas·pyarrow·uv.lock·실행기 이미지)이 바뀐 뒤 첫 실행이고 "
            "결정을 기록한 실행과도 환경이 다름 - 4번 기준 원인 불명 집계에서 제외"
        )
    return "\n".join(lines)


def data_hash_text(expected: str, got: str) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 홀드아웃 구간 데이터 해시 불일치",
            f"잠금 파일 {expected[:12]} · 이번 데이터 {got[:12]}",
            "실행은 계속함 (표본 외 카운트에는 영향 없음, 재현성 손상 기록)",
        ]
    )


def holdout_trades_text(expected: int, got: int) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 홀드아웃 재현 불일치",
            f"재계산한 홀드아웃 구간 A 거래 {got}건 · 잠금 파일 {expected}건",
            "실행은 계속함 (표본 외 카운트는 잠금 파일 값을 씀, 재현성 손상 기록)",
        ]
    )


def criteria_error_text(ny_date: str, error: str) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 실거래 전환 판정 1·2번 재계산 실패 ({ny_date} 뉴욕)",
            error[:300],
            "이전 결과를 유지하고 다음 실행에서 1회 다시 계산함 (다시 실패하면 일일 요약을 '기준 미갱신(stale)'으로 보냄)",
        ]
    )


def gap_text(seconds: int, last_ts: int) -> str:
    return f"{HEAD} 경고: 실행 공백 {seconds / H1:.1f}시간 (직전 성공 실행 {_utc(last_ts)})"


def failure_text(stage: str, message: str, run_url: str | None) -> str:
    lines = [f"{HEAD} 실패 · 단계 {stage}", message[:500]]
    if run_url:
        lines.append(f"로그: {run_url}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- events from decisions


def decision_events(
    fresh: Sequence[dict[str, Any]],
    trades: Mapping[int, Trade],
    open_key: int | None,
    epoch_start: int,
    count: Mapping[str, Any],
    cur_closed: Sequence[Trade],
    tp1_frac: float,
) -> list[dict[str, Any]]:
    """Events for the decisions first recorded in this run (spec 4.7): ENTRY for an entry fill,
    BREAKEVEN / TRAIL for a stop move, CLOSE for the fill that closed a trade. `trades` maps entry_ts
    to every trade of the run (open one included), `open_key` is the open trade's entry_ts,
    `cur_closed` the current epoch's closed paper trades (for the running N/50 of each close)."""
    out: list[dict[str, Any]] = []
    for d in fresh:
        t = trades.get(d["trade_key"])
        if t is None:
            continue  # SIGNAL: its ENTRY fill carries the notification
        if d["kind"] == "ENTRY":
            out.append(make_event("ENTRY", d["ts"], d["trade_key"], entry_text(t, tp1_frac)))
        elif d["kind"] in ("BREAKEVEN", "TRAIL"):
            out.append(make_event(d["kind"], d["ts"], d["trade_key"], stop_move_text(d, t)))
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT") and t.entry_ts != open_key and t.exit_ts == d["ts"]:
            counted = t.entry_ts > int(epoch_start)
            n_at = int(count["holdout"]) + sum(1 for x in cur_closed if x.exit_ts <= t.exit_ts)
            out.append(
                make_event("CLOSE", d["ts"], d["trade_key"],
                           close_text(t, {"total": n_at, "required": count["required"]}, counted))
            )
    return out


def queue_events(ledger: Ledger, events: Sequence[dict[str, Any]]) -> list[str]:
    """Append events to ledger.pending_events unless their id was already sent or queued (no
    duplicate notifications, also within one batch). Returns the newly queued ids."""
    seen = set(ledger.sent_event_ids) | {e["id"] for e in ledger.pending_events}
    queued: list[str] = []
    for ev in events:
        if ev["id"] not in seen:
            ledger.pending_events.append(dict(ev))
            seen.add(ev["id"])
            queued.append(ev["id"])
    return queued
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_notify.py -q
```

Expected: `27 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/notify.py tests/paper/test_notify.py
git commit -m "$(cat <<'EOF'
feat(paper): Telegram notifier, message formats and daily-summary timing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Go-live criteria status and the paper HTML report

**Files:**
- Create: `src/ewbot/paper/criteria.py`, `src/ewbot/paper/report.py`
- Test: `tests/paper/test_criteria_report.py`

**Interfaces:**
- Consumes: `ewbot.backtest.holdout.evaluate_criteria(cmp) -> dict` (`winners`, `max_dd_ok`, `n_trades_ok`, `n_trades`, `trade_count`, `random_control_evaluated`, `passing`, `paper_4_weeks`, `automatic_criteria_met`), `Ledger`, `current_epoch`, `golive_count`, `trade_from_record`, `ensure_epoch`, `new_ledger`, `replace_epoch_trades` (Task 2), `position_line` (Task 5), `compute_metrics(equity, trades, start_ts=None, end_ts=None) -> Metrics`, `write_report(path, title, sections) -> Path`, `aggregate_4h(df1h)`, `EquityPoint`, `Trade`.
- Produces (`ewbot.paper.criteria`): `PAPER_MIN_DAYS = 28`, `COVERAGE_MIN = 0.95`, `MAX_DD = 0.15`, `summarize_compare(cmp, start_ts, end_ts, epoch_id, computed_ts) -> dict` (`computed_ts, epoch_id, window, winners, max_dd_ok, random_control_evaluated, headline, random`), `run_coverage(runs, epoch_id, now_ts) -> dict`, `criterion4(ledger, now_ts) -> dict` (`first_ts`, `days`, `hours`, `covered`, `coverage`, `unexplained`, `environment` (ENVIRONMENT_CHANGE count, shown only), `met`), `golive_status(ledger, now_ts) -> dict` (`epoch_id, eligible, count, criteria_1_2, c1, c2, c3, c4, all_met`), `status_lines(st) -> list[str]`.
- Produces (`ewbot.paper.report`): `write_paper_report(path, ledger, status, equity, paper_closed, df1h, top_count, now_ts) -> Path` (atomic; the notes show the last run's runtime environment).

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_criteria_report.py`**

```python
from __future__ import annotations

import os

import pytest

from ewbot.backtest import report as bt_report
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.paper.criteria import (
    criterion4,
    golive_status,
    run_coverage,
    status_lines,
    summarize_compare,
)
from ewbot.paper.ledger import ensure_epoch, new_ledger, replace_epoch_trades
from ewbot.paper.report import write_paper_report
from ewbot.types import H1
from tests.paper.helpers import BOUNDARY, synth_frame

LOCK = {
    "sha256": "a" * 64, "params": [2.0, 0.5, 0.0], "holdout_start": 1_704_067_200,
    "data": {"last_bar_ts": BOUNDARY, "sha256": "b" * 64},
    "first_run": {"criteria": {"n_trades": {"A": 36, "HYBRID": 40}}},
}
T0 = BOUNDARY + 10 * H1
DAY = 86_400


def _m(sharpe, calmar, dd=0.05, n=40):
    return {"total_return": 0.1, "max_dd": dd, "sharpe": sharpe, "calmar": calmar, "n_trades": n}


def _cmp(a_sharpe=1.0, h_sharpe=0.4, a_dd=0.05):
    return {
        "comparison": {"A": _m(a_sharpe, 2.0, a_dd), "HYBRID": _m(h_sharpe, 2.0), "C": _m(0.5, 1.0),
                       "BH": _m(0.6, 1.5, 0.5), "EMA": _m(0.2, 0.3)},
        "random": {"runs": 2, "n_signals": 40, "dist": {"sharpe": {"mean": 0.1}, "calmar": {"mean": 0.2}}},
    }


def _trade(entry_ts: int, exit_ts: int) -> Trade:
    return Trade(entry_ts, exit_ts, "W3", 1, 2, 100.0, 101.0, 95.0, 105.0, 110.0, 2.0, 0.1, 0.0, 0.2, "TP2",
                 {"q_top": 0.7, "s_rev": 0.6, "counts": [{"pattern": "IMP", "d": 1, "phase": "W2", "q": 0.7,
                                                          "points": [[entry_ts - 40 * H1, 99.0], [entry_ts - 8 * H1, 103.0]]}]})


def _hourly_runs(start: int, hours: int, skip: set[int] = frozenset(), epoch_id: int = 1) -> list[dict]:
    return [{"ts": start + h * H1 + 420, "status": "ok", "epoch_id": epoch_id, "last_bar_ts": start + h * H1 - H1}
            for h in range(hours) if h not in skip]


def test_run_coverage_counts_distinct_hours():
    runs = _hourly_runs(T0, 100, skip={3, 4, 50})
    runs.append({**runs[0], "ts": runs[0]["ts"] + 60})  # a second run in the same hour counts once
    runs.append({**runs[1], "epoch_id": 2})  # another epoch is ignored
    cov = run_coverage(runs, 1, T0 + 99 * H1 + 600)
    assert cov["hours"] == 100 and cov["covered"] == 97 and cov["coverage"] == pytest.approx(0.97)
    assert run_coverage([], 1, T0)["coverage"] == 0.0


def test_criterion4_needs_28_days_95_percent_and_no_unexplained():
    led = new_ledger("fp", "c" * 40, LOCK)
    hours = 28 * 24 + 1
    led.runs = _hourly_runs(T0, hours, skip=set(range(10, 30)))
    now = T0 + (hours - 1) * H1 + 600
    c4 = criterion4(led, now)
    assert c4["days"] >= 28 and c4["coverage"] > 0.95 and c4["unexplained"] == 0 and c4["met"]
    assert not criterion4(led, T0 + 27 * DAY)["met"]  # too short
    led.discrepancies.append({"cause": "UNEXPLAINED", "epoch_id": 1})
    assert not criterion4(led, now)["met"]
    led.discrepancies[0]["cause"] = "DATA_REVISION"
    assert criterion4(led, now)["met"]
    led.discrepancies[0]["cause"] = "ENVIRONMENT_CHANGE"  # warned and shown, not counted (spec 5.3)
    c4 = criterion4(led, now)
    assert c4["met"] and c4["unexplained"] == 0 and c4["environment"] == 1
    line = status_lines(golive_status(led, now))[4]
    assert "원인 불명 불일치 0건" in line and "실행 환경 변경 불일치 1건(4번 집계 제외)" in line
    led.runs = _hourly_runs(T0, hours, skip=set(range(10, 60)))  # 50 missing hours: < 95%
    assert not criterion4(led, now)["met"]


def test_summarize_compare_and_status_lines():
    led = new_ledger("fp", "c" * 40, LOCK)
    st = golive_status(led, T0)
    assert st["c1"] is None and st["c2"] is None and st["c3"] is False and not st["all_met"]
    lines = status_lines(st)
    assert "미계산" in lines[1] and "3. 표본 외 거래 36/50 (홀드아웃 36 + 모의 0): 미충족" in lines[3]
    assert "실행 환경 변경" not in lines[4]  # shown only when there is one
    assert lines[-1] == "=> 실거래 전환 검토 불가"

    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    assert led.criteria["winners"] == ["A"] and led.criteria["max_dd_ok"] is True
    assert led.criteria["headline"]["A"]["sharpe"] == 1.0 and led.criteria["random"]["runs"] == 2
    st = golive_status(led, T0)
    assert st["c1"] is True and st["c2"] is True
    assert "1. 위험 대비 수익 A > C·BH·RANDOM (샤프·Calmar): 충족 [우위: A]" in status_lines(st)[1]
    assert "2. 최대 낙폭 <= 15% (A): 충족 [A 5.0%]" in status_lines(st)[2]
    led.criteria = summarize_compare(_cmp(a_dd=0.2), 1_704_067_200, T0, 1, T0)
    assert golive_status(led, T0)["c2"] is False  # A wins on risk-adjusted return but draws down 20%
    led.criteria = summarize_compare(_cmp(a_sharpe=0.3, h_sharpe=1.0), 1_704_067_200, T0, 1, T0)
    st = golive_status(led, T0)
    assert st["c1"] is False and st["c2"] is False  # HYBRID wins, but the paper runtime trades and counts A
    assert "[우위: HYBRID]" in status_lines(st)[1]


def test_all_four_criteria_met_reads_review_possible():
    led = new_ledger("fp", "c" * 40, LOCK)
    hours = 28 * 24 + 1
    led.runs = _hourly_runs(T0, hours)
    replace_epoch_trades(led, 1, [_trade(T0 + i * 3 * H1, T0 + i * 3 * H1 + H1) for i in range(14)])
    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    st = golive_status(led, T0 + (hours - 1) * H1 + 600)
    assert st["count"]["total"] == 50 and st["all_met"]
    assert status_lines(st)[-1] == "=> 실거래 전환 검토 가능 (자동 전환하지 않음)"


def test_other_epoch_is_not_eligible_and_old_criteria_do_not_count():
    led = new_ledger("fp", "c" * 40, LOCK)
    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    ensure_epoch(led, "edited", T0)
    st = golive_status(led, T0)
    assert not st["eligible"] and st["c1"] is None and st["count"]["total"] == 0
    assert "실거래 전환 판정 대상 아님" in status_lines(st)[1]


def _ledger_for_report():
    led = new_ledger("fp" * 32, "c" * 40, LOCK)
    replace_epoch_trades(led, 1, [_trade(T0, T0 + 5 * H1)])
    led.runs = [{"id": "r1", "ts": T0 + 30 * H1, "status": "ok", "epoch_id": 1, "last_bar_ts": T0 + 29 * H1,
                 "holdout_data_ok": False,
                 "env": {"python": "3.12.7", "numpy": "2.1.0", "pandas": "2.2.3", "pyarrow": "18.0.0",
                         "uv_lock_sha256": "f" * 64, "image_os": "ubuntu24", "image_version": "20261001.1.0"},
                 "env_sha256": "e" * 64}]
    led.discrepancies = [{"id": "CHANGED:ENTRY:1:1", "type": "CHANGED", "decision_id": "ENTRY:1:1",
                          "cause": "DATA_REVISION", "epoch_id": 1, "detected_ts": T0, "detected_run": "r1",
                          "recorded": None, "recomputed": None},
                         {"id": "CHANGED:EXIT:2:1", "type": "CHANGED", "decision_id": "EXIT:2:1",
                          "cause": "ENVIRONMENT_CHANGE", "epoch_id": 1, "detected_ts": T0, "detected_run": "r1",
                          "recorded": None, "recomputed": None}]
    led.open_position = {"entry_ts": T0 + 20 * H1, "setup": "C_END", "direction": -1, "contracts": 4, "remaining": 2,
                         "entry_price": 100.0, "stop": 101.0, "pnl_if_closed": -3.0, "counted": True}
    return led


def test_paper_report_sections(tmp_path):
    led = _ledger_for_report()
    led.criteria = summarize_compare(_cmp(), 1_704_067_200, T0, 1, T0)
    eq = [EquityPoint(BOUNDARY + i * H1, 10_000.0 + i) for i in range(-5, 40)]
    top = {"ts": T0, "pattern": "IMP", "d": 1, "phase": "W3", "q": 0.71, "q_top": 0.71, "s_rev": 0.4, "E_live": 21_000.0}
    path = write_paper_report(tmp_path / "report.html", led, golive_status(led, T0 + 40 * H1), eq,
                              [_trade(T0, T0 + 5 * H1)], synth_frame(), top, T0 + 40 * H1)
    html = path.read_text(encoding="utf-8")
    for part in ("실거래 전환 판정", "1. 위험 대비 수익", "4. 모의매매 재현", "현재 1등 카운트", "IMP d=1 W3",
                 "포지션: C_END 숏 2/4계약", "불일치 (재현성 손상 기록)", "Trades - epoch 1 (current) (1)",
                 "CHANGED:ENTRY:1:1", "DATA_REVISION", "ENVIRONMENT_CHANGE", "eq-chart", "dd-chart", "A paper",
                 "criteria 1-2 window", "실행 환경 eeeeeeeeeeee: Python 3.12.7 · numpy 2.1.0 · pandas 2.2.3 · "
                 "pyarrow 18.0.0 · uv.lock ffffffffffff · 이미지 ubuntu24 20261001.1.0"):
        assert part in html, part
    assert html.index("실거래 전환 판정") < html.index("eq-chart")  # the go-live block is at the top
    assert sorted(os.listdir(tmp_path)) == ["report.html"]


def test_paper_report_write_is_atomic(tmp_path, monkeypatch):
    led = _ledger_for_report()
    target = tmp_path / "report.html"
    target.write_text("previous", encoding="utf-8")

    def boom(path, title, sections):
        path.write_text("half", encoding="utf-8")
        raise RuntimeError("render failed")

    monkeypatch.setattr("ewbot.paper.report.write_report", boom)
    with pytest.raises(RuntimeError):
        write_paper_report(target, led, golive_status(led, T0), [], [], synth_frame(), None, T0)
    assert target.read_text(encoding="utf-8") == "previous" and sorted(os.listdir(tmp_path)) == ["report.html"]
    assert bt_report.write_report is not boom  # the shared backtest writer itself is untouched
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_criteria_report.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper.criteria'`.

- [ ] **Step 3: Write minimal implementation**

**File: `src/ewbot/paper/criteria.py`**

```python
"""Go-live status (Plan 1 spec 9.6 criteria 1-4, Plan 2 spec 5.3 / 5.5). Pure functions over the
ledger; the daily compare() that feeds criteria 1-2 runs in paper.runner.

The go-live candidate is A - the only strategy the paper runtime trades and counts (criteria 3 and 4
are about A's trades), so, as in backtest.holdout.evaluate_criteria's `passing`, one candidate must
meet all four. HYBRID's comparison is still computed and shown."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ewbot.backtest.holdout import evaluate_criteria
from ewbot.paper.ledger import GO_LIVE_STRATEGY, Ledger, current_epoch, golive_count
from ewbot.types import H1

PAPER_MIN_DAYS = 28
COVERAGE_MIN = 0.95
MAX_DD = 0.15
_HEADLINE = ("A", "HYBRID", "C", "BH", "EMA")
_KEYS = ("total_return", "max_dd", "sharpe", "calmar", "n_trades")


def summarize_compare(cmp: dict[str, Any], start_ts: int, end_ts: int, epoch_id: int, computed_ts: int) -> dict[str, Any]:
    """What the ledger keeps from one runner.compare() over [holdout start, last bar]."""
    crit = evaluate_criteria(cmp)
    comp = cmp["comparison"]
    rnd = cmp.get("random") or {}
    dist = rnd.get("dist") or {}
    return {
        "computed_ts": int(computed_ts),
        "epoch_id": int(epoch_id),
        "window": [int(start_ts), int(end_ts)],
        "winners": list(crit["winners"]),
        "max_dd_ok": bool(crit["max_dd_ok"]),
        "random_control_evaluated": bool(crit["random_control_evaluated"]),
        "headline": {n: {k: comp[n][k] for k in _KEYS if k in comp[n]} for n in _HEADLINE if n in comp},
        "random": {
            "runs": int(rnd.get("runs", 0)),
            "sharpe_mean": (dist.get("sharpe") or {}).get("mean"),
            "calmar_mean": (dist.get("calmar") or {}).get("mean"),
        },
    }


def run_coverage(runs: list[dict[str, Any]], epoch_id: int, now_ts: int) -> dict[str, Any]:
    """Hours since the epoch's first successful run (inclusive) that have a successful run."""
    ok = [int(r["ts"]) for r in runs if r.get("status") == "ok" and int(r["epoch_id"]) == int(epoch_id)]
    if not ok:
        return {"first_ts": None, "days": 0.0, "hours": 0, "covered": 0, "coverage": 0.0}
    first = min(ok)
    lo, hi = first // H1, int(now_ts) // H1
    covered = len({t // H1 for t in ok if lo <= t // H1 <= hi})
    hours = hi - lo + 1
    return {
        "first_ts": first,
        "days": (int(now_ts) - first) / 86400.0,
        "hours": hours,
        "covered": covered,
        "coverage": covered / hours,
    }


def criterion4(ledger: Ledger, now_ts: int) -> dict[str, Any]:
    """>= 28 days of paper runs, 0 UNEXPLAINED discrepancies in the epoch, >= 95% hourly coverage.
    ENVIRONMENT_CHANGE discrepancies are counted for display only (spec 5.3: not UNEXPLAINED)."""
    cur = current_epoch(ledger)
    cov = run_coverage(ledger.runs, cur["id"], now_ts)
    causes = [d["cause"] for d in ledger.discrepancies if int(d["epoch_id"]) == int(cur["id"])]
    unexplained = causes.count("UNEXPLAINED")
    met = cov["days"] >= PAPER_MIN_DAYS and unexplained == 0 and cov["coverage"] >= COVERAGE_MIN
    return {**cov, "unexplained": unexplained, "environment": causes.count("ENVIRONMENT_CHANGE"), "met": met}


def golive_status(ledger: Ledger, now_ts: int) -> dict[str, Any]:
    cur = current_epoch(ledger)
    count = golive_count(ledger)
    crit = ledger.criteria if ledger.criteria and int(ledger.criteria["epoch_id"]) == int(cur["id"]) else None
    c1 = c2 = None
    if crit is not None and count["eligible"]:
        c1 = GO_LIVE_STRATEGY in crit["winners"]
        c2 = c1 and crit["headline"][GO_LIVE_STRATEGY]["max_dd"] <= MAX_DD
    c3 = count["eligible"] and count["total"] >= count["required"]
    c4 = criterion4(ledger, now_ts)
    all_met = bool(count["eligible"] and c1 and c2 and c3 and c4["met"])
    return {"epoch_id": cur["id"], "eligible": count["eligible"], "count": count, "criteria_1_2": crit,
            "c1": c1, "c2": c2, "c3": c3, "c4": c4, "all_met": all_met}


def _mark(v: bool | None) -> str:
    return "미계산" if v is None else ("충족" if v else "미충족")


def _day(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")


def status_lines(st: dict[str, Any]) -> list[str]:
    """Korean status block shown at the top of report.html and in the daily summary."""
    count, crit, c4 = st["count"], st["criteria_1_2"], st["c4"]
    lines = ["실거래 전환 판정 (Plan 1 명세 §9.6)"]
    if not st["eligible"]:
        lines.append(f"현재 구간 {st['epoch_id']}의 전략 지문이 기준값(64e628a)과 다름: 실거래 전환 판정 대상 아님")
    when = f" ({_day(crit['computed_ts'])} 계산)" if crit else " (다음 일일 요약 실행에서 계산)"
    who = ", ".join(crit["winners"]) if crit and crit["winners"] else "-"
    lines.append(
        f"1. 위험 대비 수익 {GO_LIVE_STRATEGY} > C·BH·RANDOM (샤프·Calmar): {_mark(st['c1'])} [우위: {who}]{when}"
    )
    dd = f" [{GO_LIVE_STRATEGY} {crit['headline'][GO_LIVE_STRATEGY]['max_dd'] * 100:.1f}%]" if crit else ""
    lines.append(f"2. 최대 낙폭 <= {MAX_DD:.0%} ({GO_LIVE_STRATEGY}): {_mark(st['c2'])}{dd}")
    lines.append(
        f"3. 표본 외 거래 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 {count['paper']}): "
        f"{_mark(st['c3'])}"
    )
    env = f", 실행 환경 변경 불일치 {c4['environment']}건(4번 집계 제외)" if c4.get("environment") else ""
    lines.append(
        f"4. 모의매매 재현: {_mark(c4['met'])} (운영 {c4['days']:.1f}/{PAPER_MIN_DAYS}일, 커버리지 "
        f"{c4['coverage'] * 100:.1f}%, 원인 불명 불일치 {c4['unexplained']}건{env})"
    )
    lines.append("=> 실거래 전환 검토 가능 (자동 전환하지 않음)" if st["all_met"] else "=> 실거래 전환 검토 불가")
    return lines
```

**File: `src/ewbot/paper/report.py`**

```python
"""Paper-trading HTML report (Plan 2 spec 6.2), built on backtest.report.write_report.

Top of the page (notes): the go-live status block, epochs, holdout data integrity, the open position
and the current top count. Then the out-of-sample equity / drawdown (bars after the holdout's last
bar), its metrics, the trade tables with per-trade snapshot charts and the discrepancy list."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.report import write_report
from ewbot.data.candles import aggregate_4h
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.paper.criteria import status_lines
from ewbot.paper.ledger import Ledger, current_epoch, trade_from_record
from ewbot.paper.notify import position_line
from ewbot.types import H1

CANDLE_LOOKBACK_S = 45 * 86400  # 4h context before the paper window for the snapshot charts


def _utc(ts: int | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _top_note(top: dict[str, Any] | None) -> str:
    if top is None:
        return "현재 1등 카운트: 없음"
    return (
        f"현재 1등 카운트 ({_utc(top['ts'])}): {top['pattern']} d={top['d']} {top['phase']} · q {top['q']:.3f} · "
        f"q_top {top['q_top']:.3f} · S_rev {top['s_rev']:.3f} · E_live {top['E_live']:,.2f}"
    )


def _env_note(run: dict[str, Any] | None) -> str:
    env = (run or {}).get("env")
    if not env:
        return "실행 환경: -"
    lock = (env.get("uv_lock_sha256") or "-")[:12]
    image = f"{env.get('image_os') or '-'} {env.get('image_version') or '-'}"
    return (
        f"실행 환경 {str(run.get('env_sha256', ''))[:12]}: Python {env.get('python')} · numpy {env.get('numpy')} · "
        f"pandas {env.get('pandas')} · pyarrow {env.get('pyarrow')} · uv.lock {lock} · 이미지 {image}"
    )


def write_paper_report(
    path: Path,
    ledger: Ledger,
    status: dict[str, Any],
    equity: Sequence[EquityPoint],
    paper_closed: Sequence[Trade],
    df1h: pd.DataFrame,
    top_count: dict[str, Any] | None,
    now_ts: int,
) -> Path:
    """Write report.html atomically (temp file + os.replace). `equity` / `paper_closed` come from
    this run's recompute; the window starts right after the holdout's last bar."""
    path = Path(path)
    boundary = int(ledger.holdout["last_bar_ts"])
    start = boundary + H1
    cur = current_epoch(ledger)
    last_run = ledger.runs[-1] if ledger.runs else None
    notes = status_lines(status) + [
        f"생성 {_utc(now_ts)} · 마지막 봉 {_utc(last_run['last_bar_ts']) if last_run else '-'}",
        f"현재 구간 {cur['id']} (지문 {cur['fingerprint'][:12]}, 시작 {_utc(cur['started_ts'])}) · 기준 지문 "
        f"{ledger.baseline['fingerprint'][:12]} @ {ledger.baseline['commit'][:7]}",
        "홀드아웃 데이터 해시: "
        + ("일치" if last_run is None or last_run.get("holdout_data_ok", True) else "불일치 (재현성 손상 기록)"),
        _env_note(last_run),
        position_line(ledger.open_position),
        _top_note(top_count),
        f"표본 외 구간: {_utc(start)} 이후 (홀드아웃 마지막 봉 {_utc(boundary)})",
    ]
    window_eq = [p for p in equity if p.ts > boundary]
    sections: dict[str, Any] = {"notes": notes}
    if len(window_eq) >= 2:
        sections["equity"] = {"A paper": window_eq}
        sections["comparison"] = {"A paper": compute_metrics(equity, paper_closed, start, None)}
    trades: dict[str, list[Trade]] = {}
    for ep in reversed(ledger.epochs):
        rows = [trade_from_record(t) for t in ledger.trades if t["epoch_id"] == ep["id"]]
        label = "current" if ep["id"] == cur["id"] else "frozen"
        trades[f"epoch {ep['id']} ({label})"] = rows
    sections["trades"] = trades
    lo = boundary - CANDLE_LOOKBACK_S
    d4 = aggregate_4h(df1h[df1h["ts"] >= lo])
    sections["candles4h"] = {"ts": d4["ts"].astype("int64").tolist(), "close": d4["close"].astype("float64").tolist()}
    crit = status.get("criteria_1_2")
    if crit:
        rows = [{"strategy": n, **v} for n, v in crit["headline"].items()]
        rows.append({"strategy": f"RANDOM x{crit['random']['runs']} mean", "sharpe": crit["random"]["sharpe_mean"],
                     "calmar": crit["random"]["calmar_mean"]})
        sections["criteria 1-2 window " + f"{_utc(crit['window'][0])} .. {_utc(crit['window'][1])}"] = rows
    sections["discrepancies"] = [
        {"id": d["id"], "type": d["type"], "cause": d["cause"], "epoch": d["epoch_id"],
         "detected": _utc(d["detected_ts"]), "run": d["detected_run"]}
        for d in ledger.discrepancies
    ] or "none"
    sections["epochs"] = [
        {"id": e["id"], "fingerprint": e["fingerprint"][:12], "started": _utc(e["started_ts"]),
         "through": _utc(e["through_ts"]), "reason": e["reason"]}
        for e in ledger.epochs
    ]
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        write_report(tmp, f"ewbot paper - {_utc(now_ts)}", sections)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_criteria_report.py -q
```

Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/criteria.py src/ewbot/paper/report.py tests/paper/test_criteria_report.py
git commit -m "$(cat <<'EOF'
feat(paper): go-live criteria status and paper HTML report

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Paper runner orchestration

**Files:**
- Create: `src/ewbot/paper/environment.py`, `src/ewbot/paper/runner.py`
- Test: `tests/paper/test_environment.py`, `tests/paper/test_runner.py`

**Interfaces:**
- Consumes: every `ewbot.paper` module above; `ewbot.backtest.holdout.DEFAULT_LOCK_PATH`, `data_fingerprint`, `holdout_config`, `lock_digest`; `ewbot.backtest.runner.run_backtest`, `compare(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1, *, allow_holdout=False) -> dict`; `ewbot.backtest.walkforward.HOLDOUT_START`, `grid_combos()`; `ewbot.data.candles.fill_gaps`, `load_1h`; `ewbot.data.coinbase_history.update_cache(product, cache_dir)`.
- Produces (`ewbot.paper.runner`): `PRODUCT = "BTC-USD"`, `FETCH_ATTEMPTS = 3`, `class PaperRunError(RuntimeError)` (`.stage`, `.message`), `default_fetch(cache_dir) -> Path`, `@dataclass PaperEnv(ledger_dir, lock_path=DEFAULT_LOCK_PATH, cache_dir=Path("data/cache"), fetch=default_fetch, sender=None, clock=_wall_clock, fingerprint=current_fingerprint, baseline=baseline_fingerprint, random_runs=100, workers=max(1, os.cpu_count() or 1), sleep=time.sleep, fetch_backoff_s=10.0, environment=runtime_environment)` (`_wall_clock() -> int` = `int(time.time())`), `@dataclass(frozen=True) RunOutcome(run_id, last_bar_ts, epoch_id, new_decisions, new_discrepancies, events, sent, pending, notes, status)`, `precheck_lock(lock_path) -> dict` (also requires `lock["sha256"] == lock_digest(BotConfig(), params, random_runs, code_sha256)`), `run_paper(env) -> RunOutcome`.
- Produces (`ewbot.paper.environment`): `PACKAGES = ("numpy", "pandas", "pyarrow")`, `DIGEST_EXCLUDED = ("image_version",)`, `runtime_environment(repo_root=None, environ=None) -> dict` (`python, system, machine, numpy, pandas, pyarrow, uv_lock_sha256, image_os, image_version`), `environment_digest(env) -> str` (sha256 of the sorted JSON of `env` without the `DIGEST_EXCLUDED` keys).
- Run record in `ledger.runs`: `id, ts, status, last_bar_ts, data_sha256, holdout_data_ok, holdout_trades_recomputed, epoch_id, paper_trades, count_total, new_decisions, new_discrepancies, env, env_sha256, warnings, notify`, plus `criteria_recompute: {ny_date, ok}` in a run that recomputed criteria 1–2. Each new decision is stamped `run_id, data_sha256, epoch_id, env_sha256`.
- Failure stages: `precheck`, `ledger`, `fingerprint`, `data`, `recompute`, `events`, `write`, `notify`. Every step runs in a named `_stage`, so an unexpected exception surfaces as `PaperRunError(stage)`; the `notify` stage keeps only the exception type (the Telegram token is part of the API URL) and does not chain the cause. The daily criteria 1-2 recompute is the one step that never fails the run: an error there is a warning (`criteria_error_text`, once per New York date), the previous `ledger.criteria` stays and the next run retries it once (no daily summary yet); if that retry fails too, the daily summary goes out marked `기준 미갱신(stale)` (`daily_text(..., stale=error)`) with the previous result and the next try is on the next New York date.
- Epoch boundary: the run that creates a new epoch records the decisions first known since the previous epoch's `through_ts` (`_recording_floor`), and passes that floor to `verify_decisions(..., appeared_after=floor)`. `ledger.json` is bound to the whole `holdout_summary(lock)`, not only its sha256 and last bar. A recomputed holdout A trade count different from the lock's `n_trades["A"]` warns once per distinct value (`holdout_trades_text`) and the run continues.

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_environment.py`**

```python
from __future__ import annotations

import hashlib
import platform

import numpy as np
import pandas as pd
import pyarrow

from ewbot.paper.environment import environment_digest, runtime_environment


def test_runtime_environment_records_versions_lock_hash_and_runner_image(tmp_path):
    (tmp_path / "uv.lock").write_bytes(b"version = 1\n")
    env = runtime_environment(tmp_path, {"ImageOS": "ubuntu24", "ImageVersion": "20261001.1.0", "OTHER": "x"})
    assert env == {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "pyarrow": pyarrow.__version__,
        "uv_lock_sha256": hashlib.sha256(b"version = 1\n").hexdigest(),
        "image_os": "ubuntu24",
        "image_version": "20261001.1.0",
    }


def test_runtime_environment_off_actions_and_without_a_lock_file(tmp_path):
    env = runtime_environment(tmp_path, {})
    assert env["image_os"] is None and env["image_version"] is None and env["uv_lock_sha256"] is None
    assert runtime_environment()["uv_lock_sha256"] is not None  # the repository's own uv.lock


def test_environment_digest_is_order_independent_and_sensitive_to_every_compared_value():
    a = {"python": "3.12.7", "system": "Linux", "machine": "x86_64", "numpy": "2.1.0", "image_os": None}
    assert environment_digest(a) == environment_digest(dict(reversed(list(a.items()))))
    assert len(environment_digest(a)) == 64
    for key, value in (("python", "3.12.8"), ("system", "Darwin"), ("machine", "arm64"), ("numpy", "2.1.1"),
                       ("image_os", "ubuntu24")):
        assert environment_digest({**a, key: value}) != environment_digest(a), key


def test_environment_digest_ignores_the_runner_image_version_but_not_the_image_os():
    a = {"python": "3.12.13", "numpy": "2.1.0", "image_os": "ubuntu24", "image_version": "20261001.1.0"}
    assert environment_digest({**a, "image_version": "20261008.1.0"}) == environment_digest(a)
    assert environment_digest({k: v for k, v in a.items() if k != "image_version"}) == environment_digest(a)
    assert environment_digest({**a, "numpy": "2.1.1"}) != environment_digest(a)
    assert environment_digest({**a, "image_os": "ubuntu22"}) != environment_digest(a)


def test_the_platform_is_part_of_the_digest_of_a_recorded_environment(tmp_path):
    env = runtime_environment(tmp_path, {"ImageOS": "ubuntu24"})
    assert (env["system"], env["machine"]) == (platform.system(), platform.machine())
    other = "Linux" if env["system"] != "Linux" else "Darwin"
    assert environment_digest({**env, "system": other}) != environment_digest(env)
    assert environment_digest({**env, "machine": env["machine"] + "-other"}) != environment_digest(env)
```

**File: `tests/paper/test_runner.py`**

```python
from __future__ import annotations

import json
import os

import pytest

from ewbot.paper import runner
from ewbot.paper.criteria import criterion4
from ewbot.paper.decisions import decision_id
from ewbot.paper.environment import environment_digest
from ewbot.paper.ledger import LEDGER_FILE, REPORT_FILE, RUNS_LOG, load_ledger
from ewbot.paper.runner import PaperEnv, PaperRunError, run_paper
from ewbot.types import H1
from tests.paper.helpers import (
    BOUNDARY,
    Clock,
    FakeFetch,
    FakeSender,
    commit_lock,
    git,
    make_lock,
    synth_frame,
    ts,
)

FIRST = ts("2024-02-10 12:07")  # 07:07 New York: no daily summary
ENV_A = {"python": "3.12.7", "numpy": "2.1.0", "pandas": "2.2.3", "pyarrow": "18.0.0", "uv_lock_sha256": "a" * 64,
         "image_os": "ubuntu24", "image_version": "20261001.1.0"}


@pytest.fixture(scope="module")
def df():
    return synth_frame()


@pytest.fixture
def make_env(tmp_path, df):
    def make(lock: dict | None = None, now: int = FIRST, sender: FakeSender | None = None, **kw) -> PaperEnv:
        lock_path = commit_lock(tmp_path / "repo", make_lock(df) if lock is None else lock)
        clock = Clock(now)
        defaults = dict(
            ledger_dir=tmp_path / "paper", lock_path=lock_path, cache_dir=tmp_path / "cache",
            fetch=FakeFetch(df, clock), sender=FakeSender() if sender is None else sender, clock=clock,
            fingerprint=lambda: "fp-base", baseline=lambda: ("fp-base", "c" * 40), random_runs=2, workers=1,
            sleep=lambda s: None, environment=lambda: dict(ENV_A),
        )
        defaults.update(kw)
        return PaperEnv(**defaults)

    return make


def test_first_run_writes_ledger_report_log_and_notifies(make_env):
    env = make_env()
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert sorted(os.listdir(env.ledger_dir)) == [LEDGER_FILE, REPORT_FILE, RUNS_LOG]
    assert [e["id"] for e in led.epochs] == [1] and led.epochs[0]["through_ts"] == ts("2024-02-10 11:00")
    assert [t["entry_ts"] for t in led.trades] == [ts("2024-01-12 15:00"), ts("2024-02-07 21:00")]
    assert all(t["entry_ts"] > BOUNDARY and t["epoch_id"] == 1 for t in led.trades)
    assert led.open_position is None
    assert {d["kind"] for d in led.decisions} == {"SIGNAL", "ENTRY", "TRAIL", "EXIT", "STOP"}
    assert all(d["run_id"] == out.run_id == "20240210T120700Z" and d["epoch_id"] == 1 for d in led.decisions)
    assert min(d["ts"] for d in led.decisions) == ts("2024-01-12 14:00")  # SIGNAL bar of the first paper trade
    run = led.runs[0]
    assert run["status"] == "ok" and run["holdout_data_ok"] is True and run["last_bar_ts"] == ts("2024-02-10 11:00")
    assert run["env"] == ENV_A and run["env_sha256"] == environment_digest(ENV_A)
    assert all(d["env_sha256"] == run["env_sha256"] for d in led.decisions)  # stamped by the recording run
    assert run["holdout_trades_recomputed"] == 2 and run["count_total"] == 4  # 2 holdout + 2 paper
    texts = env.sender.texts
    assert sum("신규 진입" in t for t in texts) == 2 and sum("청산" in t for t in texts) == 2
    assert sum("손절 이동 (추적)" in t for t in texts) == 3
    assert len(led.sent_event_ids) == len(texts) == len(set(led.sent_event_ids)) and led.pending_events == []
    assert (env.ledger_dir / RUNS_LOG).read_text().count("\n") == 1
    assert "실거래 전환 판정" in (env.ledger_dir / REPORT_FILE).read_text(encoding="utf-8")
    assert out.status["count"]["total"] == 4 and out.new_decisions == len(led.decisions)


def test_rerun_in_the_same_hour_changes_nothing_but_the_run_log(make_env):
    env = make_env()
    run_paper(env)
    before = load_ledger(env.ledger_dir)
    env.clock.now += 60
    out = run_paper(env)
    after = load_ledger(env.ledger_dir)
    assert out.new_decisions == 0 and out.events == [] and out.new_discrepancies == 0
    assert after.decisions == before.decisions and after.trades == before.trades
    assert after.sent_event_ids == before.sent_event_ids and len(after.runs) == 2


def test_new_bars_add_only_new_decisions_and_events(make_env):
    env = make_env()
    run_paper(env)
    n_texts = len(env.sender.texts)
    env.clock.now = ts("2024-02-16 03:07")  # the 02-16 02:00 short entry has filled; 22:07 New York
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out.new_decisions == 2  # its SIGNAL (01:00) and ENTRY (02:00)
    new = env.sender.texts[n_texts:]
    assert len(new) == 3 and "신규 진입" in new[0] and "W3 숏" in new[0]
    assert "실행 공백 135.0시간" in new[1]  # 5.6 days since the previous successful run
    assert "일일 요약 2024-02-15 (뉴욕)" in new[2]  # first run after 09:00 New York on 02-15
    assert led.open_position["entry_ts"] == ts("2024-02-16 02:00") and led.open_position["counted"] is True


def _modify(path):
    data = json.loads(path.read_text())
    data["runs"].append({"note": "edited"})
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    "case", ["missing", "untracked", "modified", "staged", "grid", "start", "incomplete", "digest", "random_runs", "code"]
)
def test_precheck_refuses_and_writes_nothing(make_env, df, tmp_path, case):
    lock = make_lock(df)
    if case == "grid":
        lock["params"] = [2.2, 0.5, 0.0]
    if case == "start":
        lock["holdout_start"] = lock["holdout_start"] + H1
    if case == "incomplete":
        del lock["first_run"]
    if case == "digest":  # sha256 is not lock_digest(BotConfig(), params, random_runs, code_sha256)
        lock["sha256"] = "d" * 64
    if case == "random_runs":  # the holdout ran another random-control size than the digest says
        lock["random_runs"] = 99
    if case == "code":
        del lock["code_sha256"]
    env = make_env(lock=lock)
    path = env.lock_path
    if case == "missing":
        git(path.parent, "rm", "-q", "holdout.lock")
    elif case == "untracked":
        git(path.parent, "rm", "-q", "--cached", "holdout.lock")
        git(path.parent, "commit", "-q", "-m", "untrack")
    elif case == "modified":
        _modify(path)
    elif case == "staged":
        _modify(path)
        git(path.parent, "add", "holdout.lock")
    with pytest.raises(PaperRunError) as exc:
        run_paper(env)
    assert exc.value.stage == "precheck"
    assert not env.ledger_dir.exists() and env.fetch.calls == 0 and env.sender.texts == []


def test_fetch_is_retried_three_times(make_env, df):
    sleeps: list[float] = []
    env = make_env(sleep=sleeps.append)
    env.fetch.fail = 2
    run_paper(env)
    assert env.fetch.calls == 3 and sleeps == [10.0, 20.0]

    env2 = make_env(sleep=lambda s: None)
    env2.ledger_dir = env2.ledger_dir.parent / "paper2"
    env2.fetch.fail = 3
    with pytest.raises(PaperRunError) as exc:
        run_paper(env2)
    assert exc.value.stage == "data" and env2.fetch.calls == 3
    assert not (env2.ledger_dir / LEDGER_FILE).exists()


def test_ledger_of_another_holdout_is_refused(make_env, df):
    env = make_env()
    run_paper(env)
    other = make_env(lock=make_lock(df, code_sha256="e" * 64))  # a valid lock with another digest
    with pytest.raises(PaperRunError, match="different holdout lock"):
        run_paper(other)


def test_precheck_names_the_lock_digest_mismatch(make_env, df):
    env = make_env(lock=make_lock(df, sha256="d" * 64))
    with pytest.raises(PaperRunError, match="lock_digest") as exc:
        run_paper(env)
    assert exc.value.stage == "precheck"


def test_corrupt_ledger_stops_at_the_ledger_stage(make_env):
    env = make_env()
    env.ledger_dir.mkdir(parents=True)
    (env.ledger_dir / LEDGER_FILE).write_text("{oops", encoding="utf-8")
    with pytest.raises(PaperRunError) as exc:
        run_paper(env)
    assert exc.value.stage == "ledger"


def test_fingerprint_change_opens_epoch_two_and_resets_the_count(make_env):
    env = make_env()
    run_paper(env)
    env.fingerprint = lambda: "fp-edited"
    env.clock.now += H1
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert [e["id"] for e in led.epochs] == [1, 2] and led.epochs[1]["started_ts"] == ts("2024-02-10 12:00")
    assert out.status["count"] == {"epoch_id": 2, "eligible": False, "holdout": 0, "paper": 0, "total": 0, "required": 50}
    assert [t["epoch_id"] for t in led.trades] == [1, 1]  # epoch 1 trades kept
    assert "전략 지문 변경" in env.sender.texts[-1] and "구간 2" in env.sender.texts[-1]
    assert led.discrepancies == []  # same code, same data: every recorded decision reproduces


def test_telegram_failure_keeps_events_for_the_next_run(make_env):
    sender = FakeSender(fail=3)
    env = make_env(sender=sender)
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out.sent == [] and len(led.pending_events) == len(out.events) > 0
    assert led.runs[-1]["notify"]["pending"] == len(out.events)
    env.clock.now += 60
    out2 = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out2.sent == out.events and led.pending_events == []
    assert len(sender.texts) == len(set(sender.texts)) == len(out.events)


def test_without_telegram_secrets_the_run_succeeds_and_sends_nothing(make_env):
    env = make_env(sender=None)
    env.sender = None
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert "not configured" in out.notes[0] and led.pending_events == [] and led.sent_event_ids == []


def test_gap_over_three_hours_warns(make_env):
    env = make_env()
    run_paper(env)
    env.clock.now += 3 * H1
    run_paper(env)
    assert not any("실행 공백" in t for t in env.sender.texts)
    env.clock.now += 3 * H1 + 60
    run_paper(env)
    assert "실행 공백 3.0시간" in env.sender.texts[-1]


def test_daily_summary_once_after_nine_new_york_with_criteria(make_env):
    env = make_env(now=ts("2024-02-12 14:07"))  # 09:07 EST
    run_paper(env)
    led = load_ledger(env.ledger_dir)
    daily = [t for t in env.sender.texts if "일일 요약 2024-02-12" in t]
    assert len(daily) == 1 and "실거래 전환 판정" in daily[0] and "표본 외 누적 4/50" in daily[0]
    assert led.daily_summary_date == "2024-02-12" and led.criteria["epoch_id"] == 1
    assert led.criteria["random"]["runs"] == 2 and led.criteria["window"][1] == ts("2024-02-12 14:00")
    env.clock.now += H1
    run_paper(env)
    assert sum("일일 요약" in t for t in env.sender.texts) == 1


def test_holdout_data_hash_mismatch_warns_and_continues(make_env, df):
    lock = make_lock(df)
    lock["data"]["sha256"] = "0" * 64
    env = make_env(lock=lock)
    run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert led.runs[-1]["holdout_data_ok"] is False
    assert any("홀드아웃 구간 데이터 해시 불일치" in t for t in env.sender.texts)
    env.clock.now += H1
    run_paper(env)
    assert sum("데이터 해시 불일치" in t for t in env.sender.texts) == 1  # one warning per distinct hash


def test_new_epoch_records_the_decisions_known_since_the_previous_run(make_env):
    env = make_env(now=ts("2024-02-16 02:07"))  # last bar 01:00: the 01:00 short signal is not known yet
    run_paper(env)
    n_texts = len(env.sender.texts)
    env.fingerprint = lambda: "fp-edited"
    env.clock.now = ts("2024-02-16 03:07")  # last bar 02:00: that signal and its entry fill, seen by the new code
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert out.epoch_id == 2 and out.new_decisions == 2
    assert [(d["kind"], d["ts"], d["epoch_id"]) for d in led.decisions if d["run_id"] == out.run_id] == [
        ("SIGNAL", ts("2024-02-16 01:00"), 2), ("ENTRY", ts("2024-02-16 02:00"), 2)]
    assert led.epochs[1]["started_ts"] == led.epochs[1]["through_ts"] == ts("2024-02-16 02:00")
    for t in ("2024-02-16 04:07", "2024-02-16 05:07"):
        env.clock.now = ts(t)
        assert run_paper(env).epoch_id == 2
    entries = [t for t in env.sender.texts[n_texts:] if "신규 진입" in t]
    assert len(entries) == 1 and "W3 숏" in entries[0]
    led = load_ledger(env.ledger_dir)
    assert led.discrepancies == [] and len(led.sent_event_ids) == len(set(led.sent_event_ids)) == len(env.sender.texts)
    # the replay check of epoch 2 covers what its first run recorded: a lost record shows up as APPEARED
    sig = f"SIGNAL:{ts('2024-02-16 01:00')}:{ts('2024-02-16 01:00')}"
    path = env.ledger_dir / LEDGER_FILE
    data = json.loads(path.read_text())
    data["decisions"] = [d for d in data["decisions"] if d["id"] != sig]
    path.write_text(json.dumps(data))
    env.clock.now = ts("2024-02-16 06:07")
    run_paper(env)
    found = load_ledger(env.ledger_dir).discrepancies
    assert [(d["type"], d["decision_id"], d["cause"]) for d in found] == [("APPEARED", sig, "UNEXPLAINED")]


@pytest.mark.parametrize("env_changed", [True, False])
def test_a_discrepancy_after_an_environment_change_is_not_unexplained(make_env, env_changed):
    box = {"env": dict(ENV_A)}
    env = make_env(environment=lambda: dict(box["env"]))
    run_paper(env)
    path = env.ledger_dir / LEDGER_FILE
    data = json.loads(path.read_text())
    changed = next(d for d in data["decisions"] if d["kind"] == "ENTRY")
    changed["price"] += 1.0  # as if the recording run had computed another fill price
    lost = next(d for d in data["decisions"] if d["kind"] == "EXIT")
    data["decisions"].remove(lost)  # as if the recording run had not seen this exit
    path.write_text(json.dumps(data))
    if env_changed:
        box["env"] = {**ENV_A, "numpy": "2.1.1"}  # a dependency version moved under the frozen lock
    env.clock.now += H1
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    cause = "ENVIRONMENT_CHANGE" if env_changed else "UNEXPLAINED"
    assert [(d["type"], d["decision_id"], d["cause"]) for d in led.discrepancies] == [
        ("CHANGED", changed["id"], cause), ("APPEARED", lost["id"], cause)]
    assert out.new_discrepancies == 2 and led.runs[-1]["env_sha256"] == environment_digest(box["env"])
    warning = env.sender.texts[-1]
    assert "결정 불일치 2건" in warning and cause in warning
    assert ("4번 기준 원인 불명 집계에서 제외" in warning) is env_changed
    c4 = criterion4(led, env.clock.now)
    assert (c4["unexplained"], c4["environment"]) == ((0, 2) if env_changed else (2, 0))


def test_an_environment_change_explains_only_the_first_run_in_the_new_environment(make_env):
    box = {"env": dict(ENV_A)}
    env = make_env(environment=lambda: dict(box["env"]))
    run_paper(env)  # environment A records the decisions
    box["env"] = {**ENV_A, "numpy": "2.1.1"}
    env.clock.now += H1
    run_paper(env)  # environment B reproduces every one of them
    path = env.ledger_dir / LEDGER_FILE
    data = json.loads(path.read_text())
    assert data["discrepancies"] == []
    entry = next(d for d in data["decisions"] if d["kind"] == "ENTRY")
    lost = next(d for d in data["decisions"] if d["kind"] == "EXIT")
    assert entry["env_sha256"] == lost["env_sha256"] == environment_digest(ENV_A)
    key = entry["trade_key"] + 1
    ghost = {**entry, "id": decision_id("ENTRY", entry["ts"], key), "trade_key": key}  # a record nothing recomputes
    entry["price"] += 1.0
    data["decisions"].remove(lost)
    data["decisions"].append(ghost)
    path.write_text(json.dumps(data))
    env.clock.now += H1
    run_paper(env)  # environment B again: it already reproduced them, so its change explains nothing
    led = load_ledger(env.ledger_dir)
    assert [(d["type"], d["decision_id"], d["cause"]) for d in led.discrepancies] == [
        ("CHANGED", entry["id"], "UNEXPLAINED"), ("VANISHED", ghost["id"], "UNEXPLAINED"),
        ("APPEARED", lost["id"], "UNEXPLAINED")]
    assert "4번 기준 원인 불명 집계에서 제외" not in env.sender.texts[-1]
    c4 = criterion4(led, env.clock.now)
    assert (c4["unexplained"], c4["environment"]) == (3, 0)


RETRY_NOTE = "criteria 1-2 not recomputed (retried next run): RuntimeError: compare blew up"


def _broken_compare(calls: list[int]):
    def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("compare blew up")

    return broken


def test_criteria_failure_warns_once_and_is_retried_by_the_next_run(make_env, monkeypatch):
    env = make_env(now=ts("2024-02-12 14:07"))  # 09:07 EST: daily summary with criteria 1-2
    run_paper(env)
    real, calls = runner.compare, []
    monkeypatch.setattr(runner, "compare", _broken_compare(calls))
    env.clock.now = ts("2024-02-13 14:07")
    out = run_paper(env)
    led = load_ledger(env.ledger_dir)
    run = led.runs[-1]
    assert run["status"] == "ok" and run["warnings"] == [RETRY_NOTE] and RETRY_NOTE in out.notes
    assert run["criteria_recompute"] == {"ny_date": "2024-02-13", "ok": False}
    assert led.daily_summary_date == "2024-02-12" and not any("일일 요약 2024-02-13" in t for t in env.sender.texts)
    warned = [t for t in env.sender.texts if "1·2번 재계산 실패" in t]
    assert len(warned) == 1 and "2024-02-13" in warned[0] and "compare blew up" in warned[0]

    monkeypatch.setattr(runner, "compare", real)
    env.clock.now = ts("2024-02-13 15:07")  # the one retry succeeds: a normal daily summary
    run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert led.daily_summary_date == "2024-02-13" and led.criteria["computed_ts"] == ts("2024-02-13 15:07")
    daily = [t for t in env.sender.texts if "일일 요약 2024-02-13" in t]
    assert len(daily) == 1 and "stale" not in daily[0] and led.runs[-1]["warnings"] == []
    assert led.runs[-1]["criteria_recompute"] == {"ny_date": "2024-02-13", "ok": True} and len(calls) == 1


def test_second_criteria_failure_sends_a_stale_daily_summary_and_stops_retrying_that_date(make_env, monkeypatch):
    env = make_env(now=ts("2024-02-12 14:07"))
    run_paper(env)
    before = load_ledger(env.ledger_dir).criteria
    calls: list[int] = []
    monkeypatch.setattr(runner, "compare", _broken_compare(calls))
    for t in ("2024-02-13 14:07", "2024-02-13 15:07", "2024-02-13 16:07"):
        env.clock.now = ts(t)
        run_paper(env)
    led = load_ledger(env.ledger_dir)
    assert len(calls) == 2  # the daily run and one retry; 16:07 does not try again
    assert led.criteria == before and led.daily_summary_date == "2024-02-13"
    assert [r["warnings"] for r in led.runs[1:]] == [
        [RETRY_NOTE],
        ["criteria 1-2 not recomputed (attempt 2/2 on 2024-02-13 failed: daily summary sent stale, next try on "
         "the next New York date): RuntimeError: compare blew up"],
        [],
    ]
    assert sum("1·2번 재계산 실패" in t for t in env.sender.texts) == 1  # one warning per New York date
    daily = [t for t in env.sender.texts if "일일 요약 2024-02-13" in t]
    assert len(daily) == 1 and "기준 미갱신(stale)" in daily[0] and "compare blew up" in daily[0]
    assert "2024-02-12 계산" in daily[0]  # the previous criteria 1-2 result is what the summary shows
    assert (env.ledger_dir / RUNS_LOG).read_text().count("\n") == 4
    env.clock.now = ts("2024-02-14 14:07")  # the next New York date tries again
    run_paper(env)
    assert len(calls) == 3 and load_ledger(env.ledger_dir).runs[-1]["warnings"] == [RETRY_NOTE]


def test_holdout_trade_count_mismatch_warns_once(make_env, df):
    env = make_env(lock=make_lock(df, n_a=3))  # the lock says 3 A trades, the recompute finds 2
    out = run_paper(env)
    note = "holdout trades recomputed 2 != lock 3"
    assert load_ledger(env.ledger_dir).runs[-1]["warnings"] == [note] and note in out.notes
    env.clock.now += H1
    run_paper(env)
    warned = [t for t in env.sender.texts if "홀드아웃 재현 불일치" in t]
    assert len(warned) == 1 and "A 거래 2건 · 잠금 파일 3건" in warned[0]


@pytest.mark.parametrize("key", ["params", "data_sha256", "n_trades"])
def test_ledger_is_bound_to_the_whole_holdout_summary(make_env, df, key):
    env = make_env()
    run_paper(env)
    before = {p.name: p.read_bytes() for p in env.ledger_dir.iterdir()}
    lock = make_lock(df, params=(2.0, 0.7, 0.5)) if key == "params" else make_lock(df, n_a=3 if key == "n_trades" else 2)
    if key == "data_sha256":
        lock["data"]["sha256"] = "0" * 64
    other = make_env(lock=lock)  # same last bar (data_sha256 / n_trades: same lock sha256 too) as the ledger's
    with pytest.raises(PaperRunError, match=f"different holdout lock .*{key}") as exc:
        run_paper(other)
    assert exc.value.stage == "precheck" and other.fetch.calls == 0 and other.sender.texts == []
    assert {p.name: p.read_bytes() for p in env.ledger_dir.iterdir()} == before


def test_unexpected_errors_name_their_stage_and_withhold_secret_details(make_env, monkeypatch):
    env = make_env()

    def leaky(*args, **kwargs):
        raise RuntimeError("POST https://api.telegram.org/bot123:SECRET/sendMessage failed")

    monkeypatch.setattr(runner, "deliver", leaky)
    with pytest.raises(PaperRunError) as exc:
        run_paper(env)
    assert exc.value.stage == "notify" and "SECRET" not in str(exc.value) and exc.value.__cause__ is None
    assert "RuntimeError" in exc.value.message

    def no_git():
        raise OSError("git not found")

    fresh = make_env(baseline=no_git)
    fresh.ledger_dir = fresh.ledger_dir.parent / "paper2"
    with pytest.raises(PaperRunError) as exc:
        run_paper(fresh)
    assert exc.value.stage == "fingerprint" and "git not found" in exc.value.message
    assert not (fresh.ledger_dir / LEDGER_FILE).exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_environment.py tests/paper/test_runner.py -q
```

Expected: 2 collection errors, `ModuleNotFoundError: No module named 'ewbot.paper.environment'` and `No module named 'ewbot.paper.runner'`.

- [ ] **Step 3: Write minimal implementation**

`runner.py` is the only non-holdout caller of `allow_holdout=True` (`run_backtest` for the recompute, `compare` for the daily criteria); Task 10 adds the static guard.

**File: `src/ewbot/paper/environment.py`**

```python
"""Runtime environment of a paper run (Plan 2 spec 5.3, ENVIRONMENT_CHANGE; spec 8 freezes it).

The environment is what can change a recompute without touching the strategy code: the Python
version, the operating system and machine architecture (platform.system() / platform.machine(): fixed
on the pinned runner, another platform brings another libm), the numpy / pandas / pyarrow versions, the
sha256 of uv.lock and the GitHub Actions runner image (the ImageOS / ImageVersion variables the hosted
runners set; None elsewhere). Every run records all of it; every decision carries the digest of the
environment of the run that recorded it.

The digest compares the Python (full patch), system, machine, numpy, pandas and pyarrow versions, the
uv.lock sha256 and ImageOS. ImageVersion is recorded but left out of the digest: GitHub refreshes the
ubuntu-24.04 image weekly, so comparing it would turn every later UNEXPLAINED discrepancy into
ENVIRONMENT_CHANGE."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from ewbot.paper.fingerprint import PROJECT_ROOT

PACKAGES = ("numpy", "pandas", "pyarrow")
DIGEST_EXCLUDED = ("image_version",)  # recorded in every run, not compared


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def runtime_environment(repo_root: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """{"python", "system", "machine", "numpy", "pandas", "pyarrow", "uv_lock_sha256", "image_os",
    "image_version"}."""
    root = PROJECT_ROOT if repo_root is None else Path(repo_root)
    environ = os.environ if environ is None else environ
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        **{name: _version(name) for name in PACKAGES},
        "uv_lock_sha256": _sha256_of(root / "uv.lock"),
        "image_os": environ.get("ImageOS"),
        "image_version": environ.get("ImageVersion"),
    }


def environment_digest(env: Mapping[str, Any]) -> str:
    """sha256 of the sorted, compact JSON of an environment record, without the DIGEST_EXCLUDED keys."""
    compared = {key: value for key, value in env.items() if key not in DIGEST_EXCLUDED}
    blob = json.dumps(compared, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

**File: `src/ewbot/paper/runner.py`**

```python
"""One `ewbot paper run` (Plan 2 spec 4): precheck -> data -> fingerprint/epoch -> full recompute ->
out-of-sample extraction -> replay check -> ledger / report / runs.log -> Telegram.

Inputs are frozen: the params come from holdout.lock only, the rest of the configuration is the
BotConfig defaults (conservative risk, breakers on, Plan 1 costs) - holdout_config(BotConfig(), params),
exactly how the holdout ran; the precheck refuses a lock whose sha256 is not
lock_digest(BotConfig(), params, random_runs, code_sha256), i.e. whose configuration is not the one this
run would use. The recompute starts trading at the holdout start, so it reproduces the holdout's A run
and continues it; trades entering after the holdout's last bar are paper trades. Every run records its
runtime environment (paper.environment); decisions carry the digest of the environment that recorded
them, for the ENVIRONMENT_CHANGE cause of the replay check (only in the first run after the environment
changed: the previous successful run's digest is compared too).

Every step runs inside a named stage: any failure raises PaperRunError(stage). The one exception is
the daily criteria 1-2 recompute, which only warns (the hourly run must not depend on it): the next run
retries it once; if that fails too, the daily summary goes out marked stale with the previous result
and the next try is on the next New York date (CRITERIA_ATTEMPTS per date).

This module is the only non-holdout caller of allow_holdout=True (a static test enforces it)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, data_fingerprint, holdout_config, lock_digest
from ewbot.backtest.runner import compare, run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.config import BotConfig
from ewbot.data.candles import fill_gaps, load_1h
from ewbot.data.coinbase_history import update_cache
from ewbot.paper.criteria import golive_status, status_lines, summarize_compare
from ewbot.paper.decisions import (
    build_decisions,
    decisions_to_record,
    known_at,
    open_position_record,
    paper_trades,
    split_result,
    top_count_summary,
    verify_decisions,
)
from ewbot.paper.environment import environment_digest, runtime_environment
from ewbot.paper.fingerprint import FingerprintError, baseline_fingerprint, current_fingerprint
from ewbot.paper.ledger import (
    GO_LIVE_STRATEGY,
    REPORT_FILE,
    RUNS_LOG,
    Ledger,
    LedgerError,
    append_log_line,
    ensure_epoch,
    golive_count,
    holdout_summary,
    load_ledger,
    new_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    CRITERIA_ATTEMPTS,
    GAP_WARN_S,
    NY,
    Sender,
    criteria_error_text,
    daily_summary_due,
    daily_text,
    data_hash_text,
    decision_events,
    deliver,
    discrepancy_text,
    epoch_text,
    gap_seconds,
    gap_text,
    holdout_trades_text,
    last_ok_run_ts,
    make_event,
    position_line,
    queue_events,
)
from ewbot.paper.report import write_paper_report
from ewbot.paper.trace import traced_engine
from ewbot.types import H1

PRODUCT = "BTC-USD"
FETCH_ATTEMPTS = 3


class PaperRunError(RuntimeError):
    """A run failed at `stage` (named in the failure alert). Before the write stage nothing has been
    written; the workflow commits nothing after a failed run either way."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def default_fetch(cache_dir: Path) -> Path:
    return update_cache(PRODUCT, Path(cache_dir))


def _wall_clock() -> int:
    return int(time.time())


@dataclass
class PaperEnv:
    """Everything a run touches. Production uses the defaults; tests inject fakes (no network)."""

    ledger_dir: Path
    lock_path: Path = DEFAULT_LOCK_PATH
    cache_dir: Path = Path("data/cache")
    fetch: Callable[[Path], Path] = default_fetch
    sender: Sender | None = None
    clock: Callable[[], int] = _wall_clock
    fingerprint: Callable[[], str] = current_fingerprint
    baseline: Callable[[], tuple[str, str]] = baseline_fingerprint
    random_runs: int = 100
    workers: int = field(default_factory=lambda: max(1, os.cpu_count() or 1))
    sleep: Callable[[float], None] = time.sleep
    fetch_backoff_s: float = 10.0
    environment: Callable[[], dict[str, Any]] = runtime_environment


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    last_bar_ts: int
    epoch_id: int
    new_decisions: int
    new_discrepancies: int
    events: list[str]
    sent: list[str]
    pending: int
    notes: list[str]
    status: dict[str, Any]


@contextmanager
def _stage(name: str, *, detail: bool = True) -> Iterator[None]:
    """Any failure inside becomes PaperRunError(name). detail=False is for the Telegram stage, whose
    exceptions may carry the bot token (it is part of the API URL): only the exception type is kept
    and the original exception is not chained."""
    try:
        yield
    except PaperRunError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        if detail:
            raise PaperRunError(name, f"{type(exc).__name__}: {exc}") from exc
        raise PaperRunError(name, f"{type(exc).__name__} (details withheld: may contain the Telegram token)") from None


# ----------------------------------------------------------------------------- precheck


def _git_ok(cwd: Path, *args: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def precheck_lock(lock_path: Path) -> dict[str, Any]:
    """holdout.lock exists, is committed (in HEAD), has no staged or unstaged change, its params are
    a walk-forward grid combo, its holdout start is HOLDOUT_START and its sha256 equals
    lock_digest(BotConfig(), params, random_runs, code_sha256): the full configuration this run uses
    (BotConfig defaults with the lock's params), the grid and the holdout start are the ones the
    holdout ran with. Returns the parsed lock."""
    p = Path(lock_path)
    if not p.is_file():
        raise PaperRunError("precheck", f"{p} not found: the paper runtime needs the committed holdout lock")
    if not _git_ok(p.parent, "cat-file", "-e", f"HEAD:./{p.name}"):
        raise PaperRunError("precheck", f"{p} is not committed to git")
    if not _git_ok(p.parent, "diff", "--quiet", "HEAD", "--", p.name):
        raise PaperRunError("precheck", f"{p} differs from the committed version")
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
        params = tuple(float(x) for x in lock["params"])
        int(lock["data"]["last_bar_ts"])
        str(lock["data"]["sha256"])
        str(lock["sha256"])
        int(lock["first_run"]["criteria"]["n_trades"]["A"])
        holdout_start = int(lock["holdout_start"])
        random_runs = int(lock["random_runs"])
        code_sha = str(lock["code_sha256"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PaperRunError("precheck", f"{p} is not a completed holdout lock: {exc!r}") from exc
    if params not in grid_combos():
        raise PaperRunError("precheck", f"lock params {list(params)} are not a walk-forward grid combo")
    if holdout_start != HOLDOUT_START:
        raise PaperRunError("precheck", f"lock holdout_start {holdout_start} != {HOLDOUT_START}")
    digest = lock_digest(BotConfig(), params, random_runs, code_sha)
    if digest != lock["sha256"]:
        raise PaperRunError(
            "precheck",
            f"lock sha256 {str(lock['sha256'])[:12]} != lock_digest(BotConfig(), params, random_runs, code_sha256) "
            f"{digest[:12]}: this run's configuration is not the one the holdout ran with",
        )
    return lock


def _open_ledger(env: PaperEnv, ledger_dir: Path, lock: dict[str, Any]) -> Ledger:
    """ledger.json, which must have been started from this very lock (its whole holdout summary), or
    a new ledger whose epoch 1 carries the baseline fingerprint."""
    with _stage("ledger"):
        ledger_dir.mkdir(parents=True, exist_ok=True)
        try:
            ledger = load_ledger(ledger_dir)
        except LedgerError as exc:
            raise PaperRunError("ledger", str(exc)) from exc
        if ledger is not None:
            bound = holdout_summary(lock)
            diff = sorted(k for k in bound.keys() | ledger.holdout.keys() if ledger.holdout.get(k) != bound.get(k))
            if diff:
                raise PaperRunError(
                    "precheck", f"ledger.json was started from a different holdout lock (differs in: {', '.join(diff)})"
                )
            return ledger
    with _stage("fingerprint"):
        try:
            fp0, commit = env.baseline()
        except FingerprintError as exc:
            raise PaperRunError("fingerprint", f"baseline at the holdout commit: {exc}") from exc
        return new_ledger(fp0, commit, lock)


def _load_data(env: PaperEnv) -> pd.DataFrame:
    last: Exception | None = None
    path: Path | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            path = Path(env.fetch(Path(env.cache_dir)))
            break
        except Exception as exc:  # noqa: BLE001 - network / API errors are retried
            last = exc
            if attempt < FETCH_ATTEMPTS - 1:
                env.sleep(env.fetch_backoff_s * 2**attempt)
    if path is None:
        raise PaperRunError("data", f"candle update failed {FETCH_ATTEMPTS} times: {type(last).__name__}: {last}")
    with _stage("data"):
        raw = load_1h(path)
        if raw.empty:
            raise PaperRunError("data", f"{path} has no bars")
        return fill_gaps(raw)


class _PrefixHash:
    """data_fingerprint of every bar with ts <= t (memoised per t)."""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self._memo: dict[int, str] = {}

    def __call__(self, ts: int) -> str:
        ts = int(ts)
        if ts not in self._memo:
            self._memo[ts] = data_fingerprint(self._df, 0, ts)
        return self._memo[ts]


def _recording_floor(ledger: Ledger) -> int:
    """known_at bound (exclusive) of the current epoch's decisions: epoch 1 records after the
    holdout's last bar; a later epoch from where the previous one stopped recording (its through_ts,
    frozen since), so the decisions first known by the run that opens the epoch belong to it."""
    if len(ledger.epochs) == 1:
        return int(ledger.epochs[0]["started_ts"])
    return int(ledger.epochs[-2]["through_ts"])


def _ny9_ts(ny_date: str) -> int:
    y, m, d = (int(x) for x in ny_date.split("-"))
    return int(datetime(y, m, d, 9, tzinfo=NY).timestamp())


def _criteria_failures(runs: list[dict[str, Any]], ny_date: str) -> int:
    """Failed criteria 1-2 recomputes recorded for this New York date."""
    return sum(1 for r in runs if r.get("criteria_recompute") == {"ny_date": ny_date, "ok": False})


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


# ----------------------------------------------------------------------------- run


def run_paper(env: PaperEnv) -> RunOutcome:
    with _stage("precheck"):
        now = int(env.clock())
        run_id = _run_id(now)
        lock = precheck_lock(env.lock_path)
        cfg = holdout_config(BotConfig(), tuple(float(p) for p in lock["params"]))
        ledger_dir = Path(env.ledger_dir)
        run_env = dict(env.environment())
        env_sha = environment_digest(run_env)

    ledger = _open_ledger(env, ledger_dir, lock)
    with _stage("ledger"):
        hs = ledger.holdout
        gap = gap_seconds(now, ledger.runs)
        prev_ok = last_ok_run_ts(ledger.runs)

    df = _load_data(env)
    with _stage("data"):
        last_bar = int(df["ts"].iloc[-1])
        if last_bar < hs["last_bar_ts"]:
            raise PaperRunError("data", f"data ends at {last_bar}, before the holdout's last bar {hs['last_bar_ts']}")

    with _stage("fingerprint"):
        fp = env.fingerprint()
        epoch, created = ensure_epoch(ledger, fp, last_bar)
        floor = _recording_floor(ledger)
        if created:  # this run records the decisions first known in (floor, last bar] under the new epoch
            epoch["through_ts"] = floor

    with _stage("recompute"):
        with traced_engine() as trace:
            res = run_backtest(cfg, df, hs["holdout_start"], None, allow_holdout=True)
        closed, open_trade = split_result(res, trace)
        all_trades = closed + ([open_trade] if open_trade is not None else [])
        by_entry = {t.entry_ts: t for t in all_trades}
        holdout_n = sum(1 for t in all_trades if hs["holdout_start"] <= t.entry_ts <= hs["last_bar_ts"])
        cur_closed = paper_trades(closed, int(epoch["started_ts"]))
        replace_epoch_trades(ledger, epoch["id"], cur_closed)
        ledger.open_position = (
            None if open_trade is None else open_position_record(open_trade, trace, int(epoch["started_ts"]))
        )

        prefix = _PrefixHash(df)
        data_sha = prefix(last_bar)
        holdout_sha = data_fingerprint(df, hs["holdout_start"], hs["last_bar_ts"])
        holdout_ok = holdout_sha == hs["data_sha256"]
        ok_runs = sorted((r for r in ledger.runs if r.get("status") == "ok"), key=lambda r: r["last_bar_ts"])
        prev_run = next((r for r in reversed(ledger.runs) if r.get("status") == "ok"), None)
        prev_env = None if prev_run is None else prev_run.get("env_sha256")

        def revised_at(d: dict[str, Any]) -> bool:
            return prefix(d["ts"]) != d["data_sha256"]

        def first_knowing_run(r: dict[str, Any]) -> dict[str, Any] | None:
            """The first successful run that should have recorded r."""
            return next((x for x in ok_runs if x["last_bar_ts"] >= known_at(r)), None)

        def appeared_revised(r: dict[str, Any]) -> bool:
            run = first_knowing_run(r)
            return run is not None and prefix(run["last_bar_ts"]) != run["data_sha256"]

        def appeared_env(r: dict[str, Any]) -> str | None:
            run = first_knowing_run(r)
            return None if run is None else run.get("env_sha256")

        recomputed = build_decisions(res, trace, all_trades)
        found = verify_decisions(ledger.decisions, recomputed, epoch, revised_at, appeared_revised,
                                 appeared_after=floor, env_sha256=env_sha, appeared_env=appeared_env,
                                 previous_env_sha256=prev_env)
        known = {d["id"] for d in ledger.discrepancies}
        new_disc = [
            {**d, "epoch_id": epoch["id"], "detected_ts": last_bar, "detected_run": run_id}
            for d in found
            if d["id"] not in known
        ]
        ledger.discrepancies.extend(new_disc)
        fresh = decisions_to_record(recomputed, epoch, {d["id"] for d in ledger.decisions})
        for d in fresh:
            d.update(run_id=run_id, data_sha256=prefix(d["ts"]), epoch_id=epoch["id"], env_sha256=env_sha)
        ledger.decisions.extend(fresh)
        epoch["through_ts"] = max(int(epoch["through_ts"]), last_bar)

    with _stage("events"):
        count = golive_count(ledger)
        lock_n = int(hs["n_trades"][GO_LIVE_STRATEGY])
        warnings: list[str] = []
        events: list[dict[str, Any]] = []
        if created:
            events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], epoch_text(epoch)))
        events += decision_events(fresh, by_entry, None if open_trade is None else open_trade.entry_ts,
                                  int(epoch["started_ts"]), count, cur_closed, cfg.strategy.tp1_frac)
        if new_disc:
            events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies), discrepancy_text(new_disc)))
        if not holdout_ok:
            warnings.append(f"holdout data hash {holdout_sha[:12]} != lock {hs['data_sha256'][:12]}")
            events.append(make_event("DATA_HASH", hs["last_bar_ts"], holdout_sha[:12],
                                     data_hash_text(hs["data_sha256"], holdout_sha)))
        if holdout_n != lock_n:  # one warning per distinct recomputed count
            warnings.append(f"holdout trades recomputed {holdout_n} != lock {lock_n}")
            events.append(make_event("HOLDOUT_TRADES", hs["last_bar_ts"], holdout_n, holdout_trades_text(lock_n, holdout_n)))
        if gap is not None and prev_ok is not None and gap > GAP_WARN_S:
            events.append(make_event("GAP", now // H1 * H1, 0, gap_text(gap, prev_ok)))

        run: dict[str, Any] = {
            "id": run_id,
            "ts": now,
            "status": "ok",
            "last_bar_ts": last_bar,
            "data_sha256": data_sha,
            "holdout_data_ok": holdout_ok,
            "holdout_trades_recomputed": holdout_n,
            "epoch_id": epoch["id"],
            "paper_trades": count["paper"],
            "count_total": count["total"],
            "new_decisions": len(fresh),
            "new_discrepancies": len(new_disc),
            "env": run_env,
            "env_sha256": env_sha,
            "warnings": warnings,
        }
        ledger.runs.append(run)
        due = daily_summary_due(now, ledger.daily_summary_date)

    stale: str | None = None  # why today's daily summary shows the previous criteria 1-2 result
    if due is not None and count["eligible"]:
        failed = _criteria_failures(ledger.runs, due)
        try:
            end = last_bar + H1
            cmp = compare(cfg, df, hs["holdout_start"], end, random_runs=env.random_runs, workers=env.workers,
                          allow_holdout=True)
            ledger.criteria = summarize_compare(cmp, hs["holdout_start"], end, epoch["id"], now)
            run["criteria_recompute"] = {"ny_date": due, "ok": True}
        except Exception as exc:  # noqa: BLE001 - criteria 1-2 are a daily extra: never block the hourly run
            err = f"{type(exc).__name__}: {exc}"
            run["criteria_recompute"] = {"ny_date": due, "ok": False}
            if failed + 1 < CRITERIA_ATTEMPTS:
                warnings.append(f"criteria 1-2 not recomputed (retried next run): {err}")
                with _stage("events"):  # one warning per New York date; the previous ledger.criteria stays
                    events.append(make_event("CRITERIA", _ny9_ts(due), 0, criteria_error_text(due, err)))
                due = None  # no daily summary and no daily_summary_date: the next run retries
            else:
                warnings.append(
                    f"criteria 1-2 not recomputed (attempt {failed + 1}/{CRITERIA_ATTEMPTS} on {due} failed: daily "
                    f"summary sent stale, next try on the next New York date): {err}"
                )
                stale = err

    with _stage("events"):
        if due is not None:
            eq = {p.ts: p.equity for p in res.equity}
            base_eq = eq.get(hs["last_bar_ts"], cfg.initial_equity)
            last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
            equity_line = f"자산 {last_eq:,.2f} USD (홀드아웃 이후 {last_eq / base_eq - 1:+.2%})"
            count_line = (
                f"표본 외 누적 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 {count['paper']}) · "
                f"구간 {epoch['id']}"
            )
            st = golive_status(ledger, now)
            text = daily_text(due, position_line(ledger.open_position), equity_line, count_line, status_lines(st),
                              stale=stale)
            events.append(make_event("DAILY", _ny9_ts(due), 0, text))
            ledger.daily_summary_date = due
        queued = queue_events(ledger, events)
        status = golive_status(ledger, now)

    with _stage("write"):  # report first: if it fails, the previous ledger.json is left untouched
        write_paper_report(ledger_dir / REPORT_FILE, ledger, status, res.equity, cur_closed, df,
                           top_count_summary(trace.last_state), now)
        save_ledger(ledger_dir, ledger)
        append_log_line(
            ledger_dir / RUNS_LOG,
            f"{datetime.fromtimestamp(now, UTC).isoformat(timespec='seconds')}\tok\tlast_bar="
            f"{datetime.fromtimestamp(last_bar, UTC).isoformat(timespec='minutes')}\tdata={data_sha[:12]}\t"
            f"trades={count['total']}/{count['required']}\tepoch={epoch['id']}\tnew_decisions={len(fresh)}",
        )

    with _stage("notify", detail=False):
        remaining, sent, errors = deliver(ledger.pending_events, ledger.sent_event_ids, env.sender, sleep=env.sleep)
        ledger.pending_events = remaining
        ledger.sent_event_ids.extend(sent)
        run["notify"] = {"sent": len(sent), "pending": len(remaining), "errors": errors}
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
    return RunOutcome(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                      warnings + errors, status)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_environment.py tests/paper/test_runner.py -q
```

Expected: `39 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/environment.py src/ewbot/paper/runner.py tests/paper/test_environment.py tests/paper/test_runner.py
git commit -m "$(cat <<'EOF'
feat(paper): hourly paper run orchestration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: CLI `ewbot paper run` and `ewbot paper status`

**Files:**
- Modify: `src/ewbot/cli.py` (5 edits below)
- Test: `tests/paper/test_cli_paper.py`

**Interfaces:**
- Consumes: `PaperEnv`, `PaperRunError`, `run_paper` (Task 7); `golive_status`, `status_lines` (Task 6); `LedgerError`, `current_epoch`, `load_ledger` (Task 2); `deliver`, `failure_text`, `make_event`, `position_line`, `sender_from_env` (Task 5).
- Produces: `ewbot paper run --ledger-dir DIR [--cache-dir DIR]` (exit 0 / 1), `ewbot paper status --ledger-dir DIR` (exit 0 / 1), `cli._paper_env(args) -> PaperEnv` (production wiring, replaced in tests), `cli.RUN_URL_ENV = "EWBOT_RUN_URL"`, `cli.FAILURE_MARKER_ENV = "EWBOT_FAILURE_MARKER"`, `cli.cmd_paper(args) -> int`. No strategy, config, params or lock option exists on `paper`.

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_cli_paper.py`**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from ewbot import cli
from ewbot.backtest.holdout import DEFAULT_LOCK_PATH
from ewbot.cli import build_parser, main
from ewbot.paper.runner import PaperEnv
from tests.paper.helpers import Clock, FakeFetch, FakeSender, commit_lock, make_lock, synth_frame, ts


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    df = synth_frame()
    lock_path = commit_lock(tmp_path / "repo", make_lock(df))
    clock = Clock(ts("2024-02-10 12:07"))
    sender = FakeSender()
    built: list[PaperEnv] = []

    def factory(args):
        env = PaperEnv(
            ledger_dir=args.ledger_dir, lock_path=lock_path, cache_dir=tmp_path / "cache", fetch=FakeFetch(df, clock),
            sender=sender, clock=clock, fingerprint=lambda: "fp", baseline=lambda: ("fp", "c" * 40),
            random_runs=2, workers=1, sleep=lambda s: None,
        )
        built.append(env)
        return env

    monkeypatch.setattr(cli, "_paper_env", factory)
    return built, sender


def test_paper_parser_has_no_strategy_options():
    ns = build_parser().parse_args(["paper", "run", "--ledger-dir", "/tmp/x"])
    assert (ns.cmd, ns.paper_cmd, ns.ledger_dir, ns.cache_dir) == ("paper", "run", Path("/tmp/x"), Path("data/cache"))
    for flag in ("--params", "--profile", "--mode", "--config", "--random-runs", "--lock"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["paper", "run", "--ledger-dir", "/tmp/x", flag, "1"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["paper", "run"])  # --ledger-dir is required


def test_production_env_uses_the_repo_lock_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._paper_env(build_parser().parse_args(["paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == DEFAULT_LOCK_PATH and env.sender is None and env.random_runs == 100
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_paper_run_then_status(fake_env, tmp_path, capsys):
    ledger_dir = tmp_path / "paper"
    assert main(["paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "paper run 20240210T120700Z" in out and "out-of-sample 4/50" in out and "epoch 1" in out
    assert main(["paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "실거래 전환 판정" in out and "3. 표본 외 거래 4/50" in out and "포지션: 없음" in out
    assert "holdout trades recomputed 2 (lock: 2)" in out and "holdout data ok" in out
    assert "discrepancies 0 (unexplained 0, environment change 0)" in out


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err


def test_failed_run_alerts_with_stage_and_log_link(fake_env, tmp_path, monkeypatch, capsys):
    built, sender = fake_env
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/7")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_paper_env", failing)
    assert main(["paper", "run", "--ledger-dir", str(tmp_path / "paper")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    assert len(sender.texts) == 1 and "실패 · 단계 data" in sender.texts[0]
    assert "https://github.com/o/r/actions/runs/7" in sender.texts[0]
    assert marker.read_text() == "data" and not (tmp_path / "paper" / "ledger.json").exists()


def test_unexpected_error_is_alerted_too(fake_env, tmp_path, monkeypatch):
    _, sender = fake_env

    def boom(env):
        raise ValueError("bad state")

    monkeypatch.setattr(cli, "run_paper", boom)
    monkeypatch.delenv("EWBOT_FAILURE_MARKER", raising=False)
    assert main(["paper", "run", "--ledger-dir", str(tmp_path / "paper")]) == 1
    assert "단계 unexpected" in sender.texts[0] and "ValueError: bad state" in sender.texts[0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_cli_paper.py -q
```

Expected: `3 failed, 3 errors`: the parser rejects `paper` (`SystemExit: 2`, `invalid choice: 'paper'`) and the fixture-based tests error with `AttributeError` (module `ewbot.cli` has no attribute `_paper_env`).

- [ ] **Step 3: Write minimal implementation**

Apply these five replacements to `src/ewbot/cli.py` (each `replace` text occurs exactly once).

**Edit `src/ewbot/cli.py` (1/5) — replace:**

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout.

Holdout isolation: every command except `holdout` cuts the candle frame to ts < HOLDOUT_START
(2024-01-01) right after loading it, before any engine run, benchmark or report candle sees it, and
an explicit --start at/after HOLDOUT_START or --end after it is refused (exit 2). runner.run_backtest
and runner.compare refuse the holdout on their own as well (defence in depth)."""
```

**with:**

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper.

Holdout isolation: every research command (backtest, compare, walkforward) cuts the candle
frame to ts < HOLDOUT_START (2024-01-01) right after loading it, before any engine run, benchmark or
report candle sees it, and an explicit --start at/after HOLDOUT_START or --end after it is refused
(exit 2). runner.run_backtest and runner.compare refuse the holdout on their own as well (defence in
depth). `holdout` is the one-shot evaluation. `paper run` (Plan 2) works on the period AFTER the
holdout: it reads the params from the committed holdout.lock only and has no strategy or config
options (ewbot.paper.runner)."""
```

**Edit `src/ewbot/cli.py` (2/5) — replace:**

```python
import time
from pathlib import Path

```

**with:**

```python
import time
import traceback
from pathlib import Path

```

**Edit `src/ewbot/cli.py` (3/5) — replace:**

```python
from ewbot.data.coinbase_history import update_cache

```

**with:**

```python
from ewbot.data.coinbase_history import update_cache
from ewbot.paper.criteria import golive_status, status_lines
from ewbot.paper.ledger import LedgerError, current_epoch, load_ledger
from ewbot.paper.notify import deliver, failure_text, make_event, position_line, sender_from_env
from ewbot.paper.runner import PaperEnv, PaperRunError, run_paper

```

**Edit `src/ewbot/cli.py` (4/5) — replace:**

```python
    p_ho.add_argument("--confirm", action="store_true", help="required: acknowledge the holdout is single-use")
    return parser
```

**with:**

```python
    p_ho.add_argument("--confirm", action="store_true", help="required: acknowledge the holdout is single-use")

    p_paper = sub.add_parser("paper", help="paper-trading runtime (Plan 2): hourly recompute, ledger, Telegram")
    paper_sub = p_paper.add_subparsers(dest="paper_cmd", required=True)
    p_run = paper_sub.add_parser("run", help="one paper run; params come from holdout.lock only")
    p_run.add_argument("--ledger-dir", type=Path, required=True, help="paper branch worktree (ledger.json, report.html, runs.log)")
    p_run.add_argument("--cache-dir", type=Path, default=Path("data/cache"), help="candle cache directory")
    p_status = paper_sub.add_parser("status", help="print the ledger's go-live status (read-only, no network)")
    p_status.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```

**Edit `src/ewbot/cli.py` (5/5) — replace:**

```python
_COMMANDS = {
    "fetch": cmd_fetch,
    "backtest": cmd_backtest,
    "compare": cmd_compare,
    "walkforward": cmd_walkforward,
    "holdout": cmd_holdout,
}
```

**with:**

```python
RUN_URL_ENV = "EWBOT_RUN_URL"  # set by .github/workflows/paper.yml: link to the Actions run log
FAILURE_MARKER_ENV = "EWBOT_FAILURE_MARKER"  # file the workflow checks before its own fallback alert


def _paper_env(args: argparse.Namespace) -> PaperEnv:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    return PaperEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _alert_failure(env: PaperEnv, stage: str, message: str) -> None:
    """Failure alert with the stage and the run-log link; on success touch the marker file so the
    workflow's fallback step does not send a second one."""
    if env.sender is None:
        return
    event = make_event("FAILURE", env.clock(), stage, failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([event], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _paper_run(args: argparse.Namespace) -> int:
    env = _paper_env(args)
    try:
        out = run_paper(env)
    except PaperRunError as exc:
        print(f"paper run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    count = out.status["count"]
    print(
        f"paper run {out.run_id}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(out.last_bar_ts))} UTC, "
        f"epoch {out.epoch_id}, out-of-sample {count['total']}/{count['required']}, {out.new_decisions} new "
        f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending"
    )
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _paper_status(args: argparse.Namespace) -> int:
    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    st = golive_status(led, int(time.time()))
    for line in status_lines(st):
        print(line)
    print(position_line(led.open_position))
    cur = current_epoch(led)
    print(
        f"epoch {cur['id']} of {len(led.epochs)}: fingerprint {cur['fingerprint'][:12]} "
        f"(baseline {led.baseline['fingerprint'][:12]} @ {led.baseline['commit'][:7]})"
    )
    if led.runs:
        r = led.runs[-1]
        print(
            f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
            f"data {r['data_sha256'][:12]}, holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH'}, "
            f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['A']})"
        )
    unexplained = sum(1 for d in led.discrepancies if d["cause"] == "UNEXPLAINED")
    environment = sum(1 for d in led.discrepancies if d["cause"] == "ENVIRONMENT_CHANGE")
    print(
        f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)} (unexplained {unexplained}, "
        f"environment change {environment}), pending events {len(led.pending_events)}"
    )
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    return _paper_status(args) if args.paper_cmd == "status" else _paper_run(args)


_COMMANDS = {
    "fetch": cmd_fetch,
    "backtest": cmd_backtest,
    "compare": cmd_compare,
    "walkforward": cmd_walkforward,
    "holdout": cmd_holdout,
    "paper": cmd_paper,
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_cli_paper.py tests/backtest/test_cli.py -q
uv run ewbot paper run --help
```

Expected: `26 passed` (6 new + 20 existing CLI tests); the help lists only `--ledger-dir` and `--cache-dir`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/cli.py tests/paper/test_cli_paper.py
git commit -m "$(cat <<'EOF'
feat(cli): ewbot paper run | status

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Time-walk integration test (synthetic bars, skipped runs, data revision, no duplicate notifications)

**Files:**
- Test: `tests/paper/test_timewalk.py`

**Interfaces:**
- Consumes: `PaperEnv`, `run_paper` (Task 7), `load_ledger` (Task 2), `tests.paper.helpers` (Task 3).
- Produces: no production code. This task is integration coverage of spec §7.2 over Tasks 1–8.

- [ ] **Step 1: Write the failing test**

The hourly walk runs 28 consecutive runs (2024-03-13 21:07 .. 03-15 00:07 UTC) plus one at 03-16 19:07; the sparse walk skips most of them; the one-shot is a single run from scratch at 03-16 19:07. The data-revision scenario serves a re-downloaded frame whose 2024-03-13 23:00 open differs from 03-14 03:07 on.

**File: `tests/paper/test_timewalk.py`**

```python
"""Plan 2 spec 7.2: the hourly runtime replayed on synthetic candles with a moving clock.

The window covers the full life of a C_END long (signal 2024-03-13 22:00, entry 23:00, trailing stop
03-14 07:00, TP1 + breakeven 03-14 23:00, TP2 03-16 18:00) after a catch-up of the earlier paper
trades. Every run is the real `run_paper` (recompute, ledger, report, Telegram via a fake sender)."""

from __future__ import annotations

import pytest

from ewbot.paper.ledger import load_ledger
from ewbot.paper.runner import PaperEnv, run_paper
from ewbot.types import H1
from tests.paper.helpers import Clock, FakeFetch, FakeSender, commit_lock, make_lock, synth_frame, ts

START = ts("2024-03-13 21:07")
HOURLY = [START + k * H1 for k in range(28)]  # 03-13 21:07 .. 03-15 00:07, one run per hour
FINAL = ts("2024-03-16 19:07")
SPARSE = [HOURLY[0], HOURLY[5], HOURLY[6], HOURLY[13], HOURLY[27]]  # most runs skipped


def _walk(base, name: str, times: list[int], source=None) -> tuple[PaperEnv, FakeSender, list]:
    df = synth_frame()
    lock_path = commit_lock(base / "repo", make_lock(df))
    clock = Clock(times[0])
    sender = FakeSender()
    env = PaperEnv(
        ledger_dir=base / name, lock_path=lock_path, cache_dir=base / f"{name}-cache",
        fetch=FakeFetch(df if source is None else source, clock), sender=sender, clock=clock,
        fingerprint=lambda: "fp", baseline=lambda: ("fp", "c" * 40), random_runs=2, workers=1, sleep=lambda s: None,
    )
    outs = []
    for t in times:
        clock.now = t
        outs.append(run_paper(env))
    return env, sender, outs


def _core(decisions: list[dict]) -> list[dict]:
    return [{k: v for k, v in d.items() if k != "run_id"} for d in decisions]


@pytest.fixture(scope="module")
def walks(tmp_path_factory):
    base = tmp_path_factory.mktemp("walk")
    hourly = _walk(base, "hourly", HOURLY + [FINAL])
    sparse = _walk(base, "sparse", SPARSE + [FINAL])
    oneshot = _walk(base, "oneshot", [FINAL])
    return {k: (v[0], v[1], v[2], load_ledger(v[0].ledger_dir)) for k, v in
            {"hourly": hourly, "sparse": sparse, "oneshot": oneshot}.items()}


def test_final_ledger_equals_a_single_run_from_scratch(walks):
    led, one = walks["hourly"][3], walks["oneshot"][3]
    assert led.trades == one.trades and _core(led.decisions) == _core(one.decisions)
    assert led.open_position == one.open_position is None
    assert led.discrepancies == one.discrepancies == []
    kinds = {d["kind"] for d in led.decisions if d["trade_key"] == ts("2024-03-13 23:00")}
    assert kinds == {"ENTRY", "TRAIL", "TP1", "BREAKEVEN", "TP2"}


def test_skipped_runs_do_not_change_the_final_ledger(walks):
    led, one = walks["sparse"][3], walks["oneshot"][3]
    assert led.trades == one.trades and _core(led.decisions) == _core(one.decisions)
    assert len(led.runs) == len(SPARSE) + 1


def test_decisions_were_recorded_by_the_first_run_that_could_know_them(walks):
    env, _, outs, led = walks["hourly"]
    by_run = {o.run_id: o.last_bar_ts for o in outs}
    for d in led.decisions:
        known = d["ts"] + (H1 if d["kind"] == "SIGNAL" else 0)
        recorded_at = by_run[d["run_id"]]
        assert recorded_at >= known
        earlier = [b for b in by_run.values() if b < recorded_at]
        assert not earlier or max(earlier) < known  # no earlier run already knew it


def _expected_event_ids(led) -> set[str]:
    trades = {t["entry_ts"]: t for t in led.trades}
    ids = set()
    for d in led.decisions:
        if d["kind"] == "ENTRY" or d["kind"] in ("BREAKEVEN", "TRAIL"):
            ids.add(d["id"])
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT"):
            t = trades.get(d["trade_key"])
            if t is not None and t["exit_ts"] == d["ts"]:
                ids.add(f"CLOSE:{d['ts']}:{d['trade_key']}")
    return ids


@pytest.mark.parametrize("name", ["hourly", "sparse", "oneshot"])
def test_every_event_is_notified_exactly_once(walks, name):
    _, sender, _, led = walks[name]
    sent = led.sent_event_ids
    assert len(sent) == len(set(sent)) == len(sender.texts) and led.pending_events == []
    decision_ids = {i for i in sent if i.split(":")[0] in ("ENTRY", "BREAKEVEN", "TRAIL", "CLOSE")}
    assert decision_ids == _expected_event_ids(led)
    assert sum(i.startswith("DAILY:") for i in sent) == len({i for i in sent if i.startswith("DAILY:")})


def test_hourly_walk_notified_each_step_of_the_trade_when_it_happened(walks):
    _, _, outs, _ = walks["hourly"]
    key = ts("2024-03-13 23:00")
    at = {o.last_bar_ts: o.events for o in outs}  # a run at hh:07 has the bar of hh-1 as its last bar
    assert f"ENTRY:{key}:{key}" in at[key]  # the 03-14 00:07 run saw the 23:00 fill
    assert f"TRAIL:{ts('2024-03-14 07:00')}:{key}" in at[ts("2024-03-14 07:00")]
    assert f"BREAKEVEN:{ts('2024-03-14 23:00')}:{key}" in at[ts("2024-03-14 23:00")]
    assert f"CLOSE:{ts('2024-03-16 18:00')}:{key}" in at[ts("2024-03-16 18:00")]
    assert not any(e.startswith("ENTRY:") for o in outs[1:-1] for e in o.events if o.last_bar_ts != key)


def test_revised_past_bar_is_flagged_as_data_revision(tmp_path):
    df = synth_frame()
    revised = df.copy()
    i = int(revised.index[revised["ts"] == ts("2024-03-13 23:00")][0])
    revised.loc[i, "open"] += 7.0  # the C_END entry bar, as a re-download after cache loss might return it
    revised.loc[i, "high"] = max(revised.loc[i, "high"], revised.loc[i, "open"])
    clock_box: dict[str, int] = {}

    def source():
        return revised if clock_box["now"] >= ts("2024-03-14 03:00") else df

    times = [ts("2024-03-14 01:07"), ts("2024-03-14 02:07"), ts("2024-03-14 03:07"), ts("2024-03-14 04:07")]
    lock_path = commit_lock(tmp_path / "repo", make_lock(df))
    clock = Clock(times[0])
    sender = FakeSender()
    env = PaperEnv(
        ledger_dir=tmp_path / "paper", lock_path=lock_path, cache_dir=tmp_path / "cache",
        fetch=FakeFetch(source, clock), sender=sender, clock=clock, fingerprint=lambda: "fp",
        baseline=lambda: ("fp", "c" * 40), random_runs=2, workers=1, sleep=lambda s: None,
    )
    outs = []
    for t in times:
        clock.now = clock_box["now"] = t
        outs.append(run_paper(env))
    led = load_ledger(env.ledger_dir)
    assert [o.new_discrepancies for o in outs][:2] == [0, 0] and outs[2].new_discrepancies > 0
    assert outs[3].new_discrepancies == 0  # already recorded: no repeated warning
    assert led.discrepancies and all(d["cause"] == "DATA_REVISION" for d in led.discrepancies)
    key = ts("2024-03-13 23:00")
    assert f"CHANGED:ENTRY:{key}:{key}" in {d["id"] for d in led.discrepancies}
    warnings = [t for t in sender.texts if "결정 불일치" in t]
    assert len(warnings) == 1 and "DATA_REVISION" in warnings[0]
    recorded = next(d for d in led.decisions if d["id"] == f"ENTRY:{key}:{key}")
    assert led.open_position["entry_ts"] == key  # still open at 03-14 04:00
    assert led.open_position["entry_price"] != recorded["price"]  # state follows the recompute; the record stays
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_timewalk.py -q
```

Expected: `8 passed` already — this task only adds integration coverage of code written in Tasks 1–8, so there is no red phase. If anything fails here, the bug is in Tasks 1–8: fix the component (and its unit test), never relax this test.

- [ ] **Step 3: Write minimal implementation**

No production code. (If Step 2 failed, fix the responsible module from Tasks 1–8 and re-run Step 2.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper -q
```

Expected: `135 passed` (all paper tests so far, about 50 s; the walk fixture alone takes about 25 s).

- [ ] **Step 5: Commit**

```bash
git add tests/paper/test_timewalk.py
git commit -m "$(cat <<'EOF'
test(paper): time-walk integration on synthetic candles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Static `allow_holdout` guard, Actions workflow and ops guide

**Files:**
- Create: `.github/workflows/paper.yml`, `.github/workflows/ci.yml`, `scripts/paper_sync.sh`, `docs/paper-trading.md`
- Test: `tests/paper/test_static.py`, `tests/paper/test_paper_sync.py`

**Interfaces:**
- Consumes: `pyyaml` (already a dependency), the AST of every `src/ewbot/**/*.py`, `git` >= 2.42 (`worktree add --orphan`) and `bash` for the sync script and its tests (local bare repositories, no network).
- Produces: the guard "`allow_holdout=True` appears only in `backtest/holdout.py` and `paper/runner.py`; non-literal values only in `backtest/runner.py`; every `allow_holdout` parameter defaults to `False` (only runner's private `_init_worker` / `_map_tasks` take it without a default); no `"allow_holdout"` string and no import, attribute, name or getattr string of runner's `check_before_holdout` / `_map_tasks` / `_init_worker` outside `backtest/runner.py` (a module may define its own helper of the same name: `walkforward._init_worker`)"; the hourly workflow (`runs-on: ubuntu-24.04`); the CI guard workflow `ci.yml` (pull requests and pushes to `master`: `tests/paper/test_fingerprint.py` with the pinned baseline and `tests/paper/test_static.py`, read-only, no secrets); `scripts/paper_sync.sh` (`checkout`: paper worktree, orphan only on `ls-remote` exit 2; `push RECOMPUTE...`: commit, 3 pushes, the same commit again when `origin/paper` did not move, recompute only when it moved, never after the last push); the ops guide with the user's Telegram steps (spec §6.5), the Actions budget step, what the CI guard does and does not do, the no-revert rule, the lock binding and the frozen runtime environment (spec §8).
- Workflow env passed to `ewbot`: `EWBOT_RUN_URL` (job), `EWBOT_FAILURE_MARKER`, `PAPER_DIR` (`$RUNNER_TEMP/paper`, via `$GITHUB_ENV`); the secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` only on the steps that can send (`paper`, `push`, `alert`).

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_static.py`**

```python
"""Static guards: who may run into the holdout, and the shape of the paper workflow."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

import ewbot

SRC = Path(ewbot.__file__).resolve().parent  # src/ewbot
ROOT = SRC.parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "paper.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
PYTHON_PATCH = "3.12.13"  # the exact patch both workflows resolve; part of the compared environment
RUNNER_IMAGE = "ubuntu-24.04"  # pinned while paper trading runs (spec 8): part of the recorded environment
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py"}  # Plan 2 spec 7.1
PASS_THROUGH = {"backtest/runner.py"}  # forwards its own allow_holdout argument to the engine run
# backtest/runner.py helpers that skip the holdout check or forward allow_holdout unchecked
GUARDED = {"check_before_holdout", "_map_tasks", "_init_worker"}
# the only allow_holdout parameters without a default: runner's private pool helpers, always passed explicitly
REQUIRED_ALLOW = {("backtest/runner.py", "_init_worker"), ("backtest/runner.py", "_map_tasks")}
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"  # v10.2.0
SECRET_ENV = {
    "TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}",
    "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}",
}
RUN = 'uv run ewbot paper run --ledger-dir "$PAPER_DIR"'


def _modules():
    for f in sorted(SRC.rglob("*.py")):
        yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def _nodes():
    for rel, tree in _modules():
        for node in ast.walk(tree):
            yield rel, node


def test_only_the_holdout_and_the_paper_runner_pass_allow_holdout_true():
    literal_true, other = set(), set()
    for rel, node in _nodes():
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "allow_holdout":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        literal_true.add(rel)
                    elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        other.add(rel)
    assert literal_true == ALLOWED_TRUE
    assert other <= PASS_THROUGH


def _allow_holdout_params(tree: ast.AST) -> tuple[list[int], set[str]]:
    """(lines of allow_holdout parameters whose default is not the literal False or that are *args /
    **kwargs, names of the functions taking allow_holdout without a default)."""
    bad, required = [], set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        a = fn.args
        positional = a.posonlyargs + a.args
        defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
        pairs = list(zip(positional, defaults)) + list(zip(a.kwonlyargs, a.kw_defaults))
        bad += [s.lineno for s in (a.vararg, a.kwarg) if s is not None and s.arg == "allow_holdout"]
        for param, default in pairs:
            if param.arg != "allow_holdout":
                continue
            if default is None:
                required.add(getattr(fn, "name", "<lambda>"))
            elif not (isinstance(default, ast.Constant) and default.value is False):
                bad.append(param.lineno)
    return bad, required


@pytest.mark.parametrize(
    "src",
    [
        "def f(allow_holdout=True): ...",
        "def f(*, allow_holdout=None): ...",
        "def f(x, allow_holdout=1, /): ...",
        "async def f(*, allow_holdout=bool(1)): ...",
        "g = lambda allow_holdout=True: 0",
        "def f(**allow_holdout): ...",
    ],
)
def test_the_parameter_guard_catches_a_non_false_default(src):
    assert _allow_holdout_params(ast.parse(src))[0]


def test_allow_holdout_parameters_default_to_false():
    required = set()
    for rel, tree in _modules():
        bad, names = _allow_holdout_params(tree)
        assert not bad, (rel, bad)
        required |= {(rel, name) for name in names}
    assert required <= REQUIRED_ALLOW


def _holdout_back_doors(tree: ast.AST) -> list[tuple[int, str]]:
    """References outside backtest/runner.py that could reach the holdout without allow_holdout=True:
    an "allow_holdout" string (`**{"allow_holdout": True}`), and any import, attribute, name or getattr
    string of the GUARDED runner helpers. A module may define and call its own helper of the same name
    (walkforward.py has its own _init_worker, which takes no allow_holdout)."""
    own = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "allow_holdout" or node.value in GUARDED:
                found.append((line, f"string {node.value!r}"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] == "runner" and any(a.name == "*" for a in node.names):
                found.append((line, "import * from a runner module"))
            found += [(line, f"import {a.name}") for a in node.names if a.name in GUARDED]
        elif isinstance(node, ast.Attribute) and node.attr in GUARDED:
            found.append((line, f"attribute .{node.attr}"))
        elif isinstance(node, ast.Name) and node.id in GUARDED and node.id not in own:
            found.append((line, f"name {node.id}"))
    return found


@pytest.mark.parametrize(
    "src",
    [
        'run_backtest(cfg, df, **{"allow_holdout": True})',
        "from ewbot.backtest.runner import check_before_holdout",
        "from .runner import _init_worker as init",
        "from ewbot.backtest.runner import *",
        "from ewbot.backtest import runner\nrunner._map_tasks([], 1, cfg, df, 0, 0, True)",
        "import ewbot.backtest.runner as r\ngetattr(r, '_init_worker')(cfg, df, 0, 0, True)",
        "check_before_holdout(df, None, True)",
    ],
)
def test_the_back_door_guard_catches(src):
    assert _holdout_back_doors(ast.parse(src))


def test_the_back_door_guard_allows_a_module_s_own_helper_of_the_same_name():
    assert _holdout_back_doors(ast.parse("def _init_worker(cfg): ...\n_init_worker(cfg)")) == []


def test_no_back_doors_into_the_holdout():
    for rel, tree in _modules():
        if rel not in PASS_THROUGH:
            assert _holdout_back_doors(tree) == [], rel


def _workflow():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_the_repository_has_exactly_the_paper_and_ci_workflows():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper.yml"]


def test_workflow_triggers_concurrency_and_permissions():
    wf, _ = _workflow()
    on = wf[True]  # YAML 1.1 reads the bare key `on` as boolean True
    assert on["schedule"] == [{"cron": "52 */3 * * *"}] and "workflow_dispatch" in on
    assert wf["concurrency"] == {"group": "paper", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"}
    assert "permissions" not in wf["jobs"]["paper"]
    assert wf["jobs"]["paper"]["runs-on"] == RUNNER_IMAGE  # never ubuntu-latest: no silent OS upgrade


def test_telegram_secrets_reach_only_the_steps_that_can_send():
    wf, steps = _workflow()
    assert "secrets." not in yaml.safe_dump(wf["jobs"]["paper"].get("env", {}))
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper", "push", "alert"]  # the run, the push step's recompute, the alert
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRET_ENV, i
    assert WORKFLOW.read_text(encoding="utf-8").count("secrets.") == 2 * len(with_secrets)


def test_workflow_steps():
    wf, steps = _workflow()
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert f"uses: {SETUP_UV} # v10.2.0" in text  # third-party action pinned to a commit
    assert steps[0]["with"] == {"ref": "master"}  # shallow: the paper history is never fetched in full
    runs = [s.get("run", "") for s in steps]
    checkout = _step(steps, "checkout-paper")["run"]
    assert "git fetch --no-tags --depth=1 origin 64e628ad4865b3b04571a7cba2886881eae19176" in checkout  # baseline
    assert 'PAPER_DIR="$RUNNER_TEMP/paper" bash scripts/paper_sync.sh checkout' in checkout
    assert "uv sync --frozen" in runs
    paper, push = _step(steps, "paper"), _step(steps, "push")
    assert paper["run"] == RUN
    assert push["run"] == f"bash scripts/paper_sync.sh push {RUN}"  # recomputes only when origin/paper moved
    # a hang becomes a step failure (and an alert) well before the job-level backstop
    assert paper["timeout-minutes"] == 35 and push["timeout-minutes"] == 35
    step_budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper"]["timeout-minutes"] >= step_budget + 10
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "btc-usd-1h-"


def test_failure_alert_step():
    _, steps = _workflow()
    alert = steps[-1]
    assert alert["id"] == "alert" and alert["if"] == "failure() || cancelled()"  # timeouts cancel
    run = alert["run"]
    assert "EWBOT_FAILURE_MARKER" in run and "sendMessage" in run
    assert "curl" in run and "--fail-with-body" in run and "/dev/null" not in run  # keep Telegram's error
    assert "set -x" not in run and "echo \"${TELEGRAM_BOT_TOKEN" not in run and "echo $TELEGRAM" not in run


def test_failure_alert_curl_finishes_within_its_step_timeout():
    """curl --max-time bounds each attempt; --retry N adds N attempts after waits of 1, 2, 4, ... s.
    The worst case must end before the step timeout, or the alert step itself is killed silently."""
    _, steps = _workflow()
    alert = _step(steps, "alert")
    retries = int(re.search(r"--retry (\d+)", alert["run"]).group(1))
    max_time = int(re.search(r"--max-time (\d+)", alert["run"]).group(1))
    worst = (retries + 1) * max_time + sum(2**i for i in range(retries))
    assert (retries, max_time, alert["timeout-minutes"]) == (3, 20, 2)
    assert worst < alert["timeout-minutes"] * 60


def test_ci_workflow_runs_the_fingerprint_and_static_guards_without_secrets():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    on = wf[True]
    assert on == {"pull_request": {"branches": ["master"]}, "push": {"branches": ["master"]}}
    assert wf["permissions"] == {"contents": "read"} and "secrets." not in text
    (job,) = wf["jobs"].values()
    assert job["runs-on"] == RUNNER_IMAGE and job["timeout-minutes"] <= 15
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses == ["actions/checkout@v7", SETUP_UV] and f"uses: {SETUP_UV} # v10.2.0" in text
    runs = [s["run"] for s in job["steps"] if "run" in s]
    assert runs == ["uv sync --frozen", "uv run pytest -q tests/paper/test_fingerprint.py tests/paper/test_static.py"]


def test_both_workflows_pin_the_exact_python_patch():
    """Python is part of the compared environment, so setup-uv gets the full patch, not "3.12"."""
    for path in (WORKFLOW, CI_WORKFLOW):
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        pins = [s["with"]["python-version"] for job in wf["jobs"].values() for s in job["steps"]
                if str(s.get("uses", "")).startswith(SETUP_UV)]
        assert pins == [PYTHON_PATCH], path.name
```

**File: `tests/paper/test_paper_sync.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_static.py tests/paper/test_paper_sync.py -q
```

Expected: `24 failed, 18 passed`: the `allow_holdout` guards and their self-tests pass (Task 7 respected them), and so does `test_usage_errors` (a missing script exits non-zero too); the 8 workflow tests fail with `FileNotFoundError` (`.github/workflows/`, `paper.yml` and `ci.yml` do not exist yet) and 16 sync tests fail because `scripts/paper_sync.sh` does not exist.

- [ ] **Step 3: Write minimal implementation**

```bash
mkdir -p .github/workflows scripts
```

**File: `scripts/paper_sync.sh`** (executable: `chmod +x scripts/paper_sync.sh`; the workflow calls it with `bash`)

```bash
#!/usr/bin/env bash
# Git side of .github/workflows/paper.yml (Plan 2 spec 6.1, 6.4); run from the master checkout.
# Tested against local bare repositories in tests/paper/test_paper_sync.py.
#
#   paper_sync.sh checkout
#       Worktree of origin's `paper` branch at $PAPER_DIR (shallow), or an orphan `paper` branch
#       when origin has none (ls-remote exit 2). Any other ls-remote failure fails: a network or
#       auth error must not start a fresh ledger.
#   paper_sync.sh push RECOMPUTE...
#       Commit ledger.json / report.html / runs.log in $PAPER_DIR and push the commit to origin's
#       `paper` branch, 3 pushes at most. After a failed push:
#         origin still at our commit's parent (or unreadable) -> push the same commit again after a
#             short wait; no recompute, so no Telegram message is sent twice;
#         origin already at our commit -> the push landed, done;
#         origin moved (someone else wrote to `paper`) -> reset to it, run RECOMPUTE
#             (`uv run ewbot paper run ...`), commit and push on top of it.
#       Never recomputes after the last push; exits non-zero when every push failed.
#
# Environment: PAPER_DIR (required); PAPER_PUSH_BACKOFF = seconds, retry n waits n times this
# (default 5).
set -euo pipefail

ATTEMPTS=3
LEDGER_FILES=(ledger.json report.html runs.log)
NOTHING_TO_COMMIT=3

die() {
  echo "paper_sync: $*" >&2
  exit 1
}

# The sha of origin's paper branch, "" when origin has none; any other ls-remote failure is returned.
origin_paper() {
  local out rc=0
  out=$(git -C "$1" ls-remote --exit-code origin refs/heads/paper) || rc=$?
  case "$rc" in
    0) echo "${out%%[[:space:]]*}" ;;
    2) echo "" ;;
    *) return "$rc" ;;
  esac
}

checkout() {
  local remote rc=0
  remote=$(origin_paper .) || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "git ls-remote origin failed (exit $rc): not starting a new paper branch"
  fi
  if [ -n "$remote" ]; then
    git fetch --no-tags --depth=1 origin +refs/heads/paper:refs/remotes/origin/paper
    git worktree add -B paper "$PAPER_DIR" origin/paper
  else
    echo "origin has no paper branch: starting an orphan one"
    git worktree add --orphan -b paper "$PAPER_DIR"
  fi
}

# 0 = committed, NOTHING_TO_COMMIT = no ledger change, 1 = git error. Every status is checked by hand:
# the caller tests this function's status, which switches `set -e` off inside it.
commit_ledger() {
  local rc=0
  git -C "$PAPER_DIR" add -- "${LEDGER_FILES[@]}" || return 1
  git -C "$PAPER_DIR" diff --cached --quiet || rc=$?
  case "$rc" in
    0) return "$NOTHING_TO_COMMIT" ;;
    1) ;;
    *) return 1 ;;
  esac
  git -C "$PAPER_DIR" commit -q -m "paper: run $(date -u +%Y-%m-%dT%H:%MZ)" || return 1
}

# Commit the ledger; stop the script (successfully) when nothing changed, fail on a git error.
commit_or_stop() {
  local rc=0
  commit_ledger || rc=$?
  case "$rc" in
    0) ;;
    "$NOTHING_TO_COMMIT")
      echo "no ledger change: nothing to push"
      exit 0
      ;;
    *) die "git add / commit of the ledger failed in $PAPER_DIR" ;;
  esac
}

push() {
  [ "$#" -gt 0 ] || die "usage: paper_sync.sh push RECOMPUTE..."
  local backoff="${PAPER_PUSH_BACKOFF:-5}" attempt head parent remote rc
  commit_or_stop
  for ((attempt = 1; ; attempt++)); do
    if git -C "$PAPER_DIR" push origin HEAD:refs/heads/paper; then
      echo "pushed $(git -C "$PAPER_DIR" rev-parse --short HEAD) to origin/paper (attempt $attempt)"
      return 0
    fi
    head=$(git -C "$PAPER_DIR" rev-parse HEAD)
    parent=$(git -C "$PAPER_DIR" rev-parse -q --verify 'HEAD^' || true) # "" for the first (orphan) commit
    rc=0
    remote=$(origin_paper "$PAPER_DIR") || rc=$?
    if [ "$rc" -eq 0 ] && [ "$remote" = "$head" ]; then
      echo "origin/paper is at our commit: the push landed despite the error"
      return 0
    fi
    if [ "$attempt" -ge "$ATTEMPTS" ]; then
      die "push failed $ATTEMPTS times"
    fi
    if [ "$rc" -ne 0 ] || [ "$remote" = "$parent" ]; then
      if [ "$rc" -ne 0 ]; then
        echo "push failed (attempt $attempt) and origin is unreadable (ls-remote exit $rc): push the same commit again"
      else
        echo "push failed (attempt $attempt), origin/paper did not move: push the same commit again"
      fi
      sleep "$((backoff * attempt))"
      continue
    fi
    [ -n "$remote" ] || die "origin's paper branch disappeared"
    echo "push rejected (attempt $attempt): origin/paper moved to ${remote:0:12}, recompute on top of it"
    git -C "$PAPER_DIR" fetch --no-tags --depth=1 origin +refs/heads/paper:refs/remotes/origin/paper
    git -C "$PAPER_DIR" reset -q --hard origin/paper
    "$@" || die "recompute failed (exit $?)"
    commit_or_stop
  done
}

: "${PAPER_DIR:?PAPER_DIR is not set}"
case "${1:-}" in
  checkout) checkout ;;
  push)
    shift
    push "$@"
    ;;
  *) die "usage: paper_sync.sh checkout | push RECOMPUTE..." ;;
esac
```

**File: `.github/workflows/paper.yml`**

```yaml
# ewbot paper-trading runtime (Plan 2 spec 6.1): one deterministic recompute per hour.
# Code comes from master; ledger.json / report.html / runs.log live on the orphan `paper` branch,
# which only this workflow writes (docs/paper-trading.md: never push to it by hand).
# Every fetch is shallow: the paper branch gets one commit per hour and its history is never needed here.
# The git side (paper worktree, commit, push and retry) is scripts/paper_sync.sh.
# The Telegram secrets reach only the steps that can send: the paper run, the push step (it reruns
# ewbot when origin/paper moved) and the failure alert.
name: paper

# Primary trigger: an external Cloudflare Workers cron calls workflow_dispatch hourly at :37
# (ops/cloudflare-trigger, docs/paper-trading.md). GitHub drops many scheduled runs when Actions is busy,
# so `schedule` is only a sparse backup (minute 52, every 3 hours, off the top of the hour).
on:
  schedule:
    - cron: "52 */3 * * *"
  workflow_dispatch:

concurrency:
  group: paper
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  paper:
    # pinned while paper trading runs (spec 8): the runner image is part of each run's recorded environment
    runs-on: ubuntu-24.04
    # backstop only: the steps below have their own timeouts, so a hang fails its step and alerts
    timeout-minutes: 100
    env:
      EWBOT_RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
    steps:
      - name: Check out master
        uses: actions/checkout@v7
        with:
          ref: master

      - name: Fetch the holdout commit and check out the paper branch (orphan on first use)
        id: checkout-paper
        timeout-minutes: 5
        run: |
          set -euo pipefail
          echo "PAPER_DIR=$RUNNER_TEMP/paper" >> "$GITHUB_ENV"
          echo "EWBOT_FAILURE_MARKER=$RUNNER_TEMP/ewbot-failure-notified" >> "$GITHUB_ENV"
          git config --global user.name "ewbot-paper"
          git config --global user.email "ewbot-paper@users.noreply.github.com"
          # 64e628a: the fingerprint baseline, read once when ledger.json is created (spec 5.1)
          git fetch --no-tags --depth=1 origin 64e628ad4865b3b04571a7cba2886881eae19176
          PAPER_DIR="$RUNNER_TEMP/paper" bash scripts/paper_sync.sh checkout

      - name: Install uv (with its cache)
        uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          python-version: "3.12.13"
          enable-cache: true
          cache-dependency-glob: uv.lock

      - name: Restore the candle cache
        uses: actions/cache@v6
        with:
          path: data/cache
          key: btc-usd-1h-${{ github.run_id }}-${{ github.run_attempt }}
          restore-keys: |
            btc-usd-1h-

      - name: Sync dependencies
        timeout-minutes: 10
        run: uv sync --frozen

      # 1-2 min per hourly run, 10-20 min for the daily criteria 1-2 recompute
      - name: Paper run
        id: paper
        timeout-minutes: 35
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run ewbot paper run --ledger-dir "$PAPER_DIR"

      # 3 pushes at most; the same commit again when origin/paper did not move, ewbot reruns only when it did
      - name: Commit and push the ledger
        id: push
        timeout-minutes: 35
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: bash scripts/paper_sync.sh push uv run ewbot paper run --ledger-dir "$PAPER_DIR"

      - name: Telegram failure alert (fallback when ewbot did not send one)
        id: alert
        if: failure() || cancelled()
        timeout-minutes: 2
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          JOB_STATUS: ${{ job.status }}
        run: |
          set -euo pipefail
          if [ -f "${EWBOT_FAILURE_MARKER:-/nonexistent}" ]; then
            echo "ewbot already sent the failure alert"
            exit 0
          fi
          if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
            echo "no Telegram secrets: skipping the alert"
            exit 0
          fi
          text="[ewbot paper] 실패 · 단계 workflow (checkout / setup / sync / timeout / push) · ${JOB_STATUS}"$'\n'"로그: ${EWBOT_RUN_URL}"
          # --fail-with-body: an HTTP error fails the step and prints Telegram's reply (never the token)
          # worst case 4 x 20 s + 1 + 2 + 4 s of retry waits = 87 s, inside the 2-minute step timeout
          curl -sS --fail-with-body --retry 3 --max-time 20 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${text}"
          echo
```

**File: `.github/workflows/ci.yml`**

```yaml
# Strategy-fingerprint guard (Plan 2 spec 8): on every pull request to master and every push to master,
# check that the strategy files still hash to the holdout baseline (the value is pinned in the test, so
# no git history is needed) and run the static allow_holdout / workflow guards. No secrets, read-only.
# It reports; it does not block a merge unless you make this check required (docs/paper-trading.md).
name: ci

on:
  pull_request:
    branches: [master]
  push:
    branches: [master]

permissions:
  contents: read

jobs:
  guard:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7

      - name: Install uv (with its cache)
        uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          python-version: "3.12.13"
          enable-cache: true
          cache-dependency-glob: uv.lock

      - name: Sync dependencies
        run: uv sync --frozen

      - name: Strategy fingerprint and static guards
        run: uv run pytest -q tests/paper/test_fingerprint.py tests/paper/test_static.py
```

**File: `docs/paper-trading.md`**

````markdown
# Paper-trading runtime (Plan 2) — operations

Spec: `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`.

## What runs

- `.github/workflows/paper.yml` runs `uv run ewbot paper run --ledger-dir <paper worktree>` every hour on `ubuntu-24.04`. The primary trigger is an external Cloudflare Workers cron that calls `workflow_dispatch` at minute 37 of every hour (see Reliable hourly trigger); the workflow's own `schedule` (`52 */3 * * *`: minute 52, every 3 hours) is a sparse backup, and you can also start it by hand. `scripts/paper_sync.sh` does the git side: the `paper` worktree, the commit, the push and its retries.
- GitHub's own `schedule` is unreliable under load: it can start a run late (minutes, sometimes much more) or drop it (as of 2026-10-03 only 2 of about 13 hourly `7 * * * *` runs had fired), which leaves hours without a successful run. That is why the hourly trigger is external. Criterion 4's 95% coverage target tolerates up to 33 missed hours in 28 days (5% of 672 hours is 33.6). Coverage counts the hours with a successful run, whatever triggered it (`workflow_dispatch` or `schedule`).
- `.github/workflows/ci.yml` runs the strategy-fingerprint and static guards on every pull request to `master` and every push to `master` (see Strict fingerprint policy).
- Each run recomputes the whole engine deterministically from 2017 with the params in the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), conservative risk, breakers on and Plan 1 costs. Trading starts at the holdout start (2024-01-01), so the run reproduces the holdout's A run and continues it.
- Paper trades are the trades that **enter after the holdout's last bar** (`entry_ts > 1790938800`, 2026-10-02 11:00 UTC). A position already open at that bar belongs to the holdout and is never counted twice.
- Results go to the orphan branch `paper` (never to `master`). Only the workflow writes to `paper`: never push to it by hand (see Failures).
  - `ledger.json`: baseline fingerprint and commit, holdout summary, epochs, closed trades, open position, decisions, discrepancies, runs (each with its runtime environment), sent and pending Telegram events, the last criteria 1–2 result.
  - `report.html`: go-live status at the top, out-of-sample equity and drawdown, trade tables with snapshot charts, current top count, discrepancy list, epochs.
  - `runs.log`: one line per successful run (time, status, last bar, data hash, N/50, epoch).
- The candle cache `data/cache/BTC-USD_1h.parquet` lives in the Actions cache (a new entry per run, restored from the newest one). A missing cache means a full download (about 70 s).

## One-time setup (you)

1. Merge `feat/plan2-paper` into `master` through a pull request, after its `ci` check has passed. Scheduled workflows only run from the default branch.
2. Telegram bot:
   1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`. BotFather replies with the token (digits, a colon, then letters and digits). Keep it private.
   2. Open a chat with your new bot and send it any message (for example `hi`). A bot cannot write to you before you have written to it.
   3. Find your chat ID in a terminal. The token is typed at a hidden prompt, so it ends up neither in your shell history nor in a browser history:

      ```bash
      printf 'Bot token: '; read -rs TELEGRAM_BOT_TOKEN; echo
      curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d if not d.get('ok') else {m['chat']['id'] for u in d['result'] for m in u.values() if isinstance(m,dict) and 'chat' in m})"
      unset TELEGRAM_BOT_TOKEN
      ```

      The number printed in braces is your chat ID (a group chat ID starts with `-`). `set()` means the bot has not received a message yet — message the bot (not BotFather) and retry. If a dictionary with `'ok': False` is printed instead, Telegram rejected the request; its `description` says why (usually a mistyped token).
   4. Optionally check both values with a test message (the commands are the same in bash and zsh; they print `OK` or `FAILED`):

      ```bash
      printf 'Bot token: '; read -rs TOKEN; echo; printf 'Chat ID: '; read -r CHAT; curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" -d chat_id="$CHAT" -d text="ewbot test" | python3 -c "import sys,json; print('OK' if json.load(sys.stdin).get('ok') else 'FAILED')"; unset TOKEN CHAT
      ```
3. Store both values as repository secrets. Run each command and paste the value at the prompt (so it does not end up in your shell history):

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   ```

   Without these secrets the runtime still runs and records everything; it only skips Telegram.
4. Set a small Actions budget, for example $5, at <https://github.com/settings/billing> (budgets and alerts, product Actions). With the default $0 budget GitHub stops running the workflow once the month's free minutes are used up, and every missed hour counts against criterion 4; a small budget keeps the runs going and caps what they can cost (see Cost).
5. Start the first run by hand and watch it (then set up the hourly trigger below):

   ```bash
   gh workflow run paper.yml
   gh run watch "$(gh run list --workflow paper.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

## Reliable hourly trigger (Cloudflare Workers)

GitHub drops many scheduled runs when Actions is busy, so the hourly run is started from outside: a Cloudflare Workers cron calls the GitHub API (`workflow_dispatch` on `paper.yml`, ref `master`) at minute 37 of every hour (UTC). The workflow's `schedule: "52 */3 * * *"` stays as a backup (8 runs a day); the `paper` concurrency group keeps overlapping runs from running together. The worker is `ops/cloudflare-trigger/worker.js` (no dependencies). One-time setup (you):

1. Create a fine-grained personal access token at <https://github.com/settings/personal-access-tokens/new>:
   - Resource owner: `taejunoh`.
   - Repository access: **Only select repositories** → `elliott-wave-btc-bot`.
   - Permissions → Repository permissions → **Actions: Read and write**. Nothing else.
   - Expiration: for example 1 year. Put the renewal date in your calendar: once the token expires the primary trigger stops (the worker's runs fail with HTTP 401) and only the 3-hourly backup schedule keeps running, which leaves most hours uncovered.
   - Generate it and copy the token; GitHub shows it once.
2. In the Cloudflare dashboard open **Workers & Pages** → **Create** → **Create Worker**. Name it (for example `ewbot-paper-trigger`), **Deploy** the hello-world stub, then **Edit code**, replace everything with the contents of `ops/cloudflare-trigger/worker.js`, and **Deploy** again.
3. Worker → **Settings** → **Variables and Secrets** → **Add** → type **Secret**, name `GITHUB_TOKEN`, value: the token from step 1 → **Deploy**.
4. Worker → **Settings** → **Trigger Events** → **Cron Triggers** → **Add** → `37 * * * *` (Cloudflare cron runs in UTC).
5. Verify, either way:
   - In the Worker's **Settings** → **Trigger Events** (or the editor's **Schedule** test panel) run the cron trigger by hand, or just wait for the next :37.
   - Then check that a `workflow_dispatch` run was created:

     ```bash
     gh run list --workflow paper.yml --limit 5
     ```

     The `EVENT` column shows `workflow_dispatch` for the worker's runs and `schedule` for the backup. If nothing appears, open the Worker's **Observability** / **Logs**: the worker logs the HTTP status and GitHub's reply (never the token). 401 = wrong or expired token, 403 or 404 = the token lacks Actions: Read and write on this repository.

## Reading the results

```bash
git fetch origin paper
git show origin/paper:runs.log | tail -n 5
git show origin/paper:report.html > /tmp/ewbot-paper-report.html && open /tmp/ewbot-paper-report.html
```

Locally, with a worktree of the `paper` branch (`git worktree add ../paper-ledger paper`), `uv run ewbot paper status --ledger-dir ../paper-ledger` prints the go-live block, the open position, the epoch, the last run (data hash, holdout data check, holdout trades recomputed vs the lock's 36) and the discrepancy and pending-event counts.

## Telegram messages

| Message | When |
|---|---|
| 신규 진입 | a new entry fill (setup, side, entry, stop, TP1/TP2, blended R, top count q / S_rev) |
| 손절 이동 (본전 / 추적) | breakeven after TP1, or a structural trailing move |
| 청산 | the fill that closed a trade (reason, PnL, R, out-of-sample N/50) |
| 일일 요약 | first run at/after 09:00 New York time each day: position, equity, N/50, go-live status (criteria 1–2 are recomputed in this run: A/HYBRID vs C/BH/EMA/RANDOM x100). If that recompute fails, the run still completes, a warning is sent and the next run retries once; if the retry fails too, the summary is sent marked `기준 미갱신(stale)` with the previous criteria 1–2 result, and the next try is on the next New York date |
| 경고 | strategy fingerprint change (new epoch), decision discrepancy (with its cause, see Frozen runtime environment), holdout data hash mismatch, run gap over 3 hours |
| 실패 | the failed stage and the Actions log link (sent by `ewbot`; the workflow sends a fallback for checkout / setup / sync / push failures, step timeouts and cancellations) |

Each message has an id `kind:ts:trade_key`; an id is sent at most once per pushed ledger. A run killed after sending, or whose pushes all fail, may send it again in the next run, but a message is never lost. A failed send is retried 3 times and then kept pending for the next run.

## Strict fingerprint policy

The strategy fingerprint covers `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (every byte, comments included). Its baseline is the same file set at the holdout commit `64e628a` (`e0435223a3f9…`). If any of these files changes, the next run opens a new epoch, sends a warning and resets the go-live counter to 0; the holdout's 36 trades no longer count.

Reverting the change does not bring the 36 back. The revert opens yet another epoch, with the baseline fingerprint, so it is eligible for go-live again, but its count starts at 0 paper trades and the holdout's 36 trades never count again.

The guard is `.github/workflows/ci.yml`. On every pull request to `master` and every push to `master` it runs `tests/paper/test_fingerprint.py`, which compares the working tree with the pinned baseline value (no git history needed), and `tests/paper/test_static.py` (the `allow_holdout` and workflow guards). A red `ci` check on a pull request means that merging it resets the counter; on a push to `master` it means the next hourly run will. The check only reports: GitHub merges a pull request with a red check unless you make the `guard` check required in the branch protection settings. On GitHub Free, branch protection rules and rulesets are not available for private repositories, so for a private repository there the `guard` check stays advisory: never merge a pull request whose `guard` check is red.

The precheck also binds the run to the lock's whole configuration: it refuses to run when the lock's `sha256` is not `lock_digest(BotConfig(), params, random_runs, code_sha256)`. A changed `BotConfig` default (in `config.py`) or walk-forward grid therefore stops the hourly runs with a precheck failure alert instead of opening a new epoch. Revert it to resume: no epoch was opened, so epoch 1 and the holdout's 36 trades still count, but the hours without a successful run count against criterion 4's coverage.

## Frozen runtime environment

Each run records its runtime environment in `ledger.json` (`runs[].env`): the Python version, the operating system and machine architecture (`platform.system()` / `platform.machine()`), the numpy, pandas and pyarrow versions, the sha256 of `uv.lock` and the runner image (`ImageOS` / `ImageVersion`, set by GitHub's hosted runners). `ImageVersion` is recorded but not compared: it is left out of the digest, because GitHub refreshes the image weekly and comparing it would hide real `UNEXPLAINED` discrepancies behind `ENVIRONMENT_CHANGE`. Each decision keeps the digest of the environment of the run that recorded it, and `report.html` shows the current one. A decision that no longer reproduces is classified in this order:

1. `DATA_REVISION`: a bar up to the decision changed.
2. `FINGERPRINT_CHANGE`: it was recorded in another epoch.
3. `ENVIRONMENT_CHANGE`: this is the first run since the runtime environment changed (the previous successful run had another one), and the run that recorded the decision (or should have) had another one too. Every run rechecks every recorded decision, so once a run in the new environment has reproduced a decision, a later difference is `UNEXPLAINED`. It is warned on Telegram and listed in the report and in the go-live block, but criterion 4 does not count it as `UNEXPLAINED`.
4. `UNEXPLAINED`: anything else. One is enough to fail criterion 4.

While paper trading runs, keep the environment frozen:

- Do not change `uv.lock`: no `uv lock --upgrade`, no dependency bumps, no merged dependency-update pull requests.
- Do not change the Python version: `python-version: "3.12.13"` in `paper.yml` and `ci.yml` and `requires-python` in `pyproject.toml`.
- Do not change the runner image: `runs-on: ubuntu-24.04` in `paper.yml`, never `ubuntu-latest`.

GitHub updates the `ubuntu-24.04` image itself (a new `ImageVersion`, often weekly). That is recorded in `runs[].env` but does not change the digest, so it never turns a discrepancy into `ENVIRONMENT_CHANGE`. The Python patch is pinned to `3.12.13`, so a different patch (or a new `ImageOS`, operating system, machine architecture, numpy, pandas, pyarrow or `uv.lock`) is what changes the digest, and a discrepancy found by the first run after such a change is `ENVIRONMENT_CHANGE`. Criterion 4 does not count those, so look at every `ENVIRONMENT_CHANGE` in the report yourself: it is a decision that did not reproduce.

## Failures

| Situation | What happens |
|---|---|
| Coinbase fetch fails | 3 tries, then the run stops without a commit and alerts; the next run recomputes the missing bars |
| Cache lost | full download; changed past bars show up as `DATA_REVISION` discrepancies |
| `paper` unreadable at checkout | `git ls-remote` failed (network, auth): the job fails and alerts. Only a definite "no such branch" (first run) starts an orphan `paper`, so an existing ledger is never replaced by a fresh one |
| `ewbot` hangs | `Paper run` and the push step time out after 35 minutes (the daily recompute takes 10–20); the step fails and the workflow's fallback alert goes out. The alert also runs when the job is cancelled |
| Push to `paper` fails | 3 pushes at most, then the job fails and alerts. If `origin/paper` did not move (network, a transient error), the same commit is pushed again after 5 s, then 10 s, without a recompute, so no Telegram message goes out twice. If `origin/paper` already holds the commit, the push landed. Only if `origin/paper` moved is the ledger reset to it, recomputed and pushed on top; that recompute may resend this run's Telegram messages |
| Someone pushes to `paper` | runs never overlap (`concurrency: paper`), so a moved `origin/paper` means a push by hand: the next push recomputes on top of it (row above). Don't push to `paper`; read it with `git fetch` / `git show` (Reading the results) |
| Actions itself stops | not detectable from inside a run: GitHub's failure e-mail and a missing daily summary are the alarm |

## Cost

Inputs: about 1–2 minutes per run, plus the daily criteria 1–2 recompute (103 engine runs, roughly 10–20 minutes on a 2-vCPU runner) inside one run a day. There are 24 hourly `workflow_dispatch` runs plus 8 backup `schedule` runs a day, 32 runs in all (the backup run at :52 re-runs the engine on the same data, so it costs the same as an hourly run). GitHub rounds every job up to a whole minute (each run here is one job), so a run of 1 min 10 s costs 2 minutes.

| Estimate | Per day | 30-day month | 31-day month |
|---|---|---|---|
| low: 31 × 1 + 1 × (1 + 10) | 42 min | 1,260 min | 1,302 min |
| high: 31 × 2 + 1 × (2 + 20) | 84 min | 2,520 min | 2,604 min |

The high end is above the 2,000 free minutes a month of a private repository on GitHub Free, and it is the likely end once an hourly run takes just over a minute. Check the real usage in your GitHub billing settings after the first week.

Do not lower the cost by changing what runs: the 100 RANDOM runs are part of criteria 1–2 (fewer runs change the criteria), and the hourly trigger is what criterion 4 counts (do not drop the Cloudflare trigger to save minutes; the backup alone covers only every third hour). If a month would go over the free minutes, either allow paid Actions minutes (the small budget of One-time setup step 4: with the default $0, GitHub stops running the workflow once the free minutes are used up, and every missed hour counts against criterion 4), or move the job to a self-hosted runner (`runs-on: self-hosted`: no Actions minutes, but that machine has to be online every hour, and its own image becomes the recorded environment).
````

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_static.py tests/paper/test_paper_sync.py -q
uv run python -c "import yaml; [yaml.safe_load(open(f'.github/workflows/{n}.yml')) for n in ('paper', 'ci')]" && echo yaml-ok
command -v actionlint >/dev/null && actionlint .github/workflows/paper.yml .github/workflows/ci.yml && echo actionlint-ok || echo 'actionlint not installed (optional)'
command -v shellcheck >/dev/null && shellcheck scripts/paper_sync.sh && echo shellcheck-ok || echo 'shellcheck not installed (optional)'
uv run pytest -q
git diff --stat master -- src/ewbot/types.py src/ewbot/config.py src/ewbot/indicators.py src/ewbot/data src/ewbot/waves src/ewbot/strategy src/ewbot/engine src/ewbot/execution src/ewbot/risk src/ewbot/backtest holdout.lock pyproject.toml uv.lock
```

Expected: `42 passed`; `yaml-ok`; `actionlint-ok` / `shellcheck-ok` when installed; the full suite `620 passed, 1 skipped` without a candle cache (`621 passed` when `data/cache/BTC-USD_1h.parquet` exists); the final `git diff --stat` prints nothing (every fingerprinted file, `backtest/`, the lock and the dependencies are byte-identical to `master`).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/paper.yml .github/workflows/ci.yml scripts/paper_sync.sh docs/paper-trading.md tests/paper/test_static.py tests/paper/test_paper_sync.py
git commit -m "$(cat <<'EOF'
ci(paper): hourly paper workflow, allow_holdout static guard and ops guide

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Manual pre-deploy checklist (controller and user only — not for subagents)

Spec §7.3. These steps touch real candles at/after 2024-01-01, the network and GitHub; nothing here is automated and no code changes.

**Files:** none (a throw-away ledger directory outside the repository).

**Interfaces:**
- Consumes: `ewbot paper run | status` (Task 8), the committed `holdout.lock`, the real candle cache, `gh`.
- Produces: the go/no-go decision for deploying the workflow.

- [ ] **Step 1: Confirm the build**

```bash
git status --short
uv run pytest -q
uv run pytest tests/paper/test_fingerprint.py -q -k baseline
```

Expected: clean tree; `621 passed` (with the real cache present); both fingerprint tripwires pass, i.e. the working-tree fingerprint equals the pinned value and the `64e628a` baseline `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`.

- [ ] **Step 2: One local run on real data (no Telegram, throw-away ledger)**

```bash
env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID uv run ewbot paper run --ledger-dir /tmp/ewbot-paper-dryrun
uv run ewbot paper status --ledger-dir /tmp/ewbot-paper-dryrun
```

Expected: exit 0. If the local time is after 09:00 New York the run also recomputes criteria 1–2 (RANDOM x100, a few minutes). In the status output check:
- `epoch 1 of 1: fingerprint e0435223a3f9 (baseline e0435223a3f9 @ 64e628a)`;
- `holdout data ok` (the holdout bars hash to the lock's `f8512fdbcd8d52340b0a8a43fad89c0a57ea2ef666248fe2dfc61877e01541cb`);
- `holdout trades recomputed 36 (lock: 36)` — the recompute reproduces the holdout's A run.

Any other value is a no-go: stop and investigate before deploying.

- [ ] **Step 3: Review the out-of-sample trades since the holdout**

```bash
uv run python - <<'EOF'
import json
from datetime import datetime, UTC
led = json.load(open("/tmp/ewbot-paper-dryrun/ledger.json"))
fmt = lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M")
for t in led["trades"]:
    print(fmt(t["entry_ts"]), fmt(t["exit_ts"]), t["setup"], t["direction"], t["exit_reason"], round(t["pnl"], 2), round(t["r_multiple"], 2))
print("open position:", led["open_position"])
print("decisions:", len(led["decisions"]), "discrepancies:", len(led["discrepancies"]))
EOF
open /tmp/ewbot-paper-dryrun/report.html
```

Expected: only trades with entry after 2026-10-02 11:00 UTC; 0 discrepancies; the report's top block shows criteria 1–4 and `=> 실거래 전환 검토 불가` (criterion 3 reads 36 plus the few paper trades so far, out of 50). Then remove the throw-away directory: `rm -r /tmp/ewbot-paper-dryrun`. The official ledger is written only by the Actions runner (Linux), so that recorded decisions and their later recomputation always share one platform's floating-point and libm behaviour; never seed the `paper` branch with a ledger produced on macOS.

- [ ] **Step 4: Merge and configure (user)**

Open a PR from `feat/plan2-paper` to `master`, check that its `ci` run (fingerprint and static guards) is green, and merge it (scheduled workflows run only from the default branch). Then follow `docs/paper-trading.md` → "One-time setup": create the bot with @BotFather, message it once, read the chat ID with the guide's `curl … getUpdates` command (token typed at a hidden prompt), and run `gh secret set TELEGRAM_BOT_TOKEN` / `gh secret set TELEGRAM_CHAT_ID` (values typed by the user at the prompt). Set a small Actions budget (for example $5) at github.com/settings/billing, so the hourly runs do not stop when the free minutes run out (guide, One-time setup step 4).

- [ ] **Step 5: First Actions run**

```bash
gh workflow run paper.yml
gh run watch "$(gh run list --workflow paper.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
git fetch origin paper && git log --oneline -3 origin/paper && git show origin/paper:runs.log | tail -n 3
```

Then check the Actions-produced ledger the same way as Step 2:

```bash
git worktree add /tmp/ewbot-paper-branch origin/paper
uv run ewbot paper status --ledger-dir /tmp/ewbot-paper-branch
git worktree remove /tmp/ewbot-paper-branch
```

Expected: the run succeeds; the orphan branch `paper` exists with one commit holding `ledger.json`, `report.html`, `runs.log`; the status shows epoch 1 with the baseline fingerprint, `holdout data ok` and `holdout trades recomputed 36 (lock: 36)` on the Linux runner too; Telegram (if configured) received the entry / exit messages for the paper trades so far. A mismatch in the holdout reproduction on Linux is a no-go: disable the workflow (`gh workflow disable paper.yml`) and investigate.

- [ ] **Step 6: Observe 24 hours of scheduled runs**

```bash
gh run list --workflow paper.yml --limit 30
git fetch origin paper && git show origin/paper:runs.log | tail -n 30
```

Expected: about 24 successful runs (one per hour at minute ~7, delays of a few minutes are normal), no `실행 공백` warning, exactly one `일일 요약` after 09:00 New York, `ewbot paper status` (on a worktree of `paper`) showing coverage near 100% and 0 unexplained discrepancies. Note the billed minutes of an hourly run and of the daily-summary run against the guide's Cost table (its high end, 2,040–2,108 minutes a month, is above the 2,000 free minutes). The daily-summary run must finish under ~20 min; otherwise investigate before leaving the workflow unattended. Record the outcome in the PR or the project notes.
