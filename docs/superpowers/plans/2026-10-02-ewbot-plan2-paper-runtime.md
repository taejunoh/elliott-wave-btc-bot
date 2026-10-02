# ewbot Plan 2 — Paper-Trading Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the frozen holdout strategy (A with K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5 from `holdout.lock`) as an hourly, deterministic paper-trading runtime on GitHub Actions that keeps accumulating out-of-sample trades toward the 50-trade go-live minimum, proves for at least 4 weeks that the decisions recorded in real time equal the decisions recomputed later, and reports through a ledger and HTML report on an orphan `paper` branch and through Telegram.

**Architecture:** Every run recomputes the whole engine from the first cached bar with `run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)` (spec approach A), inside a read-only trace that records fills, stop moves and order sizes. Trades entering after the holdout's last bar are the paper trades. A JSON ledger stores epochs (strategy fingerprint), trades, decisions (with run id and the hash of the bars they depended on), discrepancies, runs and Telegram delivery state; every file write is temp file + `os.replace`. No strategy-fingerprinted module is edited: new code lives in `src/ewbot/paper/`, plus a `paper` sub-command in `cli.py`, `.github/workflows/paper.yml` and an ops guide.

**Tech Stack:** Python 3.12 (uv), numpy, pandas, pyarrow, requests, plotly (existing dependencies only); pytest; GitHub Actions (`actions/checkout@v7`, `astral-sh/setup-uv@v10`, `actions/cache@v6`); Telegram Bot API `sendMessage`.

**Spec:** `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md` (parent: `docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md`).

## Global Constraints

- Work on branch `feat/plan2-paper`, from the repository root; Python 3.12 via uv; tests with `uv run pytest`. No new dependencies (`pyproject.toml` and `uv.lock` unchanged).
- Never modify a strategy-fingerprinted file (spec §5.1): `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. Do not edit `backtest/**`, `data/coinbase_history.py` or `holdout.lock` either. Allowed: create `src/ewbot/paper/*`, `tests/paper/*`, `.github/workflows/paper.yml`, `docs/paper-trading.md`; modify `src/ewbot/cli.py`.
- Fingerprint = `sha256( for path in sorted(paths): path + "\0" + bytes + "\0" )` over the repo-relative paths (`src/ewbot/<path>`) of that file set (25 files today). Baseline = the same set at holdout commit `64e628ad4865b3b04571a7cba2886881eae19176` (`64e628a`), read with `git cat-file blob` (the plain `git show 64e628a:<path>` bytes). Today both are `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`.
- Frozen inputs: params only from `holdout.lock` (`[2.0, 0.7, 0.5]`); config = `holdout_config(BotConfig(), params)` (strategy A, conservative 1% / 3x, breakers on, fee 0.05%, slippage 0.05%, funding 10% APR long); recompute with `start_ts = HOLDOUT_START = 1704067200`; no CLI option changes any of it.
- Holdout boundary: `last_bar_ts = 1790938800` (2026-10-02 11:00 UTC); paper trades have `entry_ts > last_bar_ts`; holdout `n_trades.A = 36`. A later epoch counts `entry_ts > epoch.started_ts`.
- Go-live count (criterion 3) = holdout 36 (only while the ledger never left epoch 1) + closed paper trades of the current epoch; required 50; eligible only while the current epoch's fingerprint equals the baseline.
- Decisions: `SIGNAL, ENTRY, TP1, TP2, STOP, EXIT, BREAKEVEN, TRAIL` as `(kind, ts, price, contracts, reason)` + `trade_key, levels, setup, direction, run_id, data_sha256, epoch_id`; id `kind:ts:trade_key`; prices and levels compared with relative tolerance `1e-9`; causes `DATA_REVISION` > `FINGERPRINT_CHANGE` > `UNEXPLAINED`.
- Criterion 4: at least 28 days since the epoch's first successful run, 0 `UNEXPLAINED` discrepancies in the epoch, at least 95% of hours with a successful run.
- Telegram: secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; without them nothing is sent and the run succeeds; each event id `kind:ts:trade_key` is sent at most once; 3 tries, then the event stays pending for the next run. Daily summary: first run at/after 09:00 America/New_York per New York date; criteria 1–2 (`compare`, RANDOM 100 runs, injectable) are recomputed only in that run. Gap warning: more than 3 h since the last successful run.
- Workflow: `cron: "7 * * * *"` + `workflow_dispatch`; `concurrency: {group: paper, cancel-in-progress: false}`; `permissions: contents: write`; `paper` branch worktree created as an orphan on first use; every fetch shallow (master, the `64e628a` commit and `paper` at depth 1); push retried 3 times (refresh + recompute).
- Tests never touch the network, the real `data/cache`, or `<repo>/holdout.lock`: synthetic candles (`tests/synth.random_bars`), temp git repos holding test locks, a fake sender and a fake fetcher. Never compute metrics on real data at/after 2024-01-01 except in the manual pre-deploy step (Task 11).
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Spec Interpretations (decided here, binding for the tasks)

1. **Recompute window.** The engine runs from the first cached bar (2017-01-01, warm-up) with `start_ts = HOLDOUT_START`, exactly like the holdout's A run. The paper recompute therefore *continues* the holdout run: trades with `HOLDOUT_START <= entry_ts <= last_bar_ts` must number 36 again (recorded per run as `holdout_trades_recomputed`, checked by hand in Task 11).
2. **Stop moves, TP1 partials and order sizes** are not in `EngineResult`. `paper/trace.py` swaps the names `PaperBroker` / `WaveEngine` that `ewbot.engine.loop` looks up at run time for pass-through subclasses that only log (no file is edited; a test proves the traced run is bit-identical to `run_backtest`).
3. **When a decision is known.** A run whose last closed bar is T knows every fill and stop move with `ts <= T` and every signal with `ts <= T - 1h` (the engine never evaluates entries on the last bar); the END close of a still-open position is not a decision. Decisions are recorded by the first run that knows them (`known_at > epoch.through_ts`).
4. **Epochs.** `started_ts` of epoch 1 is the holdout's last bar; a new epoch starts at the last bar of the run that saw the new fingerprint. Earlier epochs' trades are frozen; the current epoch's closed trades are recomputed every run. Reverting the code opens a new epoch with the baseline fingerprint: eligible again, but the holdout's 36 never count again (spec §8).
5. **Discrepancies** are `CHANGED` / `VANISHED` (a recorded decision differs / is gone) and `APPEARED` (the current epoch recomputes a decision an earlier run should have recorded). `DATA_REVISION` = the hash of all bars with `ts <=` the decision's ts changed (for `APPEARED`: the hash of the bars seen by the first run that should have known it). Each discrepancy is stored and alerted once.
6. **Telegram without secrets** drops events instead of queueing them (a token added later must not flood the chat). A failed send keeps that event and every later one pending, in order.
7. **Criteria 1–2** = `backtest.holdout.evaluate_criteria(compare(cfg, df, HOLDOUT_START, last_bar + 1h, random_runs, workers, allow_holdout=True))`, i.e. over `[HOLDOUT_START, last bar + 1h)`, only while the current epoch is eligible; the stored result is ignored once the epoch changes. The go-live candidate is A, the only strategy the runtime trades and counts (criteria 3–4 are about A's trades), so — like `evaluate_criteria`'s `passing` — one candidate must meet all four: criterion 1 = `"A" in winners`, criterion 2 = criterion 1 and A's max drawdown <= 15%. HYBRID is still compared and shown.
8. **Failure alerts** are sent by `ewbot paper run` (stage + `EWBOT_RUN_URL`); it touches `EWBOT_FAILURE_MARKER`, and the workflow's `if: failure()` step only sends its own alert when that marker is missing (checkout / setup / sync / push failures).
9. **Ledger fields beyond the spec list:** `pending_events`, `daily_summary_date`, `criteria`, and `through_ts` in each epoch.

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
| `src/ewbot/paper/runner.py` | 7 | one `paper run`: precheck, data, epoch, recompute, ledger, notify |
| `src/ewbot/cli.py` (modify) | 8 | `ewbot paper run` and `ewbot paper status` |
| `tests/paper/helpers.py` | 3 | synthetic candles, test lock in a temp git repo, fakes |
| `tests/paper/test_*.py` | 1–10 | unit, integration (time walk) and static tests |
| `.github/workflows/paper.yml` | 10 | hourly Actions workflow |
| `docs/paper-trading.md` | 10 | ops guide, Telegram setup for the user |

## Task Overview

| Task | Component | New tests |
|---|---|---|
| 1 | Strategy fingerprint | 18 |
| 2 | Ledger model, atomic IO, epochs | 9 |
| 3 | Engine trace + shared test helpers | 6 |
| 4 | Out-of-sample extraction, decisions, replay check | 12 |
| 5 | Telegram notifier, formats, timing, events | 21 |
| 6 | Go-live criteria + paper HTML report | 7 |
| 7 | Paper runner orchestration | 19 |
| 8 | CLI `paper run` / `paper status` | 6 |
| 9 | Time-walk integration test | 8 |
| 10 | Static `allow_holdout` guard, workflow, ops guide | 4 |
| 11 | Manual pre-deploy checklist (controller) | — |

Baseline before Task 1: `uv run pytest -q` → `443 passed, 1 skipped` without a candle cache (`444 passed` when `data/cache/BTC-USD_1h.parquet` exists and the real-data smoke test runs). After Task 10: 110 more tests.

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

Create the test package and the fingerprint tests. `test_baseline_recomputes_from_git_and_equals_the_working_tree` is a tripwire: it recomputes the `64e628a` baseline from git and requires it to equal the working tree (true today: Plan 1 merged without later edits to these files; it is skipped only in a clone without that commit).

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

Expected: `18 passed`; both lines print `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1` (the second with `'64e628ad4865b3b04571a7cba2886881eae19176'`).

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
- Consumes: module globals `ewbot.engine.loop.PaperBroker` / `ewbot.engine.loop.WaveEngine` (looked up by `_Run.__init__` and `TradingEngine.run`); `PaperBroker.process_bar(bar) -> list[Fill]`, `queue_entry(direction, contracts, stop, tp1, tp2, tp1_contracts)`, `set_stop(price)`, `close_now(ts, price) -> Fill | None`; `WaveEngine.on_1h_close(bar1h, bar4h, sub_bars) -> WaveState`; `run_backtest(cfg, df1h, start_ts=None, end_ts=None, *, allow_holdout=False) -> EngineResult`; `holdout_config(cfg, params) -> BotConfig`; `data_fingerprint(df1h, start_ts, last_ts) -> str`; `tests.synth.random_bars(seed, n, vol, start)`.
- Produces (`ewbot.paper.trace`): `OrderRec(ts, direction, contracts, stop, tp1, tp2)`, `FillRec(ts, kind, contracts, price, trade_key)`, `StopRec(ts, trade_key, old, new, kind)` (`kind` `"BREAKEVEN"` / `"TRAIL"`), `EngineTrace(orders, fills, stops, end_fill, last_state, last_ts, brokers)`, `traced_engine() -> ContextManager[EngineTrace]`.
- Produces (`tests.paper.helpers`): `PARAMS = (2.0, 0.5, 0.0)`, `SEED = 2`, `N_BARS = 3000`, `BOUNDARY = 1704844800` (2024-01-10 00:00 UTC), `HOLDOUT_A_TRADES = 2`, `ts(text) -> int`, `synth_frame(n=N_BARS, seed=SEED) -> DataFrame`, `make_lock(df, boundary=BOUNDARY, params=PARAMS, n_a=2, **overrides) -> dict`, `git(repo, *args)`, `commit_lock(repo, lock) -> Path`, `FakeSender(fail=0)` (`.texts`, `.calls`, `.fail`), `FakeFetch(source, clock, fail=0)` (`.calls`, `.fail`), `Clock(now)` (`.now`).
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

from ewbot.backtest.holdout import data_fingerprint, holdout_config
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.types import H1
from tests.synth import random_bars

PARAMS = (2.0, 0.5, 0.0)  # a grid combo that trades often on the synthetic path
SEED = 2
N_BARS = 3000  # 2023-11-15 00:00 .. 2024-03-18 23:00 UTC
BOUNDARY = 1_704_844_800  # 2024-01-10 00:00 UTC: the synthetic holdout's last bar
HOLDOUT_A_TRADES = 2  # synthetic A trades entering in [HOLDOUT_START, BOUNDARY]


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
    """A completed-holdout lock with the real schema (v3) for synthetic data."""
    cfg = holdout_config(BotConfig(), params)
    lock = {
        "version": 3,
        "keys": ["wave.K4", "strategy.Q_MIN", "strategy.S_MIN"],
        "params": list(params),
        "sha256": "d" * 64,
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
- Produces (`ewbot.paper.decisions`): `REL_TOL = 1e-9`, `DECISION_KINDS`, `CAUSES`, `split_result(res, trace) -> tuple[list[Trade], Trade | None]`, `paper_trades(closed, boundary_ts) -> list[Trade]`, `decision_id(kind, ts, trade_key) -> str`, `known_at(d) -> int`, `build_decisions(res, trace, trades) -> list[dict]` (keys `id, kind, ts, price, contracts, reason, trade_key, levels, setup, direction`), `same_decision(a, b) -> bool`, `classify(recorded, data_revised, current_epoch_id) -> str`, `verify_decisions(recorded, recomputed, epoch, revised_at, appeared_revised) -> list[dict]` (keys `id, type, decision_id, cause, recorded, recomputed`), `decisions_to_record(recomputed, epoch, recorded_ids) -> list[dict]`, `open_position_record(open_trade, trace, epoch_start) -> dict`, `top_count_summary(ws) -> dict | None`.

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
    assert classify(rec, False, 2) == "FINGERPRINT_CHANGE"
    assert classify(rec, False, 1) == "UNEXPLAINED"
    assert classify(None, False, 2) == "UNEXPLAINED"


def _record(decs, df, epoch_id=1):
    return [{**d, "run_id": "r1", "epoch_id": epoch_id, "data_sha256": data_fingerprint(df, 0, d["ts"])} for d in decs]


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
CAUSES = ("DATA_REVISION", "FINGERPRINT_CHANGE", "UNEXPLAINED")
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


def classify(recorded: dict[str, Any] | None, data_revised: bool, current_epoch_id: int) -> str:
    """DATA_REVISION if a bar up to the decision changed; else FINGERPRINT_CHANGE if the decision
    was recorded under another epoch; else UNEXPLAINED."""
    if data_revised:
        return "DATA_REVISION"
    if recorded is not None and int(recorded["epoch_id"]) != int(current_epoch_id):
        return "FINGERPRINT_CHANGE"
    return "UNEXPLAINED"


def verify_decisions(
    recorded: Sequence[dict[str, Any]],
    recomputed: Sequence[dict[str, Any]],
    epoch: dict[str, Any],
    revised_at: Callable[[dict[str, Any]], bool],
    appeared_revised: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    """Replay check. Every recorded decision must reappear unchanged (else CHANGED / VANISHED); a
    recomputed decision of the current epoch that a previous run should already have known but did
    not record is APPEARED. revised_at(recorded) / appeared_revised(recomputed) say whether the bars
    up to that decision changed since it was (or should have been) recorded."""
    by_id = {d["id"]: d for d in recomputed}
    rec_ids = {d["id"] for d in recorded}
    found: list[dict[str, Any]] = []
    for d in recorded:
        r = by_id.get(d["id"])
        if r is not None and same_decision(d, r):
            continue
        kind = "VANISHED" if r is None else "CHANGED"
        cause = classify(d, revised_at(d), epoch["id"])
        found.append({"id": f"{kind}:{d['id']}", "type": kind, "decision_id": d["id"], "cause": cause,
                      "recorded": d, "recomputed": r})
    lo, hi = int(epoch["started_ts"]), int(epoch["through_ts"])
    for r in recomputed:
        if r["id"] in rec_ids or not lo < known_at(r) <= hi:
            continue
        cause = classify(None, appeared_revised(r), epoch["id"])
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

Expected: `12 passed`.

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
- Produces (`ewbot.paper.notify`): `NY = ZoneInfo("America/New_York")`, `DAILY_HOUR_NY = 9`, `GAP_WARN_S = 10800`, `SEND_ATTEMPTS = 3`, `TELEGRAM_API`, `HEAD = "[ewbot paper]"`, `class SendError`, `Sender` protocol `(text) -> None`, `TelegramSender(token, chat_id, session=None, timeout=15.0)`, `sender_from_env(env=None) -> TelegramSender | None`, `make_event(kind, ts, trade_key, text) -> dict` (`id, kind, text, attempts`), `deliver(pending, sent_ids, sender, attempts=3, sleep=time.sleep) -> tuple[list[dict], list[str], list[str]]` (still pending, newly sent ids, notes), `daily_summary_due(now_ts, last_date) -> str | None`, `last_ok_run_ts(runs) -> int | None`, `gap_seconds(now_ts, runs) -> int | None`, formats `entry_text(t, tp1_frac)`, `stop_move_text(dec, t)`, `close_text(t, count, counted)`, `daily_text(ny_date, position, equity, count, status)`, `epoch_text(epoch)`, `discrepancy_text(items)`, `data_hash_text(expected, got)`, `gap_text(seconds, last_ts)`, `failure_text(stage, message, run_url)`, `position_line(pos)`, `decision_events(fresh, trades, open_key, epoch_start, count, cur_closed, tp1_frac) -> list[dict]`, `queue_events(ledger, events) -> list[str]`.

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
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
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
    """POST sendMessage (plain text, no parse mode). Raises SendError on any failure."""

    def __init__(self, token: str, chat_id: str, session: Any | None = None, timeout: float = 15.0) -> None:
        self._url = TELEGRAM_API.format(token=token)
        self._chat_id = chat_id
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    def __call__(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            resp = self._session.post(self._url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise SendError(f"telegram request failed: {type(exc).__name__}") from None  # no token in messages
        ok = resp.status_code == 200 and bool(resp.json().get("ok"))
        if not ok:
            raise SendError(f"telegram HTTP {resp.status_code}: {resp.text[:200]}")


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
    batch: it and every later event stay pending, in order."""
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
            return rest, newly, [f"send failed for {ev['id']}: {err}"]
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


def daily_text(ny_date: str, position: str, equity: str, count: str, status: Sequence[str]) -> str:
    return "\n".join([f"{HEAD} 일일 요약 {ny_date} (뉴욕)", position, equity, count, *status])


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
    return "\n".join(lines)


def data_hash_text(expected: str, got: str) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 홀드아웃 구간 데이터 해시 불일치",
            f"잠금 파일 {expected[:12]} · 이번 데이터 {got[:12]}",
            "실행은 계속함 (표본 외 카운트에는 영향 없음, 재현성 손상 기록)",
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

Expected: `21 passed`.

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
- Produces (`ewbot.paper.criteria`): `PAPER_MIN_DAYS = 28`, `COVERAGE_MIN = 0.95`, `MAX_DD = 0.15`, `summarize_compare(cmp, start_ts, end_ts, epoch_id, computed_ts) -> dict` (`computed_ts, epoch_id, window, winners, max_dd_ok, random_control_evaluated, headline, random`), `run_coverage(runs, epoch_id, now_ts) -> dict`, `criterion4(ledger, now_ts) -> dict` (`first_ts`, `days`, `hours`, `covered`, `coverage`, `unexplained`, `met`), `golive_status(ledger, now_ts) -> dict` (`epoch_id, eligible, count, criteria_1_2, c1, c2, c3, c4, all_met`), `status_lines(st) -> list[str]`.
- Produces (`ewbot.paper.report`): `write_paper_report(path, ledger, status, equity, paper_closed, df1h, top_count, now_ts) -> Path` (atomic).

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
    led.runs = _hourly_runs(T0, hours, skip=set(range(10, 60)))  # 50 missing hours: < 95%
    assert not criterion4(led, now)["met"]


def test_summarize_compare_and_status_lines():
    led = new_ledger("fp", "c" * 40, LOCK)
    st = golive_status(led, T0)
    assert st["c1"] is None and st["c2"] is None and st["c3"] is False and not st["all_met"]
    lines = status_lines(st)
    assert "미계산" in lines[1] and "3. 표본 외 거래 36/50 (홀드아웃 36 + 모의 0): 미충족" in lines[3]
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
                 "holdout_data_ok": False}]
    led.discrepancies = [{"id": "CHANGED:ENTRY:1:1", "type": "CHANGED", "decision_id": "ENTRY:1:1",
                          "cause": "DATA_REVISION", "epoch_id": 1, "detected_ts": T0, "detected_run": "r1",
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
                 "CHANGED:ENTRY:1:1", "DATA_REVISION", "eq-chart", "dd-chart", "A paper", "criteria 1-2 window"):
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
    """>= 28 days of paper runs, 0 UNEXPLAINED discrepancies in the epoch, >= 95% hourly coverage."""
    cur = current_epoch(ledger)
    cov = run_coverage(ledger.runs, cur["id"], now_ts)
    unexplained = sum(
        1 for d in ledger.discrepancies if d["cause"] == "UNEXPLAINED" and int(d["epoch_id"]) == int(cur["id"])
    )
    met = cov["days"] >= PAPER_MIN_DAYS and unexplained == 0 and cov["coverage"] >= COVERAGE_MIN
    return {**cov, "unexplained": unexplained, "met": met}


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
    lines.append(
        f"4. 모의매매 재현: {_mark(c4['met'])} (운영 {c4['days']:.1f}/{PAPER_MIN_DAYS}일, 커버리지 "
        f"{c4['coverage'] * 100:.1f}%, 원인 불명 불일치 {c4['unexplained']}건)"
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
- Create: `src/ewbot/paper/runner.py`
- Test: `tests/paper/test_runner.py`

**Interfaces:**
- Consumes: every `ewbot.paper` module above; `ewbot.backtest.holdout.DEFAULT_LOCK_PATH`, `data_fingerprint`, `holdout_config`; `ewbot.backtest.runner.run_backtest`, `compare(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1, *, allow_holdout=False) -> dict`; `ewbot.backtest.walkforward.HOLDOUT_START`, `grid_combos()`; `ewbot.data.candles.fill_gaps`, `load_1h`; `ewbot.data.coinbase_history.update_cache(product, cache_dir)`.
- Produces (`ewbot.paper.runner`): `PRODUCT = "BTC-USD"`, `FETCH_ATTEMPTS = 3`, `class PaperRunError(RuntimeError)` (`.stage`, `.message`), `default_fetch(cache_dir) -> Path`, `@dataclass PaperEnv(ledger_dir, lock_path=DEFAULT_LOCK_PATH, cache_dir=Path("data/cache"), fetch=default_fetch, sender=None, clock=_wall_clock, fingerprint=current_fingerprint, baseline=baseline_fingerprint, random_runs=100, workers=max(1, os.cpu_count() or 1), sleep=time.sleep, fetch_backoff_s=10.0)` (`_wall_clock() -> int` = `int(time.time())`), `@dataclass(frozen=True) RunOutcome(run_id, last_bar_ts, epoch_id, new_decisions, new_discrepancies, events, sent, pending, notes, status)`, `precheck_lock(lock_path) -> dict`, `run_paper(env) -> RunOutcome`.
- Run record in `ledger.runs`: `id, ts, status, last_bar_ts, data_sha256, holdout_data_ok, holdout_trades_recomputed, epoch_id, paper_trades, count_total, new_decisions, new_discrepancies, notify`.
- Failure stages: `precheck`, `ledger`, `fingerprint`, `data`, `recompute`, `criteria`, `write`.

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_runner.py`**

```python
from __future__ import annotations

import json
import os

import pytest

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
            sleep=lambda s: None,
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


@pytest.mark.parametrize("case", ["missing", "untracked", "modified", "staged", "grid", "start", "incomplete"])
def test_precheck_refuses_and_writes_nothing(make_env, df, tmp_path, case):
    lock = make_lock(df)
    if case == "grid":
        lock["params"] = [2.2, 0.5, 0.0]
    if case == "start":
        lock["holdout_start"] = lock["holdout_start"] + H1
    if case == "incomplete":
        del lock["first_run"]
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
    other = make_env(lock=make_lock(df, sha256="e" * 64))
    with pytest.raises(PaperRunError, match="different holdout lock"):
        run_paper(other)


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_runner.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'ewbot.paper.runner'`.

- [ ] **Step 3: Write minimal implementation**

This module is the only non-holdout caller of `allow_holdout=True` (`run_backtest` for the recompute, `compare` for the daily criteria); Task 10 adds the static guard.

**File: `src/ewbot/paper/runner.py`**

```python
"""One `ewbot paper run` (Plan 2 spec 4): precheck -> data -> fingerprint/epoch -> full recompute ->
out-of-sample extraction -> replay check -> ledger / report / runs.log -> Telegram.

Inputs are frozen: the params come from holdout.lock only, the rest of the configuration is the
BotConfig defaults (conservative risk, breakers on, Plan 1 costs) - holdout_config(BotConfig(), params),
exactly how the holdout ran. The recompute starts trading at the holdout start, so it reproduces the
holdout's A run and continues it; trades entering after the holdout's last bar are paper trades.

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

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, data_fingerprint, holdout_config
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
from ewbot.paper.fingerprint import FingerprintError, baseline_fingerprint, current_fingerprint
from ewbot.paper.ledger import (
    REPORT_FILE,
    RUNS_LOG,
    LedgerError,
    append_log_line,
    ensure_epoch,
    golive_count,
    load_ledger,
    new_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    GAP_WARN_S,
    NY,
    Sender,
    daily_summary_due,
    daily_text,
    data_hash_text,
    decision_events,
    deliver,
    discrepancy_text,
    epoch_text,
    gap_seconds,
    gap_text,
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
    """A run stopped before writing anything; `stage` names the step for the failure alert."""

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
def _stage(name: str) -> Iterator[None]:
    try:
        yield
    except PaperRunError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        raise PaperRunError(name, f"{type(exc).__name__}: {exc}") from exc


# ----------------------------------------------------------------------------- precheck


def _git_ok(cwd: Path, *args: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def precheck_lock(lock_path: Path) -> dict[str, Any]:
    """holdout.lock exists, is committed (in HEAD), has no staged or unstaged change, and its params
    are a walk-forward grid combo. Returns the parsed lock."""
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
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PaperRunError("precheck", f"{p} is not a completed holdout lock: {exc!r}") from exc
    if params not in grid_combos():
        raise PaperRunError("precheck", f"lock params {list(params)} are not a walk-forward grid combo")
    if holdout_start != HOLDOUT_START:
        raise PaperRunError("precheck", f"lock holdout_start {holdout_start} != {HOLDOUT_START}")
    return lock


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


def _ny9_ts(ny_date: str) -> int:
    y, m, d = (int(x) for x in ny_date.split("-"))
    return int(datetime(y, m, d, 9, tzinfo=NY).timestamp())


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


# ----------------------------------------------------------------------------- run


def run_paper(env: PaperEnv) -> RunOutcome:
    now = int(env.clock())
    run_id = _run_id(now)
    lock = precheck_lock(env.lock_path)
    cfg = holdout_config(BotConfig(), tuple(float(p) for p in lock["params"]))
    ledger_dir = Path(env.ledger_dir)

    with _stage("ledger"):
        ledger_dir.mkdir(parents=True, exist_ok=True)
        try:
            ledger = load_ledger(ledger_dir)
        except LedgerError as exc:
            raise PaperRunError("ledger", str(exc)) from exc
    if ledger is None:
        try:
            fp0, commit = env.baseline()
        except FingerprintError as exc:
            raise PaperRunError("fingerprint", f"baseline at the holdout commit: {exc}") from exc
        ledger = new_ledger(fp0, commit, lock)
    elif ledger.holdout["lock_sha256"] != lock["sha256"] or ledger.holdout["last_bar_ts"] != int(
        lock["data"]["last_bar_ts"]
    ):
        raise PaperRunError("precheck", "ledger.json was started from a different holdout lock")
    hs = ledger.holdout

    df = _load_data(env)
    last_bar = int(df["ts"].iloc[-1])
    if last_bar < hs["last_bar_ts"]:
        raise PaperRunError("data", f"data ends at {last_bar}, before the holdout's last bar {hs['last_bar_ts']}")

    with _stage("fingerprint"):
        fp = env.fingerprint()
    gap = gap_seconds(now, ledger.runs)
    prev_ok = last_ok_run_ts(ledger.runs)
    epoch, created = ensure_epoch(ledger, fp, last_bar)

    with _stage("recompute"):
        with traced_engine() as trace:
            res = run_backtest(cfg, df, hs["holdout_start"], None, allow_holdout=True)
        closed, open_trade = split_result(res, trace)
        all_trades = closed + ([open_trade] if open_trade is not None else [])
        by_entry = {t.entry_ts: t for t in all_trades}
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

        def revised_at(d: dict[str, Any]) -> bool:
            return prefix(d["ts"]) != d["data_sha256"]

        def appeared_revised(r: dict[str, Any]) -> bool:
            run = next((x for x in ok_runs if x["last_bar_ts"] >= known_at(r)), None)
            return run is not None and prefix(run["last_bar_ts"]) != run["data_sha256"]

        recomputed = build_decisions(res, trace, all_trades)
        found = verify_decisions(ledger.decisions, recomputed, epoch, revised_at, appeared_revised)
        known = {d["id"] for d in ledger.discrepancies}
        new_disc = [
            {**d, "epoch_id": epoch["id"], "detected_ts": last_bar, "detected_run": run_id}
            for d in found
            if d["id"] not in known
        ]
        ledger.discrepancies.extend(new_disc)
        fresh = decisions_to_record(recomputed, epoch, {d["id"] for d in ledger.decisions})
        for d in fresh:
            d.update(run_id=run_id, data_sha256=prefix(d["ts"]), epoch_id=epoch["id"])
        ledger.decisions.extend(fresh)
        epoch["through_ts"] = max(int(epoch["through_ts"]), last_bar)

    count = golive_count(ledger)
    events: list[dict[str, Any]] = []
    if created:
        events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], epoch_text(epoch)))
    events += decision_events(fresh, by_entry, None if open_trade is None else open_trade.entry_ts,
                              int(epoch["started_ts"]), count, cur_closed, cfg.strategy.tp1_frac)
    if new_disc:
        events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies), discrepancy_text(new_disc)))
    if not holdout_ok:
        events.append(make_event("DATA_HASH", hs["last_bar_ts"], holdout_sha[:12], data_hash_text(hs["data_sha256"], holdout_sha)))
    if gap is not None and prev_ok is not None and gap > GAP_WARN_S:
        events.append(make_event("GAP", now // H1 * H1, 0, gap_text(gap, prev_ok)))

    run: dict[str, Any] = {
        "id": run_id,
        "ts": now,
        "status": "ok",
        "last_bar_ts": last_bar,
        "data_sha256": data_sha,
        "holdout_data_ok": holdout_ok,
        "holdout_trades_recomputed": sum(1 for t in all_trades if hs["holdout_start"] <= t.entry_ts <= hs["last_bar_ts"]),
        "epoch_id": epoch["id"],
        "paper_trades": count["paper"],
        "count_total": count["total"],
        "new_decisions": len(fresh),
        "new_discrepancies": len(new_disc),
    }
    ledger.runs.append(run)

    due = daily_summary_due(now, ledger.daily_summary_date)
    if due is not None:
        if count["eligible"]:
            with _stage("criteria"):
                end = last_bar + H1
                cmp = compare(cfg, df, hs["holdout_start"], end, random_runs=env.random_runs, workers=env.workers,
                              allow_holdout=True)
                ledger.criteria = summarize_compare(cmp, hs["holdout_start"], end, epoch["id"], now)
        eq = {p.ts: p.equity for p in res.equity}
        base_eq = eq.get(hs["last_bar_ts"], cfg.initial_equity)
        last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
        equity_line = f"자산 {last_eq:,.2f} USD (홀드아웃 이후 {last_eq / base_eq - 1:+.2%})"
        count_line = (
            f"표본 외 누적 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 {count['paper']}) · "
            f"구간 {epoch['id']}"
        )
        st = golive_status(ledger, now)
        text = daily_text(due, position_line(ledger.open_position), equity_line, count_line, status_lines(st))
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

    remaining, sent, errors = deliver(ledger.pending_events, ledger.sent_event_ids, env.sender, sleep=env.sleep)
    ledger.pending_events = remaining
    ledger.sent_event_ids.extend(sent)
    run["notify"] = {"sent": len(sent), "pending": len(remaining), "errors": errors}
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
    return RunOutcome(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                      errors, status)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_runner.py -q
```

Expected: `19 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/ewbot/paper/runner.py tests/paper/test_runner.py
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
    print(
        f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)} (unexplained {unexplained}), "
        f"pending events {len(led.pending_events)}"
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

Expected: `106 passed` (all paper tests so far, about 50 s; the walk fixture alone takes about 25 s).

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
- Create: `.github/workflows/paper.yml`, `docs/paper-trading.md`
- Test: `tests/paper/test_static.py`

**Interfaces:**
- Consumes: `pyyaml` (already a dependency), the AST of every `src/ewbot/**/*.py`.
- Produces: the guard "`allow_holdout=True` appears only in `backtest/holdout.py` and `paper/runner.py`; non-literal values only in `backtest/runner.py`; no `"allow_holdout"` string or `check_before_holdout` call elsewhere"; the hourly workflow; the ops guide with the user's Telegram steps (spec §6.5).
- Workflow env passed to `ewbot`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (secrets), `EWBOT_RUN_URL`, `EWBOT_FAILURE_MARKER`, `PAPER_DIR` (`$RUNNER_TEMP/paper`).

- [ ] **Step 1: Write the failing test**

**File: `tests/paper/test_static.py`**

```python
"""Static guards: who may run into the holdout, and the shape of the paper workflow."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

import ewbot

SRC = Path(ewbot.__file__).resolve().parent  # src/ewbot
ROOT = SRC.parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "paper.yml"
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py"}  # Plan 2 spec 7.1
PASS_THROUGH = {"backtest/runner.py"}  # forwards its own allow_holdout argument to the engine run


def _calls():
    for f in sorted(SRC.rglob("*.py")):
        rel = f.relative_to(SRC).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            yield rel, node


def test_only_the_holdout_and_the_paper_runner_pass_allow_holdout_true():
    literal_true, other = set(), set()
    for rel, node in _calls():
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "allow_holdout":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        literal_true.add(rel)
                    elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                        other.add(rel)
    assert literal_true == ALLOWED_TRUE
    assert other <= PASS_THROUGH


def test_no_back_doors_into_the_holdout():
    """No `**{"allow_holdout": True}` and no direct check_before_holdout call outside backtest/runner.py."""
    for rel, node in _calls():
        if isinstance(node, ast.Constant) and node.value == "allow_holdout":
            assert rel in PASS_THROUGH, rel
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name == "check_before_holdout":
                assert rel in PASS_THROUGH, rel


def _steps():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper"]["steps"]


def test_workflow_triggers_concurrency_and_permissions():
    wf, _ = _steps()
    on = wf[True]  # YAML 1.1 reads the bare key `on` as boolean True
    assert on["schedule"] == [{"cron": "7 * * * *"}] and "workflow_dispatch" in on
    assert wf["concurrency"] == {"group": "paper", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"}
    env = wf["jobs"]["paper"]["env"]
    assert env["TELEGRAM_BOT_TOKEN"] == "${{ secrets.TELEGRAM_BOT_TOKEN }}"
    assert env["TELEGRAM_CHAT_ID"] == "${{ secrets.TELEGRAM_CHAT_ID }}"


def test_workflow_steps():
    _, steps = _steps()
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses == ["actions/checkout@v7", "astral-sh/setup-uv@v10", "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"}  # shallow: the paper history is never fetched in full
    runs = [s.get("run", "") for s in steps]
    joined = "\n".join(runs)
    assert "git fetch --no-tags --depth=1 origin 64e628ad4865b3b04571a7cba2886881eae19176" in joined  # baseline
    assert "git worktree add --orphan -b paper" in joined and "git ls-remote --exit-code --heads origin paper" in joined
    assert "uv sync --frozen" in runs
    paper = next(s for s in steps if s.get("id") == "paper")
    assert paper["run"] == 'uv run ewbot paper run --ledger-dir "$PAPER_DIR"'
    push = next(r for r in runs if "git -C \"$PAPER_DIR\" push" in r)
    assert "for attempt in 1 2 3" in push and "reset -q --hard origin/paper" in push and "ewbot paper run" in push
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "btc-usd-1h-"
    last = steps[-1]
    assert last["if"] == "failure()" and "EWBOT_FAILURE_MARKER" in last["run"] and "sendMessage" in last["run"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/paper/test_static.py -q
```

Expected: `2 failed, 2 passed`: the two `allow_holdout` guards already pass (Task 7 respected them); the workflow tests fail with `FileNotFoundError` for `.github/workflows/paper.yml`.

- [ ] **Step 3: Write minimal implementation**

```bash
mkdir -p .github/workflows
```

**File: `.github/workflows/paper.yml`**

```yaml
# ewbot paper-trading runtime (Plan 2 spec 6.1): one deterministic recompute per hour.
# Code comes from master; ledger.json / report.html / runs.log live on the orphan `paper` branch.
# Every fetch is shallow: the paper branch gets one commit per hour and its history is never needed here.
name: paper

on:
  schedule:
    - cron: "7 * * * *"
  workflow_dispatch:

concurrency:
  group: paper
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  paper:
    runs-on: ubuntu-latest
    timeout-minutes: 50
    env:
      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
      TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      EWBOT_RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
    steps:
      - name: Check out master
        uses: actions/checkout@v7
        with:
          ref: master

      - name: Fetch the holdout commit and check out the paper branch (orphan on first use)
        run: |
          set -euo pipefail
          echo "PAPER_DIR=$RUNNER_TEMP/paper" >> "$GITHUB_ENV"
          echo "EWBOT_FAILURE_MARKER=$RUNNER_TEMP/ewbot-failure-notified" >> "$GITHUB_ENV"
          git config --global user.name "ewbot-paper"
          git config --global user.email "ewbot-paper@users.noreply.github.com"
          # 64e628a: the fingerprint baseline, read once when ledger.json is created (spec 5.1)
          git fetch --no-tags --depth=1 origin 64e628ad4865b3b04571a7cba2886881eae19176
          if git ls-remote --exit-code --heads origin paper >/dev/null; then
            git fetch --no-tags --depth=1 origin +refs/heads/paper:refs/remotes/origin/paper
            git worktree add -B paper "$RUNNER_TEMP/paper" origin/paper
          else
            git worktree add --orphan -b paper "$RUNNER_TEMP/paper"
          fi

      - name: Install uv (with its cache)
        uses: astral-sh/setup-uv@v10
        with:
          python-version: "3.12"
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
        run: uv sync --frozen

      - name: Paper run
        id: paper
        run: uv run ewbot paper run --ledger-dir "$PAPER_DIR"

      - name: Commit and push the ledger (recompute and retry on conflict, 3 attempts)
        run: |
          set -euo pipefail
          commit() {
            git -C "$PAPER_DIR" add ledger.json report.html runs.log
            if git -C "$PAPER_DIR" diff --cached --quiet; then
              return 1
            fi
            git -C "$PAPER_DIR" commit -q -m "paper: run $(date -u +%Y-%m-%dT%H:%MZ)"
          }
          if ! commit; then
            echo "no ledger change"
            exit 0
          fi
          for attempt in 1 2 3; do
            if git -C "$PAPER_DIR" push origin HEAD:refs/heads/paper; then
              exit 0
            fi
            echo "push rejected (attempt $attempt): refresh the paper branch and recompute"
            git -C "$PAPER_DIR" fetch --no-tags --depth=1 origin +refs/heads/paper:refs/remotes/origin/paper
            git -C "$PAPER_DIR" reset -q --hard origin/paper
            uv run ewbot paper run --ledger-dir "$PAPER_DIR"
            if ! commit; then
              exit 0
            fi
          done
          echo "push failed 3 times" >&2
          exit 1

      - name: Telegram failure alert (fallback when ewbot did not send one)
        if: failure()
        run: |
          if [ -f "${EWBOT_FAILURE_MARKER:-/nonexistent}" ]; then
            echo "ewbot already sent the failure alert"
            exit 0
          fi
          if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
            echo "no Telegram secrets: skipping the alert"
            exit 0
          fi
          text="[ewbot paper] 실패 · 단계 workflow (checkout / setup / sync / push)"$'\n'"로그: ${EWBOT_RUN_URL}"
          curl -sS --retry 3 -o /dev/null -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${text}"
```

**File: `docs/paper-trading.md`**

````markdown
# Paper-trading runtime (Plan 2) — operations

Spec: `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md`.

## What runs

- `.github/workflows/paper.yml` runs `uv run ewbot paper run --ledger-dir <paper worktree>` every hour at minute 7 (and on demand).
- Each run recomputes the whole engine deterministically from 2017 with the params in the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), conservative risk, breakers on and Plan 1 costs. Trading starts at the holdout start (2024-01-01), so the run reproduces the holdout's A run and continues it.
- Paper trades are the trades that **enter after the holdout's last bar** (`entry_ts > 1790938800`, 2026-10-02 11:00 UTC). A position already open at that bar belongs to the holdout and is never counted twice.
- Results go to the orphan branch `paper` (never to `master`):
  - `ledger.json`: baseline fingerprint and commit, holdout summary, epochs, closed trades, open position, decisions, discrepancies, runs, sent and pending Telegram events, the last criteria 1–2 result.
  - `report.html`: go-live status at the top, out-of-sample equity and drawdown, trade tables with snapshot charts, current top count, discrepancy list, epochs.
  - `runs.log`: one line per successful run (time, status, last bar, data hash, N/50, epoch).
- The candle cache `data/cache/BTC-USD_1h.parquet` lives in the Actions cache (a new entry per run, restored from the newest one). A missing cache means a full download (about 70 s).

## One-time setup (you)

1. Merge `feat/plan2-paper` into `master`. Scheduled workflows only run from the default branch.
2. Telegram bot:
   1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`. BotFather replies with the token (digits, a colon, then letters and digits). Keep it private.
   2. Open a chat with your new bot and send it any message (for example `hi`). A bot cannot write to you before you have written to it.
   3. Find your chat ID: in a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` (replace `<TOKEN>` with the token). In the JSON, the number right after `"chat":{"id":` is your chat ID (a group chat ID starts with `-`). If the result is empty, send the bot another message and reload.
3. Store both values as repository secrets. Run each command and paste the value at the prompt (so it does not end up in your shell history):

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   ```

   Without these secrets the runtime still runs and records everything; it only skips Telegram.
4. Start the first run by hand and watch it:

   ```bash
   gh workflow run paper.yml
   gh run watch "$(gh run list --workflow paper.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

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
| 일일 요약 | first run at/after 09:00 New York time each day: position, equity, N/50, go-live status (criteria 1–2 are recomputed in this run: A/HYBRID vs C/BH/EMA/RANDOM x100) |
| 경고 | strategy fingerprint change (new epoch), decision discrepancy, holdout data hash mismatch, run gap over 3 hours |
| 실패 | the failed stage and the Actions log link (sent by `ewbot`; the workflow sends a fallback for checkout/setup/push failures) |

Each message has an id `kind:ts:trade_key`; an id is sent at most once. A failed send is retried 3 times and then kept pending for the next run.

## Strict fingerprint policy

The strategy fingerprint covers `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/` (every byte, comments included). Its baseline is the same file set at the holdout commit `64e628a`. If any of these files changes, the next run opens a new epoch, sends a warning and resets the go-live counter to 0; the holdout's 36 trades no longer count. `tests/paper/test_fingerprint.py::test_baseline_recomputes_from_git_and_equals_the_working_tree` fails in that case, before anything is merged.

## Failures

| Situation | What happens |
|---|---|
| Coinbase fetch fails | 3 tries, then the run stops without a commit and alerts; the next run recomputes the missing bars |
| Cache lost | full download; changed past bars show up as `DATA_REVISION` discrepancies |
| Push conflict on `paper` | fetch the newest `paper`, recompute, push again (3 attempts) |
| Actions itself stops | not detectable from inside a run: GitHub's failure e-mail and a missing daily summary are the alarm |

## Cost

About 1–2 minutes per hourly run, plus the daily criteria 1–2 recompute (103 engine runs, roughly 10–20 minutes on a 2-vCPU runner): roughly 1,100–1,500 minutes a month, within the 2,000 free minutes of a private repository. If it gets close, lower the daily cost first (do not change the hourly schedule).
````

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/paper/test_static.py -q
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/paper.yml'))" && echo yaml-ok
command -v actionlint >/dev/null && actionlint .github/workflows/paper.yml && echo actionlint-ok || echo 'actionlint not installed (optional)'
uv run pytest -q
git diff --stat master -- src/ewbot/types.py src/ewbot/config.py src/ewbot/indicators.py src/ewbot/data src/ewbot/waves src/ewbot/strategy src/ewbot/engine src/ewbot/execution src/ewbot/risk src/ewbot/backtest holdout.lock pyproject.toml uv.lock
```

Expected: `4 passed`; `yaml-ok`; `actionlint-ok` when actionlint is installed; the full suite `553 passed, 1 skipped` without a candle cache (`554 passed` when `data/cache/BTC-USD_1h.parquet` exists); the final `git diff --stat` prints nothing (every fingerprinted file, `backtest/`, the lock and the dependencies are byte-identical to `master`).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/paper.yml docs/paper-trading.md tests/paper/test_static.py
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

Expected: clean tree; `554 passed` (with the real cache present); the fingerprint tripwire passes, i.e. the working-tree fingerprint equals the `64e628a` baseline `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`.

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

Open a PR from `feat/plan2-paper` to `master` and merge it (scheduled workflows run only from the default branch). Then follow `docs/paper-trading.md` → "One-time setup": create the bot with @BotFather, message it once, read the chat ID from `getUpdates`, and run `gh secret set TELEGRAM_BOT_TOKEN` / `gh secret set TELEGRAM_CHAT_ID` (values typed by the user at the prompt).

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

Expected: about 24 successful runs (one per hour at minute ~7, delays of a few minutes are normal), no `실행 공백` warning, exactly one `일일 요약` after 09:00 New York, `ewbot paper status` (on a worktree of `paper`) showing coverage near 100% and 0 unexplained discrepancies. Record the outcome in the PR or the project notes.
