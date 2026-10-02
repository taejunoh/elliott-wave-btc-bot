# tests/strategy/test_hybrid.py
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy import ElliottStrategy, FibSwingStrategy, HybridStrategy, Strategy, make_strategy
from ewbot.strategy.hybrid import CONFIDENT, CONTRARY, UNCERTAIN
from ewbot.types import Bar
from ewbot.waves.engine import WaveState

HYB = HybridStrategy(StrategyParams())


def scen(sb: SimpleNamespace, q: float = 0.7, s_rev: float = 0.5, with_top: bool = True) -> tuple[WaveState, Bar]:
    """Both A and C would fire here.

    4h pivots 100(L) 200(H) 150(L) 300(H): up-trend for C (r = 0.767).
    Top count IMP+ W2 S=150 -> P1=300 with E=185: r2 = 0.767 for A.
    """
    pivots = [sb.piv(0, 100, -1), sb.piv(8, 200, 1), sb.piv(14, 150, -1), sb.piv(24, 300, 1)]
    top = sb.count("IMP", 1, "W2", pivots[2:], q=q) if with_top else None
    ws = sb.ws(top=top, E=185.0, leg_dir=-1, pivots=pivots, s_rev=s_rev, i4h=30)
    return ws, sb.bar(ws, 192.0)


@pytest.mark.parametrize(
    ("q", "s_rev", "with_top", "state"),
    [
        (0.7, 0.5, True, CONFIDENT),
        (0.7, 0.25, True, CONFIDENT),  # boundaries inclusive
        (0.5, 0.5, True, UNCERTAIN),  # q too low
        (0.7, 0.1, True, UNCERTAIN),  # S_rev between -S_MIN and S_MIN
        (0.7, -0.25, True, CONTRARY),
        (0.5, -0.6, True, CONTRARY),
        (0.0, 0.0, False, UNCERTAIN),  # no alive count -> no Elliott opinion
    ],
)
def test_classify(sb: SimpleNamespace, q: float, s_rev: float, with_top: bool, state: str) -> None:
    ws, _ = scen(sb, q=q, s_rev=s_rev, with_top=with_top)
    assert HYB.classify(ws) == state


def test_classify_with_zero_s_min_tie_goes_to_confident_then_contrary(sb: SimpleNamespace) -> None:
    hyb = HybridStrategy(StrategyParams(S_MIN=0.0))
    ws, _ = scen(sb, q=0.7, s_rev=0.0)
    assert hyb.classify(ws) == CONFIDENT
    ws, _ = scen(sb, q=0.5, s_rev=0.0)
    assert hyb.classify(ws) == CONTRARY
    ws, _ = scen(sb, s_rev=0.0, with_top=False)
    assert hyb.classify(ws) == UNCERTAIN


def test_confident_uses_elliott_only(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb)
    sig = HYB.on_bar(bar, ws, sb.FakeChoch())
    assert sig is not None
    assert sig.setup == "W3"
    assert sig.stop == pytest.approx(148.0)  # 150 - 0.25*8
    assert sig.tp1 == pytest.approx(335.0)  # 185 + 150
    assert sig.tp2 == pytest.approx(427.7)  # 185 + 1.618*150
    assert sig.time_stop_4h == 15
    assert sig.instance == ("IMP", 1, 14, "W2")
    assert sig.reason.startswith("CONFIDENT: W3 ")
    # A has no signal (W3 disabled) -> C is NOT consulted even though it would fire
    hyb = HybridStrategy(StrategyParams(setups_enabled=("W5", "W5_REV", "C_END")))
    assert hyb.on_bar(bar, ws, sb.FakeChoch()) is None


def test_uncertain_uses_fib_only(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb, q=0.5, s_rev=0.5)
    sig = HYB.on_bar(bar, ws, sb.FakeChoch())
    assert sig is not None
    assert sig.setup == "FIB"
    assert (sig.stop, sig.tp1, sig.tp2) == (pytest.approx(148.0), pytest.approx(335.0), pytest.approx(427.7))  # same geometry as W3
    assert sig.instance is None and sig.lineage is None
    assert sig.reason.startswith("UNCERTAIN: FIB ")


def test_contrary_blocks_everything(sb: SimpleNamespace) -> None:
    ws, bar = scen(sb, q=0.7, s_rev=-0.5)
    choch = sb.FakeChoch()
    assert HYB.on_bar(bar, ws, choch) is None
    assert choch.calls == []


def test_make_strategy_by_mode() -> None:
    assert isinstance(make_strategy(StrategyParams(mode="A")), ElliottStrategy)
    assert isinstance(make_strategy(StrategyParams(mode="C")), FibSwingStrategy)
    assert isinstance(make_strategy(StrategyParams(mode="HYBRID")), HybridStrategy)
    for mode in ("A", "C", "HYBRID"):
        strat = make_strategy(StrategyParams(mode=mode))
        assert isinstance(strat, Strategy)
        assert strat.name == mode
    with pytest.raises(ValueError, match="unknown strategy mode"):
        make_strategy(StrategyParams(mode="B"))
