"""CoinbaseExchange: the Exchange protocol over the Coinbase Advanced Trade REST API (Plan 3 spec 2).

The SDK `coinbase-advanced-py==1.8.4` (dependency group `live`) signs every request with a fresh JWT
from the CDP key (ECDSA or Ed25519; 2-minute expiry) - this module never touches the key. Only the
SDK's generic `client.get(path, params, public=...)` / `client.post(path, data=...)` are used, so the
parsers below read the raw JSON of the API (the recorded fixtures in tests/live/fixtures/coinbase.json)
and do not depend on the SDK's response classes.

Retries (spec 5.1): reads and cancels are retried up to 3 times on HTTP 429 / 5xx / network errors
(429 waits for the server's Retry-After, at most RETRY_AFTER_MAX_S); order placements are sent ONCE -
their failure is ambiguous (the order may exist) and the executor looks the order up by client_order_id
before sending it again. Every requests failure other than an HTTP status (a broken or undecodable
reply after the send, a bad header, ...) is ambiguous too: Unavailable, never a final rejection. When
`x-ratelimit-remaining` drops to `min_remaining`, the next request waits for the reset.

Only ExchangeError (or ValueError for a side that is not +1/-1, a programming error) leaves this module:
an unexpected reply shape is an ExchangeError, a reply that is not a JSON object is Unavailable. Error
messages never carry an exception's text (it may hold the JWT): the HTTP status and the start of the
reply body, or the exception's type name."""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import requests

from ewbot.live.exchange import (
    FUTURES_PRODUCT,
    OPEN_STATUSES,
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
_STATUS = {"UNKNOWN_ORDER_STATUS": "UNKNOWN"}  # EDIT_QUEUED / CANCEL_QUEUED keep their name: open (exchange.py)
_BRACKETS = ("trigger_bracket_gtc", "trigger_bracket_gtd")
_ORDER_SIDES = {"BUY": 1, "SELL": -1}  # OrderSide
_POSITION_SIDES = {"LONG": 1, "SHORT": -1}  # FCMPositionSide (UNKNOWN is refused)
INVALID_SIZE = "INVALID_SIZE_PRECISION"  # the reasons SimExchange gives, too
INVALID_PRICE = "INVALID_PRICE_PRECISION"
INVALID_STOP = "INVALID_STOP_PRICE_PRECISION"
DUPLICATE_ID = "DUPLICATE_CLIENT_ORDER_ID"
_REASON_FIELDS = ("new_order_failure_reason", "error", "preview_failure_reason")  # most specific first
CANCEL_DONE = frozenset({"DUPLICATE_CANCEL_REQUEST", "ORDER_IS_FULLY_FILLED"})  # nothing rests any more
RETRY_AFTER_MAX_S = 10.0  # never wait longer than the pacing cap on one 429
_PARSE_ERRORS = (KeyError, ValueError, TypeError, AttributeError, IndexError)


class DuplicateClientOrderId(ExchangeError):
    """An HTTP 4xx whose body names DUPLICATE_CLIENT_ORDER_ID: an order with this id exists. The order
    calls turn it into PlaceResult(duplicate=True), so the executor looks the order up."""


@contextmanager
def _parsing(what: str) -> Iterator[None]:
    """A reply that does not have the expected shape -> ExchangeError (type name only)."""
    try:
        yield
    except _PARSE_ERRORS as exc:
        raise ExchangeError(f"unexpected {what} reply ({type(exc).__name__})") from None


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


def bad_price(price: float) -> bool:
    """True unless price is a finite number above zero (fmt_decimal would send "NaN" or "-5")."""
    try:
        x = float(price)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(x) or x <= 0


def _bracket_problem(size: float, limit_price: float, stop_price: float) -> str | None:
    if bad_size(size):
        return INVALID_SIZE
    if bad_price(limit_price):
        return INVALID_PRICE
    if bad_price(stop_price):
        return INVALID_STOP
    return None


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


def retry_after_s(value: Any, now: float | None = None) -> float:
    """Retry-After in seconds - delta-seconds or an HTTP-date - clamped to [0, RETRY_AFTER_MAX_S];
    missing or unreadable -> 1 s."""
    if value is None:
        return 1.0
    text = str(value).strip()
    try:
        secs = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return 1.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        secs = when.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(secs):
        return 1.0
    return min(max(secs, 0.0), RETRY_AFTER_MAX_S)


def map_error(exc: BaseException) -> ExchangeError:
    """SDK / requests exception -> ExchangeError (RateLimited, Unavailable or a final error). Messages
    keep the status and the start of the reply only, or the exception's type name - never its text or
    headers (they may carry the JWT)."""
    if isinstance(exc, ExchangeError):
        return exc
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        resp = exc.response
        status = int(resp.status_code)
        body = str(getattr(resp, "text", "") or "")
        text = body[:200]
        if status == 429:
            return RateLimited(f"HTTP 429: {text}", retry_after=retry_after_s((resp.headers or {}).get("Retry-After")))
        if status >= 500:
            return Unavailable(f"HTTP {status}: {text}")
        if DUPLICATE_ID in body:
            return DuplicateClientOrderId(f"HTTP {status}: {text}")
        return ExchangeError(f"HTTP {status}: {text}")
    if isinstance(exc, requests.exceptions.RequestException):
        # connection, timeout, a reply cut short or undecodable after the send, an invalid header, ...:
        # the request may have reached the venue -> ambiguous (the executor looks the order up first)
        return Unavailable(type(exc).__name__)
    return ExchangeError(type(exc).__name__)


def _side_of(text: Any, sides: dict[str, int], what: str) -> int:
    side = sides.get(str(text)) if text is not None else None
    if side is None:
        raise ExchangeError(f"unexpected {what} side {str(text)[:40]!r}")
    return side


def parse_order(o: dict[str, Any]) -> Order:
    with _parsing("order"):
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
            side=_side_of(o.get("side"), _ORDER_SIDES, "order"),
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


def _reasons(resp: dict[str, Any]) -> list[str]:
    """The failure reasons of a create / close reply, most specific first: error_response's
    new_order_failure_reason, then its deprecated error and preview_failure_reason, then failure_reason."""
    err = resp.get("error_response") or {}
    found = [err.get(k) for k in _REASON_FIELDS] + [resp.get("failure_reason")]
    return [str(r) for r in found if r]


def parse_place(resp: dict[str, Any], client_order_id: str) -> PlaceResult:
    """A create / close_position reply -> PlaceResult. Accepted without an order id is ambiguous
    (Unavailable: the executor looks the order up by client_order_id)."""
    with _parsing("order placement"):
        if resp.get("success"):
            order_id = str((resp.get("success_response") or {}).get("order_id") or "")
            if not order_id:
                raise Unavailable("order accepted without an order_id")
            return PlaceResult(client_order_id, order_id, True)
        reasons = _reasons(resp)
        reason = next((r for r in reasons if not r.startswith("UNKNOWN_")), "UNKNOWN_FAILURE_REASON")
        err = resp.get("error_response") or {}
        message = str(err.get("message") or err.get("error_details") or "")
        error = f"{reason}: {message}"[:200] if message else reason
        return PlaceResult(client_order_id, None, False, error, duplicate=DUPLICATE_ID in reasons)


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
        """Rate-limit headers (merged into the reply by the SDK); unreadable values are ignored."""
        remaining = resp.pop("x-ratelimit-remaining", None)
        reset = resp.pop("x-ratelimit-reset", None)
        resp.pop("x-ratelimit-limit", None)
        try:
            if remaining is None or int(_num(remaining)) > self.min_remaining:
                return
            r = _num(reset, 1.0)
        except _PARSE_ERRORS:
            return
        if not math.isfinite(r):
            return
        wait = r - self.clock() if r > 1e9 else r  # epoch seconds or seconds-to-reset
        self._wait_until = self.clock() + min(max(wait, 0.0), RETRY_AFTER_MAX_S)

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
                if not isinstance(resp, dict):  # after the send: the request may have been executed
                    raise Unavailable(f"reply is not a JSON object ({type(resp).__name__})")
            except Exception as exc:  # noqa: BLE001 - mapped below
                err = map_error(exc)
                if not err.retryable or attempt == tries - 1:
                    raise err from None
                wait = min(err.retry_after, RETRY_AFTER_MAX_S) if isinstance(err, RateLimited) else self.backoff_s * 2**attempt
                self.sleep(wait)
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
        with _parsing("product"):
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
        p = self._product_json()
        with _parsing("session"):
            s = p.get("fcm_trading_session_details") or {}
            state = str(s.get("session_state") or "UNKNOWN").removeprefix(SESSION_PREFIX)
            m = s.get("maintenance") or {}
            return SessionInfo(state, iso_ts(m.get("start_time")), iso_ts(m.get("end_time")))

    def quote(self, product_id: str) -> Quote:
        book = self._call("GET", "/market/product_book", params={"product_id": product_id, "limit": 1}, public=True)
        with _parsing("product_book"):
            pb = book.get("pricebook") or {}
            bids, asks = pb.get("bids") or [], pb.get("asks") or []
            if not bids or not asks:
                raise Unavailable(f"empty order book for {product_id}")
            return Quote(product_id, _num(bids[0]["price"]), _num(asks[0]["price"]), self.clock())

    def position(self) -> Position | None:
        reply = self._call("GET", "/cfm/positions")
        with _parsing("positions"):
            for p in reply.get("positions") or []:
                if p.get("product_id") != self.futures_id:
                    continue
                n = abs(int(_num(p.get("number_of_contracts"))))
                if n == 0:
                    return None
                return Position(_side_of(p.get("side"), _POSITION_SIDES, "position"), n, _num(p.get("avg_entry_price")))
            return None

    def _orders(self, params: dict[str, Any]) -> list[Order]:
        out: list[Order] = []
        cursor: str | None = None
        while True:
            page = self._call("GET", "/orders/historical/batch",
                              params={**params, "product_ids": [self.futures_id], "limit": 250, "cursor": cursor})
            with _parsing("orders"):
                out += [parse_order(o) for o in page.get("orders") or []]
                cursor = page.get("cursor") or None
                more = bool(page.get("has_next"))
            if not more or not cursor:
                return out

    def open_orders(self) -> list[Order]:
        """Every resting order: order_status is a repeated query parameter (List Orders reference,
        `explode: true`; SDK 1.8.4 list_orders takes `order_status: List[str]`)."""
        return [o for o in self._orders({"order_status": list(OPEN_STATUSES)}) if o.is_open]

    def get_order(self, order_id: str) -> Order:
        reply = self._call("GET", f"/orders/historical/{order_id}")
        with _parsing("order"):
            return parse_order(reply["order"])

    def find_order(self, client_order_id: str, since_ts: float) -> Order | None:
        for o in self._orders({"start_date": _iso(since_ts)}):
            if o.client_order_id == client_order_id:
                return o
        return None

    def balance(self) -> Balance:
        """Equity = total_usd_balance (cash) + unrealized PnL. Without a readable, finite cash total there
        is no equity: an error, never PnL alone (the breakers and sizing read it)."""
        reply = self._call("GET", "/cfm/balance_summary")
        with _parsing("balance_summary"):
            bs = reply.get("balance_summary") or {}
            cash = _num(bs.get("total_usd_balance"), math.nan)
            if not math.isfinite(cash):
                raise ExchangeError("balance_summary without a total_usd_balance")
            equity = cash + _num(bs.get("unrealized_pnl"))
            avail = bs.get("available_margin")
            return Balance(equity, None if avail is None else _num(avail))

    def key_permissions(self) -> KeyPermissions:
        """Fail closed: a missing can_view / can_trade reads False, a missing can_transfer reads True
        (the preflight refuses a key that can transfer)."""
        k = self._call("GET", "/key_permissions")
        with _parsing("key_permissions"):
            transfer = k.get("can_transfer")
            return KeyPermissions(bool(k.get("can_view")), bool(k.get("can_trade")),
                                  True if transfer is None else bool(transfer), k.get("portfolio_type"))

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

    def _send_order(self, path: str, data: dict[str, Any], client_order_id: str) -> PlaceResult:
        """POST once; a 4xx naming DUPLICATE_CLIENT_ORDER_ID is the duplicate path, not a final error."""
        try:
            resp = self._call("POST", path, data=data, retry=False)
        except DuplicateClientOrderId as exc:
            return PlaceResult(client_order_id, None, False, f"{DUPLICATE_ID}: {exc}"[:200], duplicate=True)
        return parse_place(resp, client_order_id)

    def _create(self, client_order_id: str, side: int, cfg: dict[str, Any]) -> PlaceResult:
        data = {"client_order_id": client_order_id, "product_id": self.futures_id, "side": self._side(side),
                "order_configuration": cfg}
        return self._send_order("/orders", data, client_order_id)

    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult:
        self._side(side)
        if bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        return self._create(client_order_id, side, self._market_cfg(size))

    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult:
        self._side(side)
        problem = _bracket_problem(size, limit_price, stop_price)
        if problem is not None:
            return PlaceResult(client_order_id, None, False, problem)
        return self._create(client_order_id, side, self._bracket_cfg(size, limit_price, stop_price))

    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool:
        """Edit a resting bracket's stop (spec 4.2). Whether CFM brackets accept stop_price edits is
        confirmed in CANARY; any failure returns False and the executor replaces the order instead."""
        if _bracket_problem(size, limit_price, stop_price) is not None:
            return False
        data = {"order_id": order_id, "size": str(int(size)), "price": fmt_decimal(limit_price),
                "stop_price": fmt_decimal(stop_price)}
        try:
            return bool(self._call("POST", "/orders/edit", data=data, retry=False).get("success"))
        except ExchangeError:
            return False

    def cancel(self, order_ids: Sequence[str]) -> None:
        """batch_cancel, retried like a read (a repeated cancel is answered DUPLICATE_CANCEL_REQUEST).
        Raises ExchangeError naming each order that may still rest: refused, or missing from results."""
        if not order_ids:
            return
        ids = [str(i) for i in order_ids]
        reply = self._call("POST", "/orders/batch_cancel", data={"order_ids": ids})
        with _parsing("batch_cancel"):
            results = {str(r.get("order_id")): r for r in reply.get("results") or []}
            refused = []
            for oid in ids:
                r = results.get(oid)
                if r is None:
                    refused.append(f"{oid}: NO_RESULT")
                elif not r.get("success") and str(r.get("failure_reason")) not in CANCEL_DONE:
                    refused.append(f"{oid}: {r.get('failure_reason') or 'UNKNOWN_CANCEL_FAILURE_REASON'}")
        if refused:
            raise ExchangeError("cancel refused: " + ", ".join(refused))

    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult:
        if size is not None and bad_size(size):
            return PlaceResult(client_order_id, None, False, INVALID_SIZE)
        data: dict[str, Any] = {"client_order_id": client_order_id, "product_id": self.futures_id}
        if size is not None:
            data["size"] = str(int(size))
        return self._send_order("/orders/close_position", data, client_order_id)

    def _preview(self, side: int, cfg: dict[str, Any]) -> PreviewResult:
        resp = self._call("POST", "/orders/preview",
                          data={"product_id": self.futures_id, "side": self._side(side), "order_configuration": cfg})
        with _parsing("preview"):
            errs = tuple(str(e) for e in resp.get("errs") or ())
            warns = tuple(str(w) for w in resp.get("warning") or ())
            return PreviewResult(not errs, errs, warns)

    def preview_market(self, side: int, size: int) -> PreviewResult:
        self._side(side)
        if bad_size(size):
            return PreviewResult(False, (f"PREVIEW_{INVALID_SIZE}",))
        return self._preview(side, self._market_cfg(size))

    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult:
        self._side(side)
        problem = _bracket_problem(size, limit_price, stop_price)
        if problem is not None:
            return PreviewResult(False, (f"PREVIEW_{problem}",))
        return self._preview(side, self._bracket_cfg(size, limit_price, stop_price))
