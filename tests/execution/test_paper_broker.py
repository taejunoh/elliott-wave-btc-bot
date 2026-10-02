from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.execution.paper import Fill, PaperBroker
from ewbot.types import H1, Bar

T0 = 1_704_067_200          # Mon 2024-01-01 00:00 UTC (futures open)
FRI_16ET = 1_704_488_400    # Fri 2024-01-05 21:00 UTC = 16:00 EST (open, but entry blackout)
FRI_17ET = 1_704_492_000    # Fri 2024-01-05 22:00 UTC = 17:00 EST (futures closed)
FRI_18ET = 1_704_495_600    # Fri 2024-01-05 23:00 UTC = 18:00 EST (re-open)

ZERO = CostParams(fee_rate=0.0, slippage=0.0, funding_apr_long=0.0)
HOURLY_FUNDING_5K = 5_000.0 * 0.10 / 8760.0  # 10 contracts @ 50_000


def bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=1.0)


def long_10_at_50k(costs: CostParams = ZERO, ts: int = T0) -> PaperBroker:
    b = PaperBroker(costs, 10_000.0)
    b.queue_entry(+1, 10, stop=49_000.0, tp1=51_000.0, tp2=52_000.0, tp1_contracts=5)
    fills = b.process_bar(bar(ts, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert [f.kind for f in fills] == ["ENTRY"]
    return b


def test_entry_fills_at_next_open_with_slippage_and_fee() -> None:
    b = PaperBroker(CostParams(), 10_000.0)
    b.queue_entry(+1, 10, stop=49_000.0, tp1=51_000.0, tp2=52_000.0, tp1_contracts=5)
    assert b.position is None  # nothing happens until the next bar is processed
    fills = b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert fills == [Fill(ts=T0, kind="ENTRY", side=+1, contracts=10, price=pytest.approx(50_025.0),
                          fee=pytest.approx(2.50125))]
    pos = b.position
    assert pos is not None
    assert (pos.direction, pos.contracts, pos.entry_ts, pos.tp1_contracts) == (1, 10, T0, 5)
    assert pos.entry_price == pytest.approx(50_025.0)
    # cash = 10_000 - entry fee - one hour of long funding (mark = close 50_000)
    assert b.cash == pytest.approx(10_000.0 - 2.50125 - HOURLY_FUNDING_5K)
    # equity marks the open position: (50_000 - 50_025) * 10 * 0.01 = -2.5
    assert b.equity(50_000.0) == pytest.approx(b.cash - 2.5)


def test_short_entry_sells_below_open() -> None:
    b = PaperBroker(CostParams(), 10_000.0)
    b.queue_entry(-1, 4, stop=51_000.0, tp1=49_000.0, tp2=None, tp1_contracts=2)
    fills = b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert fills[0].side == -1
    assert fills[0].price == pytest.approx(49_975.0)


def test_queue_entry_validates_arguments() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    with pytest.raises(ValueError):
        b.queue_entry(0, 10, 49_000.0, 51_000.0, None, 5)
    with pytest.raises(ValueError):
        b.queue_entry(1, 0, 49_000.0, 51_000.0, None, 0)
    with pytest.raises(ValueError):
        b.queue_entry(1, 10, 49_000.0, 51_000.0, None, 11)


def test_gap_through_stop_fills_at_open_not_at_stop() -> None:
    b = long_10_at_50k()
    fills = b.process_bar(bar(T0 + H1, 48_500.0, 48_700.0, 48_200.0, 48_600.0))
    assert [(f.kind, f.contracts, f.price) for f in fills] == [("STOP", 10, 48_500.0)]
    assert b.position is None
    assert b.cash == pytest.approx(10_000.0 - 150.0)  # (48_500 - 50_000) * 0.1


def test_gap_stop_fill_includes_slippage_and_fee() -> None:
    b = long_10_at_50k(CostParams(funding_apr_long=0.0))  # entry 50_025, fee 2.50125
    fills = b.process_bar(bar(T0 + H1, 48_500.0, 48_700.0, 48_200.0, 48_600.0))
    assert fills[0].price == pytest.approx(48_475.75)  # 48_500 * (1 - 0.0005)
    assert fills[0].fee == pytest.approx(2.4237875)    # 0.0005 * 48_475.75 * 0.1
    assert b.cash == pytest.approx(10_000.0 - 2.50125 + (48_475.75 - 50_025.0) * 0.1 - 2.4237875)


def test_intrabar_stop_fills_at_stop_minus_slippage() -> None:
    b = long_10_at_50k(CostParams(fee_rate=0.0, funding_apr_long=0.0))
    fills = b.process_bar(bar(T0 + H1, 49_500.0, 49_600.0, 48_950.0, 49_200.0))
    assert fills[0].kind == "STOP"
    assert fills[0].price == pytest.approx(48_975.5)  # 49_000 * (1 - 0.0005)


def test_stop_and_tp_in_same_bar_stop_wins() -> None:
    b = long_10_at_50k()
    fills = b.process_bar(bar(T0 + H1, 50_000.0, 51_500.0, 48_800.0, 50_000.0))
    assert [(f.kind, f.contracts, f.price) for f in fills] == [("STOP", 10, 49_000.0)]
    assert b.position is None
    assert b.cash == pytest.approx(9_900.0)


def test_tp_requires_strict_cross() -> None:
    b = long_10_at_50k()
    assert b.process_bar(bar(T0 + H1, 50_500.0, 51_000.0, 50_400.0, 50_900.0)) == []  # high == TP1: no fill
    fills = b.process_bar(bar(T0 + 2 * H1, 50_900.0, 51_000.5, 50_800.0, 50_950.0))
    assert [(f.kind, f.contracts, f.price, f.side) for f in fills] == [("TP1", 5, 51_000.0, -1)]


def test_short_tp_strict_cross_uses_low() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    b.queue_entry(-1, 10, stop=51_000.0, tp1=49_000.0, tp2=48_000.0, tp1_contracts=5)
    b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert b.process_bar(bar(T0 + H1, 49_500.0, 49_600.0, 49_000.0, 49_100.0)) == []  # low == TP1
    fills = b.process_bar(bar(T0 + 2 * H1, 49_100.0, 49_200.0, 48_900.0, 49_000.0))
    assert [(f.kind, f.contracts, f.price, f.side) for f in fills] == [("TP1", 5, 49_000.0, +1)]
    assert b.cash == pytest.approx(10_050.0)


def test_partial_tp1_then_tp2() -> None:
    b = long_10_at_50k()
    f1 = b.process_bar(bar(T0 + H1, 50_500.0, 51_200.0, 50_400.0, 51_100.0))
    assert [(f.kind, f.contracts, f.price) for f in f1] == [("TP1", 5, 51_000.0)]
    pos = b.position
    assert pos is not None and pos.contracts == 5 and pos.tp1 is None
    f2 = b.process_bar(bar(T0 + 2 * H1, 51_100.0, 52_100.0, 51_000.0, 52_050.0))
    assert [(f.kind, f.contracts, f.price) for f in f2] == [("TP2", 5, 52_000.0)]
    assert b.position is None
    assert b.cash == pytest.approx(10_000.0 + 5 * 0.01 * 1_000.0 + 5 * 0.01 * 2_000.0)


def test_tp1_and_tp2_can_fill_in_the_same_bar() -> None:
    b = long_10_at_50k()
    fills = b.process_bar(bar(T0 + H1, 50_500.0, 52_500.0, 50_400.0, 52_400.0))
    assert [(f.kind, f.contracts) for f in fills] == [("TP1", 5), ("TP2", 5)]
    assert b.position is None


def test_single_contract_tp1_closes_everything() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    b.queue_entry(+1, 1, stop=49_000.0, tp1=51_000.0, tp2=52_000.0, tp1_contracts=1)
    b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    fills = b.process_bar(bar(T0 + H1, 50_500.0, 52_500.0, 50_400.0, 52_400.0))
    assert [(f.kind, f.contracts) for f in fills] == [("TP1", 1)]
    assert b.position is None


def test_funding_sign_long_pays_short_receives() -> None:
    costs = CostParams(fee_rate=0.0, slippage=0.0)  # funding 10% APR
    long_b = PaperBroker(costs, 10_000.0)
    long_b.queue_entry(+1, 10, 49_000.0, 51_000.0, None, 5)
    long_b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert long_b.cash == pytest.approx(10_000.0 - HOURLY_FUNDING_5K)
    assert long_b.position is not None and long_b.position.funding == pytest.approx(HOURLY_FUNDING_5K)

    short_b = PaperBroker(costs, 10_000.0)
    short_b.queue_entry(-1, 10, 51_000.0, 49_000.0, None, 5)
    short_b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert short_b.cash == pytest.approx(10_000.0 + HOURLY_FUNDING_5K)
    assert short_b.position is not None and short_b.position.funding == pytest.approx(-HOURLY_FUNDING_5K)


def test_no_funding_when_flat() -> None:
    b = PaperBroker(CostParams(), 10_000.0)
    b.process_bar(bar(T0, 50_000.0, 50_100.0, 49_900.0, 50_000.0))
    assert b.cash == 10_000.0


def test_pending_entry_deferred_while_futures_closed() -> None:
    b = PaperBroker(ZERO, 10_000.0)
    b.queue_entry(+1, 10, 49_000.0, 51_000.0, 52_000.0, 5)
    assert b.process_bar(bar(FRI_17ET, 50_000.0, 50_200.0, 49_800.0, 50_100.0)) == []
    assert b.position is None
    fills = b.process_bar(bar(FRI_18ET, 50_300.0, 50_400.0, 50_200.0, 50_350.0))
    assert [(f.kind, f.ts, f.price) for f in fills] == [("ENTRY", FRI_18ET, 50_300.0)]


def test_pending_exit_deferred_while_futures_closed() -> None:
    b = long_10_at_50k(ts=FRI_16ET)
    b.queue_exit()
    assert b.process_bar(bar(FRI_17ET, 50_000.0, 50_200.0, 49_800.0, 50_100.0)) == []
    assert b.position is not None
    fills = b.process_bar(bar(FRI_18ET, 50_400.0, 50_500.0, 50_300.0, 50_450.0))
    assert [(f.kind, f.price) for f in fills] == [("EXIT", 50_400.0)]
    assert b.position is None


def test_stop_passed_while_closed_fills_at_reopen_price() -> None:
    b = long_10_at_50k(ts=FRI_16ET)
    # closed hour trades through the stop (49_000) and recovers: nothing can fill while closed
    assert b.process_bar(bar(FRI_17ET, 50_000.0, 50_000.0, 48_500.0, 49_500.0)) == []
    assert b.position is not None
    # re-open above the stop: the triggered stop fills at the first tradable price (the open)
    fills = b.process_bar(bar(FRI_18ET, 49_600.0, 49_700.0, 49_400.0, 49_650.0))
    assert [(f.kind, f.price) for f in fills] == [("STOP", 49_600.0)]


def test_queue_exit_closes_at_next_open_with_slippage() -> None:
    b = long_10_at_50k(CostParams(fee_rate=0.0, funding_apr_long=0.0))  # entry 50_025
    b.queue_exit()
    fills = b.process_bar(bar(T0 + H1, 50_500.0, 50_600.0, 50_400.0, 50_550.0))
    assert [(f.kind, f.side, f.contracts) for f in fills] == [("EXIT", -1, 10)]
    assert fills[0].price == pytest.approx(50_474.75)
    assert b.cash == pytest.approx(10_000.0 + (50_474.75 - 50_025.0) * 0.1)


def test_set_stop_effective_from_next_bar() -> None:
    b = long_10_at_50k()
    b.set_stop(49_800.0)
    assert b.process_bar(bar(T0 + H1, 50_000.0, 50_100.0, 49_850.0, 50_000.0)) == []
    fills = b.process_bar(bar(T0 + 2 * H1, 50_000.0, 50_100.0, 49_790.0, 49_900.0))
    assert [(f.kind, f.price) for f in fills] == [("STOP", 49_800.0)]


def test_exit_then_entry_in_same_bar_flips() -> None:
    b = long_10_at_50k()
    b.queue_exit()
    b.queue_entry(-1, 8, stop=51_000.0, tp1=49_000.0, tp2=48_000.0, tp1_contracts=4)
    fills = b.process_bar(bar(T0 + H1, 50_200.0, 50_300.0, 50_100.0, 50_250.0))
    assert [(f.kind, f.side, f.contracts, f.price) for f in fills] == [
        ("EXIT", -1, 10, 50_200.0),
        ("ENTRY", -1, 8, 50_200.0),
    ]
    pos = b.position
    assert pos is not None and pos.direction == -1 and pos.contracts == 8
    assert b.cash == pytest.approx(10_020.0)


def test_close_now_closes_whole_position() -> None:
    b = long_10_at_50k()
    fill = b.close_now(T0 + H1, 50_300.0)
    assert fill is not None and (fill.kind, fill.contracts, fill.price) == ("EXIT", 10, 50_300.0)
    assert b.position is None and b.cash == pytest.approx(10_030.0)
    assert b.close_now(T0 + H1, 50_300.0) is None
