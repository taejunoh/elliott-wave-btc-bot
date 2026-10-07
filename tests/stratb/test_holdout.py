from __future__ import annotations

import json

import pandas as pd
import pytest

from ewbot.backtest.metrics import Metrics
from ewbot.stratb.data15 import REPLAY_ANCHOR, HoldoutGuardError, cache_paths, load_15m, load_caches_15m
from ewbot.stratb.fingerprint import stratb_fingerprint
from ewbot.stratb.holdout import (
    HoldoutBError,
    create_lock,
    data_fingerprint_b,
    evaluate_criteria_b,
    lock_digest_b,
    read_lock,
    run_holdout_b,
)
from tests.paper.helpers import git
from tests.stratb.helpers import save_world, world_dfs

HS = 1_704_067_200
NOW = HS + 150 * 86_400 + 1_234
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    cache = tmp_path / "cache"
    cache.mkdir()
    save_world(cache, world_dfs(REPLAY_ANCHOR))  # a BTC long and an ETH short per 232 bars, to 2024-08
    return repo, cache, repo / "holdout-b.lock"


def _lock(world, **kw):
    _, cache, lock_path = world
    args = dict(params_source="test", now_ts=NOW, lock_path=lock_path, random_runs=4)
    args.update(kw)
    return create_lock(COMBO, load_caches_15m(cache), **args)


def _commit(world):
    repo, _, lock_path = world
    git(repo, "add", lock_path.name)
    git(repo, "commit", "-q", "-m", "lock")


def test_2024_bars_are_unreadable_until_the_lock_exists(world):
    _, cache, lock_path = world
    for p in cache_paths(cache).values():
        with pytest.raises(HoldoutGuardError):
            load_15m(p, include_holdout=True, lock_path=lock_path)
    _lock(world)
    for p in cache_paths(cache).values():
        assert int(load_15m(p, include_holdout=True, lock_path=lock_path)["ts"].max()) > HS


def test_the_lock_binds_params_code_data_and_window_and_is_written_once(world):
    lock = _lock(world)
    assert lock == read_lock(world[2])
    assert lock["params"] == COMBO and lock["sha256"] == lock_digest_b(lock)
    assert lock["last_bar_ts"] == NOW // 3600 * 3600 - 900 and (lock["holdout_start"], lock["replay_anchor"]) == (HS, REPLAY_ANCHOR)
    assert lock["code_sha256"] == stratb_fingerprint() and lock["random_runs"] == 4 and lock["first_run"] is None
    assert [(a, w["last_ts"]) for a, w in lock["wf_data"].items()] == [("BTC", HS - 900), ("ETH", HS - 900)]
    assert lock["wf_data"]["BTC"]["sha256"] != lock["wf_data"]["ETH"]["sha256"]
    assert lock["config"]["risk"]["risk_pct"] == 0.005 and lock["config"]["max_total_leverage"] == 3.0
    assert [a["future"] for a in lock["config"]["assets"]] == ["BIP-20DEC30-CDE", "ETP-20DEC30-CDE"]
    with pytest.raises(HoldoutBError, match="written once"):
        _lock(world)


def test_the_lock_refuses_off_grid_params_2024_or_one_asset_data_and_an_early_clock(world):
    _, cache, lock_path = world
    wf = load_caches_15m(cache)
    with pytest.raises(HoldoutBError):
        create_lock([2.5, [0.382, 0.618], 1.0, 1.272, "breakeven"], wf, params_source="t", now_ts=NOW, lock_path=lock_path)
    full = {a: pd.read_parquet(p) for a, p in cache_paths(cache).items()}
    with pytest.raises(HoldoutBError, match="before 2024"):
        create_lock(COMBO, full, params_source="t", now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutBError, match="every asset"):
        create_lock(COMBO, {"BTC": wf["BTC"]}, params_source="t", now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutBError, match="after 2024"):
        create_lock(COMBO, wf, params_source="t", now_ts=HS + 600, lock_path=lock_path)
    assert not lock_path.exists()


def test_an_edited_bound_field_breaks_the_lock(world):
    _lock(world)
    lock = json.loads(world[2].read_text())
    lock["params"][2] = 1.5
    world[2].write_text(json.dumps(lock))
    with pytest.raises(HoldoutBError, match="digest"):
        read_lock(world[2])


def test_the_holdout_needs_confirm_and_the_committed_lock(world):
    _, cache, lock_path = world
    _lock(world)
    with pytest.raises(HoldoutBError, match="single-use"):
        run_holdout_b(confirm=False, cache_dir=cache, lock_path=lock_path)
    with pytest.raises(HoldoutBError, match="not committed"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_a_b_code_change_or_other_walk_forward_bars_are_refused(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    with pytest.raises(HoldoutBError, match="code changed"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path, fingerprint=lambda: "0" * 64)
    eth = cache_paths(cache)["ETH"]
    df = pd.read_parquet(eth)
    df.loc[5, "close"] += 1.0
    df.to_parquet(eth, index=False)
    with pytest.raises(HoldoutBError, match=r"\['ETH'\] 15m bars before 2024-01-01 differ"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_the_holdout_runs_once_and_records_the_criteria(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    out = run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)
    lock = read_lock(lock_path)
    fr = lock["first_run"]
    assert fr == out["first_run"] and fr["end_ts"] == lock["last_bar_ts"] + 900
    assert fr["data"]["start_ts"] == REPLAY_ANCHOR and len(lock["attempts"]) == 1 and lock["attempts"][0]["completed"]
    win = {a: df[df["ts"] <= lock["last_bar_ts"]] for a, df in world_dfs(REPLAY_ANCHOR).items()}
    assert fr["data"]["sha256"] == data_fingerprint_b(win, REPLAY_ANCHOR, lock["last_bar_ts"])
    crit = fr["criteria"]
    assert fr["metrics"]["B"]["n_trades"] == crit["n_trades"] == 124 and fr["random"]["runs"] == 4
    assert fr["n_trades_by_asset"] == fr["random"]["n_signals"] == {"BTC": 62, "ETH": 62}
    assert crit["passed"] is True  # the synthetic setups win every time: all four criteria hold
    assert all(HS <= t.entry_ts <= lock["last_bar_ts"] + 900 for t in out["result"].trades)
    with pytest.raises(HoldoutBError, match="already run once"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_an_unfinished_attempt_must_have_seen_the_same_bars(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    lock = json.loads(lock_path.read_text())
    lock["attempts"].append({"started": "x", "data_sha256": "f" * 64})
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(HoldoutBError, match="other holdout bars"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def _m(**kw):
    base = dict(total_return=0.2, cagr=0.1, max_dd=0.1, sharpe=2.0, sortino=2.0, calmar=1.0, win_rate=0.5,
                avg_r=0.3, n_trades=60, exposure=0.2)
    base.update(kw)
    return Metrics(**base)


def test_each_criterion_can_fail_the_holdout():
    rnd = {"runs": 1000, "dist": {"sharpe": {"p95": 1.5}}}
    ok = evaluate_criteria_b(_m(), _m(sharpe=1.0), rnd, _m(total_return=0.05))
    assert ok["passed"] and ok["random_sharpe_p95"] == 1.5
    assert not evaluate_criteria_b(_m(sharpe=1.4), _m(sharpe=1.0), rnd, _m())["passed"]  # below random p95
    assert not evaluate_criteria_b(_m(), _m(sharpe=2.5), rnd, _m())["passed"]  # below buy & hold
    assert not evaluate_criteria_b(_m(max_dd=0.151), _m(sharpe=1.0), rnd, _m())["c2_max_dd_le_15pct"]
    assert not evaluate_criteria_b(_m(n_trades=49), _m(sharpe=1.0), rnd, _m())["passed"]
    assert not evaluate_criteria_b(_m(), _m(sharpe=1.0), rnd, _m(total_return=0.0))["passed"]
    assert not evaluate_criteria_b(_m(), _m(sharpe=1.0), {"runs": 0, "dist": {}}, _m())["passed"]
