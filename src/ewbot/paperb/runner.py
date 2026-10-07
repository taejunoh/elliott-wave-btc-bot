"""One `ewbot stratb paper run` (Plan 4 spec 7, 8): precheck -> data -> fingerprint/epoch -> full recompute
from REPLAY_ANCHOR -> replay check -> ledger and runs.log -> Telegram. BTC and ETH run together in this one
runtime and ledger (spec 7 as revised 2026-10-07): one portfolio recompute, one count of 30 trades of both
assets, messages that name the asset. A run ends at the latest bar BOTH assets have: when one asset's newest
bar is not out yet, the other's newer bars wait for the next run (they are never decided against a flat
padded bar of the late asset), and the run records a warning.

Inputs are frozen by holdout-b.lock: the params and the whole run config come from it (the precheck refuses
a lock whose sha256 is not the digest of its fields, that is not committed unchanged, whose holdout has
not run or failed - strategy B ended -, whose keys, grid, config, holdout start or replay anchor are not
this code's (holdout.lock_mismatch_b), or whose deps_sha256 is not the hash of the strategy-A modules B
imports now - a changed A module is refused like a lock problem, not a new epoch). The
recompute starts at REPLAY_ANCHOR (2023-10-01) and trades from HOLDOUT_START, exactly like the holdout
run, so it reproduces the holdout and continues it; trades entering after the lock's last bar are paper
trades. When the holdout window's bars (both assets) no longer hash to first_run.data.sha256 (a Coinbase candle
revision, a re-download after a cache eviction) the run is NOT refused (spec 8 as amended 2026-10-07):
like A it warns once per distinct hash (DATA_HASH event) and goes on, and a recorded decision that changes
because of it is classified DATA_REVISION by A's replay check. A different B fingerprint opens a new epoch
(spec 7). Decisions, discrepancies, the environment record and Telegram delivery are strategy A's (paper/
imported, never changed).

Every step runs inside a named stage: any failure raises PaperBRunError(stage); the CLI sends the failure
alert. Nothing is written before the write stage."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.paper.decisions import decisions_to_record, known_at, paper_trades, verify_decisions
from ewbot.paper.environment import environment_digest, runtime_environment
from ewbot.paper.ledger import (
    RUNS_LOG,
    Ledger,
    LedgerError,
    append_log_line,
    ensure_epoch,
    load_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    NY,
    Sender,
    daily_summary_due,
    data_hash_text,
    deliver,
    discrepancy_text,
    epoch_text,
    make_event,
    queue_events,
)
from ewbot.paperb.decisions import build_decisions_b, key_of, open_positions_b, split_result_b
from ewbot.paperb.ledger import holdout_summary_b, new_ledger_b, paper_count_b
from ewbot.paperb.notify import b_head, daily_text_b, decision_events_b, holdout_trades_text_b, position_lines_b
from ewbot.stratb.config import ASSETS, config_for, normalize_combo
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH, REPLAY_ANCHOR, load_15m, update_caches_15m
from ewbot.stratb.fingerprint import deps_fingerprint, stratb_fingerprint
from ewbot.stratb.holdout import HoldoutBError, committed_lock, data_fingerprint_b, lock_mismatch_b, read_lock
from ewbot.stratb.research import backtest_b
from ewbot.types import H1

FETCH_ATTEMPTS = 3
DAILY_HOUR_NY = 9


class PaperBRunError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def default_fetch_b(cache_dir: Path, lock_path: Path) -> dict[str, Path]:
    """Extend both 15m caches (an empty one starts at REPLAY_ANCHOR: the recompute needs nothing older)."""
    return update_caches_15m(Path(cache_dir), lock_path=Path(lock_path), start_ts=REPLAY_ANCHOR)


def _wall_clock() -> int:
    return int(time.time())


@dataclass
class PaperBEnv:
    """Everything a run touches; tests inject fakes (no network, temp git repos)."""

    ledger_dir: Path
    lock_path: Path = DEFAULT_LOCK_PATH
    cache_dir: Path = Path("data/cache")
    fetch: Callable[[Path, Path], dict[str, Path]] = default_fetch_b
    sender: Sender | None = None
    clock: Callable[[], int] = _wall_clock
    fingerprint: Callable[[], str] = stratb_fingerprint
    deps: Callable[[], str] = deps_fingerprint
    sleep: Callable[[float], None] = time.sleep
    fetch_backoff_s: float = 10.0
    environment: Callable[[], dict[str, Any]] = runtime_environment


@dataclass(frozen=True)
class RunOutcomeB:
    run_id: str
    last_bar_ts: int
    epoch_id: int
    new_decisions: int
    new_discrepancies: int
    events: list[str]
    sent: list[str]
    pending: int
    notes: list[str]
    count: dict[str, Any]


@contextmanager
def _stage(name: str, *, detail: bool = True) -> Iterator[None]:
    try:
        yield
    except PaperBRunError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        if detail:
            raise PaperBRunError(name, f"{type(exc).__name__}: {exc}") from exc
        raise PaperBRunError(name, f"{type(exc).__name__} (details withheld: may contain the token)") from None


def precheck_lock_b(lock_path: Path, deps: Callable[[], str] = deps_fingerprint) -> dict[str, Any]:
    """The committed, passed strategy-B lock of this code: its config is this code's config for its params, and
    the strategy-A modules B imports still hash to its deps_sha256."""
    try:
        lock = read_lock(lock_path)
        if committed_lock(lock_path) != lock:
            raise PaperBRunError("precheck", f"{lock_path} differs from the committed version")
    except HoldoutBError as exc:
        raise PaperBRunError("precheck", str(exc)) from exc
    fr = lock.get("first_run")
    if not isinstance(fr, dict) or "criteria" not in fr or "data" not in fr:
        raise PaperBRunError("precheck", "the strategy-B holdout has not run: paper trading starts after it passes")
    if not fr["criteria"].get("passed"):
        raise PaperBRunError("precheck", "the strategy-B holdout failed (spec 6.5): strategy B ended, no paper trading")
    stale = lock_mismatch_b(lock)
    if stale:
        raise PaperBRunError("precheck", f"the lock's {stale} are not this code's (keys, grid, config_for(params), "
                                         "holdout start, replay anchor)")
    if deps() != lock.get("deps_sha256"):
        raise PaperBRunError("precheck", "the strategy-A modules strategy B imports changed since the lock "
                                         "(deps_sha256 differs): refusing")
    return lock


def _open_ledger(ledger_dir: Path, lock: dict[str, Any]) -> Ledger:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    try:
        ledger = load_ledger(ledger_dir)
    except LedgerError as exc:
        raise PaperBRunError("ledger", str(exc)) from exc
    if ledger is None:
        return new_ledger_b(lock)
    bound = holdout_summary_b(lock)
    diff = sorted(k for k in bound.keys() | ledger.holdout.keys() if ledger.holdout.get(k) != bound.get(k))
    if diff:
        raise PaperBRunError("precheck", f"ledger.json was started from another holdout-b.lock (differs in: {diff})")
    return ledger


def _load_data(env: PaperBEnv) -> dict[str, pd.DataFrame]:
    paths: dict[str, Path] | None = None
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            paths = {a: Path(p) for a, p in env.fetch(Path(env.cache_dir), Path(env.lock_path)).items()}
            break
        except Exception as exc:  # noqa: BLE001 - network / API errors are retried
            last = exc
            if attempt < FETCH_ATTEMPTS - 1:
                env.sleep(env.fetch_backoff_s * 2**attempt)
    if paths is None:
        raise PaperBRunError("data", f"candle update failed {FETCH_ATTEMPTS} times: {type(last).__name__}: {last}")
    with _stage("data"):
        if sorted(paths) != sorted(ASSETS):
            raise PaperBRunError("data", f"the candle update returned {sorted(paths)}, not every asset {list(ASSETS)}")
        dfs: dict[str, pd.DataFrame] = {}
        for a in ASSETS:
            df = load_15m(paths[a], include_holdout=True, lock_path=env.lock_path)
            dfs[a] = df[df["ts"] >= REPLAY_ANCHOR].reset_index(drop=True)
            if dfs[a].empty:
                raise PaperBRunError("data", f"{paths[a]} has no bars from the replay anchor on")
        return dfs


def _recording_floor(ledger: Ledger) -> int:
    if len(ledger.epochs) == 1:
        return int(ledger.epochs[0]["started_ts"])
    return int(ledger.epochs[-2]["through_ts"])


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


def status_lines_b(ledger: Ledger, count: dict[str, Any]) -> list[str]:
    """Go-live checklist (spec 7.1), Korean, for the daily summary and `paper status`."""
    cur = int(count["epoch_id"])
    causes = [d["cause"] for d in ledger.discrepancies if int(d["epoch_id"]) == cur]
    unexplained = causes.count("UNEXPLAINED")
    lines = ["실거래 전환 조건 (Plan 4 명세 7.1)"]
    if not count["eligible"]:
        lines.append(f"현재 구간 {cur}의 B 지문이 잠금 파일과 다름: 실거래 전환 판정 대상 아님")
    def mark(ok: bool) -> str:
        return "충족" if ok else "미충족"

    lines.append(f"1. 홀드아웃 합격: {mark(bool(ledger.holdout.get('passed')))}")
    lines.append(f"2. 모의 거래 {count['paper']}/{count['required']}: {mark(count['paper'] >= count['required'])}")
    lines.append(f"3. 원인 불명 불일치 {unexplained}건 (전체 {len(causes)}건): {mark(unexplained == 0)}")
    lines.append("4. 사용자 승인: 수동")
    return lines


def run_paper_b(env: PaperBEnv) -> RunOutcomeB:
    with _stage("precheck"):
        now = int(env.clock())
        run_id = _run_id(now)
        lock = precheck_lock_b(Path(env.lock_path), env.deps)
        cfg = config_for(normalize_combo(lock["params"]))
        ledger_dir = Path(env.ledger_dir)
        run_env = dict(env.environment())
        env_sha = environment_digest(run_env)
    with _stage("ledger"):
        ledger = _open_ledger(ledger_dir, lock)
        hs = ledger.holdout
    dfs = _load_data(env)
    with _stage("data"):
        ends = {a: int(df["ts"].iloc[-1]) for a, df in dfs.items()}
        early = {a: e for a, e in ends.items() if e < hs["last_bar_ts"]}
        if early:
            raise PaperBRunError("data", f"data ends at {early}, before the lock's last bar {hs['last_bar_ts']}")
        last_bar = min(ends.values())  # the latest bar both assets have: no flat padding of a late asset's bar
        late = [a for a in ASSETS if ends[a] == last_bar < max(ends.values())]
        dfs = {a: df[df["ts"] <= last_bar].reset_index(drop=True) for a, df in dfs.items()}
        holdout_sha = data_fingerprint_b(dfs, REPLAY_ANCHOR, hs["last_bar_ts"])
        holdout_ok = holdout_sha == hs["data_sha256"]
    with _stage("fingerprint"):
        epoch, created = ensure_epoch(ledger, env.fingerprint(), last_bar)
        floor = _recording_floor(ledger)
        if created:
            epoch["through_ts"] = floor
    with _stage("recompute"):
        res = backtest_b(dfs, cfg, HOLDOUT_START, None, include_holdout=True)
        closed, still = split_result_b(res)
        all_trades = closed + still
        by_key = {key_of(t): t for t in all_trades}
        holdout_n = sum(1 for t in all_trades if HOLDOUT_START <= t.entry_ts <= hs["last_bar_ts"])
        cur_closed = paper_trades(closed, int(epoch["started_ts"]))
        replace_epoch_trades(ledger, epoch["id"], cur_closed)
        start = int(epoch["started_ts"])
        ledger.open_position = open_positions_b(still, res, start)
        memo: dict[int, str] = {}

        def prefix(ts: int) -> str:
            if ts not in memo:
                memo[ts] = data_fingerprint_b(dfs, 0, int(ts))
            return memo[ts]

        ok_runs = sorted((r for r in ledger.runs if r.get("status") == "ok"), key=lambda r: r["last_bar_ts"])
        prev_env = ok_runs[-1].get("env_sha256") if ok_runs else None

        def first_knowing(r: dict[str, Any]) -> dict[str, Any] | None:
            return next((x for x in ok_runs if x["last_bar_ts"] >= known_at(r)), None)

        def appeared_revised(r: dict[str, Any]) -> bool:
            run = first_knowing(r)
            return run is not None and prefix(run["last_bar_ts"]) != run["data_sha256"]

        def appeared_env(r: dict[str, Any]) -> str | None:
            run = first_knowing(r)
            return None if run is None else run.get("env_sha256")

        recomputed = build_decisions_b(res)
        found = verify_decisions(ledger.decisions, recomputed, epoch, lambda d: prefix(d["ts"]) != d["data_sha256"],
                                 appeared_revised, appeared_after=floor, env_sha256=env_sha, appeared_env=appeared_env,
                                 previous_env_sha256=prev_env)
        known = {d["id"] for d in ledger.discrepancies}
        new_disc = [{**d, "epoch_id": epoch["id"], "detected_ts": last_bar, "detected_run": run_id}
                    for d in found if d["id"] not in known]
        ledger.discrepancies.extend(new_disc)
        fresh = decisions_to_record(recomputed, epoch, {d["id"] for d in ledger.decisions})
        for d in fresh:
            d.update(run_id=run_id, data_sha256=prefix(d["ts"]), epoch_id=epoch["id"], env_sha256=env_sha)
        ledger.decisions.extend(fresh)
        epoch["through_ts"] = max(int(epoch["through_ts"]), last_bar)
    with _stage("events"):
        count = paper_count_b(ledger)
        events: list[dict[str, Any]] = []
        warnings: list[str] = []
        lock_n = int(hs["n_trades"]["B"])
        if late:
            newest = datetime.fromtimestamp(max(ends.values()), UTC).isoformat(timespec="minutes")
            warnings.append(f"{late} data ends before the newest bar {newest}: the run ends at the last bar both "
                            "assets have")
        if not holdout_ok:  # one message per distinct revised hash (the event id carries it)
            warnings.append(f"holdout data hash {holdout_sha[:12]} != lock {hs['data_sha256'][:12]}")
            text = b_head(data_hash_text(hs["data_sha256"], holdout_sha))
            events.append(make_event("DATA_HASH", hs["last_bar_ts"], holdout_sha[:12], text))
        if holdout_n != lock_n:
            warnings.append(f"holdout trades recomputed {holdout_n} != lock {lock_n}")
            events.append(make_event("HOLDOUT_TRADES", hs["last_bar_ts"], holdout_n, holdout_trades_text_b(lock_n, holdout_n)))
        if created:
            events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], b_head(epoch_text(epoch))))
        events += decision_events_b(fresh, by_key, {key_of(t) for t in still}, int(epoch["started_ts"]), count,
                                    cur_closed)
        if new_disc:
            text = b_head(discrepancy_text(new_disc))
            events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies), text))
        data_sha = prefix(last_bar)
        run: dict[str, Any] = {
            "id": run_id, "ts": now, "status": "ok", "last_bar_ts": last_bar, "data_sha256": data_sha,
            "holdout_data_ok": holdout_ok, "holdout_trades_recomputed": holdout_n, "epoch_id": epoch["id"],
            "paper_trades": count["paper"],
            "new_decisions": len(fresh), "new_discrepancies": len(new_disc), "env": run_env, "env_sha256": env_sha,
            "warnings": warnings,
        }
        ledger.runs.append(run)
        due = daily_summary_due(now, ledger.daily_summary_date)
        if due is not None:
            eq = {p.ts: p.equity for p in res.equity}
            base = eq.get(hs["last_bar_ts"] // H1 * H1, cfg.initial_equity)
            last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
            lines = [*position_lines_b(ledger.open_position),
                     f"자산 {last_eq:,.2f} USD (잠금 이후 {last_eq / base - 1:+.2%})",
                     f"모의 {count['paper']}/{count['required']} · 구간 {epoch['id']}",
                     *status_lines_b(ledger, count)]
            y, m, d = (int(x) for x in due.split("-"))
            day_ts = int(datetime(y, m, d, DAILY_HOUR_NY, tzinfo=NY).timestamp())
            events.append(make_event("DAILY", day_ts, 0, daily_text_b(due, lines)))
            ledger.daily_summary_date = due
        queued = queue_events(ledger, events)
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
        append_log_line(
            ledger_dir / RUNS_LOG,
            f"{datetime.fromtimestamp(now, UTC).isoformat(timespec='seconds')}\tok\tlast_bar="
            f"{datetime.fromtimestamp(last_bar, UTC).isoformat(timespec='minutes')}\tdata={data_sha[:12]}\t"
            f"paper={count['paper']}/{count['required']}\tepoch={epoch['id']}\tnew_decisions={len(fresh)}",
        )
    with _stage("notify", detail=False):
        remaining, sent, errors = deliver(ledger.pending_events, ledger.sent_event_ids, env.sender, sleep=env.sleep)
        ledger.pending_events = remaining
        ledger.sent_event_ids.extend(sent)
        run["notify"] = {"sent": len(sent), "pending": len(remaining), "errors": errors}
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
    return RunOutcomeB(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                       warnings + errors, count)

