from __future__ import annotations

import copy
import dataclasses
import inspect
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytest

import ewbot.engine.loop as loop_mod
import ewbot.live.target as target_mod
from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.data.candles import futures_closed
from ewbot.engine.position_mgr import EngineResult
from ewbot.live.target import TargetError, compute_target, frozen_config, traced_with_exits, with_placeholder
from ewbot.paper.decisions import build_decisions
from ewbot.paper.trace import EngineTrace
from ewbot.strategy.base import Signal
from ewbot.types import H1
from tests.paper.helpers import PARAMS, make_lock, synth_frame, ts

SWEEP_EVERY = 23  # coprime with 24: the sampled sweep visits every hour of the day and every weekday
LONG_ENTRY = ts("2024-01-12 15:00")  # the synthetic long open across two CFM halts (trade key)
HALT = ts("2024-01-12 22:00")  # Fri 17:00 New York (EST): the CFM halt bar while that long is open
FLIP_AT = ts("2024-01-16 10:00")  # a Tuesday bar while that long is open, with no exit queued


@dataclass
class Ref:
    df: pd.DataFrame
    lock: dict[str, Any]
    res: EngineResult
    trace: EngineTrace
    exits: list[tuple[int, str]]
    halt_stops: list[int]
    decisions: list[dict[str, Any]]


def replay(df: pd.DataFrame) -> tuple[EngineResult, EngineTrace, list[tuple[int, str]], list[int]]:
    with traced_with_exits() as (trace, exits, halt_stops):
        res = run_backtest(holdout_config(BotConfig(), PARAMS), df, HOLDOUT_START, None, allow_holdout=True)
    return res, trace, exits, halt_stops


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    return make_lock(synth_frame())


@pytest.fixture(scope="module")
def ref(lock) -> Ref:
    """One replay over every synthetic bar: the reference the hourly targets must agree with."""
    df = synth_frame()
    res, trace, exits, halt_stops = replay(df)
    return Ref(df, lock, res, trace, exits, halt_stops, build_decisions(res, trace, list(res.trades)))


def _event_bars(ref: Ref) -> list[int]:
    """Every bar where something happens in the reference, with its neighbours."""
    marks = {o.ts for o in ref.trace.orders} | {f.ts for f in ref.trace.fills} | {s.ts for s in ref.trace.stops}
    marks |= {t.exit_ts - H1 for t in ref.res.trades}
    last = int(ref.res.equity[-1].ts)
    return sorted({m + k * H1 for m in marks for k in (-1, 0, 1) if HOLDOUT_START <= m + k * H1 < last})


def _assert_matches(ref: Ref, t: int) -> None:
    """compute_target at bar t (frame cut at t + the placeholder) equals the full replay's state after t."""
    tg = compute_target(ref.lock, ref.df, t)
    # entry queued at T's close
    queued = [o for o in ref.trace.orders if o.ts == t]
    if queued:
        o = queued[0]
        assert tg.entry is not None, t
        assert (tg.entry.direction, tg.entry.stop, tg.entry.tp1, tg.entry.tp2, tg.entry.contracts) == (
            o.direction, o.stop, o.tp1, o.tp2, o.contracts)
    else:
        assert tg.entry is None, t
    # position still open after T (a trade exiting at T + 1h's open is still open after T)
    live = [x for x in ref.res.trades if x.entry_ts <= t < x.exit_ts]
    if live:
        x = live[0]
        stops = [s.new for s in ref.trace.stops if s.trade_key == x.entry_ts and s.ts <= t]
        tp1 = any(f.kind == "TP1" and f.trade_key == x.entry_ts and f.ts <= t for f in ref.trace.fills)
        p = tg.position
        assert p is not None, t
        assert (p.trade_key, p.direction, p.setup, p.entry_price, p.tp1, p.tp2) == (
            x.entry_ts, x.direction, x.setup, x.entry_price, x.tp1, x.tp2)
        assert p.stop == (stops[-1] if stops else x.stop0) and p.tp1_done == tp1, t
        assert p.signal_ts == x.entry_ts - H1
        # the exit queued for this trade and not executed by T (queued at T, or before T across the halt)
        pending = [(e, r) for e, r in ref.exits if x.entry_ts <= e <= t]
        assert (tg.exit_queued_ts, tg.exit_reason) == (pending[-1] if pending else (None, None)), t
        assert tg.stop_pending_reopen == (t in ref.halt_stops), t
    else:
        assert tg.position is None, t
        assert (tg.exit_reason, tg.exit_queued_ts, tg.stop_pending_reopen) == (None, None, False), t
    # decisions up to T
    assert list(tg.decisions) == [d for d in ref.decisions if d["ts"] <= t], t


def _force_exit(monkeypatch, at: int, reason: str) -> None:
    """The engine requests `reason` at the close of bar `at` and again at the next close (BREAKER and LINEAGE
    repeat while their condition holds; only a trade's first request counts): a constructed breaker or
    broken lineage on a chosen bar."""

    class Forced(loop_mod._Run):  # type: ignore[misc, valid-type]
        def _may_enter(self, bar, t_close):  # type: ignore[no-untyped-def]
            if bar.ts in (at, at + H1):
                self._request_exit(reason)
            return super()._may_enter(bar, t_close)

    monkeypatch.setattr(loop_mod, "_Run", Forced)


def test_the_placeholder_bar_is_flat_and_refuses_a_missing_bar(ref):
    df = ref.df
    t = ts("2024-03-13 22:00")
    out = with_placeholder(df, t)
    assert int(out["ts"].iloc[-1]) == t + H1 and int(out["ts"].iloc[-2]) == t
    last = out.iloc[-1]
    assert last["open"] == last["high"] == last["low"] == last["close"] == df[df["ts"] == t]["close"].iloc[0]
    assert last["volume"] == 0.0
    with pytest.raises(TargetError):
        with_placeholder(df[df["ts"] < t], t)


def test_the_hourly_target_equals_the_full_replay_at_every_event_bar(ref):
    bars = _event_bars(ref)
    assert len(bars) > 60
    for t in bars:
        _assert_matches(ref, t)


def test_a_sampled_sweep_of_the_holdout_bars_equals_the_full_replay(ref):
    """Bar by bar beyond the event bars: every SWEEP_EVERY-th holdout bar, every CFM halt bar and the bar
    before it (the event bars are covered above)."""
    last = int(ref.res.equity[-1].ts)
    hours = [int(x) for x in ref.df["ts"] if HOLDOUT_START <= int(x) < last]
    halts = [h for h in hours if futures_closed(h)]
    bars = sorted((set(hours[::SWEEP_EVERY]) | set(halts) | {h - H1 for h in halts}) - set(_event_bars(ref)))
    assert len(bars) >= 80 and len(halts) >= 10
    assert any(x.entry_ts <= h < x.exit_ts for h in halts for x in ref.res.trades)  # a position across a halt
    for t in bars:
        _assert_matches(ref, t)


def test_queued_exits_carry_the_engine_reason(ref):
    timed = [x for x in ref.res.trades if x.exit_reason == "TIME"]
    assert timed
    for x in timed:
        tg = compute_target(ref.lock, ref.df, x.exit_ts - H1)
        assert tg.exit_reason == "TIME" and tg.exit_queued_ts == x.exit_ts - H1
        assert tg.position is not None and tg.position.trade_key == x.entry_ts


def test_a_signal_bar_gives_an_entry_with_the_engine_levels(ref):
    t = ts("2024-03-13 22:00")
    tg = compute_target(ref.lock, ref.df, t)
    sig = next(s for s in ref.res.signals if s.ts == t)
    assert tg.entry is not None and tg.position is None
    assert (tg.entry.setup, tg.entry.direction, tg.entry.entry_ref, tg.entry.risk_mult, tg.entry.reason) == (
        sig.setup, sig.direction, sig.entry_ref, sig.risk_mult, sig.reason)
    assert tg.decisions[-1]["id"] == f"SIGNAL:{t}:{t}"
    js = tg.to_json()
    assert js["entry"]["signal_ts"] == t and js["position"] is None
    assert (js["exit_reason"], js["exit_queued_ts"], js["stop_pending_reopen"]) == (None, None, False)


def test_the_engine_class_is_restored_after_an_error():
    original = loop_mod._Run
    with pytest.raises(RuntimeError):
        with traced_with_exits():
            assert loop_mod._Run is not original
            raise RuntimeError("boom")
    assert loop_mod._Run is original


def test_the_target_is_bound_to_the_holdout_lock(lock, ref):
    """No caller passes a config or a start: both come from the lock, checked like the paper precheck."""
    assert list(inspect.signature(compute_target).parameters) == ["lock", "df1h", "bar_ts"]
    assert frozen_config(lock) == holdout_config(BotConfig(), PARAMS)
    bad = {
        "sha256": dict(lock, sha256="0" * 64),  # not lock_digest(BotConfig(), params, ...): another configuration
        "code": dict(lock, code_sha256="f" * 64),  # bound through lock_digest as well
        "start": dict(lock, holdout_start=HOLDOUT_START + H1),
        "grid": make_lock(ref.df, params=(9.0, 9.0, 9.0)),  # self-consistent digest, but not a grid combo
        "missing": {k: v for k, v in lock.items() if k != "random_runs"},
        "garbage": dict(lock, params="2.0,0.5,0.0x"),
    }
    for b in bad.values():
        with pytest.raises(TargetError):
            frozen_config(b)
        with pytest.raises(TargetError):
            compute_target(b, ref.df, ts("2024-03-13 22:00"))


@pytest.mark.parametrize("reason", ["BREAKER", "LINEAGE"])
def test_an_exit_queued_before_the_cfm_halt_is_still_pending_at_the_halt_bar(lock, monkeypatch, reason):
    fri16 = HALT - H1  # Fri 16:00 New York
    assert futures_closed(HALT) and not futures_closed(fri16) and not futures_closed(HALT + H1)
    _force_exit(monkeypatch, fri16, reason)
    df = synth_frame()
    before = compute_target(lock, df, fri16)
    assert before.position is not None and before.position.trade_key == LONG_ENTRY
    assert (before.exit_reason, before.exit_queued_ts) == (reason, fri16)
    halt = compute_target(lock, df, HALT)  # nothing fills while the futures are closed: the exit is still queued
    assert halt.position is not None and halt.position.trade_key == LONG_ENTRY
    assert (halt.exit_reason, halt.exit_queued_ts, halt.stop_pending_reopen) == (reason, fri16, False)
    assert halt.to_json()["exit_queued_ts"] == fri16
    after = compute_target(lock, df, HALT + H1)  # the engine closed it at the reopen's open
    assert after.position is None and (after.exit_reason, after.exit_queued_ts) == (None, None)
    res, _, exits, _ = replay(df)
    x = next(t for t in res.trades if t.entry_ts == LONG_ENTRY)
    assert (x.exit_ts, x.exit_reason) == (HALT + H1, reason) and (fri16, reason) in exits


def test_a_stop_touched_during_the_halt_bar_is_flagged(lock, ref):
    x = next(t for t in ref.res.trades if t.entry_ts == LONG_ENTRY)
    assert x.direction == 1 and x.exit_ts > HALT + H1
    stops = [s.new for s in ref.trace.stops if s.trade_key == LONG_ENTRY and s.ts < HALT]
    stop = stops[-1] if stops else x.stop0  # the stop in force during the halt bar
    df = ref.df.copy()
    i = df.index[df["ts"] == HALT][0]
    assert df.loc[i, "low"] > stop
    df.loc[i, "low"] = stop - 10.0  # the halt bar's range crosses the stop (spot trades through the CFM halt)
    tg = compute_target(lock, df, HALT)
    assert tg.position is not None and tg.position.trade_key == LONG_ENTRY
    assert tg.stop_pending_reopen and tg.exit_reason is None and tg.to_json()["stop_pending_reopen"] is True
    assert not compute_target(lock, df, HALT - H1).stop_pending_reopen
    after = compute_target(lock, df, HALT + H1)
    assert after.position is None or after.position.trade_key != LONG_ENTRY
    assert not after.stop_pending_reopen
    res, _, _, halt_stops = replay(df)
    y = next(t for t in res.trades if t.entry_ts == LONG_ENTRY)
    assert (y.exit_ts, y.exit_reason) == (HALT + H1, "STOP") and halt_stops == [HALT]


def test_a_flip_target_holds_the_position_its_queued_exit_and_the_opposite_entry(lock, monkeypatch):
    """The engine flips at FLIP_AT: the long is still open after that bar, its FLIP exit and the short entry are
    both queued at the close; one hour later the short is the position."""

    class Flipping(loop_mod._Run):  # type: ignore[misc, valid-type]
        def __init__(self, cfg, strategy, wave, choch, start_ts) -> None:  # type: ignore[no-untyped-def]
            super().__init__(cfg, strategy, wave, choch, start_ts)
            inner = self.strategy

            class Opposite:
                def on_bar(self, cbar, ws, ch):  # type: ignore[no-untyped-def]
                    sig = inner.on_bar(cbar, ws, ch)
                    if cbar.ts != FLIP_AT:
                        return sig
                    c = cbar.close
                    return Signal(FLIP_AT, "C_END", -1, c, c + 300.0, c - 600.0, c - 1200.0, 1.0, None, None, None,
                                  "constructed flip")

            self.strategy = Opposite()

    monkeypatch.setattr(loop_mod, "_Run", Flipping)
    df = synth_frame()
    tg = compute_target(lock, df, FLIP_AT)
    assert tg.position is not None and (tg.position.trade_key, tg.position.direction) == (LONG_ENTRY, 1)
    assert (tg.exit_reason, tg.exit_queued_ts) == ("FLIP", FLIP_AT)
    assert tg.entry is not None and (tg.entry.signal_ts, tg.entry.direction, tg.entry.reason) == (
        FLIP_AT, -1, "constructed flip")
    assert tg.decisions[-1]["id"] == f"SIGNAL:{FLIP_AT}:{FLIP_AT}"
    nxt = compute_target(lock, df, FLIP_AT + H1)
    assert nxt.position is not None and (nxt.position.trade_key, nxt.position.signal_ts, nxt.position.direction) == (
        FLIP_AT + H1, FLIP_AT, -1)
    assert (nxt.exit_reason, nxt.exit_queued_ts, nxt.entry) == (None, None, None)
    res, _, _, _ = replay(df)
    assert next(t for t in res.trades if t.entry_ts == LONG_ENTRY).exit_reason == "FLIP"


def test_an_inconsistent_replay_raises_a_target_error(lock, ref, monkeypatch):
    real = target_mod.run_backtest
    signal_bar = ts("2024-03-13 22:00")
    monkeypatch.setattr(target_mod, "run_backtest", lambda *a, **k: dataclasses.replace(real(*a, **k), signals=()))
    with pytest.raises(TargetError, match="signal"):
        compute_target(lock, ref.df, signal_bar)
    monkeypatch.setattr(target_mod, "run_backtest", lambda *a, **k: dataclasses.replace(real(*a, **k), trades=()))
    with pytest.raises(TargetError, match="trade"):
        compute_target(lock, ref.df, signal_bar + 5 * H1)  # the long entered at signal_bar + 1h is open


# ---------------------------------------------------------------------------- Final fix
def test_an_incomplete_holdout_lock_is_refused_like_the_paper_precheck(lock):
    """paper.runner.precheck_lock also requires the data fingerprint, the last holdout bar and the holdout's trade count:
    a lock without them is not a completed holdout lock (its digest does not cover them)."""
    def without(path: tuple[str, ...]) -> dict:
        d = copy.deepcopy(lock)
        node = d
        for k in path[:-1]:
            node = node[k]
        del node[path[-1]]
        return d

    for path in (("data", "last_bar_ts"), ("data", "sha256"), ("first_run", "criteria", "n_trades", "A"), ("data",),
                 ("first_run",)):
        with pytest.raises(TargetError, match="not a completed holdout lock"):
            frozen_config(without(path))
    with pytest.raises(TargetError):
        frozen_config(dict(lock, data={**lock["data"], "last_bar_ts": "soon"}))


def test_a_decision_the_paper_builder_refuses_is_a_target_error(lock, ref, monkeypatch):
    """build_decisions raises ValueError for a trace it cannot pair (queued entries vs. signals): an inconsistent replay,
    a TargetError like the others - the cycle fails as ERROR, never as an unexpected exception."""
    def refuse(*a, **k):
        raise ValueError("2 signals but 1 queued entries in the trace")

    monkeypatch.setattr(target_mod, "build_decisions", refuse)
    with pytest.raises(TargetError, match="decisions"):
        compute_target(lock, ref.df, ts("2024-03-13 22:00"))
