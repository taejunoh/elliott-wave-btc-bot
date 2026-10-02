from __future__ import annotations

import pytest

from ewbot.engine.position_mgr import (
    EngineResult,
    EquityPoint,
    Trade,
    _make_snapshot,
    _OpenTrade,
    _tighten,
    _tp1_split,
    _trail_candidate,
)
from ewbot.execution.paper import Fill, PaperPosition
from ewbot.strategy.base import Signal
from ewbot.waves.engine import WaveState
from ewbot.waves.enumerate import Count
from ewbot.waves.zigzag import Pivot

T0 = 1_704_067_200


def make_signal(**kw: object) -> Signal:
    base: dict[str, object] = dict(
        ts=T0, setup="W3", direction=1, entry_ref=50_000.0, stop=49_000.0, tp1=51_000.0, tp2=52_000.0,
        risk_mult=1.0, time_stop_4h=12, instance=("IMP", 1, 7, "W2"), lineage=("IMP", 1, 7), reason="unit",
    )
    base.update(kw)
    return Signal(**base)  # type: ignore[arg-type]


def make_count(i0: int, phase: str = "W2", q: float = 0.7) -> Count:
    pts = (
        Pivot(i=i0, sub_ts=T0 + i0 * 3600, price=48_000.0 + i0, kind=-1, confirmed_at=i0 + 1),
        Pivot(i=i0 + 1, sub_ts=T0 + (i0 + 1) * 3600, price=51_000.0 + i0, kind=1, confirmed_at=i0 + 2),
    )
    return Count(pattern="IMP", d=1, phase=phase, points=pts, q=q)


def make_ws(counts: tuple[Count, ...], probs: tuple[float, ...]) -> WaveState:
    return WaveState(
        ts=T0, ready=True, counts=counts, probs=probs, top=counts[0] if counts else None,
        q_top=counts[0].q if counts else 0.0, s_rev=0.4, lineages=frozenset(c.lineage for c in counts),
        E_live=49_500.0, E_live_ts=T0, leg_dir=-1, leg_start=None, pivots4h=(), new_pivot_4h=None,
        atr4h=400.0, closed_4h=False, i4h=10,
    )


def test_result_types_are_frozen_value_objects() -> None:
    ep = EquityPoint(ts=T0, equity=10_000.0)
    res = EngineResult(trades=(), equity=(ep,), signals=(), events=((T0, "DAILY_HALT"),))
    assert res.equity[0].equity == 10_000.0
    with pytest.raises(AttributeError):
        ep.equity = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("contracts", "frac", "expected"),
    [(10, 0.5, 5), (9, 0.5, 4), (2, 0.5, 1), (1, 0.5, 1), (2, 0.3, 1), (4, 1.0, 4)],
)
def test_tp1_split(contracts: int, frac: float, expected: int) -> None:
    assert _tp1_split(contracts, frac) == expected


def test_tighten_only_moves_in_favour() -> None:
    assert _tighten(1, 49_000.0, 49_700.0) == 49_700.0
    assert _tighten(1, 49_000.0, 48_500.0) == 49_000.0
    assert _tighten(-1, 51_000.0, 50_300.0) == 50_300.0
    assert _tighten(-1, 51_000.0, 51_500.0) == 51_000.0


def test_trail_candidate_uses_protective_pivot_side() -> None:
    low = Pivot(i=5, sub_ts=T0, price=49_800.0, kind=-1, confirmed_at=6)
    high = Pivot(i=6, sub_ts=T0, price=50_200.0, kind=1, confirmed_at=7)
    assert _trail_candidate(1, low, 400.0, 0.25) == pytest.approx(49_700.0)
    assert _trail_candidate(1, high, 400.0, 0.25) is None
    assert _trail_candidate(-1, high, 400.0, 0.25) == pytest.approx(50_300.0)
    assert _trail_candidate(-1, low, 400.0, 0.25) is None
    assert _trail_candidate(1, low, None, 0.25) is None
    assert _trail_candidate(1, None, 400.0, 0.25) is None


def test_snapshot_keeps_top_k_counts_with_sub_ts_points() -> None:
    counts = tuple(make_count(i0) for i0 in (1, 3, 5, 7, 9, 11))
    probs = (0.5, 0.2, 0.1, 0.1, 0.05, 0.05)
    snap = _make_snapshot(make_ws(counts, probs), make_signal(), top_k=5)
    assert len(snap["counts"]) == 5
    assert snap["counts"][0] == {
        "pattern": "IMP", "d": 1, "phase": "W2",
        "points": [(T0 + 3600, 48_001.0), (T0 + 7200, 51_001.0)],
        "q": 0.7, "p": 0.5,
    }
    assert [c["p"] for c in snap["counts"]] == [0.5, 0.2, 0.1, 0.1, 0.05]
    assert snap["q_top"] == 0.7 and snap["s_rev"] == 0.4 and snap["E_live"] == 49_500.0
    assert snap["reason"] == "unit" and snap["setup"] == "W3" and snap["ts"] == T0


def test_open_trade_accounting_partial_exit() -> None:
    sig = make_signal()
    pos = PaperPosition(direction=1, contracts=10, entry_price=50_000.0, entry_ts=T0, stop=49_000.0,
                        tp1=51_000.0, tp2=52_000.0, tp1_contracts=5, funding=3.0)
    ot = _OpenTrade(sig=sig, snapshot={"k": 1}, contract_size=0.01, entry_ts=T0, entry_price=50_000.0,
                    contracts=10, pos=pos, fees=2.0)
    assert not ot.add_exit_fill(Fill(T0 + 3600, "TP1", -1, 5, 51_000.0, 1.0))
    assert ot.tp1_done
    assert ot.add_exit_fill(Fill(T0 + 7200, "STOP", -1, 5, 50_000.0, 1.0))
    tr = ot.to_trade(T0 + 7200, "STOP")
    assert isinstance(tr, Trade)
    # gross 5 * 0.01 * 1_000 = 50; fees 2 + 1 + 1 = 4; funding 3 -> pnl 43; risk 1_000 * 10 * 0.01 = 100
    assert tr.pnl == pytest.approx(43.0)
    assert tr.fees == pytest.approx(4.0) and tr.funding == pytest.approx(3.0)
    assert tr.r_multiple == pytest.approx(0.43)
    assert tr.exit_price_avg == pytest.approx(50_500.0)
    assert (tr.entry_ts, tr.exit_ts, tr.contracts) == (T0, T0 + 7200, 10)
    assert (tr.stop0, tr.tp1, tr.tp2) == (49_000.0, 51_000.0, 52_000.0)
    assert tr.snapshot == {"k": 1} and tr.exit_reason == "STOP"
