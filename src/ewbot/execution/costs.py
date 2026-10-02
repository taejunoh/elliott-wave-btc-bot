"""Cost model for CFM nano BTC perp-style futures (spec 9.2): taker fee, slippage, hourly funding."""
from __future__ import annotations

from ewbot.config import CostParams

_HOURS_PER_YEAR = 8760.0


def fee(price: float, contracts: int, c: CostParams) -> float:
    """One-side fee in USD: per contract max(rate * notional, min) if apply_min_fee else rate * notional."""
    if contracts <= 0:
        return 0.0
    per_contract = c.fee_rate * price * c.contract_size
    if c.apply_min_fee:
        per_contract = max(per_contract, c.fee_min_per_contract)
    return per_contract * contracts


def slip(price: float, side: int, c: CostParams) -> float:
    """Execution price after slippage. side=+1 (buy) pays up, side=-1 (sell) receives less."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side}")
    return price * (1.0 + side * c.slippage)


def funding_per_hour(mark: float, contracts: int, direction: int, c: CostParams) -> float:
    """Funding cost for one hour in USD (>0 = pay). Longs pay funding_apr_long, shorts receive it."""
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    notional = mark * contracts * c.contract_size
    return direction * notional * c.funding_apr_long / _HOURS_PER_YEAR
