# tests/strategy/test_fib_swing.py
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy.base import Strategy
from ewbot.strategy.fib_swing import FibSwingStrategy
from ewbot.types import Bar
from ewbot.waves.engine import WaveState

FIB = FibSwingStrategy(StrategyParams())

UPTREND = [(0, 100.0, -1), (8, 200.0, 1), (14, 150.0, -1), (24, 300.0, 1)]  # HH 300>200, HL 150>100


def scen(
    sb: SimpleNamespace,
    pivots: list[tuple[int, float, int]] = UPTREND,
    E: float = 185.0,
    close: float = 192.0,
    leg_dir: int = -1,
) -> tuple[WaveState, Bar]:
    ws = sb.ws(top=None, E=E, leg_dir=leg_dir, pivots=[sb.piv(i, p, k) for i, p, k in pivots], s_rev=0.0, i4h=30)
    return ws, sb.bar(ws, close)


def test_long_in_uptrend_pullback(sb: SimpleNamespace) -> None:
    # H=300 (i24), L=150 (i14), E=185 -> r = 115/150 = 0.767; stop = 150 - 0.25*8;
    # TP1 = 185 + 1.0*150, TP2 = 185 + 1.618*150 (setup W3 geometry)
    ws, bar = scen(sb)
    choch = sb.FakeChoch()
    sig = FIB.on_bar(bar, ws, choch)
    assert sig is not None
    assert (sig.setup, sig.direction, sig.entry_ref) == ("FIB", 1, 192.0)
    assert sig.stop == pytest.approx(148.0)
    assert sig.tp1 == pytest.approx(335.0)
    assert sig.tp2 == pytest.approx(427.7)
    assert sig.time_stop_4h == 15  # 1.5 * (24 - 14)
    assert sig.risk_mult == 1.0
    assert sig.instance is None and sig.lineage is None
    assert choch.calls == [(1, ws.leg_start.sub_ts, ws.E_live_ts)]


def test_short_in_downtrend_rally_is_mirror(sb: SimpleNamespace) -> None:
    # highs 1300 -> 1250 (LH), lows 1200 -> 1100 (LL); E = 1215 (prices high enough that TP2 stays > 0)
    down = [(0, 1300.0, 1), (8, 1200.0, -1), (14, 1250.0, 1), (24, 1100.0, -1)]
    ws, bar = scen(sb, pivots=down, E=1215.0, close=1208.0, leg_dir=1)
    choch = sb.FakeChoch()
    sig = FIB.on_bar(bar, ws, choch)
    assert sig is not None
    assert (sig.setup, sig.direction) == ("FIB", -1)
    assert sig.stop == pytest.approx(1252.0)
    assert sig.tp1 == pytest.approx(1065.0)  # 1215 - 1.0*150
    assert sig.tp2 == pytest.approx(972.3)  # 1215 - 1.618*150
    assert sig.time_stop_4h == 15
    assert choch.calls[0][0] == -1


def test_generic_mirror_matches(sb: SimpleNamespace) -> None:
    m = 1000.0
    ws, bar = scen(sb)
    bull = FIB.on_bar(bar, ws, sb.FakeChoch())
    bear = FIB.on_bar(sb.mirror_bar(bar, m), sb.mirror_ws(ws, m), sb.FakeChoch())
    assert bull is not None and bear is not None
    assert bear.direction == -bull.direction
    for key in ("entry_ref", "stop", "tp1", "tp2"):
        assert getattr(bear, key) == pytest.approx(2 * m - getattr(bull, key))


def test_no_trend_no_signal(sb: SimpleNamespace) -> None:
    hh_ll = [(0, 100.0, -1), (8, 200.0, 1), (14, 90.0, -1), (24, 300.0, 1)]  # HH but LL
    ws, bar = scen(sb, pivots=hh_ll, E=150.0, close=160.0)
    assert FIB.on_bar(bar, ws, sb.FakeChoch()) is None
    too_few = [(8, 200.0, 1), (14, 150.0, -1), (24, 300.0, 1)]  # only one low
    ws, bar = scen(sb, pivots=too_few)
    assert FIB.on_bar(bar, ws, sb.FakeChoch()) is None


def test_leg_with_the_trend_no_signal(sb: SimpleNamespace) -> None:
    # still HH/HL (lows 150 -> 250) but the in-progress leg is rising
    ws, bar = scen(sb, pivots=UPTREND + [(30, 250.0, -1)], E=320.0, close=318.0, leg_dir=1)
    assert FIB.on_bar(bar, ws, sb.FakeChoch()) is None
    # same numbers as the firing long case, only the leg direction flipped -> blocked by leg_dir alone
    ws, bar = scen(sb)
    assert FIB.on_bar(bar, replace(ws, leg_dir=1), sb.FakeChoch()) is None


@pytest.mark.parametrize(("E", "close"), [(280.0, 285.0), (140.0, 150.0)])  # r = 0.133 / 1.067
def test_zone_gate(sb: SimpleNamespace, E: float, close: float) -> None:
    ws, bar = scen(sb, E=E, close=close)
    choch = sb.FakeChoch()
    assert FIB.on_bar(bar, ws, choch) is None
    assert choch.calls == []


def test_choch_and_r_gates(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb)
    assert FIB.on_bar(bar, ws, sb.FakeChoch(fire=False)) is None
    # blended R = 0.5*143/44 + 0.5*235.7/44 = 4.303
    assert FibSwingStrategy(StrategyParams(r_gate=4.30)).on_bar(bar, ws, sb.FakeChoch()) is not None
    assert FibSwingStrategy(StrategyParams(r_gate=4.31)).on_bar(bar, ws, sb.FakeChoch()) is None


def test_not_ready_or_no_atr(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb)
    assert FIB.on_bar(bar, replace(ws, ready=False), sb.FakeChoch()) is None
    assert FIB.on_bar(bar, replace(ws, atr4h=None), sb.FakeChoch()) is None


def test_is_a_strategy() -> None:
    assert FIB.name == "C"
    assert isinstance(FIB, Strategy)
