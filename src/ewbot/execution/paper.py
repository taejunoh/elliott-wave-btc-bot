"""PaperBroker: bar-by-bar fill simulator for one position (spec 9.2).

Order of events inside one 1h bar (process_bar):
  0. Futures closed (Fri 17-18 ET): nothing fills. Pending market orders stay pending; if the bar's
     range crossed the stop, the stop is marked as triggered and fills at the next tradable open.
  1. Pending market exit, then pending market entry, at open +/- slippage.
  2. Gap: open already at/beyond the stop (or stop triggered while closed) -> stop fills at open +/- slippage.
  3. Intrabar: stop touched -> stop at stop +/- slippage and nothing else (stop wins over TP in the same bar).
     Otherwise TP1 then TP2 (limit, no slippage), each only on a STRICT cross of the level.
  4. Funding for the hour on the position still open at the close (mark = bar.close).
"""
from __future__ import annotations

from dataclasses import dataclass

from ewbot.config import CostParams
from ewbot.data.candles import futures_closed
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.types import Bar


@dataclass(frozen=True, slots=True)
class Fill:
    ts: int
    kind: str  # "ENTRY","STOP","TP1","TP2","EXIT"
    side: int  # +1 buy, -1 sell
    contracts: int
    price: float
    fee: float


@dataclass(slots=True)
class PaperPosition:
    direction: int
    contracts: int  # contracts still open
    entry_price: float
    entry_ts: int
    stop: float
    tp1: float | None  # None once TP1 has filled
    tp2: float | None
    tp1_contracts: int
    funding: float = 0.0  # extension: funding accrued on this position (USD, >0 = paid)


@dataclass(slots=True)
class _PendingEntry:
    direction: int
    contracts: int
    stop: float
    tp1: float
    tp2: float | None
    tp1_contracts: int


class PaperBroker:
    def __init__(self, costs: CostParams, equity: float) -> None:
        self.costs = costs
        self.cash = float(equity)
        self.position: PaperPosition | None = None
        self._pending_entry: _PendingEntry | None = None
        self._pending_exit = False
        self._stop_triggered_while_closed = False

    # ------------------------------------------------------------------ queries
    def equity(self, mark: float) -> float:
        pos = self.position
        if pos is None:
            return self.cash
        upnl = pos.direction * (mark - pos.entry_price) * pos.contracts * self.costs.contract_size
        return self.cash + upnl

    # ------------------------------------------------------------------ orders
    def queue_entry(
        self,
        direction: int,
        contracts: int,
        stop: float,
        tp1: float,
        tp2: float | None,
        tp1_contracts: int,
    ) -> None:
        """Market entry at the NEXT processed bar's open. It only fills while flat: to flip,
        call queue_exit() first (the exit is always processed before the entry)."""
        if direction not in (1, -1):
            raise ValueError(f"direction must be +1 or -1, got {direction}")
        if contracts < 1:
            raise ValueError("contracts must be >= 1")
        if not 1 <= tp1_contracts <= contracts:
            raise ValueError("tp1_contracts must be in [1, contracts]")
        self._pending_entry = _PendingEntry(direction, contracts, stop, tp1, tp2, tp1_contracts)

    def queue_exit(self) -> None:
        """Market close of the whole position at the NEXT processed bar's open."""
        self._pending_exit = True

    def set_stop(self, price: float) -> None:
        """Move the stop; the engine calls this after process_bar, so it is effective from the next bar."""
        if self.position is not None:
            self.position.stop = price
        elif self._pending_entry is not None:
            self._pending_entry.stop = price

    def close_now(self, ts: int, price: float) -> Fill | None:
        """Extension (used for END): market-close the whole position immediately at price +/- slippage."""
        self._pending_exit = False
        pos = self.position
        if pos is None:
            return None
        return self._close(ts, "EXIT", slip(price, -pos.direction, self.costs), pos.contracts)

    # ------------------------------------------------------------------ simulation
    def process_bar(self, bar: Bar) -> list[Fill]:
        fills: list[Fill] = []
        if futures_closed(bar.ts):
            pos = self.position
            if pos is not None and self._stop_touched(pos, bar):
                self._stop_triggered_while_closed = True
            self._charge_funding(bar)
            return fills

        # (1) pending market orders at the open
        if self._pending_exit:
            self._pending_exit = False
            if self.position is not None:
                pos = self.position
                price = slip(bar.open, -pos.direction, self.costs)
                fills.append(self._close(bar.ts, "EXIT", price, pos.contracts))
        if self._pending_entry is not None and self.position is None:
            pe = self._pending_entry
            self._pending_entry = None
            price = slip(bar.open, pe.direction, self.costs)
            f = fee(price, pe.contracts, self.costs)
            self.cash -= f
            self.position = PaperPosition(
                direction=pe.direction,
                contracts=pe.contracts,
                entry_price=price,
                entry_ts=bar.ts,
                stop=pe.stop,
                tp1=pe.tp1,
                tp2=pe.tp2,
                tp1_contracts=pe.tp1_contracts,
            )
            fills.append(Fill(bar.ts, "ENTRY", pe.direction, pe.contracts, price, f))

        pos = self.position
        if pos is not None:
            d = pos.direction
            if self._stop_triggered_while_closed or d * (bar.open - pos.stop) <= 0:
                # (2) gap through the stop (or stop passed during the futures close): fill at the open
                fills.append(self._close(bar.ts, "STOP", slip(bar.open, -d, self.costs), pos.contracts))
            elif self._stop_touched(pos, bar):
                # (3a) stop has priority over any TP in the same bar
                fills.append(self._close(bar.ts, "STOP", slip(pos.stop, -d, self.costs), pos.contracts))
            else:
                # (3b) limit TPs need a strict cross
                fav = bar.high if d == 1 else bar.low
                if pos.tp1 is not None and d * (fav - pos.tp1) > 0:
                    tp1 = pos.tp1
                    pos.tp1 = None
                    fills.append(self._close(bar.ts, "TP1", tp1, min(pos.tp1_contracts, pos.contracts)))
                if self.position is not None and pos.tp1 is None and pos.tp2 is not None:
                    if d * (fav - pos.tp2) > 0:
                        fills.append(self._close(bar.ts, "TP2", pos.tp2, pos.contracts))

        # (4) funding for the hour
        self._charge_funding(bar)
        return fills

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _stop_touched(pos: PaperPosition, bar: Bar) -> bool:
        return bar.low <= pos.stop if pos.direction == 1 else bar.high >= pos.stop

    def _close(self, ts: int, kind: str, price: float, qty: int) -> Fill:
        pos = self.position
        assert pos is not None and 1 <= qty <= pos.contracts
        realized = pos.direction * (price - pos.entry_price) * qty * self.costs.contract_size
        f = fee(price, qty, self.costs)
        self.cash += realized - f
        pos.contracts -= qty
        if pos.contracts == 0:
            self.position = None
            self._stop_triggered_while_closed = False
        return Fill(ts, kind, -pos.direction, qty, price, f)

    def _charge_funding(self, bar: Bar) -> None:
        pos = self.position
        if pos is None:
            return
        cost = funding_per_hour(bar.close, pos.contracts, pos.direction, self.costs)
        self.cash -= cost
        pos.funding += cost
