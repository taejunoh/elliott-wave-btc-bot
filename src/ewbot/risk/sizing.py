"""Position sizing (spec 6.2)."""
from __future__ import annotations

import math

from ewbot.config import CostParams, RiskProfile

_EPS = 1e-9  # guards floor() against 59.99999999 -> 59


def size_contracts(
    equity: float,
    entry: float,
    stop: float,
    direction: int,
    risk: RiskProfile,
    risk_mult: float,
    c: CostParams,
) -> int:
    """Whole contracts to trade; 0 means skip the signal.

    risk$     = equity * risk_pct * risk_mult
    distance  = |entry - stop| + slippage * (entry + stop)   (entry and stop fill both slip against us)
    qty (BTC) = risk$ / distance, capped so notional <= equity * max_leverage (stop never widened)
    contracts = floor(qty / contract_size); < 1 -> 0
    liquidation (isolated margin, maintenance ~ 0): liq = entry * (1 - direction / lev_eff);
    reject if liq is at/inside the stop.
    """
    if direction not in (1, -1):
        return 0
    if equity <= 0.0 or entry <= 0.0 or stop <= 0.0 or risk_mult <= 0.0:
        return 0
    raw = direction * (entry - stop)
    if raw <= 0.0:
        return 0  # stop on the wrong side of the entry
    distance = raw + c.slippage * (entry + stop)
    qty = equity * risk.risk_pct * risk_mult / distance
    qty = min(qty, equity * risk.max_leverage / entry)
    contracts = math.floor(qty / c.contract_size + _EPS)
    if contracts < 1:
        return 0
    lev_eff = contracts * c.contract_size * entry / equity
    liq = entry * (1.0 - direction / lev_eff)
    if direction * (liq - stop) >= 0.0:
        return 0  # liquidation would hit before (or at) the stop
    return contracts
