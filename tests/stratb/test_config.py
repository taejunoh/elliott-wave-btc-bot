from __future__ import annotations

import dataclasses

import pytest

from ewbot.config import CostParams
from ewbot.stratb.config import (
    ASSET_SPECS,
    ASSETS,
    GRID_B,
    M15,
    RISK_B,
    BConfig,
    StratBParams,
    asset_costs,
    asset_spec,
    combo_of,
    combo_to_json,
    config_for,
    grid_combos,
    normalize_combo,
    params_for,
    scale_costs,
)


def test_the_grid_has_32_combos_in_key_order():
    combos = grid_combos()
    assert list(GRID_B) == ["zz_k", "zone", "tp1_r", "ext", "remainder"]
    assert len(combos) == 32 and len(set(combos)) == 32
    assert combos[0] == (2.0, (0.382, 0.618), 1.0, 1.272, "breakeven")
    assert combos[-1] == (3.0, (0.5, 0.786), 1.5, 1.618, "original")


def test_defaults_are_the_first_combo_and_the_spec_fixed_values():
    p = StratBParams()
    assert combo_of(p) == grid_combos()[0]
    assert (p.rsi_len, p.pivot_w, p.confluence_atr, p.stop_buf_atr, p.max_stop_atr1h) == (14, 3, 0.25, 0.2, 2.5)
    assert (p.confirm_hours, p.time_stop_hours, p.atr_len, p.confluence_lookback_hours) == (72, 48, 14, 720)
    assert M15 == 900


def test_normalize_accepts_json_lists_and_refuses_off_grid_values():
    assert normalize_combo([3, [0.5, 0.786], 1.5, 1.618, "original"]) == (3.0, (0.5, 0.786), 1.5, 1.618, "original")
    assert combo_to_json(grid_combos()[5]) == [2.0, [0.382, 0.618], 1.5, 1.272, "original"]
    for bad in ([2.5, [0.382, 0.618], 1.0, 1.272, "breakeven"], [2.0, [0.382, 0.618], 1.0, 1.272, "trail"],
                [2.0, [0.382, 0.618], 1.0, 1.272], [2.0, 0.5, 1.0, 1.272, "breakeven"]):
        with pytest.raises(ValueError):
            normalize_combo(bad)


def test_params_round_trip_every_combo():
    for c in grid_combos():
        assert combo_of(params_for(c)) == c


def test_run_config_uses_the_spec_risk_and_strategy_a_breakers_costs_and_equity():
    cfg = config_for(grid_combos()[0])
    assert cfg.risk == RISK_B and (RISK_B.risk_pct, RISK_B.max_leverage) == (0.005, 3.0)
    assert (cfg.breakers.daily_loss, cfg.breakers.total_dd, cfg.breakers.enabled) == (0.03, 0.15, True)
    assert cfg.costs == CostParams() and cfg.initial_equity == 10_000.0 and cfg.tp1_frac == 0.5
    assert cfg.assets == ASSET_SPECS and cfg.max_total_leverage == 3.0
    assert config_for(grid_combos()[0], breakers_enabled=False).breakers.enabled is False
    assert BConfig().params == StratBParams()


def test_btc_and_eth_trade_their_own_contracts_with_strategy_a_costs():
    assert ASSETS == ("BTC", "ETH")
    btc, eth = (asset_spec(BConfig(), a) for a in ASSETS)
    assert (btc.product, btc.future, btc.contract_size, btc.tick) == ("BTC-USD", "BIP-20DEC30-CDE", 0.01, 5.0)
    assert (eth.product, eth.future, eth.contract_size, eth.tick) == ("ETH-USD", "ETP-20DEC30-CDE", 0.1, 0.5)
    assert asset_costs(BConfig(), "BTC") == CostParams()
    assert asset_costs(BConfig(), "ETH") == dataclasses.replace(CostParams(), contract_size=0.1)
    with pytest.raises(KeyError):
        asset_spec(BConfig(), "SOL")


def test_scale_costs_scales_fees_and_slippage_only():
    c = scale_costs(CostParams(), 1.5)
    base = CostParams()
    assert c.fee_rate == pytest.approx(base.fee_rate * 1.5) and c.slippage == pytest.approx(base.slippage * 1.5)
    assert c.fee_min_per_contract == pytest.approx(base.fee_min_per_contract * 1.5)
    assert (c.funding_apr_long, c.contract_size, c.apply_min_fee) == (base.funding_apr_long, base.contract_size, base.apply_min_fee)


def test_invalid_params_are_refused():
    with pytest.raises(ValueError):
        StratBParams(remainder="trail")
    with pytest.raises(ValueError):
        StratBParams(zone=(0.7, 0.5))
