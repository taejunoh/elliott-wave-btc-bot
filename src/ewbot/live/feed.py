"""WebSocket ticker feed (Plan 3 spec 2 `WebSocket`, 5.1).

The SDK's WSClient (coinbase-advanced-py 1.8.4, dependency group `live`) runs the connection in its own
event-loop thread; this class subscribes to the `ticker` and `heartbeats` channels of the futures and spot
products, keeps the price per product - the mid of the ticker's best bid and ask (what the REST quote
measures), its last trade price only when those are missing; only a finite price above 0 - and the time of
the last message, and watches the per-connection `sequence_num` for gaps.

`run` owns every reconnect (the SDK's own retry is off: its backoff would race this one). A connection that
fails, raises, or brings no message for SILENT_S (the heartbeats channel sends one every second; a server
close 1000/1001 ends the SDK's handler without any error, so silence is how that shows) is torn down and
opened again after an exponential backoff: 1 s doubling to 60 s, back to 1 s only once a connection has
carried messages for HEALTHY_S. `stop()` also ends a backoff wait at once. Every (re)connect and every
sequence gap sets `resync`, so the runtime re-reads positions and orders over REST. A message that cannot
be read is counted (`bad_messages`) and never raised into the SDK (that would end its handler for good).
`failures` and `last_error` (an exception's type name only - its text might carry a token - or "silent for
15 s", or a server error message) tell the runtime why the feed is down. The runtime reads prices through
watchdog.PriceSource, which uses REST polling once the feed has been silent for 60 s."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

CHANNELS = ("ticker", "heartbeats")
MAX_BACKOFF_S = 60.0
SILENT_S = 15.0  # no message for this long (heartbeats come every second): the connection is gone
HEALTHY_S = 60.0  # a connection that carried messages this long resets the backoff to 1 s
OPEN_TIMEOUT_S = 10  # the SDK's opening-handshake timeout (none by default: a stalled handshake hangs)


@dataclass(frozen=True, slots=True)
class Tick:
    price: float
    ts: float


def usable_price(x: Any) -> float | None:
    """A finite price above 0, else None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def ticker_price(t: dict[str, Any]) -> float | None:
    """The mid of the ticker's best bid and ask; its last trade price when either is missing or unusable."""
    bid, ask = usable_price(t.get("best_bid")), usable_price(t.get("best_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return usable_price(t.get("price"))


class _Silent(Exception):
    pass


class TickerFeed:
    def __init__(self, products: Sequence[str], ws_factory: Callable[..., Any], *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Any] | None = None,
                 max_backoff: float = MAX_BACKOFF_S, silent_s: float = SILENT_S,
                 healthy_s: float = HEALTHY_S) -> None:
        self.products = list(products)
        self.ws_factory = ws_factory
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = self._stop.wait if sleep is None else sleep  # the backoff wait: stop() ends the default one
        self.max_backoff = max_backoff
        self.silent_s = silent_s
        self.healthy_s = healthy_s
        self.last_msg: float | None = None
        self.last_seq: int | None = None
        self.gaps = 0
        self.connects = 0
        self.failures = 0
        self.messages = 0
        self.bad_messages = 0
        self.last_error: str | None = None
        self._ticks: dict[str, Tick] = {}
        self._resync = False
        self._lock = threading.Lock()

    def on_message(self, raw: str) -> None:
        """Called on the SDK's event-loop thread: never raises."""
        try:
            self._read(raw)
        except Exception:  # noqa: BLE001 - a message we cannot read is counted, never raised into the SDK
            with self._lock:
                self.bad_messages += 1

    def _read(self, raw: str) -> None:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError("a message that is not a JSON object")
        ticks: dict[str, float] = {}
        unusable = 0
        if msg.get("channel") == "ticker":
            for ev in msg.get("events") or []:
                for t in ev.get("tickers") or []:
                    if not t.get("product_id"):
                        continue
                    price = ticker_price(t)
                    if price is None:
                        unusable += 1
                    else:
                        ticks[t["product_id"]] = price
        now = self.clock()
        with self._lock:
            self.last_msg = now
            self.messages += 1
            self.bad_messages += unusable
            if msg.get("type") == "error":
                self.last_error = f"server error: {msg.get('message')}"[:160]
            seq = msg.get("sequence_num")
            if isinstance(seq, int):
                if self.last_seq is not None and seq != self.last_seq + 1:
                    self.gaps += 1
                    self._resync = True
                self.last_seq = seq
            for product, price in ticks.items():
                self._ticks[product] = Tick(price, now)

    def latest(self, product_id: str) -> Tick | None:
        with self._lock:
            return self._ticks.get(product_id)

    def age(self) -> float | None:
        with self._lock:
            return None if self.last_msg is None else self.clock() - self.last_msg

    def take_resync(self) -> bool:
        with self._lock:
            flag, self._resync = self._resync, False
            return flag

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                ws = self.ws_factory(on_message=self.on_message)
                ws.open()
                ws.subscribe(self.products, list(CHANNELS))
                with self._lock:
                    self.connects += 1
                    self.last_seq = None
                    self._resync = True
                    seen = self.messages
                up = silent = 0
                while not self._stop.is_set():
                    ws.sleep_with_exception_check(1)  # raises what the SDK's handler hit (a lost connection)
                    up += 1
                    with self._lock:
                        count = self.messages
                    silent, seen = (0 if count != seen else silent + 1), count
                    if silent >= self.silent_s:
                        raise _Silent(f"silent for {silent} s")
                    if up >= self.healthy_s:
                        backoff = 1.0  # a healthy connection: the next outage starts from 1 s again
            except Exception as exc:  # noqa: BLE001 - any connection failure: back off and reconnect
                with self._lock:
                    self.failures += 1
                    self.last_error = str(exc) if isinstance(exc, _Silent) else type(exc).__name__
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - closing a dead socket may fail too
                        pass
            if self._stop.is_set():
                return
            self.sleep(backoff)
            backoff = min(backoff * 2.0, self.max_backoff)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="ewbot-ticker", daemon=True)
        th.start()
        return th


class SdkSocket:
    """The SDK's WSClient as TickerFeed drives it, with a close() that always releases the client's
    event-loop thread: WSClient.close() (SDK 1.8.4) raises before it stops and joins its loop when the
    socket is not open - a connection that never opened, or one the server closed - which would leak one
    thread and three file descriptors per failed connection."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def open(self) -> None:
        self.client.open()

    def subscribe(self, products: list[str], channels: list[str]) -> None:
        self.client.subscribe(products, channels)

    def sleep_with_exception_check(self, s: int) -> None:
        self.client.sleep_with_exception_check(s)

    def close(self) -> None:
        try:
            self.client.close()
            return
        except Exception:  # noqa: BLE001 - the socket was not open: stop the client's loop here
            pass
        loop, thread = getattr(self.client, "loop", None), getattr(self.client, "thread", None)
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(5.0)
        if thread is None or not thread.is_alive():
            loop.close()


def sdk_ws_factory(key_file: Path) -> Callable[..., Any]:
    """WSClient factory for production (the CDP key signs the subscription JWTs). The SDK's retry is off -
    TickerFeed.run reconnects - and the opening handshake times out after OPEN_TIMEOUT_S."""

    def make(on_message: Callable[[str], None]) -> SdkSocket:
        from coinbase.websocket import WSClient  # dependency group `live`

        return SdkSocket(WSClient(key_file=str(key_file), on_message=on_message, retry=False,
                                  timeout=OPEN_TIMEOUT_S))

    return make
