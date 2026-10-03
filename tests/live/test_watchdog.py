from __future__ import annotations

import inspect
import json
import math
import ssl
import time
from importlib import metadata

import pytest

from ewbot.live.exchange import Quote, SessionInfo, Unavailable
from ewbot.live.feed import HEALTHY_S, PUBLIC_WS_URL, SILENT_S, Tick, TickerFeed, public_ws_factory
from ewbot.live.sim import BIP_SPEC
from ewbot.live.watchdog import AccountGuard, BadEquity, PriceSource, SessionWatch, StopGapWatch, margin_status
from tests.live.helpers import Clock, journal, sim

NY_NOON = 1_791_043_200  # 2026-10-03 12:00 New York (16:00 UTC)
FUT = "BIP-20DEC30-CDE"


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


def test_stop_gap_fires_at_most_one_grace_period_late_when_the_clock_steps_back():
    w = StopGapWatch()
    assert not w.check(1, 60_000.0, 56_900.0, now=10_000.0)
    assert not w.check(1, 60_000.0, 56_900.0, now=6_405.0)  # the wall clock stepped back an hour: re-armed
    assert not w.check(1, 60_000.0, 56_900.0, now=6_414.0)
    assert w.check(1, 60_000.0, 56_900.0, now=6_415.0)  # 10 s after the step, not an hour


def test_margin_levels_use_the_overnight_rate():
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "OK"  # 17.7%
    assert margin_status(5, 84_000.0, 1, BIP_SPEC, 1_000.0).level == "WARN"  # 88.3%
    st = margin_status(6, 84_000.0, 1, BIP_SPEC, 1_000.0)  # 106%
    assert st.level == "REDUCE" and st.reduce_by == 3  # back to 3 contracts (<= 70%)
    assert margin_status(1, 84_000.0, 1, BIP_SPEC, 100.0).reduce_by == 1  # nothing fits: close it all


def test_account_guard_trips_on_two_breaching_reads_once_a_day_and_survives_a_restart(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    assert g.update(10_000.0, NY_NOON) is None
    assert g.update(9_701.0, NY_NOON + 60) is None
    assert g.update(9_700.0, NY_NOON + 120) is None  # -3%: one breaching read is not enough
    assert g.update(9_700.0, NY_NOON + 180) == "DAILY_LOSS"  # the second in a row trips (one poll later)
    assert g.update(9_600.0, NY_NOON + 240) is None  # once per New York day
    g2 = AccountGuard(jr)  # restart
    assert g2.state()["daily_tripped"] and g2.state()["peak"] == 10_000.0
    assert g2.update(9_600.0, NY_NOON + 86_400) is None  # next day: new reference 9_600
    assert g2.update(8_500.0, NY_NOON + 86_460) is None
    assert g2.update(8_500.0, NY_NOON + 86_520) == "TOTAL_DD"  # 15% below the 10_000 peak, twice
    assert g2.update(8_000.0, NY_NOON + 86_580) is None
    jr.close()


def test_one_bad_balance_read_never_trips_a_breaker_nor_raises_the_peak(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    assert g.update(10_000.0, NY_NOON) is None
    assert g.update(9_000.0, NY_NOON + 60) is None  # one low read (-10%) between normal ones
    assert g.update(10_000.0, NY_NOON + 120) is None
    assert g.update(20_000.0, NY_NOON + 180) is None  # one high read
    assert [g.update(10_000.0, NY_NOON + 240 + 60 * k) for k in range(3)] == [None, None, None]
    assert g.state()["peak"] == 10_000.0  # the peak only rises to what two reads in a row reached
    assert g.update(10_400.0, NY_NOON + 480) is None and g.state()["peak"] == 10_000.0
    assert g.update(10_500.0, NY_NOON + 540) is None and g.state()["peak"] == 10_400.0
    for bad in (math.nan, math.inf, 0.0, -5.0):
        with pytest.raises(BadEquity):
            g.update(bad, NY_NOON + 600)  # a read failure, not an observation
    assert g.state()["equity"] == 10_500.0 and g.update(10_500.0, NY_NOON + 660) is None
    jr.close()


def test_resume_rebases_only_the_breaker_that_tripped(tmp_path):
    jr = journal(tmp_path)
    g = AccountGuard(jr)
    g.update(10_000.0, NY_NOON)
    g.update(9_800.0, NY_NOON + 60)
    before = g.state()
    assert g.reset(9_800.0, NY_NOON + 120) == [] and g.state() == before  # /pause then /resume: nothing changes
    g.update(9_700.0, NY_NOON + 180)
    assert g.update(9_650.0, NY_NOON + 240) == "DAILY_LOSS"
    assert g.reset(9_650.0, NY_NOON + 300) == ["DAILY_LOSS"]
    st = g.state()
    assert (st["day_ref"], st["peak"], st["daily_tripped"]) == (9_650.0, 10_000.0, False)  # the peak stays
    g.update(8_400.0, NY_NOON + 360)
    assert g.update(8_400.0, NY_NOON + 420) == "TOTAL_DD"  # 16% below the peak (it marks the day too)
    assert g.reset(8_400.0, NY_NOON + 480) == ["TOTAL_DD", "DAILY_LOSS"]
    st = g.state()
    assert (st["peak"], st["day_ref"], st["total_tripped"], st["daily_tripped"]) == (8_400.0, 8_400.0, False, False)
    with pytest.raises(BadEquity):
        g.reset(math.nan, NY_NOON + 540)
    jr.close()


def test_session_watch_reports_changes_and_maintenance_once():
    w = SessionWatch()
    start = NY_NOON + 3_600
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 예정 2026-10-03 13:00 ~ 16:00 (뉴욕)")]
    assert w.update(SessionInfo("OPEN", start, start + 3 * 3600), NY_NOON + 60) == []
    assert w.update(SessionInfo("CLOSE"), NY_NOON + 120) == [("SESSION", "장 운영 상태 OPEN → CLOSE")]
    assert w.update(SessionInfo("OPEN", NY_NOON + 3 * 86_400), NY_NOON + 180)[0][0] == "SESSION"  # too far ahead


def test_session_watch_announces_a_window_in_progress_and_an_open_end():
    w = SessionWatch()  # a (re)start during maintenance
    assert w.update(SessionInfo("OPEN", NY_NOON - 600, NY_NOON + 3_000), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 진행 중 2026-10-03 11:50 ~ 12:50 (뉴욕)")]
    assert w.update(SessionInfo("OPEN", NY_NOON - 600, NY_NOON + 3_000), NY_NOON + 60) == []
    assert SessionWatch().update(SessionInfo("OPEN", NY_NOON + 3_600, None), NY_NOON) == [
        ("MAINTENANCE", "정기 점검 예정 2026-10-03 13:00 ~ 미정 (뉴욕)")]
    assert SessionWatch().update(SessionInfo("OPEN", NY_NOON - 7_200, NY_NOON - 3_600), NY_NOON) == []  # over


def ticker(product: str, price: float | str, seq: int, **book: str) -> str:
    return json.dumps({"channel": "ticker", "sequence_num": seq, "events": [
        {"type": "update", "tickers": [{"type": "ticker", "product_id": product, "price": str(price), **book}]}]})


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


class Book:
    """A futures quote source whose book can break (a price read as 0, NaN, ...) or fail."""

    futures_id = FUT

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.bid: float | None = 60_000.0
        self.ask = 60_010.0
        self.calls = 0

    def quote(self, product_id: str) -> Quote:
        self.calls += 1
        if self.bid is None:
            raise Unavailable("503")
        return Quote(product_id, self.bid, self.ask, self.clock())


class StuckFeed:
    def __init__(self, tick: Tick) -> None:
        self.tick = tick

    def latest(self, product_id: str) -> Tick:
        return self.tick


def test_price_source_never_returns_an_unusable_or_stale_price():
    clock = Clock(1_000.0)
    book = Book(clock)
    src = PriceSource(book, StuckFeed(Tick(math.nan, 1_000.0)), clock=clock)  # type: ignore[arg-type]
    assert src.futures_price() == 60_005.0 and src.mode == "rest"  # a NaN tick is never fresh
    for bid, ask in ((0.0, 60_010.0), (math.nan, 60_010.0), (-1.0, -1.0), (math.inf, math.inf)):
        clock.now += 10.0
        book.bid, book.ask = bid, ask
        assert src.futures_price() == 60_005.0  # a broken book is a failed read: the last good price (<= 40 s old)
    clock.now += 10.0
    book.bid = None  # outage
    assert src.futures_price() == 60_005.0 and book.calls == 6  # 50 s old
    clock.now += 5.0
    assert src.futures_price() == 60_005.0 and book.calls == 6  # one attempt per 10 s, failed or not
    clock.now += 6.0
    assert src.futures_price() is None and book.calls == 7  # the last good price is 61 s old: no price
    clock.now += 10.0
    book.bid, book.ask = 60_100.0, 60_110.0
    assert src.futures_price() == 60_105.0


def test_feed_parses_ticks_and_flags_sequence_gaps():
    clock = Clock(5.0)
    feed = TickerFeed([FUT], ws_factory=lambda **kw: None, clock=clock)
    feed.on_message(ticker(FUT, 60_000.0, 7))
    feed.on_message(json.dumps({"channel": "heartbeats", "sequence_num": 8}))
    assert feed.latest(FUT).price == 60_000.0 and not feed.take_resync()
    feed.on_message(ticker(FUT, 60_010.0, 11))  # 9 and 10 missing
    assert feed.gaps == 1 and feed.take_resync() and not feed.take_resync()
    feed.on_message("not json")
    clock.now = 9.0
    assert feed.age() == pytest.approx(4.0)


def test_feed_takes_the_bid_ask_mid_and_ignores_unusable_prices():
    clock = Clock(5.0)
    feed = TickerFeed([FUT], ws_factory=lambda **kw: None, clock=clock)
    feed.on_message(ticker(FUT, 60_100.0, 1, best_bid="60000", best_ask="60010"))
    assert feed.latest(FUT).price == 60_005.0  # the REST quote's measure, not the last trade
    feed.on_message(ticker(FUT, 60_020.0, 2, best_bid="", best_ask="60030"))
    assert feed.latest(FUT).price == 60_020.0  # no usable bid: the last trade price
    clock.now = 6.0
    for seq, bad in enumerate(("NaN", "inf", "0", "-5", "", "abc"), start=3):
        feed.on_message(ticker(FUT, bad, seq, best_bid=bad, best_ask="60030"))
    assert feed.latest(FUT) == Tick(60_020.0, 5.0) and feed.bad_messages == 6


def test_a_message_the_feed_cannot_read_never_reaches_the_sdk():
    feed = TickerFeed([FUT], ws_factory=lambda **kw: None, clock=Clock(5.0))
    for raw in ("not json", "[]", "5", json.dumps({"channel": "ticker", "events": [None]}),
                json.dumps({"channel": "ticker", "events": [{"tickers": "x"}]})):
        feed.on_message(raw)  # an exception here would end the SDK's message handler for good
    assert feed.bad_messages == 5 and feed.latest(FUT) is None
    feed.on_message(json.dumps({"type": "error", "message": "failure to subscribe"}))
    assert feed.last_error == "server error: failure to subscribe"
    feed.on_message(ticker(FUT, 60_000.0, 1))
    assert feed.latest(FUT).price == 60_000.0


class ScriptedWS:
    """One connection. `script` is "refuse" (open fails) or steps consumed one per pump(1): "msg" delivers a
    heartbeat, "quiet" returns without one (a connection that fell silent), "drop" raises like a lost
    connection, "stop" stops the feed. An exhausted script stays quiet; after 500 opens and steps in
    all the feed is stopped (a feed that never notices silence must not hang the test)."""

    steps = 0

    def __init__(self, feed: TickerFeed, scripts: list, log: list[str], **kw) -> None:
        self.feed, self.log = feed, log
        self.on_message = kw["on_message"]
        self.script = scripts.pop(0) if scripts else ["stop"]
        self.used = 0

    def open(self):
        ScriptedWS.steps += 1
        if ScriptedWS.steps > 500:
            self.feed.stop()
        self.log.append("open")
        if self.script == "refuse":
            raise ConnectionRefusedError("refused")

    def subscribe(self, products, channels):
        self.log.append(f"subscribe {','.join(products)} {','.join(channels)}")

    def pump(self, s):
        ScriptedWS.steps += 1
        self.used += 1
        step = self.script.pop(0) if self.script else "quiet"
        if ScriptedWS.steps > 500:
            step = "stop"
        if step == "msg":
            self.on_message(json.dumps({"channel": "heartbeats", "sequence_num": self.used}))
        elif step == "drop":
            raise ConnectionError("dropped")
        elif step == "stop":
            self.feed.stop()

    def close(self):
        self.log.append(f"close {self.used}")


def scripted(scripts: list, **kw) -> tuple[TickerFeed, list[str], list[float]]:
    ScriptedWS.steps = 0
    log: list[str] = []
    waits: list[float] = []
    feed = TickerFeed([FUT, "BTC-USD"], ws_factory=None, sleep=waits.append, **kw)  # type: ignore[arg-type]
    feed.ws_factory = lambda **k: ScriptedWS(feed, scripts, log, **k)
    return feed, log, waits


def test_feed_reconnects_with_backoff_and_requests_a_resync():
    feed, log, waits = scripted([["msg", "msg", "drop"], ["drop"], ["msg", "stop"]])
    feed.run()
    assert feed.connects == 3 and waits == [1.0, 2.0]  # a short-lived connection does not reset the backoff
    assert log.count("subscribe BIP-20DEC30-CDE,BTC-USD ticker,heartbeats") == 3
    assert [x for x in log if x.startswith("close")] == ["close 3", "close 1", "close 2"]
    assert feed.failures == 2 and feed.last_error == "ConnectionError" and feed.take_resync()


def test_a_silent_connection_is_torn_down_and_opened_again():
    """A connection can stay open and bring nothing (a half-open TCP connection, a server that stopped
    sending): pump keeps returning and no message comes. The heartbeats channel sends one every second, so
    SILENT_S without one means the connection is gone."""
    feed, log, waits = scripted([["msg"], ["msg", "stop"]])
    feed.run()
    assert feed.connects == 2 and waits == [1.0]
    assert [x for x in log if x.startswith("close")] == [f"close {1 + int(SILENT_S)}", "close 2"]
    assert feed.failures == 1 and feed.last_error == f"silent for {SILENT_S:.0f} s"


def test_backoff_doubles_to_the_cap_and_resets_only_after_a_healthy_minute():
    healthy = ["msg"] * int(HEALTHY_S) + ["drop"]
    feed, log, waits = scripted(["refuse"] * 8 + [["msg"] * 5 + ["drop"], "refuse", healthy, "refuse", ["stop"]])
    feed.run()
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0, 1.0, 2.0]
    assert feed.connects == 3 and feed.failures == 12 and feed.last_error == "ConnectionRefusedError"


class Refused:
    def __init__(self, **kw) -> None:
        pass

    def open(self):
        raise ConnectionRefusedError("refused")

    def close(self):
        pass


def test_stop_ends_a_backoff_wait_at_once():
    feed = TickerFeed([FUT], ws_factory=lambda **kw: Refused(**kw))
    th = feed.start()
    deadline = time.monotonic() + 2.0
    while feed.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    feed.stop()  # the feed is waiting 1 s before the next attempt
    th.join(0.5)
    assert feed.failures >= 1 and not th.is_alive()


class FakeConnection:
    """Stands in for a websockets ClientConnection: recv() hands out `inbox`, then raises `end`."""

    def __init__(self, inbox: list[str], end: BaseException) -> None:
        self.inbox, self.end = inbox, end
        self.sent: list[dict] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self, timeout: float | None = None) -> str:
        if self.inbox:
            return self.inbox.pop(0)
        raise self.end

    def close(self) -> None:
        self.closed = True


def test_the_public_socket_subscribes_without_a_key_over_verified_tls():
    beat = json.dumps({"channel": "heartbeats", "sequence_num": 1})
    conn = FakeConnection([beat, ticker(FUT, 60_000.0, 2)], ConnectionResetError("lost"))
    opened: list[tuple[str, dict]] = []

    def connect(url, **kw):  # no network: what the socket asks of websockets.sync.client.connect
        opened.append((url, kw))
        return conn

    got: list[str] = []
    ws = public_ws_factory(connect=connect)(on_message=got.append)
    ws.open()
    [(url, kw)] = opened
    assert url == PUBLIC_WS_URL == "wss://advanced-trade-ws.coinbase.com" and kw["open_timeout"] == 10
    ctx = kw["ssl"]  # the default context: the certificate chain and the host name are verified
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    ws.subscribe([FUT, "BTC-USD"], ["ticker", "heartbeats"])
    assert conn.sent == [{"type": "subscribe", "product_ids": [FUT, "BTC-USD"], "channel": "ticker"},
                         {"type": "subscribe", "product_ids": [FUT, "BTC-USD"], "channel": "heartbeats"}]  # no jwt
    with pytest.raises(ConnectionResetError):  # a lost connection reaches TickerFeed.run
        ws.pump(1)
    assert got == [beat, ticker(FUT, 60_000.0, 2)]
    quiet = FakeConnection([], TimeoutError())
    ws2 = public_ws_factory(connect=lambda url, **kw: quiet)(on_message=got.append)
    ws2.open()
    ws2.pump(0.01)  # nothing within the time: returns (TickerFeed counts the silence)
    ws2.close()
    assert quiet.closed and len(got) == 2


def test_the_pinned_websockets_client_takes_the_arguments_the_socket_passes():
    """Runs only with the `live` dependency group installed (websockets 13.1, declared there)."""
    client = pytest.importorskip("websockets.sync.client")
    assert metadata.version("websockets") == "13.1"
    assert {"ssl", "open_timeout"} <= set(inspect.signature(client.connect).parameters)
    for name in ("send", "recv", "close"):
        assert callable(getattr(client.ClientConnection, name))
    assert "timeout" in inspect.signature(client.ClientConnection.recv).parameters
