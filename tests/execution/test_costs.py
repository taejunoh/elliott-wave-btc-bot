from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.execution.costs import fee, funding_per_hour, slip

C = CostParams()  # fee 0.05%, slippage 0.05%, funding 10% APR, 0.01 BTC/contract, no min fee


def test_fee_is_rate_times_notional() -> None:
    # 10 contracts * 0.01 BTC * 50_000 = 5_000 USD notional; 0.05% -> 2.50
    assert fee(50_000.0, 10, C) == pytest.approx(2.5)


def test_fee_zero_contracts_is_zero() -> None:
    assert fee(50_000.0, 0, C) == 0.0


def test_fee_min_per_contract_applies_only_when_enabled() -> None:
    cheap = 1_000.0  # 0.0005 * 1_000 * 0.01 = 0.005 USD per contract < 0.15 minimum
    assert fee(cheap, 10, C) == pytest.approx(0.05)
    with_min = CostParams(apply_min_fee=True)
    assert fee(cheap, 10, with_min) == pytest.approx(1.5)
    # above the minimum the proportional fee wins: 0.0005 * 50_000 * 0.01 = 0.25 > 0.15
    assert fee(50_000.0, 10, with_min) == pytest.approx(2.5)


def test_slip_buy_pays_up_sell_receives_less() -> None:
    assert slip(50_000.0, +1, C) == pytest.approx(50_025.0)
    assert slip(50_000.0, -1, C) == pytest.approx(49_975.0)


def test_slip_rejects_bad_side() -> None:
    with pytest.raises(ValueError):
        slip(50_000.0, 0, C)


def test_funding_long_pays_short_receives() -> None:
    # 5_000 USD notional * 10% / 8760 h
    hourly = 5_000.0 * 0.10 / 8760.0
    assert funding_per_hour(50_000.0, 10, +1, C) == pytest.approx(hourly)
    assert funding_per_hour(50_000.0, 10, -1, C) == pytest.approx(-hourly)
    assert hourly == pytest.approx(0.0570776, rel=1e-6)
