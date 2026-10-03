from __future__ import annotations

import pytest

import ewbot.engine.loop as loop_mod
from ewbot.backtest.holdout import holdout_config
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.live.target import TargetError, compute_target, traced_with_exits, with_placeholder
from ewbot.paper.decisions import build_decisions
from ewbot.types import H1
from tests.paper.helpers import PARAMS, synth_frame, ts


@pytest.fixture(scope="module")
def ref():
    """One replay over every synthetic bar: the reference the hourly targets must agree with."""
    df = synth_frame()
    cfg = holdout_config(BotConfig(), PARAMS)
    with traced_with_exits() as (trace, exits):
        res = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
    decisions = build_decisions(res, trace, list(res.trades))
    return df, cfg, res, trace, exits, decisions


def _event_bars(res, trace) -> list[int]:
    """Every bar where something happens in the reference, with its neighbours."""
    marks = {o.ts for o in trace.orders} | {f.ts for f in trace.fills} | {s.ts for s in trace.stops}
    marks |= {t.exit_ts - H1 for t in res.trades}
    last = int(res.equity[-1].ts)
    return sorted({m + k * H1 for m in marks for k in (-1, 0, 1)
                   if HOLDOUT_START <= m + k * H1 < last})


def test_the_placeholder_bar_is_flat_and_refuses_a_missing_bar(ref):
    df = ref[0]
    t = ts("2024-03-13 22:00")
    out = with_placeholder(df, t)
    assert int(out["ts"].iloc[-1]) == t + H1 and int(out["ts"].iloc[-2]) == t
    last = out.iloc[-1]
    assert last["open"] == last["high"] == last["low"] == last["close"] == df[df["ts"] == t]["close"].iloc[0]
    assert last["volume"] == 0.0
    with pytest.raises(TargetError):
        with_placeholder(df[df["ts"] < t], t)


def test_the_hourly_target_equals_the_full_replay_at_every_event_bar(ref):
    df, cfg, res, trace, exits, decisions = ref
    bars = _event_bars(res, trace)
    assert len(bars) > 60
    for t in bars:
        tg = compute_target(cfg, df, t)
        # entry queued at T's close
        queued = [o for o in trace.orders if o.ts == t]
        if queued:
            o = queued[0]
            assert tg.entry is not None, t
            assert (tg.entry.direction, tg.entry.stop, tg.entry.tp1, tg.entry.tp2, tg.entry.contracts) == (
                o.direction, o.stop, o.tp1, o.tp2, o.contracts)
        else:
            assert tg.entry is None, t
        # position still open after T (a trade exiting at T + 1h's open is still open after T)
        live = [x for x in res.trades if x.entry_ts <= t < x.exit_ts]
        if live:
            x = live[0]
            stops = [s.new for s in trace.stops if s.trade_key == x.entry_ts and s.ts <= t]
            tp1 = any(f.kind == "TP1" and f.trade_key == x.entry_ts and f.ts <= t for f in trace.fills)
            p = tg.position
            assert p is not None, t
            assert (p.trade_key, p.direction, p.setup, p.entry_price, p.tp1, p.tp2) == (
                x.entry_ts, x.direction, x.setup, x.entry_price, x.tp1, x.tp2)
            assert p.stop == (stops[-1] if stops else x.stop0) and p.tp1_done == tp1, t
            assert p.signal_ts == x.entry_ts - H1
        else:
            assert tg.position is None, t
        # exit queued at T's close
        assert tg.exit_reason == next((r for e, r in exits if e == t), None), t
        # decisions up to T
        assert list(tg.decisions) == [d for d in decisions if d["ts"] <= t], t


def test_queued_exits_carry_the_engine_reason(ref):
    df, cfg, res, trace, exits, _ = ref
    timed = [x for x in res.trades if x.exit_reason == "TIME"]
    assert timed
    for x in timed:
        tg = compute_target(cfg, df, x.exit_ts - H1)
        assert tg.exit_reason == "TIME" and tg.position is not None and tg.position.trade_key == x.entry_ts


def test_a_signal_bar_gives_an_entry_with_the_engine_levels(ref):
    df, cfg, res, trace, _, _ = ref
    t = ts("2024-03-13 22:00")
    tg = compute_target(cfg, df, t)
    sig = next(s for s in res.signals if s.ts == t)
    assert tg.entry is not None and tg.position is None
    assert (tg.entry.setup, tg.entry.direction, tg.entry.entry_ref, tg.entry.risk_mult, tg.entry.reason) == (
        sig.setup, sig.direction, sig.entry_ref, sig.risk_mult, sig.reason)
    assert tg.decisions[-1]["id"] == f"SIGNAL:{t}:{t}"
    assert tg.to_json()["entry"]["signal_ts"] == t and tg.to_json()["position"] is None


def test_the_engine_class_is_restored_after_an_error():
    original = loop_mod._Run
    with pytest.raises(RuntimeError):
        with traced_with_exits():
            assert loop_mod._Run is not original
            raise RuntimeError("boom")
    assert loop_mod._Run is original
