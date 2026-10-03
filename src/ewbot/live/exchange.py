"""Exchange boundary of the live runtime (Plan 3 spec 3.2 `exchange`): value types, errors and the
Exchange protocol implemented by CoinbaseExchange (live/coinbase.py), SimExchange (live/sim.py) and
ShadowExchange (live/shadow.py).

Conventions: side +1 = buy / long, -1 = sell / short; sizes are whole contracts; prices are USD floats
already on the product's tick grid when they are sent (live/pricing.py does the rounding)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

FUTURES_PRODUCT = "BIP-20DEC30-CDE"  # nano Bitcoin perp-style future, 0.01 BTC per contract
SPOT_PRODUCT = "BTC-USD"  # the signal source (same candles as the paper runtime)
SESSION_OPEN = "OPEN"
# Statuses of an order that still rests and can fill. EDIT_QUEUED and CANCEL_QUEUED are open until the
# venue confirms the edit / cancel: a queued cancel can still fill, so it still counts as protection and as
# an order of ours (List Orders reference, OrderExecutionStatus).
OPEN_STATUSES = ("OPEN", "PENDING", "QUEUED", "EDIT_QUEUED", "CANCEL_QUEUED")
TERMINAL_STATUSES = ("FILLED", "CANCELLED", "EXPIRED", "FAILED")
# A triggered trigger_bracket stop rests as a limit this far beyond the stop. Coinbase Advanced Trade API
# reference, Create Order, trigger_bracket_gtc.stop_trigger_price (checked 2026-10-03): "When triggered, a
# stop limit order is automatically placed with a limit price 5% higher for BUYS and 5% lower for SELLS."
# (Spec 4.2 assumed 1%; the reference wins. Confirm on the first stop fill in CANARY.)
STOP_LIMIT_OFFSET = 0.05


@dataclass(frozen=True, slots=True)
class ProductSpec:
    product_id: str
    contract_size: float
    price_increment: float
    base_increment: float
    intraday_margin: tuple[float, float]  # (long, short) initial margin rates
    overnight_margin: tuple[float, float]  # (long, short): from 16:00 New York

    def margin_rate(self, side: int, overnight: bool = True) -> float:
        rates = self.overnight_margin if overnight else self.intraday_margin
        return rates[0] if side > 0 else rates[1]


@dataclass(frozen=True, slots=True)
class SessionInfo:
    state: str  # OPEN, CLOSE, PRE_OPEN, HALTED, ... (the FCM_TRADING_SESSION_STATE_ prefix stripped)
    maintenance_start: int | None = None  # next scheduled maintenance window (epoch s)
    maintenance_end: int | None = None

    @property
    def is_open(self) -> bool:
        return self.state == SESSION_OPEN


@dataclass(frozen=True, slots=True)
class Quote:
    product_id: str
    bid: float
    ask: float
    ts: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class Position:
    side: int
    contracts: int
    avg_entry: float


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    client_order_id: str
    side: int
    kind: str  # "MARKET" | "BRACKET" | "OTHER"
    size: int
    filled: int
    status: str  # OPEN_STATUSES, TERMINAL_STATUSES or UNKNOWN
    limit_price: float | None = None  # bracket take-profit
    stop_price: float | None = None  # bracket stop trigger
    avg_fill_price: float | None = None
    created_ts: float | None = None
    fee: float | None = None  # USD paid on the fills so far (None: not reported)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class Balance:
    equity: float  # USD: cash balances + unrealized PnL
    available_margin: float | None = None


@dataclass(frozen=True, slots=True)
class KeyPermissions:
    can_view: bool
    can_trade: bool
    can_transfer: bool
    portfolio_type: str | None = None


@dataclass(frozen=True, slots=True)
class PlaceResult:
    client_order_id: str
    order_id: str | None
    ok: bool
    error: str | None = None
    duplicate: bool = False  # refused because the client_order_id exists: look the order up, never re-send blind


@dataclass(frozen=True, slots=True)
class PreviewResult:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ExchangeError(RuntimeError):
    """A request failed. `retryable` errors (rate limit, 5xx, network) may succeed later; for an order
    placement the outcome is then unknown and the order must be looked up by client_order_id first."""

    retryable = False


class RateLimited(ExchangeError):
    retryable = True

    def __init__(self, message: str, retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = float(retry_after)


class Unavailable(ExchangeError):
    """HTTP 5xx, connection error or timeout."""

    retryable = True


class Exchange(Protocol):
    futures_id: str
    spot_id: str

    def product(self) -> ProductSpec: ...
    def session(self) -> SessionInfo: ...
    def quote(self, product_id: str) -> Quote: ...
    def position(self) -> Position | None: ...
    def open_orders(self) -> list[Order]: ...
    def get_order(self, order_id: str) -> Order: ...
    def find_order(self, client_order_id: str, since_ts: float) -> Order | None: ...
    def balance(self) -> Balance: ...
    def key_permissions(self) -> KeyPermissions: ...
    def place_market(self, client_order_id: str, side: int, size: int) -> PlaceResult: ...
    def place_bracket(
        self, client_order_id: str, side: int, size: int, limit_price: float, stop_price: float
    ) -> PlaceResult: ...
    def edit_bracket(self, order_id: str, size: int, limit_price: float, stop_price: float) -> bool: ...
    def cancel(self, order_ids: Sequence[str]) -> None:
        """Cancel resting orders. Raises ExchangeError naming every order that may still rest (refused or
        unknown); an order that already filled, or whose cancel is already queued, is not an error."""
        ...
    def close_position(self, client_order_id: str, size: int | None = None) -> PlaceResult: ...
    def preview_market(self, side: int, size: int) -> PreviewResult: ...
    def preview_bracket(self, side: int, size: int, limit_price: float, stop_price: float) -> PreviewResult: ...
