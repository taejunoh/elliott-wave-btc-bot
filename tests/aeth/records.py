"""A-ETH records for tests: a stage-1 record as write_stage1 writes it (the fields create_lock checks) and a
completed holdout-aeth.lock as run_holdout_aeth leaves it (the fields the paper runtime reads), both for the
synthetic bars of tests.aeth.helpers and the paper tests' params."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ewbot.aeth.config import FUTURE, PRODUCT, config_json
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.lockfile import LOCK_KIND, LOCK_VERSION, lock_digest
from ewbot.aeth.stage1 import STAGE1_KIND, data_record, stage1_verdict
from ewbot.backtest.holdout import data_fingerprint
from ewbot.data.candles import fill_gaps
from ewbot.paper.fingerprint import current_fingerprint
from ewbot.types import H1
from tests.aeth.helpers import HOLDOUT_TRADES, HS, a_lock_sha
from tests.paper.helpers import BOUNDARY, PARAMS

PASSING_HEADLINE = {
    "A": {"total_return": 0.2, "cagr": 0.03, "max_dd": 0.08, "sharpe": 0.9, "calmar": 0.4, "win_rate": 0.4,
          "avg_r": 0.3, "n_trades": 70},
    "C": {"total_return": 0.05, "cagr": 0.01, "max_dd": 0.1, "sharpe": 0.2, "calmar": 0.1, "win_rate": 0.4,
          "avg_r": 0.1, "n_trades": 80},
    "RANDOM": {"runs": 100, "n_signals": 70, "sharpe": {"mean": -0.1, "p05": -1.0, "p50": -0.1, "p95": 0.6},
               "calmar": {"mean": 0.0, "p05": -0.3, "p50": 0.0, "p95": 0.5}},
}


def stage1_record(raw_pre2024: pd.DataFrame, a_lock: Path, *, headline: dict[str, Any] | None = None,
                  **provenance: Any) -> dict[str, Any]:
    """A stage-1 record with a hand-set headline (default: one that passes, criteria computed from it) and
    provenance computed from the bars and this code (each field overridable)."""
    head = json.loads(json.dumps(PASSING_HEADLINE if headline is None else headline))
    prov = {"a_lock_sha256": a_lock_sha(a_lock), "a_fingerprint": current_fingerprint(),
            "aeth_sha256": aeth_fingerprint(), "code_sha256": "c0de" * 16, "data": data_record(raw_pre2024)}
    prov.update(provenance)
    return {"kind": STAGE1_KIND, "version": 1, "product": PRODUCT, "future": FUTURE, "params": list(PARAMS),
            "config": config_json(PARAMS), "headline": head, "criteria": stage1_verdict(head), "provenance": prov}


def aeth_lock(df: pd.DataFrame, a_lock: Path, *, passed: bool = True, n_trades: int = HOLDOUT_TRADES,
              last_bar: int = BOUNDARY, **overrides: Any) -> dict[str, Any]:
    """A completed A-ETH lock for the bars `df` (crossing 2024): first_run says the holdout ran with `n_trades` A
    trades and passed (or not). sha256 is the real digest of the bound fields after `overrides`."""
    filled = fill_gaps(df)
    filled = filled[filled["ts"] <= last_bar].reset_index(drop=True)
    lock: dict[str, Any] = {
        "kind": LOCK_KIND, "version": LOCK_VERSION, "product": PRODUCT, "future": FUTURE, "params": list(PARAMS),
        "a_lock_sha256": a_lock_sha(a_lock), "config": config_json(PARAMS), "holdout_start": HS,
        "last_bar_ts": last_bar, "random_runs": 2, "a_fingerprint": current_fingerprint(),
        "aeth_sha256": aeth_fingerprint(), "code_sha256": "c0de" * 16, "pre2024": data_record(df),
        "stage1_sha256": "5" * 64,
    }
    lock.update(overrides)
    lock.update(sha256=lock_digest(lock), git_head="f" * 40, git_dirty=False, attempts=[], first_run={
        "start_ts": HS, "end_ts": last_bar + H1, "last_bar_ts": last_bar,
        "data": {"start_ts": HS, "last_bar_ts": last_bar, "sha256": data_fingerprint(filled, HS, last_bar)},
        "verdict": {"c1_a_beats_c_bh_random_mean": passed, "c2_max_dd_le_15pct": True, "n_trades": n_trades,
                    "passed": passed},
    })
    return lock
