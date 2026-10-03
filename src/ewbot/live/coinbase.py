"""CoinbaseExchange: the Exchange protocol over the Coinbase Advanced Trade REST API (Plan 3 spec 2).

The SDK `coinbase-advanced-py==1.8.4` (dependency group `live`) signs every request with a fresh JWT
from the CDP key (ECDSA or Ed25519; 2-minute expiry) - this module never touches the key. Only the
SDK's generic `client.get(path, params, public=...)` / `client.post(path, data=...)` are used, so the
parsers below read the raw JSON of the API (the recorded fixtures in tests/live/fixtures/coinbase.json)
and do not depend on the SDK's response classes.

Retries (spec 5.1): reads and cancels are retried up to 3 times on HTTP 429 / 5xx / network errors
(429 waits for the server's Retry-After); order placements are sent ONCE - their failure is ambiguous
(the order may exist) and the executor looks the order up by client_order_id before sending it again.
When `x-ratelimit-remaining` drops to `min_remaining`, the next request waits for the reset."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence

import requests

from ewbot.live.exchange import (
    FUTURES_PRODUCT,
    SPOT_PRODUCT,
    Balance,
    ExchangeError,
    KeyPermissions,
    Order,
    PlaceResult,
    Position,
    PreviewResult,
    ProductSpec,
    Quote,
    RateLimited,
    SessionInfo,
    Unavailable,
)

API = "/api/v3/brokerage"
SESSION_PREFIX = "FCM_TRADING_SESSION_STATE_"
_STATUS = {"CANCEL_QUEUED": "OPEN", "UNKNOWN_ORDER_STATUS": "UNKNOWN"}
_BRACKETS = ("trigger_bracket_gtc", "trigger_bracket_gtd")
INVALID_SIZE = "INVALID_SIZE_PRECISION"  # the reason SimExchange gives, too


def fmt_decimal(x: float | int) -> str:
    """84665.0 -> "84665", 0.5 -> "0.5" (the API takes decimal strings)."""
    d = Decimal(repr(float(x))).normalize()
    return format(d, "f")


def bad_size(size: float) -> bool:
    """True unless size is a whole number of contracts, at least 1. The adapter never truncates
    (str(int(2.9)) would silently send fewer contracts than asked): it refuses, like the venue."""
    try:
        x = float(size)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(x) or x != int(x) or x < 1


def iso_ts(text: str | None) -> int | None:
    if not text:
        return None
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    if isinstance(x, dict):  # {"value": "12.3", "currency": "USD"}
        return _num(x.get("value"), default)
    return float(x)


def map_error(exc: BaseException) -> ExchangeError:
    """SDK / requests exception -> ExchangeError (RateLimited, Unavailable or a final error). Messages
    keep the status and the start of the reply only (never headers: they carry the JWT)."""
    if isinstance(exc, ExchangeError):
        return exc
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        resp = exc.response
        status = int(resp.status_code)
        text = str(getattr(resp, "text", ""))[:200]
        if status == 429:
            retry_after = _num(resp.headers.get("Retry-After"), 1.0) if resp.headers else 1.0
            return RateLimited(f"HTTP 429: {text}", retry_after=retry_after)
        if status >= 500:
            return Unavailable(f"HTTP {status}: {text}")
        return ExchangeError(f"HTTP {status}: {text}")
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return Unavailable(f"{type(exc).__name__}")
    return ExchangeError(f"{type(exc).__name__}: {str(exc)[:200]}")


def parse_order(o: dict[str, Any]) -> Order:
    cfg = o.get("order_configuration") or {}
    kind, limit, stop, size = "OTHER", None, None, None
    if "market_market_ioc" in cfg:
        kind, size = "MARKET", cfg["market_market_ioc"].get("base_size")
    for key in _BRACKETS:
        if key in cfg:
            b = cfg[key]
            kind, size = "BRACKET", b.get("base_size")
            limit, stop = _num(b.get("limit_price"), 0.0) or None, _num(b.get("stop_trigger_price"), 0.0) or None
    filled = int(_num(o.get("filled_size")))
    avg = _num(o.get("average_filled_price")) or None
    status = str(o.get("status") or "UNKNOWN")
    return Order(
        order_id=str(o["order_id"]),
        client_order_id=str(o.get("client_order_id") or ""),
        side=1 if o.get("side") == "BUY" else -1,
        kind=kind,
        size=int(_num(size, filled)),
        filled=filled,
        status=_STATUS.get(status, status),
        limit_price=limit,
        stop_price=stop,
        avg_fill_price=avg,
        created_ts=iso_ts(o.get("created_time")),
        fee=None if o.get("total_fees") in (None, "") else _num(o.get("total_fees")),
    )


def parse_place(resp: dict[str, Any], client_order_id: str) -> PlaceResult:
    if resp.get("success"):
        ok = resp.get("success_response") or {}
        return PlaceResult(client_order_id, str(ok.get("order_id") or "") or None, True)
    err = resp.get("error_response") or {}
    reason = (err.get("error") or err.get("new_order_failure_reason") or err.get("preview_failure_reason")
              or resp.get("failure_reason") or "UNKNOWN_FAILURE_REASON")
    message = err.get("message") or err.get("error_details") or ""
    return PlaceResult(client_order_id, None, False, f"{reason}: {message}"[:200] if message else str(reason))


class CoinbaseExchange:
    def __init__(
        self,
        client: Any,
        futures_id: str = FUTURES_PRODUCT,
        spot_id: str = SPOT_PRODUCT,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        attempts: int = 3,
        backoff_s: float = 1.0,
        min_remaining: int = 2,
    ) -> None:
        self.client = client
        self.futures_id = futures_id
        self.spot_id = spot_id
        self.sleep = sleep
        self.clock = clock
        self.attempts = attempts
        self.backoff_s = backoff_s
        self.min_remaining = min_remaining
        self._wait_until = 0.0

    @classmethod
    def from_key_file(cls, key_file: Path, futures_id: str = FUTURES_PRODUCT, spot_id: str = SPOT_PRODUCT,
                      timeout: int = 10, **kw: Any) -> CoinbaseExchange:
        """The CDP key JSON as downloaded ({"name": ..., "privateKey": ...}); read by the SDK only."""
        from coinbase.rest import RESTClient  # dependency group `live`

        return cls(RESTClient(key_file=str(key_file), timeout=timeout, rate_limit_headers=True), futures_id, spot_id, **kw)

    # ------------------------------------------------------------------ transport
    def _pace(self, resp: dict[str, Any]) -> None:
        remaining = resp.pop("x-ratelimit-remaining", None)
        reset = resp.pop("x-ratelimit-reset", None)
        resp.pop("x-ratelimit-limit", None)
        if remaining is None or int(_num(remaining)) > self.min_remaining:
            return
        r = _num(reset, 1.0)
        wait = r - self.clock() if r > 1e9 else r  # epoch seconds or seconds-to-reset
        self._wait_until = self.clock() + min(max(wait, 0.0), 10.0)

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              data: dict[str, Any] | None = None, public: bool = False, retry: bool = True) -> dict[str, Any]:
        tries = self.attempts if retry else 1
        for attempt in range(tries):
            delay = self._wait_until - self.clock()
            if delay > 0:
                self.sleep(delay)
            try:
                if method == "GET":
                    resp = self.client.get(API + path, params=params, public=public)
                else:
                    resp = self.client.post(API + path, data=data)
            except Exception as exc:  # noqa: BLE001 - mapped below
                err = map_error(exc)
                if not err.retryable or attempt == tries - 1:
                    raise err from None
                self.sleep(err.retry_after if isinstance(err, RateLimited) else self.backoff_s * 2**attempt)
                continue
            resp = dict(resp)
            self._pace(resp)
            return resp
        raise ExchangeError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ reads
    def _product_json(self) -> dict[str, Any]:
        return self._call("GET", f"/products/{self.futures_id}")

    def product(self) -> ProductSpec:
        p = self._product_json()
        fpd = p.get("future_product_details") or {}
        im, om = fpd.get("intraday_margin_rate") or {}, fpd.get("overnight_margin_rate") or {}
        return ProductSpec(
            product_id=str(p["product_id"]),
            contract_size=_num(fpd.get("contract_size")),
            price_increment=_num(p.get("price_increment")),
            base_increment=_num(p.get("base_increment")),
            intraday_margin=(_num(im.get("long_margin_rate")), _num(im.get("short_margin_rate"))),
            overnight_margin=(_num(om.get("long_margin_rate")), _num(om.get("short_margin_rate"))),
        )

    def session(self) -> SessionInfo:
        s = self._product_json().get("fcm_trading_session_details") or {}
        state = str(s.get("session_state") or "UNKNOWN").removeprefix(SESSION_PREFIX)
        m = s.get("maintenance") or {}
        return SessionInfo(state, iso_ts(m.get("start_time")), iso_ts(m.get("end_time")))

    def quote(self, product_id: str) -> Quote:
        book = self._call("GET", "/market/product_book", params={"product_id": product_id, "limit": 1}, public=True)
        pb = book.get("pricebook") or {}
        bids, asks = pb.get("bids") or [], pb.get("asks") or []
        if not bids or not asks:
            raise Unavailable(f"empty order book for {product_id}")
        return Quote(product_id, _num(bids[0]["price"]), _num(asks[0]["price"]), self.clock())

    def position(self) -> Position | None:
        for p in self._call("GET", "/cfm/positions").get("positions") or []:
            if p.get("product_id") != self.futures_id:
                continue
            n = int(_num(p.get("number_of_contracts")))
            if n == 0:
                return None
            side = 1 if "LONG" in str(p.get("side", "")).upper() else -1
            return Position(side, n, _num(p.get("avg_entry_price")))
        return None

    def _orders(self, params: dict[str, Any]) -> list[Order]:
        out: list[Order] = []
        cursor: str | None = None
        while True:
            page = self._call("GET", "/orders/historical/batch",
                              params={**params, "product_ids": [self.futures_id], "limit": 250, "cursor": cursor})
            out += [parse_order(o) for o in page.get("orders") or []]
            cursor = page.get("cursor") or None
            if not page.get("has_next") or not cursor:
                return out

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders({"order_status": ["OPEN"]}) if o.is_open]

    def get_order(self, order_id: str) -> Order:
        return parse_order(self._call("GET", f"/orders/historical/{order_id}")["order"])

    def find_order(self, client_order_id: str, since_ts: float) -> Order | None:
        for o in self._orders({"start_date": _iso(since_ts)}):
            if o.client_order_id == client_order_id:
                return o
        return None

    def balance(self) -> Balance:
        bs = self._call("GET", "/cfm/balance_summary").get("balance_summary") or {}
        equity = _num(bs.get("total_usd_balance")) + _num(bs.get("unrealized_pnl"))
        avail = bs.get("available_margin")
        return Balance(equity, None if avail is None else _num(avail))

    def key_permissions(self) -> KeyPermissions:
        k = self._call("GET", "/key_permissions")
        return KeyPermissions(bool(k.get("can_view")), bool(k.get("can_trade")), bool(k.get("can_transfer")),
                              k.get("portfolio_type"))

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _side(side: int) -> str:
        if side not in (1, -1):
            raise ValueError(f"side must be +1 or -1, got {side}")
        return "BUY" if side > 0 else "SELL"

    def _market_cfg(self, size: int) -> dict[str, Any]:
        return {"market_market_ioc": {"base_size": str(int(size))}}

    def _bracket_cfg(self, size: int, limit_price: float, stop_price: float) -> dict[str, Any]:
        return {"trigger_bracket_gtc": {"base_size": str(int(size)), "limit_price": fmt_decimal(limit_price),
                                        "stop_trigger_price": fmt_decimal(stop_price)}}

    def _create(self, client_order_id: str, side: int, cfg: dict[str, Any]) -> PlaceResult:
        data = {"client_order_id": client_order_id, "product_id": self.futures_id, "side": self._side(side),
                "order_configuration": cfg}
        return parse_place(self._call("POST", "/orders", data=data, retry=False), client_order_id)

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._side(side)
        if bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        return self._create(client_order_id, side, self._market_cfg(size))

    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult:
        self._side(side)
        if bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        return self._create(client_order_id, side, self._bracket_cfg(size, limit_price, stop_price))

    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool:
        """Edit a resting bracket's stop (spec 4.2). Whether CFM brackets accept stop_price edits is
        confirmed in CANARY; any failure returns False and the executor replaces the order instead."""
        if bad_size(size):
            return False
        data = {"order_id": order_id, "size": str(int(size)), "price": fmt_decimal(limit_price),
                "stop_price": fmt_decimal(stop_price)}
        try:
            return bool(self._call("POST", "/orders/edit", data=data, retry=False).get("success"))
        except ExchangeError:
            return False

    def cancel(self, order_ids: Sequence[str]) -> None:
        if order_ids:
            self._call("POST", "/orders/batch_cancel", data={"order_ids": list(order_ids)})

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        if size is not None and bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        data: dict[str, Any] = {"client_order_id": client_order_id, "product_id": self.futures_id}
        if size is not None:
            data["size"] = str(int(size))
        return parse_place(self._call("POST", "/orders/close_position", data=data, retry=False), client_order_id)

    def _preview(self, side: int, cfg: dict[str, Any]) -> PreviewResult:
        resp = self._call("POST", "/orders/preview",
                          data={"product_id": self.futures_id, "side": self._side(side), "order_configuration": cfg})
        errs = tuple(str(e) for e in resp.get("errs") or ())
        warns = tuple(str(w) for w in resp.get("warning") or ())
        return PreviewResult(not errs, errs, warns)

    def preview_market(self, side: int, size: int) -> PreviewResult:
        if bad_size(size):
            return PreviewResult(False, (f"PREVIEW_{INVALID_SIZE}",))
        return self._preview(side, self._market_cfg(size))

    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult:
        if bad_size(size):
            return PreviewResult(False, (f"PREVIEW_{INVALID_SIZE}",))
        return self._preview(side, self._bracket_cfg(size, limit_price, stop_price))
