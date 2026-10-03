from __future__ import annotations

import json

import pytest

from ewbot.live.exchange import SessionInfo, Unavailable
from ewbot.live.feed import TickerFeed
from ewbot.live.sim import BIP_SPEC
from ewbot.live.watchdog import AccountGuard, PriceSource, SessionWatch, StopGapWatch, margin_status
from tests.live.helpers import Clock, journal, sim

NY_NOON = 1_791_043_200  # 2026-10-03 12:00 New York (16:00 UTC)


def test_stop_gap_closes_only_after_ten_seconds_beyond_the_limit():
    w = StopGapWatch()
    assert not w.check(1, 60_000.0, 57_500.0, now=0.0)  # inside the 5% limit (57_000)
    assert not w.check(1, 60_000.0, 56_900.0, now=1.0)
    assert not w.check(1, 60_000.0, 56_900.0, now=10.9)
    assert w.check(1, 60_000.0, 56_900.0, now=11.0)
    assert not w.check(1, 60_000.0, 57_100.0, now=12.0) and w.since is None  # back inside: reset
    assert not w.check(-1, 60_000.0, 63_100.0, now=20.0)  # a short's limit is 63_000
    assert not w.check(-1, 60_000.0, 62_900.0, now=25.0) and w.since is None
    assert not w.check(-1, 60_000.0, 63_100.0, now=26.0)
    assert w.check(-1, 60_000.0, 63_100.0, now=36.0)


def test_margin_levels_use_the_overnight_rate():
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "OK"  # 17.7%
    assert margin_status(5, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "WARN"  # 88.3%
    st = margin_status(6, 84_000.0, 1, BIP_SPEC, 1_000.0)  # 106%
    assert st.level == "REDUCE" and st.reduce_by == 3  # back to 3 contracts (<= 70%)
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 100.0).reduce_by == 1  # nothing fits: close it all


def test_account_guard_trips_daily_and_total_once_and_survives_a_restart(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    assert g.update(10_000.0, NY_NOON) is None
    assert g.update(9_701.0, NY_NOON + 60) is None
    assert g.update(9_700.0, NY_NOON + 120) == "DAILY_LOSS"
    assert g.update(9_600.0, NY_NOON + 180) is None  # once per New York day
    g2 = AccountGuard(jr)  # restart
    assert g2.state()["daily_tripped"] and g2.state()["peak"] == 10_000.0
    assert g2.update(9_600.0, NY_NOON + 86_400) is None  # next day: new reference 9_600
    assert g2.update(8_500.0, NY_NOON + 86_460) == "TOTAL_DD"  # 15% below the 10_000 peak
    assert g2.update(8_000.0, NY_NOON + 86_520) is None
    g2.reset(8_000.0, NY_NOON + 86_580)
    assert g2.state()["peak"] == 8_000.0 and not g2.state()["total_tripped"]
    jr.close()


def test_session_watch_reports_changes_and_maintenance_once():
    w = SessionWatch()
    start = NY_NOON + 3_600
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 예정 2026-10-03 13:00 ~ 16:00 (뉴욕)")]
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON + 60) == []
    assert w.update(SessionInfo("CLOSE"), NY_NOON + 120) == [("SESSION", "장 운영 상태 OPEN → CLOSE")]
    assert w.update(SessionInfo("OPEN", NY_NOON + 3 * 86_400), NY_NOON + 180)[0][0] == "SESSION"  # too far ahead


def ticker(product: str, price: float, seq: int) -> str:
    return json.dumps({"channel": "ticker", "sequence_num": seq,
                       "events": [{"type": "update", "tickers": [{"type": "ticker", "product_id": product, "price": str(price)}]}]})


def test_price_source_uses_a_fresh_feed_then_rest_every_ten_seconds():
    clock = Clock(1_000.0)
    ex = sim(clock, fut=60_000.0)
    feed = TickerFeed([ex.futures_id], ws_factory=lambda **kw: None, clock=clock)
    src = PriceSource(ex, feed, clock=clock)
    feed.on_message(ticker(ex.futures_id, 60_100.0, 1))
    assert src.futures_price() == 60_100.0 and src.mode == "ws" and "quote" not in ex.calls
    clock.now += 60.5  # silent for more than 60 s
    assert src.futures_price() == 60_000.0 and src.mode == "rest" and ex.calls.count("quote") == 1
    clock.now += 5.0
    src.futures_price()
    assert ex.calls.count("quote") == 1  # polled at most every 10 s
    clock.now += 5.0
    ex.fail_next("quote", Unavailable("503"))
    assert src.futures_price() == 60_000.0  # the last REST price is kept on an error
    assert PriceSource(ex, None, clock=clock).futures_price() == 60_000.0


def test_feed_parses_ticks_and_flags_sequence_gaps():
    clock = Clock(5.0)
    feed = TickerFeed(["BIP-20DEC30-CDE"], ws_factory=lambda **kw: None, clock=clock)
    feed.on_message(ticker("BIP-20DEC30-CDE", 60_000.0, 7))
    feed.on_message(json.dumps({"channel": "heartbeats", "sequence_num": 8}))
    assert feed.latest("BIP-20DEC30-CDE").price == 60_000.0 and not feed.take_resync()
    feed.on_message(ticker("BIP-20DEC30-CDE", 60_010.0, 11))  # 9 and 10 missing
    assert feed.gaps == 1 and feed.take_resync() and not feed.take_resync()
    feed.on_message("not json")
    clock.now = 9.0
    assert feed.age() == pytest.approx(4.0)


class FakeWS:
    def __init__(self, feed: TickerFeed, plan: list[int], log: list[str], **kw) -> None:
        self.feed, self.log = feed, log
        self.on_message = kw["on_message"]
        self.beats = plan.pop(0) if plan else 0

    def open(self):
        self.log.append("open")

    def subscribe(self, products, channels):
        self.log.append(f"subscribe {','.join(products)} {','.join(channels)}")

    def sleep_with_exception_check(self, s):
        if self.beats <= 0:
            if not self.feed.connects < 3:
                self.feed.stop()
                return
            raise ConnectionError("dropped")
        self.beats -= 1
        self.on_message(ticker("BIP-20DEC30-CDE", 60_000.0, 0))

    def close(self):
        self.log.append("close")


def test_feed_reconnects_with_backoff_and_requests_a_resync():
    log: list[str] = []
    waits: list[float] = []
    plan = [2, 0, 1]
    feed = TickerFeed(["BIP-20DEC30-CDE", "BTC-USD"], ws_factory=None, sleep=waits.append)  # type: ignore[arg-type]
    feed.ws_factory = lambda **kw: FakeWS(feed, plan, log, **kw)
    feed.run()
    assert feed.connects == 3 and waits == [1.0, 1.0]  # reset to 1 s after each successful connect
    assert log.count("subscribe BIP-20DEC30-CDE,BTC-USD ticker,heartbeats") == 3 and log.count("close") == 3
    assert feed.take_resync()
