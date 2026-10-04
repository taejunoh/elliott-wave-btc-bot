from __future__ import annotations

import pytest

from ewbot.live.exchange import (
    STOP_LIMIT_OFFSET,
    ExchangeError,
    PlaceResult,
    Position,
    RateLimited,
    Unavailable,
)
from ewbot.live.sim import BIP_SPEC, SimCrash, SimExchange


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ex():
    sim = SimExchange(equity=5_000.0, clock=Clock())
    sim.set_prices(60_000.0, 59_990.0)
    return sim


def test_product_spec_is_the_bip_contract():
    assert (BIP_SPEC.contract_size, BIP_SPEC.price_increment, BIP_SPEC.base_increment) == (0.01, 5.0, 1.0)
    assert BIP_SPEC.margin_rate(1) == 0.21025 and BIP_SPEC.margin_rate(-1) == 0.256625
    assert BIP_SPEC.margin_rate(1, overnight=False) == 0.100079


def test_market_buy_fills_at_the_ask_and_marks_equity(ex):
    r = ex.place_market("e1", 1, 2)
    assert r.ok and ex.get_order(r.order_id).status == "FILLED" and ex.get_order(r.order_id).fee == 0.0
    assert ex.position() == Position(1, 2, 60_002.5)
    ex.set_prices(60_102.5)
    assert ex.balance().equity == pytest.approx(5_000.0 + 100.0 * 2 * 0.01)
    assert ex.quote("BTC-USD").mid == pytest.approx(59_990.0)


def test_brackets_only_reduce_and_take_profit_at_the_limit(ex):
    ex.place_market("e1", 1, 3)
    ex.place_bracket("b1", -1, 1, 60_500.0, 59_000.0)
    ex.place_bracket("b2", -1, 2, 61_000.0, 59_000.0)
    fills = ex.set_prices(60_600.0)
    assert [(f["client_order_id"], f["size"], f["price"]) for f in fills] == [("b1", 1, 60_500.0)]
    assert ex.position().contracts == 2
    ex.set_prices(61_200.0)
    assert ex.position() is None
    assert [o.client_order_id for o in ex.open_orders()] == []
    ex.place_market("e2", -1, 1)  # a later bracket for a long cannot increase the new short
    r = ex.place_bracket("b3", -1, 1, 61_000.0, 59_000.0)
    assert not r.ok and r.error == "NO_POSITION_TO_REDUCE"


def test_a_triggered_stop_rests_five_percent_beyond_the_stop():
    """Coinbase create-order reference, trigger_bracket_gtc.stop_trigger_price: a stop limit order is placed
    "with a limit price 5% higher for BUYS and 5% lower for SELLS"."""
    assert STOP_LIMIT_OFFSET == 0.05


def test_stop_fills_inside_the_five_percent_limit(ex):
    ex.place_market("e1", 1, 2)
    ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    ex.place_bracket("b2", -1, 1, 62_000.0, 59_500.0)
    ex.bracket_fill_cap = 1
    fills = ex.set_prices(59_400.0)
    assert [(f["size"], f["price"]) for f in fills] == [(1, 59_400.0), (1, 59_400.0)] and ex.position() is None
    ex.set_prices(60_000.0)
    ex.place_market("e2", 1, 1)
    ex.place_bracket("b3", -1, 1, 62_000.0, 59_500.0)
    fills = ex.set_prices(56_600.0)  # 4.9% through the stop: still inside its 56_525 limit
    assert [f["price"] for f in fills] == [56_600.0] and ex.position() is None


def test_stop_gap_beyond_the_limit_rests_until_the_price_comes_back(ex):
    ex.place_market("e1", 1, 1)
    ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    assert ex.set_prices(56_000.0) == []  # limit 56_525: gapped through, nothing fills
    assert ex.position() is not None and ex.open_orders()[0].client_order_id == "b1"
    fills = ex.set_prices(56_550.0)
    assert [f["price"] for f in fills] == [56_550.0] and ex.position() is None


def test_short_bracket_mirrors_the_long(ex):
    ex.place_market("s1", -1, 2)
    ex.place_bracket("b1", 1, 2, 59_000.0, 60_500.0)
    fills = ex.set_prices(60_600.0)
    assert [(f["side"], f["size"], f["price"]) for f in fills] == [(1, 2, 60_600.0)] and ex.position() is None
    ex.set_prices(60_000.0)
    ex.place_market("s2", -1, 1)
    ex.place_bracket("b2", 1, 1, 59_000.0, 60_500.0)  # buy limit 5% above the stop: 63_525
    assert ex.set_prices(64_000.0) == [] and ex.position() is not None
    assert [f["price"] for f in ex.set_prices(63_500.0)] == [63_500.0] and ex.position() is None


def test_partial_fills(ex):
    ex.market_fill_ratio = 0.5
    r = ex.place_market("e1", 1, 3)
    o = ex.get_order(r.order_id)
    assert (o.filled, o.status) == (1, "CANCELLED") and ex.position().contracts == 1
    ex.market_fill_ratio = 1.0
    ex.place_market("e2", 1, 2)
    ex.bracket_fill_cap = 1
    ex.place_bracket("b1", -1, 3, 60_100.0, 59_000.0)
    ex.set_prices(60_200.0)
    assert ex.position().contracts == 2 and ex.open_orders()[0].filled == 1


def test_nothing_fills_while_the_session_is_closed(ex):
    ex.place_market("e1", 1, 1)
    ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    ex.set_session("CLOSE")
    assert ex.set_prices(56_000.0) == []
    assert not ex.place_market("e2", 1, 1).ok and not ex.close_position("x1").ok
    assert ex.session().state == "CLOSE" and not ex.session().is_open
    fills = ex.set_session("OPEN")  # reopens far below the stop limit: the stop rests
    assert fills == [] and ex.position() is not None


def test_injected_faults_raise_once(ex):
    ex.fail_next("place_market", RateLimited("429", retry_after=2.0))
    ex.fail_next("position", Unavailable("503"), times=2)
    with pytest.raises(RateLimited) as err:
        ex.place_market("e1", 1, 1)
    assert err.value.retry_after == 2.0 and ex.orders == {}
    assert ex.place_market("e1", 1, 1).ok
    for _ in range(2):
        with pytest.raises(Unavailable):
            ex.position()
    assert ex.position().contracts == 1
    ex.fail_next("quote", Unavailable("503"), times=50)
    ex.fail_next("balance", Unavailable("503"), times=2)
    ex.clear_faults("quote")  # the venue answers again: one method's queued faults dropped, the others kept
    assert ex.quote(ex.futures_id).mid == 60_000.0
    with pytest.raises(Unavailable):
        ex.balance()
    ex.clear_faults()  # every method's
    assert ex.balance().equity > 0


def test_crash_after_accept_leaves_the_order_on_the_exchange(ex):
    ex.crash_after_accept("place_market")
    with pytest.raises(SimCrash):
        ex.place_market("e1", 1, 1)
    found = ex.find_order("e1", since_ts=0.0)
    assert found is not None and found.status == "FILLED" and ex.position().contracts == 1
    assert ex.find_order("e1", since_ts=10_000.0) is None
    ex.crash_after_accept("place_bracket", Unavailable("timeout"))  # the reply is lost, the order exists
    with pytest.raises(Unavailable):
        ex.place_bracket("b1", -1, 1, 62_000.0, 59_000.0)
    assert [o.client_order_id for o in ex.open_orders()] == ["b1"]


def test_duplicate_client_order_id_modes():
    existing = SimExchange(clock=Clock())
    existing.set_prices(60_000.0)
    first = existing.place_market("e1", 1, 1)
    again = existing.place_market("e1", 1, 1)
    assert again.ok and again.order_id == first.order_id and existing.position().contracts == 1
    strict = SimExchange(clock=Clock(), duplicate="error")
    strict.set_prices(60_000.0)
    strict.place_market("e1", 1, 1)
    dup = strict.place_market("e1", 1, 1)
    assert not dup.ok and dup.duplicate and strict.position().contracts == 1


def test_overlapping_brackets_can_be_rejected(ex):
    ex.reject_overlap = True
    ex.place_market("e1", 1, 2)
    assert ex.place_bracket("b1", -1, 2, 61_000.0, 59_000.0).ok
    r = ex.place_bracket("b2", -1, 2, 61_000.0, 59_500.0)
    assert not r.ok and r.error == "INSUFFICIENT_POSITION_SIZE"


def test_fees_are_charged_per_contract():
    sim = SimExchange(clock=Clock(), fee_per_contract=0.15, equity=1_000.0)
    sim.set_prices(60_000.0)
    r = sim.place_market("e1", 1, 2)
    assert sim.get_order(r.order_id).fee == pytest.approx(0.30) and sim.cash == pytest.approx(999.70)


def test_edit_and_close(ex):
    ex.place_market("e1", 1, 2)
    b = ex.place_bracket("b1", -1, 2, 61_000.0, 59_000.0)
    assert not ex.edit_bracket(b.order_id, 2, 61_000.0, 59_500.0)  # unsupported by default
    ex.edit_supported = True
    assert ex.edit_bracket(b.order_id, 2, 61_000.0, 59_500.0)
    assert ex.get_order(b.order_id).stop_price == 59_500.0
    assert ex.close_position("x1", size=1).ok and ex.position().contracts == 1
    assert ex.close_position("x2").ok and ex.position() is None
    assert ex.close_position("x3").error == "NO_OPEN_POSITION"
    assert ex.open_orders() == []  # the orphaned bracket is cancelled by the close itself


def test_prices_off_the_tick_grid_are_rejected(ex):
    ex.place_market("e1", 1, 1)
    r = ex.place_bracket("b1", -1, 1, 60_502.5, 59_000.0)  # $5 tick
    assert not r.ok and r.error == "INVALID_PRICE_PRECISION"
    r = ex.place_bracket("b2", -1, 1, 60_500.0, 59_001.0)
    assert not r.ok and r.error == "INVALID_STOP_PRICE_PRECISION"
    assert not ex.place_bracket("b3", -1, 1, float("nan"), 59_000.0).ok
    assert ex.open_orders() == [] and not ex.preview_bracket(-1, 1, 60_500.0, 59_001.0).ok
    b = ex.place_bracket("b4", -1, 1, 60_500.0, 59_000.0)
    ex.edit_supported = True
    assert not ex.edit_bracket(b.order_id, 1, 60_500.0, 59_002.5)
    assert ex.get_order(b.order_id).stop_price == 59_000.0


def test_sizes_must_be_whole_contracts(ex):
    for size in (1.5, 0, -1):
        r = ex.place_market(f"e{size}", 1, size)
        assert not r.ok and r.error == "INVALID_SIZE_PRECISION"
    assert ex.orders == {} and ex.position() is None and not ex.preview_market(1, 1.5).ok
    assert ex.place_market("e2", 1, 2.0).ok and ex.position().contracts == 2  # a whole number is fine
    assert ex.place_bracket("b1", -1, 0.5, 61_000.0, 59_000.0).error == "INVALID_SIZE_PRECISION"
    assert ex.close_position("x1", size=1.5).error == "INVALID_SIZE_PRECISION"
    assert ex.position().contracts == 2 and ex.open_orders() == []


def test_bracket_take_profit_and_stop_must_be_on_their_sides(ex):
    ex.place_market("e1", 1, 1)  # long: the sell bracket's take-profit must be above its stop
    for tp, stop in ((59_000.0, 61_000.0), (60_000.0, 60_000.0)):
        r = ex.place_bracket(f"b{tp}", -1, 1, tp, stop)
        assert not r.ok and r.error == "INVALID_BRACKET_PRICES"
    b = ex.place_bracket("b1", -1, 1, 61_000.0, 59_000.0)
    ex.edit_supported = True
    assert not ex.edit_bracket(b.order_id, 1, 59_000.0, 61_000.0)
    ex.close_position("x1")
    ex.place_market("s1", -1, 1)  # short: the buy bracket's take-profit must be below its stop
    assert ex.place_bracket("b2", 1, 1, 61_000.0, 59_000.0).error == "INVALID_BRACKET_PRICES"
    assert ex.place_bracket("b3", 1, 1, 59_000.0, 61_000.0).ok


def test_close_position_honours_the_market_fill_ratio(ex):
    ex.place_market("e1", 1, 3)
    ex.market_fill_ratio = 0.5
    r = ex.close_position("x1")
    o = ex.get_order(r.order_id)
    assert r.ok and (o.size, o.filled, o.status) == (3, 1, "CANCELLED") and ex.position().contracts == 2
    r = ex.close_position("x2", size=1)  # floor(0.5): nothing fills, the IOC rest is cancelled
    assert r.ok and ex.get_order(r.order_id).status == "CANCELLED" and ex.position().contracts == 2


def test_a_market_order_cancels_orphaned_brackets_at_once(ex):
    ex.place_market("e1", 1, 1)
    b = ex.place_bracket("b1", -1, 1, 62_000.0, 59_500.0)
    assert ex.place_market("x1", -1, 1).ok and ex.position() is None
    assert ex.open_orders() == [] and ex.get_order(b.order_id).status == "CANCELLED"


def test_fault_injection_only_accepts_methods_that_honour_it(ex):
    for bad in ("place_order", "set_prices"):
        with pytest.raises(ValueError):
            ex.fail_next(bad, RateLimited("429"))
    for bad in ("cancel", "edit_bracket", "quote", "place_order"):
        with pytest.raises(ValueError):
            ex.crash_after_accept(bad)
    ex.fail_next("cancel", Unavailable("503"))
    ex.crash_after_accept("close_position")
    with pytest.raises(Unavailable):
        ex.cancel([])


def test_cancel_reports_orders_it_could_not_cancel(ex):
    """Like the adapter honouring batch_cancel's results[].success: an unknown or no longer cancellable
    order raises after the others were cancelled; an order that already filled no longer rests (the venue's
    ORDER_IS_FULLY_FILLED) and is not an error."""
    ex.place_market("e1", 1, 2)
    b1 = ex.place_bracket("b1", -1, 1, 62_000.0, 59_000.0)
    b2 = ex.place_bracket("b2", -1, 1, 61_000.0, 59_000.0)
    ex.cancel([b1.order_id])
    with pytest.raises(ExchangeError) as err:
        ex.cancel(["nope", b1.order_id, b2.order_id])
    assert not err.value.retryable and "nope" in str(err.value) and b1.order_id in str(err.value)
    assert b2.order_id not in str(err.value) and ex.get_order(b2.order_id).status == "CANCELLED"
    b3 = ex.place_bracket("b3", -1, 2, 60_500.0, 59_000.0)
    ex.set_prices(60_600.0)
    assert ex.get_order(b3.order_id).status == "FILLED"
    ex.cancel([b3.order_id])


def test_a_duplicate_result_is_flagged_explicitly():
    strict = SimExchange(clock=Clock(), duplicate="error")
    strict.set_prices(60_000.0)
    strict.place_market("e1", 1, 1)
    dup = strict.place_market("e1", 1, 1)
    assert dup == PlaceResult("e1", None, False, "DUPLICATE_CLIENT_ORDER_ID", duplicate=True)
    assert not PlaceResult("x", None, False, "INVALID_REQUEST: duplicate field").duplicate
