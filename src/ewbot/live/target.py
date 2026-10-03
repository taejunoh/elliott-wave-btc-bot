"""Target state from the frozen engine (Plan 3 spec 3.1, 4.1 step 2).

Every cycle replays the whole engine - `run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)`
over every cached bar, exactly like the paper runtime - and reads what the engine wants after the
latest closed bar T:
  - `position`: the engine position still open after bar T (its current stop, TPs, TP1 state);
  - `exit_reason` / `exit_queued_ts`: the exit the engine has queued for that position and not executed
    yet (TIME, LINEAGE, BREAKER, FLIP) and the bar at whose close it was queued. Usually that bar is T
    (the engine fills the exit at T+1's open). Nothing fills during the CFM halt (Fri 17-18 New York,
    `futures_closed`), so an exit queued at the Fri 16:00 close is still pending after the halt bar
    T = 17:00 (exit_queued_ts = 16:00 < T) and fills at the 18:00 open;
  - `stop_pending_reopen`: the halt bar T crossed the position's stop - the paper broker marks the stop
    triggered while closed and the engine stops the position out at the next tradable open;
  - `entry`: an entry the engine queued at T's close (it fills at T+1's open in the engine);
  - `decisions`: the engine's decisions with ts <= T in the paper ledger's format (audit, spec 3.5).

Frozen inputs (Plan 3 global constraints): the configuration is built here from the holdout lock -
`holdout_config(BotConfig(), params)` with the lock checked like the paper precheck (`lock_params`) -
and the replay starts at HOLDOUT_START. No caller passes a configuration or a start.

The engine never evaluates entries on the LAST bar of a run (it has no next bar to fill on), so the
frame gets one placeholder bar T+1 (flat at T's close, volume 0, synthetic). Everything decided at or
before T's close is computed before the engine reaches T+1, so it is identical to a replay that has
the real bar T+1 (tests/live/test_target.py proves it bar by bar); nothing at T+1 is used.

Queued exits and halt stops are not in EngineResult or in the paper trace, so the engine's per-run
`_Run` class is swapped (the paper trace's technique: the name is looked up at run time, no file is
edited) for a subclass that records each fresh `_request_exit(reason)` with the bar it happened on and
every bar after which the broker holds a stop triggered during the halt. The swap is process-global
inside the `with` block: never run two replays concurrently.

This module is the live runtime's only caller of allow_holdout=True (tests/paper/test_static.py)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pandas as pd

import ewbot.engine.loop as loop_mod
from ewbot.backtest.holdout import holdout_config, lock_digest
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START, grid_combos
from ewbot.config import CONSERVATIVE, BotConfig
from ewbot.paper.decisions import build_decisions
from ewbot.paper.trace import EngineTrace, traced_engine
from ewbot.types import H1
from ewbot.waves.engine import WaveState


class TargetError(RuntimeError):
    """The lock is not the holdout's, the candles do not end at the requested bar, or the replay is
    inconsistent."""


@dataclass(frozen=True, slots=True)
class TargetPosition:
    trade_key: int  # engine entry_ts (the paper ledger's trade key)
    signal_ts: int  # bar whose close queued the entry (= the live trade id)
    setup: str
    direction: int
    entry_price: float  # engine (spot) entry fill
    stop: float  # engine stop after bar T (spot)
    stop0: float
    tp1: float
    tp2: float | None
    tp1_done: bool
    contracts: int  # engine size on the paper equity (informational)
    remaining: int


@dataclass(frozen=True, slots=True)
class TargetEntry:
    signal_ts: int  # = T
    setup: str
    direction: int
    entry_ref: float  # spot close of T
    stop: float
    tp1: float
    tp2: float | None
    risk_mult: float
    contracts: int  # engine size on the paper equity (informational)
    reason: str


@dataclass(frozen=True, slots=True)
class Target:
    bar_ts: int
    close: float
    position: TargetPosition | None
    exit_reason: str | None  # exit queued for `position`, not executed by T (None when flat)
    entry: TargetEntry | None
    decisions: tuple[dict[str, Any], ...]
    exit_queued_ts: int | None = None  # bar whose close queued `exit_reason` (< bar_ts: carried across the halt)
    stop_pending_reopen: bool = False  # the halt bar crossed the stop: the engine stops out at the reopen

    def to_json(self) -> dict[str, Any]:
        def pos(p: Any) -> dict[str, Any] | None:
            return None if p is None else {f: getattr(p, f) for f in p.__slots__}

        return {"bar_ts": self.bar_ts, "close": self.close, "position": pos(self.position),
                "exit_reason": self.exit_reason, "exit_queued_ts": self.exit_queued_ts,
                "stop_pending_reopen": self.stop_pending_reopen, "entry": pos(self.entry)}


def lock_params(lock: Mapping[str, Any]) -> tuple[float, ...]:
    """The params of a completed holdout lock, checked with the paper precheck's semantics
    (paper.runner.precheck_lock) minus its git checks - the image has no git history; preflight pins the
    committed lock's sha256 instead: the params are a walk-forward grid combo, the holdout start is
    HOLDOUT_START and sha256 == lock_digest(BotConfig(), params, random_runs, code_sha256), i.e. the
    configuration built from them is the one the holdout ran with (conservative, breakers on)."""
    try:
        params = tuple(float(x) for x in lock["params"])
        random_runs = int(lock["random_runs"])
        code_sha = str(lock["code_sha256"])
        holdout_start = int(lock["holdout_start"])
        sha = str(lock["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TargetError(f"not a completed holdout lock: {exc!r}") from exc
    if params not in grid_combos():
        raise TargetError(f"lock params {list(params)} are not a walk-forward grid combo")
    if holdout_start != HOLDOUT_START:
        raise TargetError(f"lock holdout_start {holdout_start} != {HOLDOUT_START}")
    digest = lock_digest(BotConfig(), params, random_runs, code_sha)
    if digest != sha:
        raise TargetError(f"lock sha256 {sha[:12]} != lock_digest(BotConfig(), params, random_runs, code_sha256) "
                          f"{digest[:12]}: not the configuration the holdout ran with")
    return params


def frozen_config(lock: Mapping[str, Any]) -> BotConfig:
    """The holdout's configuration: BotConfig defaults with the lock's params (strategy A, conservative risk,
    circuit breakers on)."""
    cfg = holdout_config(BotConfig(), lock_params(lock))
    if cfg.risk != CONSERVATIVE or not cfg.breakers.enabled:
        raise TargetError("the frozen configuration must be conservative with the circuit breakers on")
    return cfg


def with_placeholder(df1h: pd.DataFrame, bar_ts: int) -> pd.DataFrame:
    """Bars up to and including bar_ts plus the flat placeholder bar bar_ts + 1h."""
    d = df1h[df1h["ts"] <= bar_ts]
    if d.empty or int(d["ts"].iloc[-1]) != int(bar_ts):
        raise TargetError(f"the candles do not contain the bar {bar_ts}")
    close = float(d["close"].iloc[-1])
    row: dict[str, Any] = {"ts": int(bar_ts) + H1, "open": close, "high": close, "low": close, "close": close,
                           "volume": 0.0}
    if "synthetic" in d.columns:
        row["synthetic"] = True
    out = pd.concat([d, pd.DataFrame([row])], ignore_index=True)
    return out.astype({"ts": "int64"})


@contextmanager
def traced_with_exits() -> Iterator[tuple[EngineTrace, list[tuple[int, str]], list[int]]]:
    """paper.trace.traced_engine() plus what the engine queues: (trace, exits [(bar ts, reason)] - each
    fresh exit request with the bar whose close made it -, halt_stops [bar ts] - every bar after which
    the open position's stop has been triggered during the futures halt, PaperBroker's
    `_stop_triggered_while_closed`: it stops out at the next tradable open)."""
    exits: list[tuple[int, str]] = []
    halt_stops: list[int] = []
    with traced_engine() as trace:
        original = loop_mod._Run

        class _ExitRecordingRun(original):  # type: ignore[misc, valid-type]
            def _manage(self, ws: WaveState, closed_4h: bool) -> None:
                b = self.broker  # step 5 runs right after this bar's fills (process_bar)
                if b.position is not None and b._stop_triggered_while_closed and trace.last_ts is not None:
                    halt_stops.append(int(trace.last_ts))
                super()._manage(ws, closed_4h)

            def _request_exit(self, reason: str) -> None:
                ot = self.open
                fresh = ot is not None and self.broker.position is not None and ot.exit_reason is None
                super()._request_exit(reason)
                if fresh and trace.last_ts is not None:
                    exits.append((int(trace.last_ts), reason))

        loop_mod._Run = _ExitRecordingRun  # type: ignore[misc]
        try:
            yield trace, exits, halt_stops
        finally:
            loop_mod._Run = original  # type: ignore[misc]


def compute_target(lock: Mapping[str, Any], df1h: pd.DataFrame, bar_ts: int) -> Target:
    """The engine's target after bar `bar_ts` (module docstring) for the holdout `lock`. Raises TargetError
    when the lock is not the holdout's, the candles do not end at bar_ts or the replay is inconsistent."""
    cfg = frozen_config(lock)
    bar_ts = int(bar_ts)
    frame = with_placeholder(df1h, bar_ts)
    with traced_with_exits() as (trace, exits, halt_stops):
        res = run_backtest(cfg, frame, HOLDOUT_START, None, allow_holdout=True)

    def inconsistent(what: str) -> TargetError:
        return TargetError(f"inconsistent replay at bar {bar_ts}: {what}")

    fills = [f for f in trace.fills if f.ts <= bar_ts]
    left: dict[int, int] = {}
    open_key: int | None = None
    for f in fills:
        if f.kind == "ENTRY":
            left[f.trade_key] = f.contracts
            open_key = f.trade_key
        elif f.trade_key not in left:
            raise inconsistent(f"a {f.kind} fill at {f.ts} for the unknown position {f.trade_key}")
        else:
            left[f.trade_key] -= f.contracts
            if left[f.trade_key] == 0 and f.trade_key == open_key:
                open_key = None

    position: TargetPosition | None = None
    exit_reason: str | None = None
    exit_queued_ts: int | None = None
    if open_key is not None:
        t = next((x for x in res.trades if x.entry_ts == open_key), None)
        if t is None:
            raise inconsistent(f"no engine trade for the position entered at {open_key}")
        orders = [o for o in trace.orders if o.ts < open_key]
        if not orders:
            raise inconsistent(f"no queued entry before the position entered at {open_key}")
        stops = [s for s in trace.stops if s.trade_key == open_key and s.ts <= bar_ts]
        tp1 = sum(f.contracts for f in fills if f.trade_key == open_key and f.kind == "TP1")
        position = TargetPosition(
            trade_key=open_key, signal_ts=orders[-1].ts, setup=t.setup, direction=t.direction,
            entry_price=t.entry_price, stop=stops[-1].new if stops else t.stop0, stop0=t.stop0, tp1=t.tp1,
            tp2=t.tp2, tp1_done=tp1 > 0, contracts=t.contracts, remaining=t.contracts - tp1,
        )
        # the position is still open after T, so an exit queued for it since its entry has not filled yet
        pending = [(ts, r) for ts, r in exits if open_key <= ts <= bar_ts]
        if pending:
            exit_queued_ts, exit_reason = pending[-1]
    stop_pending_reopen = position is not None and bar_ts in halt_stops

    entry: TargetEntry | None = None
    queued = [o for o in trace.orders if o.ts == bar_ts]
    if queued:
        sig = next((s for s in res.signals if s.ts == bar_ts), None)
        if sig is None:
            raise inconsistent(f"an entry queued at {bar_ts} without its engine signal")
        o = queued[-1]
        entry = TargetEntry(
            signal_ts=bar_ts, setup=sig.setup, direction=sig.direction, entry_ref=sig.entry_ref, stop=o.stop,
            tp1=o.tp1, tp2=o.tp2, risk_mult=sig.risk_mult, contracts=o.contracts, reason=sig.reason,
        )

    try:
        decisions = tuple(d for d in build_decisions(res, trace, list(res.trades)) if d["ts"] <= bar_ts)
    except (KeyError, StopIteration) as exc:
        raise inconsistent(f"the decisions cannot be built: {exc!r}") from exc
    close = float(frame["close"].iloc[-1])
    return Target(bar_ts, close, position, exit_reason, entry, decisions, exit_queued_ts, stop_pending_reopen)
