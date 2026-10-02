# tests/strategy/test_elliott.py
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy.base import Strategy
from ewbot.strategy.elliott import ElliottStrategy
from ewbot.types import Bar
from ewbot.waves.engine import WaveState

STRAT = ElliottStrategy(StrategyParams())


# ---------------------------------------------------------------- bull scenarios (ATR4h = 8)


def scen_w3(sb: SimpleNamespace, E: float = 140.0, close: float = 150.0, q: float = 0.7, s_rev: float = 0.5) -> tuple[WaveState, Bar]:
    # IMP+ W2: S=100 (i10) -> P1=200 (i20); W2 running down, r2 = (200-E)/100
    s, p1 = sb.piv(10, 100, -1), sb.piv(20, 200, 1)
    top = sb.count("IMP", 1, "W2", [s, p1], q=q)
    ws = sb.ws(top=top, E=E, leg_dir=-1, pivots=[sb.piv(2, 120, 1), s, p1], s_rev=s_rev, i4h=26)
    return ws, sb.bar(ws, close)


def scen_w5(sb: SimpleNamespace, E: float = 258.0, close: float = 262.0) -> tuple[WaveState, Bar]:
    # IMP+ W4: S=100 P1=200 P2=110 P3=400; |W1|=100 |W3|=290 (no H3 cap); r4 = (400-E)/290
    pts = [sb.piv(0, 100, -1), sb.piv(10, 200, 1), sb.piv(16, 110, -1), sb.piv(30, 400, 1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W4", pts), E=E, leg_dir=-1, pivots=pts, i4h=36)
    return ws, sb.bar(ws, close)


def scen_w5_cap(sb: SimpleNamespace, E: float = 310.0, close: float = 315.0) -> tuple[WaveState, Bar]:
    # IMP+ W4 with |W3|=180 < |W1|=200 -> TP2 capped at E + 180
    pts = [sb.piv(0, 100, -1), sb.piv(10, 300, 1), sb.piv(16, 200, -1), sb.piv(24, 380, 1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W4", pts), E=E, leg_dir=-1, pivots=pts, i4h=30)
    return ws, sb.bar(ws, close)


def scen_w5_rev(sb: SimpleNamespace, E: float = 480.0, close: float = 470.0) -> tuple[WaveState, Bar]:
    # IMP+ W5: S=100 P1=200 P2=150 P3=400 P4=320 (i36); W5 running up; ws.i4h=50 -> W5 leg = 14 bars
    pts = [sb.piv(0, 100, -1), sb.piv(10, 200, 1), sb.piv(16, 150, -1), sb.piv(30, 400, 1), sb.piv(36, 320, -1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W5", pts), E=E, leg_dir=1, pivots=pts, i4h=50)
    return ws, sb.bar(ws, close)


def scen_w5_rev_cap(sb: SimpleNamespace, E: float = 508.0, close: float = 500.0) -> tuple[WaveState, Bar]:
    # IMP+ W5 with |W3|=180 < |W1|=200 -> H3 cap = P4 + 180 = 510
    pts = [sb.piv(0, 100, -1), sb.piv(10, 300, 1), sb.piv(16, 200, -1), sb.piv(24, 380, 1), sb.piv(30, 330, -1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W5", pts), E=E, leg_dir=1, pivots=pts, i4h=40)
    return ws, sb.bar(ws, close)


def scen_c_end(
    sb: SimpleNamespace,
    E: float = 220.0,
    close: float = 230.0,
    prior: list[tuple[int, float, int]] | None = None,
    b_end: float = 280.0,
) -> tuple[WaveState, Bar]:
    # ABC d=-1 (A down) phase C: X=300 (i20) A_end=240 (i26) B_end=280 (i32); C running down.
    # Prior swing low = lowest low among the 4 pivots before X = 100 -> (300-E)/200 <= 0.786
    prior = [(0, 100.0, -1), (6, 180.0, 1), (10, 150.0, -1)] if prior is None else prior
    x, a, b = sb.piv(20, 300, 1), sb.piv(26, 240, -1), sb.piv(32, b_end, 1)
    pivots = [sb.piv(i, p, k) for i, p, k in prior] + [x, a, b]
    ws = sb.ws(top=sb.count("ABC", -1, "C", [x, a, b]), E=E, leg_dir=-1, pivots=pivots, i4h=38)
    return ws, sb.bar(ws, close)


SCENARIOS: dict[str, Any] = {
    "W3": scen_w3,
    "W5": scen_w5,
    "W5_CAP": scen_w5_cap,
    "W5_REV": scen_w5_rev,
    "W5_REV_CAP": scen_w5_rev_cap,
    "C_END": scen_c_end,
}

EXPECTED: dict[str, dict[str, Any]] = {
    # stop = S - 0.25*8; TP1 = E + |W1|; TP2 = E + 1.618|W1|; time stop 1.5*W1(10 bars) = 15
    "W3": dict(setup="W3", direction=1, entry_ref=150.0, stop=98.0, tp1=240.0, tp2=301.8,
               time_stop_4h=15, risk_mult=1.0, instance=("IMP", 1, 10, "W2"), lineage=("IMP", 1, 10)),
    # stop = P1 - 2; TP1 = P3; TP2 = max(258+100, 258+0.618*300=443.4)
    "W5": dict(setup="W5", direction=1, entry_ref=262.0, stop=198.0, tp1=400.0, tp2=443.4,
               time_stop_4h=15, risk_mult=1.0, instance=("IMP", 1, 0, "W4"), lineage=("IMP", 1, 0)),
    # TP2 = min(cap 310+180=490, max(310+200, 310+0.618*280)=510) = 490
    "W5_CAP": dict(setup="W5", direction=1, entry_ref=315.0, stop=298.0, tp1=380.0, tp2=490.0,
                   time_stop_4h=15, risk_mult=1.0, instance=("IMP", 1, 0, "W4"), lineage=("IMP", 1, 0)),
    # short: stop = E + 0.5*8 = 484; TP1 = 480 - 0.382*380 = 334.84; TP2 = P4; time stop 1.5*14 = 21
    "W5_REV": dict(setup="W5_REV", direction=-1, entry_ref=470.0, stop=484.0, tp1=334.84, tp2=320.0,
                   time_stop_4h=21, risk_mult=0.5, instance=("IMP", 1, 0, "W5"), lineage=("IMP", 1, 0)),
    # stop = min(cap 510, 508 + 4) = 510; TP1 = 508 - 0.382*408 = 352.144; time stop 1.5*10 = 15
    "W5_REV_CAP": dict(setup="W5_REV", direction=-1, entry_ref=500.0, stop=510.0, tp1=352.144, tp2=330.0,
                       time_stop_4h=15, risk_mult=0.5, instance=("IMP", 1, 0, "W5"), lineage=("IMP", 1, 0)),
    # long: stop = E - 4 = 216; TP1 = 220 + 0.618*80 = 269.44; TP2 = X; time stop 1.5*A(6) = 9 -> 12
    "C_END": dict(setup="C_END", direction=1, entry_ref=230.0, stop=216.0, tp1=269.44, tp2=300.0,
                  time_stop_4h=12, risk_mult=1.0, instance=("ABC", -1, 20, "C"), lineage=("ABC", -1, 20)),
}


def assert_signal(sig: Any, exp: dict[str, Any]) -> None:
    assert sig is not None
    for key, val in exp.items():
        got = getattr(sig, key)
        if isinstance(val, float):
            assert got == pytest.approx(val), key
        else:
            assert got == val, key


# ---------------------------------------------------------------- every setup fires


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_setup_fires_with_expected_levels(sb: SimpleNamespace, name: str) -> None:
    ws, bar = SCENARIOS[name](sb)
    choch = sb.FakeChoch()
    sig = STRAT.on_bar(bar, ws, choch)
    assert_signal(sig, EXPECTED[name])
    assert sig.ts == bar.ts
    assert sig.reason.startswith(EXPECTED[name]["setup"] + " ")
    assert choch.calls == [(EXPECTED[name]["direction"], ws.leg_start.sub_ts, ws.E_live_ts)]


def test_w5_rev_orders_targets_near_to_far(sb: SimpleNamespace) -> None:
    # E=450: spec TP1 = 450 - 0.382*350 = 316.3 lies BEYOND TP2 = P4 = 320 -> swapped
    ws, bar = scen_w5_rev(sb, E=450.0, close=440.0)
    sig = STRAT.on_bar(bar, ws, sb.FakeChoch())
    assert sig is not None
    assert sig.stop == pytest.approx(454.0)
    assert (sig.tp1, sig.tp2) == (pytest.approx(320.0), pytest.approx(316.3))


# ---------------------------------------------------------------- bear = mirror image


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_bear_mirror_of_each_setup(sb: SimpleNamespace, name: str) -> None:
    m = 1000.0
    ws, bar = SCENARIOS[name](sb)
    bull = STRAT.on_bar(bar, ws, sb.FakeChoch())
    choch = sb.FakeChoch()
    bear = STRAT.on_bar(sb.mirror_bar(bar, m), sb.mirror_ws(ws, m), choch)
    assert bull is not None and bear is not None
    assert bear.setup == bull.setup
    assert bear.direction == -bull.direction
    assert choch.calls[0][0] == -bull.direction
    for key in ("entry_ref", "stop", "tp1", "tp2"):
        assert getattr(bear, key) == pytest.approx(2 * m - getattr(bull, key)), key
    assert bear.time_stop_4h == bull.time_stop_4h
    assert bear.risk_mult == bull.risk_mult
    pattern, d, i0, phase = bull.instance
    assert bear.instance == (pattern, -d, i0, phase)
    assert bear.lineage == (pattern, -d, i0)


def test_bear_w3_explicit_numbers(sb: SimpleNamespace) -> None:
    # IMP- W2: S=400 -> P1=300, W2 bouncing to E=360 (r2=0.6); short on close 350
    s, p1 = sb.piv(10, 400, 1), sb.piv(20, 300, -1)
    ws = sb.ws(top=sb.count("IMP", -1, "W2", [s, p1]), E=360.0, leg_dir=1, pivots=[s, p1], i4h=26)
    sig = STRAT.on_bar(sb.bar(ws, 350.0), ws, sb.FakeChoch())
    assert_signal(sig, dict(setup="W3", direction=-1, stop=402.0, tp1=260.0, tp2=198.2, time_stop_4h=15, risk_mult=1.0))


# ---------------------------------------------------------------- gates (W3 scenario unless noted)


def test_q_gate(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb, q=0.59)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w3(sb, q=0.6)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is not None


def test_s_rev_gate(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb, s_rev=0.2)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w3(sb, s_rev=0.25)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is not None


@pytest.mark.parametrize(("E", "close"), [(170.0, 175.0), (115.0, 125.0)])  # r2 = 0.30 / 0.85
def test_w3_zone_gate_and_choch_not_consulted(sb: SimpleNamespace, E: float, close: float) -> None:
    ws, bar = scen_w3(sb, E=E, close=close)
    choch = sb.FakeChoch()
    assert STRAT.on_bar(bar, ws, choch) is None
    assert choch.calls == []


def test_disabled_setup_is_ignored(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)
    strat = ElliottStrategy(StrategyParams(setups_enabled=("W5", "W5_REV", "C_END")))
    assert strat.on_bar(bar, ws, sb.FakeChoch()) is None


def test_r_gate(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)  # blended R = 0.5*90/52 + 0.5*151.8/52 = 2.325
    assert ElliottStrategy(StrategyParams(r_gate=2.3)).on_bar(bar, ws, sb.FakeChoch()) is not None
    assert ElliottStrategy(StrategyParams(r_gate=2.4)).on_bar(bar, ws, sb.FakeChoch()) is None


def test_no_choch_no_signal(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch(fire=False)) is None


def test_not_ready_missing_top_atr_or_inconsistent_leg(sb: SimpleNamespace) -> None:
    ws, bar = scen_w3(sb)
    choch = sb.FakeChoch()
    assert STRAT.on_bar(bar, replace(ws, ready=False), choch) is None
    assert STRAT.on_bar(bar, replace(ws, top=None, counts=(), probs=()), choch) is None
    assert STRAT.on_bar(bar, replace(ws, atr4h=None), choch) is None
    assert STRAT.on_bar(bar, replace(ws, leg_dir=1), choch) is None
    assert STRAT.on_bar(bar, replace(ws, leg_start=None), choch) is None


def test_phases_without_setup_never_fire(sb: SimpleNamespace) -> None:
    pts = [sb.piv(0, 100, -1), sb.piv(10, 200, 1), sb.piv(16, 150, -1)]
    ws = sb.ws(top=sb.count("IMP", 1, "W3", pts), E=300.0, leg_dir=1, pivots=pts)
    assert STRAT.on_bar(sb.bar(ws, 290.0), ws, sb.FakeChoch()) is None
    ab = [sb.piv(0, 300, 1), sb.piv(10, 240, -1)]
    ws = sb.ws(top=sb.count("ABC", -1, "B", ab), E=270.0, leg_dir=1, pivots=ab)
    assert STRAT.on_bar(sb.bar(ws, 265.0), ws, sb.FakeChoch()) is None


def test_w5_zone_gates(sb: SimpleNamespace) -> None:
    ws, bar = scen_w5(sb, E=380.0, close=385.0)  # r4 = 20/290 = 0.069 < 0.236
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w5_cap(sb, E=295.0, close=300.0)  # r4 = 0.472 in zone but E < P1 = 300
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_w5_rev_zone_gates(sb: SimpleNamespace) -> None:
    ws, bar = scen_w5_rev_cap(sb, E=515.0, close=505.0)  # beyond H3 cap 510
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w5_rev_cap(sb, E=440.0, close=430.0)  # W5 = 110 < 0.618*200
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    ws, bar = scen_w5_rev(sb, E=395.0, close=385.0)  # W5 = 75 ok but E not beyond P3 = 400
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_c_end_zone_gates(sb: SimpleNamespace) -> None:
    for E, close in ((235.0, 238.0), (170.0, 180.0)):  # C/A = 0.75 and 1.833
        ws, bar = scen_c_end(sb, E=E, close=close)
        assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    # prior swing low 230 -> (300-220)/(300-230) = 1.14 > 0.786
    ws, bar = scen_c_end(sb, prior=[(0, 230.0, -1), (6, 270.0, 1), (10, 250.0, -1)])
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    # no confirmed pivot before X -> no prior swing -> no trade
    ws, bar = scen_c_end(sb, prior=[])
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None
    # B_end=290: E=241 gives C/A = 49/60 = 0.817 in zone, but E has not passed A_end = 240
    ws, bar = scen_c_end(sb, E=241.0, close=245.0, b_end=290.0)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_c_end_prior_swing_lookback_is_four_pivots(sb: SimpleNamespace) -> None:
    # The low of 10 at i0 would allow the trade but lies 5 pivots before X -> ignored -> swing 230
    prior = [(0, 10.0, -1), (2, 260.0, 1), (6, 230.0, -1), (8, 270.0, 1), (10, 250.0, -1)]
    ws, bar = scen_c_end(sb, prior=prior)
    assert STRAT.on_bar(bar, ws, sb.FakeChoch()) is None


def test_is_a_strategy() -> None:
    assert STRAT.name == "A"
    assert isinstance(STRAT, Strategy)
