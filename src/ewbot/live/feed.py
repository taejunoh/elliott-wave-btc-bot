"""WebSocket ticker feed (Plan 3 spec 2 `WebSocket`, 5.1).

The SDK's WSClient (coinbase-advanced-py 1.8.4, dependency group `live`) runs the connection in its own
thread; this class subscribes to the `ticker` and `heartbeats` channels of the futures and spot
products, keeps the last trade price per product and the time of the last message, and watches the
per-connection `sequence_num` for gaps. When the SDK gives up on a connection, `run` reconnects with
exponential backoff (1 s doubling to 60 s). Every (re)connect and every sequence gap sets `resync`, so
the runtime re-reads positions and orders over REST. The runtime reads prices through
watchdog.PriceSource, which uses REST polling once the feed has been silent for 60 s."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

CHANNELS = ("ticker", "heartbeats")
MAX_BACKOFF_S = 60.0


@dataclass(frozen=True, slots=True)
class Tick:
    price: float
    ts: float


class TickerFeed:
    def __init__(self, products: Sequence[str], ws_factory: Callable[..., Any], *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 max_backoff: float = MAX_BACKOFF_S) -> None:
        self.products = list(products)
        self.ws_factory = ws_factory
        self.clock = clock
        self.sleep = sleep
        self.max_backoff = max_backoff
        self.last_msg: float | None = None
        self.last_seq: int | None = None
        self.gaps = 0
        self.connects = 0
        self._ticks: dict[str, Tick] = {}
        self._resync = False
        self._stop = False
        self._lock = threading.Lock()

    def on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        now = self.clock()
        with self._lock:
            self.last_msg = now
            seq = msg.get("sequence_num")
            if isinstance(seq, int):
                if self.last_seq is not None and seq != self.last_seq + 1:
                    self.gaps += 1
                    self._resync = True
                self.last_seq = seq
            if msg.get("channel") == "ticker":
                for ev in msg.get("events") or []:
                    for t in ev.get("tickers") or []:
                        if t.get("product_id") and t.get("price"):
                            self._ticks[t["product_id"]] = Tick(float(t["price"]), now)

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
        self._stop = True

    def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            ws = None
            try:
                ws = self.ws_factory(on_message=self.on_message)
                ws.open()
                ws.subscribe(self.products, list(CHANNELS))
                with self._lock:
                    self.connects += 1
                    self.last_seq = None
                    self._resync = True
                backoff = 1.0
                while not self._stop:
                    ws.sleep_with_exception_check(1)
            except Exception:  # noqa: BLE001 - any connection failure: back off and reconnect
                pass
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - closing a dead socket may fail too
                        pass
            if self._stop:
                return
            self.sleep(backoff)
            backoff = min(backoff * 2.0, self.max_backoff)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run, name="ewbot-ticker", daemon=True)
        th.start()
        return th


def sdk_ws_factory(key_file: Path) -> Callable[..., Any]:
    """WSClient factory for production (the CDP key signs the subscription JWTs)."""

    def make(on_message: Callable[[str], None]) -> Any:
        from coinbase.websocket import WSClient  # dependency group `live`

        return WSClient(key_file=str(key_file), on_message=on_message, retry=True)

    return make
