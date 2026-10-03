"""Spot -> futures price mapping, tick rounding, per-stage sizing and margin projection
(Plan 3 spec 3.3, 4.3, 4.5).

Signals, stops and targets are computed on spot BTC-USD candles (the paper runtime's input); orders go
to the BIP future. A level is mapped with the basis ratio at order time (futures mid / spot mid) and
put on the $5 tick conservatively: long levels round DOWN, short levels round UP. For a stop that is
the spec's rule (never triggered earlier than the engine's stop); for a take-profit it moves the
level toward the entry, so the live limit is reached no later than the engine's strict-cross TP.

Sizing: SHADOW and CANARY trade 1 contract and skip a signal whose 1-contract risk exceeds 2% of the
account; FULL calls the frozen risk.sizing.size_contracts with the real equity and the conservative
profile (no strategy file is changed), capped at 3 contracts for the first 30 days of FULL. Margin is
always projected at the OVERNIGHT rate (the stricter one; it applies from 16:00 New York)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ewbot.config import CONSERVATIVE, BotConfig, CostParams
from ewbot.live.exchange import STOP_LIMIT_OFFSET, ProductSpec
from ewbot.risk.sizing import size_contracts

CANARY_MAX_RISK = 0.02  # spec 4.3
FULL_CAP_CONTRACTS = 3  # spec 4.3 / 6.2: first 30 days of FULL
FULL_CAP_DAYS = 30
MARGIN_ENTRY_MAX = 0.70  # spec 4.5: refuse an entry whose overnight projection exceeds 70%
MARGIN_WARN = 0.80
MARGIN_REDUCE = 0.90
_EPS = 1e-9


def to_tick(price: float, tick: float, direction: int) -> float:
    """Long (+1): floor to the tick grid; short (-1): ceil."""
    if tick <= 0 or direction not in (1, -1):
        raise ValueError(f"bad tick {tick} / direction {direction}")
    q = price / tick
    n = math.floor(q + _EPS) if direction > 0 else math.ceil(q - _EPS)
    return float(n) * tick


def basis_ratio(fut_mid: float, spot_mid: float) -> float:
    if not (math.isfinite(fut_mid) and math.isfinite(spot_mid)) or fut_mid <= 0 or spot_mid <= 0:
        raise ValueError(f"bad mids {fut_mid} / {spot_mid}")
    return fut_mid / spot_mid


def map_level(spot_level: float, ratio: float, tick: float, direction: int) -> float:
    return to_tick(spot_level * ratio, tick, direction)


@dataclass(frozen=True, slots=True)
class Levels:
    stop: float
    tp1: float
    tp2: float | None


def map_levels(direction: int, stop: float, tp1: float, tp2: float | None, ratio: float, tick: float) -> Levels:
    return Levels(
        map_level(stop, ratio, tick, direction),
        map_level(tp1, ratio, tick, direction),
        None if tp2 is None else map_level(tp2, ratio, tick, direction),
    )


def stop_limit(stop: float, direction: int, offset: float = STOP_LIMIT_OFFSET) -> float:
    """The limit a triggered stop of a `direction` position rests at (a long sells 5% below)."""
    return stop * (1.0 - direction * offset)


def stop_gapped(price: float, stop: float, direction: int, offset: float = STOP_LIMIT_OFFSET) -> bool:
    """The price is beyond the stop's limit: the triggered stop cannot fill (spec 4.2 stop gap)."""
    return direction * (price - stop_limit(stop, direction, offset)) < 0


def risk_per_contract(entry: float, stop: float, costs: CostParams) -> float:
    """USD lost on one contract stopped out, with both fills slipping (risk.sizing's distance)."""
    return (abs(entry - stop) + costs.slippage * (entry + stop)) * costs.contract_size


def margin_ratio(contracts: int, price: float, side: int, product: ProductSpec, equity: float,
                 overnight: bool = True) -> float:
    if contracts <= 0:
        return 0.0
    if not (math.isfinite(price) and math.isfinite(equity)) or equity <= 0:
        return math.inf
    return contracts * product.contract_size * price * product.margin_rate(side, overnight) / equity


def max_contracts_for_margin(price: float, side: int, product: ProductSpec, equity: float,
                             limit: float = MARGIN_ENTRY_MAX) -> int:
    per = product.contract_size * price * product.margin_rate(side, True)
    if per <= 0 or equity <= 0:
        return 0
    return max(0, math.floor(limit * equity / per + _EPS))


def stage_contracts(
    stage: str,
    *,
    equity: float,
    entry: float,
    stop: float,
    direction: int,
    risk_mult: float,
    cfg: BotConfig,
    full_cap: int | None = None,
) -> tuple[int, str]:
    """(contracts, reason). 0 contracts = skip the signal (recorded, not traded). Fails closed on a
    non-finite price or equity, a bad direction, or a negative `full_cap`."""
    if full_cap is not None and full_cap < 0:
        raise ValueError(f"bad full_cap {full_cap}")
    if direction not in (1, -1) or not all(map(math.isfinite, (entry, stop, equity))) or equity <= 0:
        return 0, "bad sizing inputs"
    if direction * (entry - stop) <= 0:
        return 0, "stop on the wrong side of the entry"
    if stage in ("SHADOW", "CANARY"):
        risk = risk_per_contract(entry, stop, cfg.costs)
        if risk > CANARY_MAX_RISK * equity:
            return 0, f"1-contract risk {risk:.2f} USD > {CANARY_MAX_RISK:.0%} of equity {equity:.2f}"
        return 1, f"{stage.lower()}: 1 contract"
    if stage == "FULL":
        n = size_contracts(equity, entry, stop, direction, CONSERVATIVE, risk_mult, cfg.costs)
        if n == 0:
            return 0, "size_contracts returned 0"
        if full_cap is not None and n > full_cap:
            return full_cap, f"full: {n} capped at {full_cap}"
        return n, f"full: {n}"
    raise ValueError(f"unknown stage {stage}")
