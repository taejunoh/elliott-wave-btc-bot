from __future__ import annotations

import json

import pandas as pd
import pytest

from ewbot.backtest import holdout
from ewbot.backtest.holdout import (
    HOLDOUT_START,
    HoldoutError,
    evaluate_criteria,
    params_digest,
    run_holdout,
    trade_count_lines,
)
from ewbot.config import BotConfig

H = 3600
PARAMS = (3.0, 0.6, 0.25)


def _m(sharpe, calmar, dd=0.1, n=60):
    return {"sharpe": sharpe, "calmar": calmar, "max_dd": dd, "n_trades": n}


def _fake_compare_factory(calls):
    def fake(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1):
        calls.append((cfg, start_ts, end_ts, random_runs))
        return {
            "comparison": {
                "A": _m(1.0, 2.0),
                "C": _m(0.5, 1.0),
                "HYBRID": _m(0.4, 0.5),
                "BH": _m(0.6, 1.5),
                "EMA": _m(0.2, 0.3),
            },
            "random": {"dist": {"sharpe": {"mean": 0.1}, "calmar": {"mean": 0.2}}},
        }

    return fake


@pytest.fixture
def df():
    ts = [HOLDOUT_START - 3 * H + i * H for i in range(10)]
    return pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})


def test_requires_confirm_and_writes_nothing(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match="confirm"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=False)
    assert not lock.exists() and calls == []


def test_lock_created_rerun_same_ok_different_refused(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "sub" / "holdout.lock"
    out = run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=7)
    data = json.loads(lock.read_text())
    assert data["params"] == [3.0, 0.6, 0.25] and data["sha256"] == params_digest(PARAMS)
    assert len(data["sha256"]) == 64 and len(data["runs"]) == 1
    cfg_used, start, end, runs = calls[0]
    assert start == HOLDOUT_START == 1_704_067_200
    assert end == int(df["ts"].iloc[-1]) + H and runs == 7
    assert (cfg_used.wave.K4, cfg_used.strategy.Q_MIN, cfg_used.strategy.S_MIN) == PARAMS
    assert out["window"] == (start, end) and out["sha256"] == data["sha256"]

    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True)  # identical params: allowed
    assert len(json.loads(lock.read_text())["runs"]) == 2

    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(BotConfig(), df, (2.5, 0.6, 0.25), lock_path=lock, confirm=True)
    assert len(calls) == 2  # the refused call never ran anything
    assert json.loads(lock.read_text())["params"] == [3.0, 0.6, 0.25]  # lock untouched


def test_no_data_in_holdout_does_not_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    old = pd.DataFrame({"ts": [HOLDOUT_START - 10 * H], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match="no data"):
        run_holdout(BotConfig(), old, PARAMS, lock_path=lock, confirm=True)
    assert not lock.exists()


def test_params_digest_is_stable_and_sensitive():
    assert params_digest((3.0, 0.6, 0.25)) == params_digest([3, 0.6, 0.25])
    assert params_digest((3.0, 0.6, 0.25)) != params_digest((3.0, 0.6, 0.5))


def test_criteria(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    out = run_holdout(BotConfig(), df, PARAMS, lock_path=tmp_path / "l.lock", confirm=True)
    c = out["criteria"]
    assert c["winners"] == ["A"] and c["passing"] == ["A"] and c["automatic_criteria_met"] is True
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    cmp["comparison"]["A"]["max_dd"] = 0.2
    assert evaluate_criteria(cmp)["passing"] == [] and evaluate_criteria(cmp)["max_dd_ok"] is False
    cmp["comparison"]["A"].update(max_dd=0.1, n_trades=49)
    assert evaluate_criteria(cmp)["n_trades_ok"] is False
    cmp["comparison"]["A"].update(n_trades=60, sharpe=0.05)  # below random-control mean
    assert evaluate_criteria(cmp)["winners"] == []


def test_trade_count_lines_state_out_of_sample_progress():
    comp = {"A": _m(1.0, 2.0, n=27), "HYBRID": _m(0.4, 0.5, n=50), "C": _m(0.5, 1.0, n=3)}
    assert trade_count_lines(comp) == [
        "A: 표본 외 거래 27/50 — paper 모드로 누적 필요; 실거래 전환 기준 미충족",
        "HYBRID: 표본 외 거래 50/50 — 거래 수 기준 충족",
    ]
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    cmp["comparison"]["A"]["n_trades"] = 27
    crit = evaluate_criteria(cmp)
    assert crit["n_trades"] == {"A": 27, "HYBRID": 60}
    assert crit["trade_count"][0] == "A: 표본 외 거래 27/50 — paper 모드로 누적 필요; 실거래 전환 기준 미충족"
    assert crit["n_trades_ok"] is False and crit["automatic_criteria_met"] is False
