"""Target state from the frozen engine (Plan 3 spec 3.1, 4.1 step 2).

Every cycle replays the whole engine - `run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)`
over every cached bar, exactly like the paper runtime - and reads what the engine wants after the
latest closed bar T:
  - `position`: the engine position still open after bar T (its current stop, TPs, TP1 state);
  - `exit_reason`: an exit the engine queued at T's close (TIME, LINEAGE, BREAKER, FLIP);
  - `entry`: an entry the engine queued at T's close (it fills at T+1's open in the engine);
  - `decisions`: the engine's decisions with ts <= T in the paper ledger's format (audit, spec 3.5).

The engine never evaluates entries on the LAST bar of a run (it has no next bar to fill on), so the
frame gets one placeholder bar T+1 (flat at T's close, volume 0, synthetic). Everything decided at or
before T's close is computed before the engine reaches T+1, so it is identical to a replay that has
the real bar T+1 (tests/live/test_target.py proves it bar by bar); nothing at T+1 is used.

Queued exits are not in EngineResult or in the paper trace, so the engine's per-run `_Run` class is
swapped (the paper trace's technique: the name is looked up at run time, no file is edited) for a
subclass that records each fresh `_request_exit(reason)` with the bar it happened on. The swap is
process-global inside the `with` block: never run two replays concurrently.

This module is the live runtime's only caller of allow_holdout=True (tests/paper/test_static.py)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pandas as pd

import ewbot.engine.loop as loop_mod
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.paper.decisions import build_decisions
from ewbot.paper.trace import EngineTrace, traced_engine
from ewbot.types import H1


class TargetError(RuntimeError):
    """The candles do not end at the requested bar (or the replay is inconsistent)."""


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
    exit_reason: str | None
    entry: TargetEntry | None
    decisions: tuple[dict[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        def pos(p: Any) -> dict[str, Any] | None:
            return None if p is None else {f: getattr(p, f) for f in p.__slots__}

        return {"bar_ts": self.bar_ts, "close": self.close, "position": pos(self.position),
                "exit_reason": self.exit_reason, "entry": pos(self.entry)}


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
def traced_with_exits() -> Iterator[tuple[EngineTrace, list[tuple[int, str]]]]:
    """paper.trace.traced_engine() plus the exits the engine queues: [(bar ts, reason)]."""
    exits: list[tuple[int, str]] = []
    with traced_engine() as trace:
        original = loop_mod._Run

        class _ExitRecordingRun(original):  # type: ignore[misc, valid-type]
            def _request_exit(self, reason: str) -> None:
                ot = self.open
                fresh = ot is not None and self.broker.position is not None and ot.exit_reason is None
                super()._request_exit(reason)
                if fresh and trace.last_ts is not None:
                    exits.append((int(trace.last_ts), reason))

        loop_mod._Run = _ExitRecordingRun  # type: ignore[misc]
        try:
            yield trace, exits
        finally:
            loop_mod._Run = original  # type: ignore[misc]


def compute_target(cfg: BotConfig, df1h: pd.DataFrame, bar_ts: int, start_ts: int = HOLDOUT_START) -> Target:
    frame = with_placeholder(df1h, bar_ts)
    with traced_with_exits() as (trace, exits):
        res = run_backtest(cfg, frame, start_ts, None, allow_holdout=True)
    trades = {t.entry_ts: t for t in res.trades}
    fills = [f for f in trace.fills if f.ts <= bar_ts]

    left: dict[int, int] = {}
    open_key: int | None = None
    for f in fills:
        if f.kind == "ENTRY":
            left[f.trade_key] = f.contracts
            open_key = f.trade_key
        else:
            left[f.trade_key] -= f.contracts
            if left[f.trade_key] == 0 and f.trade_key == open_key:
                open_key = None

    position: TargetPosition | None = None
    if open_key is not None:
        t = trades[open_key]
        stops = [s for s in trace.stops if s.trade_key == open_key and s.ts <= bar_ts]
        orders = [o for o in trace.orders if o.ts < open_key]
        if not orders:
            raise TargetError(f"no queued entry before the position entered at {open_key}")
        tp1 = sum(f.contracts for f in fills if f.trade_key == open_key and f.kind == "TP1")
        position = TargetPosition(
            trade_key=open_key, signal_ts=orders[-1].ts, setup=t.setup, direction=t.direction,
            entry_price=t.entry_price, stop=stops[-1].new if stops else t.stop0, stop0=t.stop0, tp1=t.tp1,
            tp2=t.tp2, tp1_done=tp1 > 0, contracts=t.contracts, remaining=t.contracts - tp1,
        )

    exit_reason = next((r for ts, r in exits if ts == bar_ts), None) if position is not None else None

    entry: TargetEntry | None = None
    queued = [o for o in trace.orders if o.ts == bar_ts]
    if queued:
        sig = next(s for s in res.signals if s.ts == bar_ts)
        o = queued[-1]
        entry = TargetEntry(
            signal_ts=bar_ts, setup=sig.setup, direction=sig.direction, entry_ref=sig.entry_ref, stop=o.stop,
            tp1=o.tp1, tp2=o.tp2, risk_mult=sig.risk_mult, contracts=o.contracts, reason=sig.reason,
        )

    decisions = tuple(d for d in build_decisions(res, trace, list(res.trades)) if d["ts"] <= bar_ts)
    close = float(frame["close"].iloc[-1])
    return Target(int(bar_ts), close, position, exit_reason, entry, decisions)
