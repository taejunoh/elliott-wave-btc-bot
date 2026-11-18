"""Spot -> futures price mapping, tick rounding, per-stage sizing and margin projection
(Plan 3 spec 3.3, 4.3, 4.5).

Signals, stops and targets are computed on spot BTC-USD candles (the paper runtime's input); orders go
to the BIP future. A level is mapped with the basis ratio at order time (futures mid / spot mid; one more than
BASIS_BAND = 2% from 1 is a stale or one-sided book, not a basis: refused) and put on the $5 tick conservatively: long levels round DOWN, short levels round UP. For a stop that is
the spec's rule (never triggered earlier than the engine's stop); for a take-profit it moves the
level toward the entry, so the live limit is reached no later than the engine's strict-cross TP.

Sizing: SHADOW and CANARY trade 1 contract and skip a signal whose 1-contract risk exceeds 15% of the
account (CANARY_MAX_RISK); FULL calls the frozen risk.sizing.size_contracts with the real equity and FULL_RISK
(10% risk per trade, leverage cap 5x; user decision 2026-11-05; no strategy file is changed, and the paper replay
stays conservative), with no contract cap (the 3 contracts for the first 30 clean days were removed by the user's
decision of 2026-11-05; only a manual intervention still in force holds it to 3: live/stage.full_cap). Margin is always projected at the OVERNIGHT rate (the stricter one; it applies from 16:00
New York); the exchange's overnight rates (about 4.8x long, 3.9x short) are below FULL_RISK's 5x, so the 70% entry
limit (MARGIN_ENTRY_MAX) binds first, on purpose. In FULL an entry above it is reduced to the most contracts the limit holds
(runtime._entry_size, max_contracts_for_margin; user decision 2026-11-05: the real risk is then under 10%), refused only
when not even one fits; SHADOW and CANARY (1 contract) refuse.

FULL's one-contract floor (user decision 2026-11-18): the 10% risk of a small account is often less than one contract's
risk (0.01 BTC, whole contracts), and the frozen sizing then gives 0 - at 500 USD every signal whose stop is more than
about 6% away. FULL then enters ONE contract when that contract's risk (`risk_per_contract`, CANARY's check) is at most
FULL_MIN_ONE_MAX_RISK (20%) of the account and its notional is within FULL_RISK's 5x; above either it is skipped as
before. The signal's risk_mult does not scale the floor (CANARY's check does not either). The real risk of such an entry
is above 10% - up to 20% - and the runtime says so (`ENTRY_FLOOR`, `[최소 1계약 진입]`); the margin limit applies to it
like to any entry. The sizing's liquidation check cannot bind here: one contract's isolated liquidation distance is
equity / contract_size, five times the widest stop the 20% allows."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ewbot.config import BotConfig, CostParams, RiskProfile
from ewbot.live.exchange import STOP_LIMIT_OFFSET, ProductSpec
from ewbot.risk.sizing import size_contracts

CANARY_MAX_RISK = 0.15  # user decision 2026-10-19 (Plan 3 spec amendment; spec 4.3 had 2%)
FULL_RISK = RiskProfile("full", 0.10, 5.0)  # user decision 2026-11-05 (Plan 3 spec amendment; it was CONSERVATIVE 1% / 3x)
# user decision 2026-11-18: when the 10% risk sizes less than one contract, FULL enters one contract whose risk is at most
# this share of the account (it was skipped); the reason stage_contracts gives for such an entry is FULL_FLOOR
FULL_MIN_ONE_MAX_RISK = 0.20
FULL_FLOOR = "full: 1 (floor)"
FULL_CAP_CONTRACTS = 3  # only while a manual intervention is still in force in FULL (stage.full_cap)
MARGIN_ENTRY_MAX = 0.70  # spec 4.5: an entry whose overnight projection exceeds 70% is refused (FULL: reduced to fit, 2026-11-05)
MARGIN_WARN = 0.80
MARGIN_REDUCE = 0.90
# FULL sizes by one balance read, while the breakers need two: a read this far from the last observation is read again
# (runtime._sizing_equity), and three reads must agree this closely to size an entry outside the band
EQUITY_SIZING_BAND = 0.20
EQUITY_READS_AGREE = 0.01
BASIS_BAND = 0.02  # a futures mid this far from the spot mid (either way) is no basis: a stale or one-sided book
_EPS = 1e-9


class BasisOutOfBand(ValueError):
    """The futures and spot mids are more than BASIS_BAND apart: no level is mapped and no entry sized with them."""


def to_tick(price: float, tick: float, direction: int) -> float:
    """Long (+1): floor to the tick grid; short (-1): ceil."""
    if tick <= 0 or direction not in (1, -1):
        raise ValueError(f"bad tick {tick} / direction {direction}")
    q = price / tick
    n = math.floor(q + _EPS) if direction > 0 else math.ceil(q - _EPS)
    return float(n) * tick


def basis_ratio(fut_mid: float, spot_mid: float) -> float:
    """futures mid / spot mid; ValueError for a mid that is not a finite number above 0, BasisOutOfBand for a ratio
    more than BASIS_BAND from 1."""
    if not (math.isfinite(fut_mid) and math.isfinite(spot_mid)) or fut_mid <= 0 or spot_mid <= 0:
        raise ValueError(f"bad mids {fut_mid} / {spot_mid}")
    ratio = fut_mid / spot_mid
    if abs(ratio - 1.0) > BASIS_BAND:
        raise BasisOutOfBand(f"futures mid {fut_mid} / spot mid {spot_mid} = {ratio:.4f}: more than {BASIS_BAND:.0%} "
                             "apart (a stale or one-sided book)")
    return ratio


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
    if not (math.isfinite(price) and math.isfinite(equity)) or price <= 0 or equity <= 0:
        return math.inf
    return contracts * product.contract_size * price * product.margin_rate(side, overnight) / equity


def max_contracts_for_margin(price: float, side: int, product: ProductSpec, equity: float,
                             limit: float = MARGIN_ENTRY_MAX) -> int:
    per = product.contract_size * price * product.margin_rate(side, True)
    if not (math.isfinite(per) and math.isfinite(equity)) or per <= 0 or equity <= 0:
        return 0
    return max(0, math.floor(limit * equity / per + _EPS))


def _full_floor(equity: float, entry: float, stop: float, risk_mult: float, cfg: BotConfig) -> tuple[int, str]:
    """FULL, the frozen sizing gave 0: (1, FULL_FLOOR) when one contract is within FULL_RISK's leverage cap and risks at
    most FULL_MIN_ONE_MAX_RISK of the account, else (0, why). `risk_mult` only has to be above 0."""
    if risk_mult <= 0:
        return 0, "size_contracts returned 0"
    if cfg.costs.contract_size * entry > equity * FULL_RISK.max_leverage + _EPS:
        return 0, (f"1 contract's notional {cfg.costs.contract_size * entry:.2f} USD > {FULL_RISK.max_leverage:g}x "
                   f"of equity {equity:.2f}")
    risk = risk_per_contract(entry, stop, cfg.costs)
    if risk > FULL_MIN_ONE_MAX_RISK * equity:
        return 0, f"1-contract risk {risk:.2f} USD > {FULL_MIN_ONE_MAX_RISK:.0%} of equity {equity:.2f}"
    return 1, FULL_FLOOR


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
    non-finite price, equity or `risk_mult`, a bad direction, or a negative `full_cap`."""
    if full_cap is not None and full_cap < 0:
        raise ValueError(f"bad full_cap {full_cap}")
    if direction not in (1, -1) or not all(map(math.isfinite, (entry, stop, equity, risk_mult))) or equity <= 0:
        return 0, "bad sizing inputs"
    if direction * (entry - stop) <= 0:
        return 0, "stop on the wrong side of the entry"
    if stage in ("SHADOW", "CANARY"):
        risk = risk_per_contract(entry, stop, cfg.costs)
        if risk > CANARY_MAX_RISK * equity:
            return 0, f"1-contract risk {risk:.2f} USD > {CANARY_MAX_RISK:.0%} of equity {equity:.2f}"
        return 1, f"{stage.lower()}: 1 contract"
    if stage == "FULL":
        n = size_contracts(equity, entry, stop, direction, FULL_RISK, risk_mult, cfg.costs)
        if n == 0:
            return _full_floor(equity, entry, stop, risk_mult, cfg)
        if full_cap is not None and n > full_cap:
            return full_cap, f"full: {n} capped at {full_cap}"
        return n, f"full: {n}"
    raise ValueError(f"unknown stage {stage}")
