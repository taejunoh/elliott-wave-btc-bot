from __future__ import annotations

import dataclasses
import json

import pytest

from ewbot.aeth.config import (
    A_LOCK_PATH,
    CONTRACT_SIZE,
    DATA_START,
    FROZEN_A_FINGERPRINT,
    FUTURE,
    LOCK_PATH,
    PRODUCT,
    RANDOM_RUNS,
    STAGE1_PATH,
    SUB_START,
    AethError,
    aeth_config,
    config_json,
    eth_costs,
    frozen_a_params,
    scale_trading_costs,
)
from ewbot.backtest.holdout import holdout_config
from ewbot.config import CONSERVATIVE, BotConfig, BreakerParams, CostParams
from ewbot.paper.fingerprint import PROJECT_ROOT
from tests.paper.helpers import make_lock, synth_frame


def test_product_contract_windows_and_paths():
    assert (PRODUCT, FUTURE, CONTRACT_SIZE) == ("ETH-USD", "ETP-20DEC30-CDE", 0.1)
    assert (DATA_START, SUB_START, RANDOM_RUNS) == (1_483_228_800, 1_546_300_800, 100)  # 2017-01-01, 2019-01-01
    assert FROZEN_A_FINGERPRINT.startswith("e0435223")
    assert (LOCK_PATH, STAGE1_PATH) == (PROJECT_ROOT / "holdout-aeth.lock", PROJECT_ROOT / "aeth-stage1.json")
    assert A_LOCK_PATH == PROJECT_ROOT / "holdout.lock"


def test_eth_costs_are_a_s_costs_with_the_etp_contract_size():
    assert eth_costs() == dataclasses.replace(CostParams(), contract_size=0.1)
    assert (eth_costs().fee_rate, eth_costs().slippage, eth_costs().funding_apr_long) == (0.0005, 0.0005, 0.10)
    assert eth_costs().apply_min_fee is False and eth_costs().fee_min_per_contract == 0.15


def test_scale_trading_costs_doubles_fees_and_slippage_only():
    c = scale_trading_costs(eth_costs(), 2.0)
    assert (c.fee_rate, c.fee_min_per_contract, c.slippage) == (0.001, 0.3, 0.001)
    assert (c.funding_apr_long, c.contract_size, c.apply_min_fee) == (0.10, 0.1, False)


def test_the_frozen_params_are_those_of_strategy_a_s_committed_holdout_lock():
    """Reads (never writes) the real holdout.lock: A's walk-forward pick K4=2.0, Q_MIN=0.7, S_MIN=0.5."""
    params, sha = frozen_a_params()
    assert params == (2.0, 0.7, 0.5)
    assert sha == json.loads(A_LOCK_PATH.read_text(encoding="utf-8"))["sha256"]


def test_aeth_config_is_a_s_holdout_config_with_eth_costs():
    params = (2.0, 0.7, 0.5)
    cfg = aeth_config(params)
    assert cfg == dataclasses.replace(holdout_config(BotConfig(), params), costs=eth_costs())
    assert (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN, cfg.strategy.mode) == (2.0, 0.7, 0.5, "A")
    assert cfg.risk == CONSERVATIVE and cfg.breakers == BreakerParams() and cfg.breakers.enabled
    assert cfg.initial_equity == 10_000.0 and cfg.costs.contract_size == 0.1
    assert config_json(params)["costs"]["contract_size"] == 0.1
    assert config_json(params)["strategy"]["setups_enabled"] == ["W3", "W5", "W5_REV", "C_END"]


@pytest.mark.parametrize("case, why", [
    ("missing", "not strategy A's completed"), ("digest", "not the digest"), ("params", "are not strategy A's"),
    ("keys", "are not strategy A's"), ("start", "are not strategy A's"), ("not_run", "not strategy A's completed"),
])
def test_frozen_params_refuse_anything_but_a_s_completed_lock(tmp_path, case, why):
    lock = make_lock(synth_frame())
    if case == "digest":
        lock["sha256"] = "0" * 64
    if case == "params":
        lock = make_lock(synth_frame(), params=(2.2, 0.5, 0.0))
    if case == "keys":
        lock["keys"] = ["wave.K4", "strategy.Q_MIN"]
    if case == "start":
        lock["holdout_start"] = 0
    if case == "not_run":
        lock.pop("first_run")
    path = tmp_path / "holdout.lock"
    if case != "missing":
        path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(AethError, match=why):
        frozen_a_params(path)
