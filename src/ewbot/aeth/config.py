"""A-ETH run configuration (Plan 5a spec 5): strategy A exactly as frozen, on ETH-USD. Nothing here is tuned.

The params come from strategy A's committed holdout.lock (K4, Q_MIN, S_MIN), which must be A's real, completed
lock: its sha256 is lock_digest(BotConfig(), params, random_runs, code_sha256), the check A's own paper runtime
makes. The rest is holdout_config(BotConfig(), params) - A's conservative profile, A's circuit breakers, A's wave
and strategy defaults - with A's CostParams whose contract size is that of ETP-20DEC30-CDE (0.1 ETH). Fee 0.05%,
slippage 0.05% and long-pays funding of 10% a year stay A's; the 0.15 USD minimum fee per contract is not applied,
as in A (apply_min_fee=False)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH as A_LOCK_PATH
from ewbot.backtest.holdout import holdout_config, lock_digest
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import GRID, HOLDOUT_START, WF_START, grid_combos
from ewbot.config import BotConfig, CostParams
from ewbot.paper.fingerprint import PROJECT_ROOT

PRODUCT = "ETH-USD"  # Coinbase spot candles for the signals, like A's BTC-USD
FUTURE = "ETP-20DEC30-CDE"  # the modelled contract (only Plan 5c would trade it)
CONTRACT_SIZE = 0.1  # ETH per ETP contract
FROZEN_A_FINGERPRINT = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
DATA_START = utc_ts(WF_START)  # 2017-01-01: the first ETH bar fetched and the start of stage 1
SUB_START = utc_ts("2019-01-01")  # stage 1, information only: 2019-01-01..2023-12-31
RANDOM_RUNS = 100  # strategy A's random-entry control (compare() default, holdout.lock random_runs)
COST_INFO_MULT = 2.0  # stage 1, information only: A with fees and slippage x 2
LOCK_PATH = PROJECT_ROOT / "holdout-aeth.lock"
STAGE1_PATH = PROJECT_ROOT / "aeth-stage1.json"

Combo = tuple[float, ...]


class AethError(RuntimeError):
    """An A-ETH step is refused: the lock, the stage-1 record, the data or the code state is not as required."""


def eth_costs() -> CostParams:
    """Strategy A's CostParams with the ETP contract size; every other field is A's default."""
    return dataclasses.replace(CostParams(), contract_size=CONTRACT_SIZE)


def scale_trading_costs(c: CostParams, mult: float) -> CostParams:
    """Fee rate, minimum fee and slippage times `mult`; funding and contract size unchanged."""
    return dataclasses.replace(
        c, fee_rate=c.fee_rate * mult, fee_min_per_contract=c.fee_min_per_contract * mult, slippage=c.slippage * mult
    )


def frozen_a_params(a_lock_path: Path = A_LOCK_PATH) -> tuple[Combo, str]:
    """(params, sha256) of strategy A's completed holdout lock - read only. AethError unless its keys are A's grid
    keys, its params a grid combo, its holdout start A's and its sha256 lock_digest(BotConfig(), params,
    random_runs, code_sha256)."""
    p = Path(a_lock_path)
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
        params = tuple(float(x) for x in lock["params"])
        keys = list(lock["keys"])
        sha = str(lock["sha256"])
        holdout_start = int(lock["holdout_start"])
        digest = lock_digest(BotConfig(), params, int(lock["random_runs"]), str(lock["code_sha256"]))
        int(lock["first_run"]["criteria"]["n_trades"]["A"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AethError(f"{p} is not strategy A's completed holdout lock: {exc!r}") from exc
    if keys != list(GRID) or params not in grid_combos() or holdout_start != HOLDOUT_START:
        raise AethError(f"{p}: keys {keys}, params {list(params)} or holdout start {holdout_start} are not "
                        "strategy A's")
    if digest != sha:
        raise AethError(f"{p}: sha256 {sha[:12]} is not the digest of its config, params and code ({digest[:12]})")
    return params, sha


def aeth_config(params: Combo) -> BotConfig:
    """The full A-ETH run config: strategy A's holdout config for `params`, with the ETH costs."""
    return dataclasses.replace(holdout_config(BotConfig(), tuple(float(p) for p in params)), costs=eth_costs())


def config_json(params: Combo) -> dict[str, Any]:
    """aeth_config(params) as JSON (what the stage-1 record and the lock store and compare)."""
    return json.loads(json.dumps(dataclasses.asdict(aeth_config(params))))
