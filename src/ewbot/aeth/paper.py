"""One `ewbot aeth paper run` (Plan 5a spec 8): strategy A's paper runtime (ewbot.paper.runner) adapted to ETH - the
product (ETH-USD), the costs (ETP, 0.1 ETH per contract) and the lock (holdout-aeth.lock) - with strategy A's paper
components imported unchanged: the traced engine run, the decisions and the replay check, the ledger schema, epochs
and the go-live count, criterion 4, the runtime environment, Telegram delivery and message formats (with the prefix
`[ewbot paper-A-ETH]`).

precheck -> data -> epoch -> full recompute -> out-of-sample extraction -> replay check -> ledger and runs.log ->
Telegram. The precheck refuses a lock that is not valid (digest) or not committed unchanged, whose holdout has not
run or did not pass (A-ETH ended), or whose fields are not this code's for strategy A's params now (lock_mismatch).
Each run recomputes from the first cached bar (2017-01-01) and trades from 2024-01-01, like the holdout, so it
reproduces the holdout and continues it; closed trades entering after the lock's last bar are paper trades, and the
count to 50 is the holdout's A trades plus the paper trades of the baseline epoch (strategy A's golive_count, on
this ledger only: ETH trades are never pooled with strategy A's BTC count). An epoch is bound to strategy A's
fingerprint and the A-ETH fingerprint together (fingerprint.epoch_fingerprint): a change of either opens a new epoch
and restarts the count. The holdout window's bars no longer hashing to first_run.data.sha256 is warned once per
hash, never refused; decisions that change because of it are DATA_REVISION. No report.html and no daily criteria
1-2 recompute (spec 8): the go-live block is status_aeth / status_lines_aeth. Every step runs inside a named stage:
any failure raises AethPaperError(stage); nothing is written before the write stage."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.aeth.config import A_LOCK_PATH, LOCK_PATH, AethError, aeth_config
from ewbot.aeth.data import fetch_eth, load_eth
from ewbot.aeth.fingerprint import current_epoch_fingerprint, epoch_fingerprint
from ewbot.aeth.holdout import lock_mismatch
from ewbot.aeth.lockfile import committed_text, read_lock
from ewbot.backtest.holdout import data_fingerprint
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.candles import fill_gaps
from ewbot.paper import notify as a_notify
from ewbot.paper.criteria import PAPER_MIN_DAYS, criterion4
from ewbot.paper.decisions import (
    build_decisions,
    decisions_to_record,
    known_at,
    open_position_record,
    paper_trades,
    split_result,
    verify_decisions,
)
from ewbot.paper.environment import environment_digest, runtime_environment
from ewbot.paper.ledger import (
    RUNS_LOG,
    Ledger,
    LedgerError,
    append_log_line,
    current_epoch,
    ensure_epoch,
    golive_count,
    load_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    GAP_WARN_S,
    NY,
    Sender,
    daily_summary_due,
    decision_events,
    deliver,
    gap_seconds,
    last_ok_run_ts,
    make_event,
    position_line,
    queue_events,
)
from ewbot.paper.trace import traced_engine
from ewbot.types import H1

HEAD_AETH = "[ewbot paper-A-ETH]"
FETCH_ATTEMPTS = 3


class AethPaperError(RuntimeError):
    """A run failed at `stage` (named in the failure alert); the workflow commits nothing after it."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def eth_head(text: str) -> str:
    """Strategy A's message with the A-ETH prefix in place of `[ewbot paper]`."""
    if not text.startswith(a_notify.HEAD):
        raise ValueError(f"not a paper message: {text[:40]!r}")
    return HEAD_AETH + text[len(a_notify.HEAD):]


def default_fetch(cache_dir: Path, lock_path: Path) -> Path:
    return fetch_eth(Path(cache_dir), lock_path=Path(lock_path))


def _wall_clock() -> int:
    return int(time.time())


@dataclass
class AethPaperEnv:
    """Everything a run touches. Production uses the defaults; tests inject fakes (no network, temp git repos)."""

    ledger_dir: Path
    lock_path: Path = LOCK_PATH
    a_lock_path: Path = A_LOCK_PATH
    cache_dir: Path = Path("data/cache")
    fetch: Callable[[Path, Path], Path] = default_fetch
    sender: Sender | None = None
    clock: Callable[[], int] = _wall_clock
    fingerprint: Callable[[], str] = current_epoch_fingerprint
    sleep: Callable[[float], None] = time.sleep
    fetch_backoff_s: float = 10.0
    environment: Callable[[], dict[str, Any]] = runtime_environment


@dataclass(frozen=True)
class AethRunOutcome:
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
    try:
        yield
    except AethPaperError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        if detail:
            raise AethPaperError(name, f"{type(exc).__name__}: {exc}") from exc
        raise AethPaperError(name, f"{type(exc).__name__} (details withheld: may contain the Telegram token)") from None


def precheck_lock_aeth(lock_path: Path, a_lock_path: Path = A_LOCK_PATH) -> dict[str, Any]:
    """The committed, passed A-ETH lock of this code for strategy A's params now."""
    try:
        lock = read_lock(lock_path)
        text = committed_text(lock_path)
        if text is None or json.loads(text) != lock:
            raise AethPaperError("precheck", f"{lock_path} differs from the committed version (or is not committed)")
        fr = lock.get("first_run")
        if not isinstance(fr, dict) or "verdict" not in fr or "data" not in fr:
            raise AethPaperError("precheck", "the ETH holdout has not run: paper trading starts after it passes")
        if not fr["verdict"].get("passed"):
            raise AethPaperError("precheck", "the ETH holdout did not pass (spec 7.3): A-ETH ended, no paper trading")
        stale = lock_mismatch(lock, a_lock_path)
    except AethError as exc:
        raise AethPaperError("precheck", str(exc)) from exc
    if stale:
        raise AethPaperError("precheck", f"the lock's {stale} are not this code's for strategy A's params")
    return lock


def holdout_summary_aeth(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout-aeth.lock the ledger is bound to (the precheck validated it)."""
    fr = lock["first_run"]
    return {
        "lock_sha256": lock["sha256"],
        "params": [float(p) for p in lock["params"]],
        "holdout_start": int(lock["holdout_start"]),
        "last_bar_ts": int(lock["last_bar_ts"]),
        "data_sha256": fr["data"]["sha256"],
        "n_trades": {"A": int(fr["verdict"]["n_trades"])},
        "passed": bool(fr["verdict"]["passed"]),
    }


def new_ledger_aeth(lock: dict[str, Any]) -> Ledger:
    """Epoch 1 starts at the lock's last bar with the lock's epoch fingerprint (A's and A-ETH's at the lock)."""
    hs = holdout_summary_aeth(lock)
    fp = epoch_fingerprint(lock["a_fingerprint"], lock["aeth_sha256"])
    start = hs["last_bar_ts"]
    epoch = {"id": 1, "fingerprint": fp, "started_ts": start, "reason": "baseline: holdout-aeth.lock",
             "through_ts": start}
    return Ledger(baseline={"fingerprint": fp, "commit": str(lock.get("git_head") or "holdout-aeth.lock")}, holdout=hs,
                  epochs=[epoch])


def status_aeth(ledger: Ledger, now_ts: int) -> dict[str, Any]:
    """Spec 8.1, the automatic part: holdout passed, 50 ETH out-of-sample trades, criterion 4 (strategy A's)."""
    count = golive_count(ledger)
    c4 = criterion4(ledger, now_ts)
    passed = bool(ledger.holdout.get("passed"))
    enough = bool(count["eligible"] and count["total"] >= count["required"])
    return {"epoch_id": int(current_epoch(ledger)["id"]), "eligible": bool(count["eligible"]), "count": count,
            "holdout_passed": passed, "count_met": enough, "c4": c4,
            "auto_met": bool(count["eligible"] and passed and enough and c4["met"])}


def _mark(ok: bool) -> str:
    return "충족" if ok else "미충족"


def status_lines_aeth(st: dict[str, Any]) -> list[str]:
    """Korean go-live block for the daily summary and `ewbot aeth paper status`."""
    count, c4 = st["count"], st["c4"]
    lines = ["A-ETH 실거래 검토 조건 (Plan 5a 명세 8.1)"]
    if not st["eligible"]:
        lines.append(f"현재 구간 {st['epoch_id']}의 지문(A 지문 + A-ETH 지문)이 잠금 파일과 다름: 판정 대상 아님")
    lines.append(f"1. 단계 2 홀드아웃 합격 (A의 9.6절 1·2번): {_mark(st['holdout_passed'])}")
    lines.append(f"2. ETH 표본 외 거래 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 "
                 f"{count['paper']}): {_mark(st['count_met'])}")
    env = f", 실행 환경 변경 불일치 {c4['environment']}건(집계 제외)" if c4.get("environment") else ""
    lines.append(f"3. 모의매매 재현: {_mark(c4['met'])} (운영 {c4['days']:.1f}/{PAPER_MIN_DAYS}일, 커버리지 "
                 f"{c4['coverage'] * 100:.1f}%, 원인 불명 불일치 {c4['unexplained']}건{env})")
    lines.append("4. A-BTC CANARY 통과: 수동 확인")
    lines.append("5. 전체 표본 외 구간의 9.6절 1·2번 재판정, 사용자 승인: Plan 5c에서 수동")
    lines.append("=> 자동 조건 충족: Plan 5c 검토 가능 (자동 전환하지 않음)" if st["auto_met"] else "=> Plan 5c 검토 불가")
    return lines


def _open_ledger(ledger_dir: Path, lock: dict[str, Any]) -> Ledger:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    try:
        ledger = load_ledger(ledger_dir)
    except LedgerError as exc:
        raise AethPaperError("ledger", str(exc)) from exc
    if ledger is None:
        return new_ledger_aeth(lock)
    bound = holdout_summary_aeth(lock)
    diff = sorted(k for k in bound.keys() | ledger.holdout.keys() if ledger.holdout.get(k) != bound.get(k))
    if diff:
        raise AethPaperError("precheck", f"ledger.json was started from another holdout-aeth.lock (differs in: {diff})")
    return ledger


def _load_data(env: AethPaperEnv) -> pd.DataFrame:
    path: Path | None = None
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            path = Path(env.fetch(Path(env.cache_dir), Path(env.lock_path)))
            break
        except Exception as exc:  # noqa: BLE001 - network / API errors are retried
            last = exc
            if attempt < FETCH_ATTEMPTS - 1:
                env.sleep(env.fetch_backoff_s * 2**attempt)
    if path is None:
        raise AethPaperError("data", f"candle update failed {FETCH_ATTEMPTS} times: {type(last).__name__}: {last}")
    with _stage("data"):
        raw = load_eth(path, unlock_2024=True, lock_path=env.lock_path)
        if raw.empty:
            raise AethPaperError("data", f"{path} has no bars")
        return fill_gaps(raw)


def _recording_floor(ledger: Ledger) -> int:
    if len(ledger.epochs) == 1:
        return int(ledger.epochs[0]["started_ts"])
    return int(ledger.epochs[-2]["through_ts"])


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


def _ny9_ts(ny_date: str) -> int:
    y, m, d = (int(x) for x in ny_date.split("-"))
    return int(datetime(y, m, d, 9, tzinfo=NY).timestamp())


def run_paper_aeth(env: AethPaperEnv) -> AethRunOutcome:
    with _stage("precheck"):
        now = int(env.clock())
        run_id = _run_id(now)
        lock = precheck_lock_aeth(Path(env.lock_path), Path(env.a_lock_path))
        cfg = aeth_config(tuple(lock["params"]))
        ledger_dir = Path(env.ledger_dir)
        run_env = dict(env.environment())
        env_sha = environment_digest(run_env)
    with _stage("ledger"):
        ledger = _open_ledger(ledger_dir, lock)
        hs = ledger.holdout
        gap = gap_seconds(now, ledger.runs)
        prev_ok = last_ok_run_ts(ledger.runs)
    df = _load_data(env)
    with _stage("data"):
        last_bar = int(df["ts"].iloc[-1])
        if last_bar < hs["last_bar_ts"]:
            raise AethPaperError("data", f"data ends at {last_bar}, before the lock's last bar {hs['last_bar_ts']}")
    with _stage("fingerprint"):
        epoch, created = ensure_epoch(ledger, env.fingerprint(), last_bar)
        floor = _recording_floor(ledger)
        if created:
            epoch["through_ts"] = floor
    with _stage("recompute"):
        with traced_engine() as trace:
            res = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
        closed, open_trade = split_result(res, trace)
        all_trades = closed + ([open_trade] if open_trade is not None else [])
        by_entry = {t.entry_ts: t for t in all_trades}
        holdout_n = sum(1 for t in all_trades if HOLDOUT_START <= t.entry_ts <= hs["last_bar_ts"])
        cur_closed = paper_trades(closed, int(epoch["started_ts"]))
        replace_epoch_trades(ledger, epoch["id"], cur_closed)
        ledger.open_position = (
            None if open_trade is None else open_position_record(open_trade, trace, int(epoch["started_ts"]))
        )
        memo: dict[int, str] = {}

        def prefix(ts: int) -> str:
            if ts not in memo:
                memo[ts] = data_fingerprint(df, 0, int(ts))
            return memo[ts]

        data_sha = prefix(last_bar)
        holdout_sha = data_fingerprint(df, HOLDOUT_START, hs["last_bar_ts"])
        holdout_ok = holdout_sha == hs["data_sha256"]
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

        recomputed = build_decisions(res, trace, all_trades)
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
        count = golive_count(ledger)
        lock_n = int(hs["n_trades"]["A"])
        warnings: list[str] = []
        events: list[dict[str, Any]] = []
        if created:
            events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], eth_head(a_notify.epoch_text(epoch))))
        for ev in decision_events(fresh, by_entry, None if open_trade is None else open_trade.entry_ts,
                                  int(epoch["started_ts"]), count, cur_closed, cfg.strategy.tp1_frac):
            events.append({**ev, "text": eth_head(ev["text"])})
        if new_disc:
            events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies),
                                     eth_head(a_notify.discrepancy_text(new_disc))))
        if not holdout_ok:  # one message per distinct revised hash (the event id carries it)
            warnings.append(f"holdout data hash {holdout_sha[:12]} != lock {hs['data_sha256'][:12]}")
            events.append(make_event("DATA_HASH", hs["last_bar_ts"], holdout_sha[:12],
                                     eth_head(a_notify.data_hash_text(hs["data_sha256"], holdout_sha))))
        if holdout_n != lock_n:  # one message per distinct recomputed count
            warnings.append(f"holdout trades recomputed {holdout_n} != lock {lock_n}")
            events.append(make_event("HOLDOUT_TRADES", hs["last_bar_ts"], holdout_n,
                                     eth_head(a_notify.holdout_trades_text(lock_n, holdout_n))))
        if gap is not None and prev_ok is not None and gap > GAP_WARN_S:
            events.append(make_event("GAP", now // H1 * H1, 0, eth_head(a_notify.gap_text(gap, prev_ok))))
        run: dict[str, Any] = {
            "id": run_id, "ts": now, "status": "ok", "last_bar_ts": last_bar, "data_sha256": data_sha,
            "holdout_data_ok": holdout_ok, "holdout_trades_recomputed": holdout_n, "epoch_id": epoch["id"],
            "paper_trades": count["paper"], "count_total": count["total"], "new_decisions": len(fresh),
            "new_discrepancies": len(new_disc), "env": run_env, "env_sha256": env_sha, "warnings": warnings,
        }
        ledger.runs.append(run)
        status = status_aeth(ledger, now)
        due = daily_summary_due(now, ledger.daily_summary_date)
        if due is not None:
            eq = {p.ts: p.equity for p in res.equity}
            base_eq = eq.get(hs["last_bar_ts"], cfg.initial_equity)
            last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
            text = "\n".join([
                f"{HEAD_AETH} 일일 요약 {due} (뉴욕)",
                position_line(ledger.open_position),
                f"자산 {last_eq:,.2f} USD (홀드아웃 이후 {last_eq / base_eq - 1:+.2%})",
                f"ETH 표본 외 누적 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 "
                f"{count['paper']}) · 구간 {epoch['id']}",
                *status_lines_aeth(status),
            ])
            events.append(make_event("DAILY", _ny9_ts(due), 0, text))
            ledger.daily_summary_date = due
        queued = queue_events(ledger, events)
    with _stage("write"):
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
    return AethRunOutcome(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                          warnings + errors, status)
