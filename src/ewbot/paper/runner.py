"""One `ewbot paper run` (Plan 2 spec 4): precheck -> data -> fingerprint/epoch -> full recompute ->
out-of-sample extraction -> replay check -> ledger / report / runs.log -> Telegram.

Inputs are frozen: the params come from holdout.lock only, the rest of the configuration is the
BotConfig defaults (conservative risk, breakers on, Plan 1 costs) - holdout_config(BotConfig(), params),
exactly how the holdout ran; the precheck refuses a lock whose sha256 is not
lock_digest(BotConfig(), params, random_runs, code_sha256), i.e. whose configuration is not the one this
run would use. The recompute starts trading at the holdout start, so it reproduces the holdout's A run
and continues it; trades entering after the holdout's last bar are paper trades. Every run records its
runtime environment (paper.environment); decisions carry the digest of the environment that recorded
them, for the ENVIRONMENT_CHANGE cause of the replay check.

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
                                 appeared_after=floor, env_sha256=env_sha, appeared_env=appeared_env)
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
