from __future__ import annotations

import math

import pytest

from ewbot.config import BotConfig
from ewbot.live.pricing import (
    basis_ratio,
    map_level,
    map_levels,
    margin_ratio,
    max_contracts_for_margin,
    risk_per_contract,
    stage_contracts,
    stop_gapped,
    stop_limit,
    to_tick,
)
from ewbot.live.sim import BIP_SPEC
from ewbot.risk.sizing import size_contracts

CFG = BotConfig()


@pytest.mark.parametrize(
    "price,direction,expected",
    [(84667.3, 1, 84665.0), (84667.3, -1, 84670.0), (84665.0, 1, 84665.0), (84665.0, -1, 84665.0),
     (84669.99, 1, 84665.0), (84665.01, -1, 84670.0),
     (84669.9999999999, 1, 84670.0), (84665.0000000001, -1, 84665.0)],  # float noise stays on the grid
)
def test_to_tick_rounds_longs_down_and_shorts_up(price, direction, expected):
    assert to_tick(price, 5.0, direction) == expected


def test_to_tick_rejects_bad_inputs():
    with pytest.raises(ValueError):
        to_tick(100.0, 0.0, 1)
    with pytest.raises(ValueError):
        to_tick(100.0, 5.0, 0)


def test_mapping_uses_the_basis_ratio_conservatively():
    ratio = basis_ratio(84667.5, 84671.265)
    assert ratio == pytest.approx(0.99995553, rel=1e-8)
    long_ = map_levels(1, stop=82_000.0, tp1=86_000.0, tp2=88_000.0, ratio=ratio, tick=5.0)
    assert (long_.stop, long_.tp1, long_.tp2) == (81995.0, 85995.0, 87995.0)  # all rounded down
    short = map_levels(-1, stop=86_000.0, tp1=82_000.0, tp2=None, ratio=ratio, tick=5.0)
    assert (short.stop, short.tp1, short.tp2) == (86000.0, 82000.0, None)  # rounded up
    assert map_level(100.0, 1.0, 5.0, -1) == 100.0
    with pytest.raises(ValueError):
        basis_ratio(0.0, 1.0)


def test_stop_limit_and_gap():
    assert stop_limit(80_000.0, 1) == pytest.approx(76_000.0)  # STOP_LIMIT_OFFSET 5%
    assert stop_limit(80_000.0, -1) == pytest.approx(84_000.0)
    assert not stop_gapped(76_100.0, 80_000.0, 1) and stop_gapped(75_900.0, 80_000.0, 1)
    assert not stop_gapped(83_900.0, 80_000.0, -1) and stop_gapped(84_100.0, 80_000.0, -1)


def test_risk_per_contract_matches_the_sizing_distance():
    r = risk_per_contract(60_000.0, 59_000.0, CFG.costs)
    assert r == pytest.approx((1000.0 + 0.0005 * 119_000.0) * 0.01)


def test_canary_and_shadow_trade_one_contract_unless_the_risk_exceeds_two_percent():
    kw = dict(entry=60_000.0, stop=59_000.0, direction=1, risk_mult=1.0, cfg=CFG)
    assert stage_contracts("CANARY", equity=1_000.0, **kw) == (1, "canary: 1 contract")
    assert stage_contracts("SHADOW", equity=1_000.0, **kw)[0] == 1
    n, why = stage_contracts("CANARY", equity=500.0, **kw)  # risk 10.6 > 10.0
    assert n == 0 and "2%" in why
    assert stage_contracts("CANARY", equity=1_000.0, entry=60_000.0, stop=61_000.0, direction=1,
                           risk_mult=1.0, cfg=CFG)[0] == 0


def test_full_uses_the_frozen_sizing_with_the_conservative_profile_and_the_cap():
    kw = dict(entry=60_000.0, stop=59_400.0, direction=1, risk_mult=1.0, cfg=CFG)
    expected = size_contracts(20_000.0, 60_000.0, 59_400.0, 1, CFG.risk, 1.0, CFG.costs)
    assert expected > 3
    assert stage_contracts("FULL", equity=20_000.0, **kw) == (expected, f"full: {expected}")
    assert stage_contracts("FULL", equity=20_000.0, full_cap=3, **kw)[0] == 3
    assert stage_contracts("FULL", equity=10.0, **kw)[0] == 0
    with pytest.raises(ValueError):
        stage_contracts("PAPER", equity=1.0, **kw)


def test_margin_projection_uses_the_overnight_rate():
    assert margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0) == pytest.approx(840.0 * 0.21025 / 1_000.0)
    assert margin_ratio(1, 84_000.0, -1, BIP_SPEC, 1_000.0) == pytest.approx(840.0 * 0.256625 / 1_000.0)
    assert margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0, overnight=False) < margin_ratio(1, 84_000.0, 1, BIP_SPEC, 1_000.0)
    assert margin_ratio(0, 84_000.0, 1, BIP_SPEC, 0.0) == 0.0 and math.isinf(margin_ratio(1, 1.0, 1, BIP_SPEC, 0.0))
    n = max_contracts_for_margin(84_000.0, 1, BIP_SPEC, 1_000.0)
    assert n == 3 and margin_ratio(n, 84_000.0, 1, BIP_SPEC, 1_000.0) <= 0.70 < margin_ratio(n + 1, 84_000.0, 1, BIP_SPEC, 1_000.0)
