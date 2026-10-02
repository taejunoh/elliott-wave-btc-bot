from __future__ import annotations

from ewbot.config import AGGRESSIVE, CONSERVATIVE, CostParams, RiskProfile
from ewbot.risk.sizing import size_contracts

C = CostParams()                     # slippage 0.05%, 0.01 BTC per contract
NO_SLIP = CostParams(slippage=0.0)


def test_risk_one_percent_long_example() -> None:
    # risk$ = 10_000 * 1% = 100; distance = 1_000 + 0.0005 * (50_000 + 49_000) = 1_049.5
    # qty = 100 / 1_049.5 = 0.09528 BTC = 9.53 contracts -> 9
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, C) == 9


def test_risk_one_percent_short_example() -> None:
    # distance = 1_000 + 0.0005 * (50_000 + 51_000) = 1_050.5 -> 0.09519 BTC -> 9 contracts
    assert size_contracts(10_000.0, 50_000.0, 51_000.0, -1, CONSERVATIVE, 1.0, C) == 9


def test_without_slippage_risk_is_exact() -> None:
    # 100 / 1_000 = 0.1 BTC = 10 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, NO_SLIP) == 10


def test_risk_mult_half() -> None:
    # 50 / 1_049.5 = 0.04764 BTC -> 4 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 0.5, C) == 4


def test_leverage_cap_limits_notional() -> None:
    # tight stop: 100 / 100 = 1 BTC = 50_000 notional > 3x * 10_000 -> capped to 0.6 BTC = 60 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_900.0, +1, CONSERVATIVE, 1.0, NO_SLIP) == 60
    # aggressive: 3% risk -> 3 BTC, capped at 10x -> 2 BTC = 200 contracts
    assert size_contracts(10_000.0, 50_000.0, 49_900.0, +1, AGGRESSIVE, 1.0, NO_SLIP) == 200


def test_below_one_contract_skips() -> None:
    # 10 / 1_049.5 = 0.0095 BTC = 0.95 contracts -> 0
    assert size_contracts(1_000.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, C) == 0


def test_stop_on_wrong_side_or_bad_inputs_skip() -> None:
    assert size_contracts(10_000.0, 50_000.0, 51_000.0, +1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, -1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(10_000.0, 50_000.0, 50_000.0, +1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(0.0, 50_000.0, 49_000.0, +1, CONSERVATIVE, 1.0, C) == 0
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, 0, CONSERVATIVE, 1.0, C) == 0


def test_liquidation_inside_stop_rejects() -> None:
    # 200% "risk" with 100x cap: qty = 20_000 / 1_000 = 20 BTC (cap 20 BTC) -> lev 100x
    # liq = 50_000 * (1 - 1/100) = 49_500, which is above the 49_000 stop -> reject
    degenerate = RiskProfile("degenerate", 2.0, 100.0)
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, degenerate, 1.0, NO_SLIP) == 0
    # short mirror: liq = 50_500 < stop 51_000 -> reject
    assert size_contracts(10_000.0, 50_000.0, 51_000.0, -1, degenerate, 1.0, NO_SLIP) == 0


def test_liquidation_beyond_stop_accepts() -> None:
    # 50% risk: qty = 5 BTC = 500 contracts, lev 25x, liq = 48_000 < stop 49_000 -> accept
    wide = RiskProfile("wide", 0.5, 100.0)
    assert size_contracts(10_000.0, 50_000.0, 49_000.0, +1, wide, 1.0, NO_SLIP) == 500
