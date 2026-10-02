from __future__ import annotations

import dataclasses
import json
from pathlib import Path

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
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import GRID, WF_END, config_for
from ewbot.config import BotConfig, with_overrides

H = 3600
PARAMS = (3.0, 0.6, 0.25)
_MODULE_DEFAULT_LOCK = getattr(holdout, "DEFAULT_LOCK_PATH", None)  # read before any test patches it


def _m(sharpe, calmar, dd=0.1, n=60):
    return {"sharpe": sharpe, "calmar": calmar, "max_dd": dd, "n_trades": n}


def _fake_compare_factory(calls, allowed=None):
    def fake(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1, *, allow_holdout=False):
        calls.append((cfg, start_ts, end_ts, random_runs))
        if allowed is not None:
            allowed.append(allow_holdout)
        return {
            "comparison": {
                "A": _m(1.0, 2.0),
                "C": _m(0.5, 1.0),
                "HYBRID": _m(0.4, 0.5),
                "BH": _m(0.6, 1.5),
                "EMA": _m(0.2, 0.3),
            },
            "random": {
                "runs": random_runs,
                "n_signals": 60,
                "dist": {"sharpe": {"mean": 0.1}, "calmar": {"mean": 0.2}},
            },
        }

    return fake


@pytest.fixture(autouse=True)
def _never_the_repo_lock(tmp_path, monkeypatch):
    """No test may write <repo>/holdout.lock, even one that forgets lock_path."""
    monkeypatch.setattr(holdout, "DEFAULT_LOCK_PATH", tmp_path / "default" / "holdout.lock", raising=False)
    monkeypatch.chdir(tmp_path)  # and nothing CWD-relative lands in the repo either


@pytest.fixture
def df():
    ts = [HOLDOUT_START - 3 * H + i * H for i in range(10)]
    return pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})


def test_holdout_starts_exactly_where_the_walk_forward_ends():
    assert HOLDOUT_START == utc_ts(WF_END) == 1_704_067_200


def test_requires_confirm_and_writes_nothing(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match="confirm"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=False)
    assert not lock.exists() and calls == []


def test_lock_created_rerun_same_ok_different_refused(tmp_path, df, monkeypatch):
    calls, allowed = [], []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls, allowed))
    lock = tmp_path / "sub" / "holdout.lock"
    out = run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=7)
    data = json.loads(lock.read_text())
    assert data["params"] == [3.0, 0.6, 0.25]
    assert data["sha256"] == holdout.lock_digest(BotConfig(), PARAMS, random_runs=7)
    assert len(data["sha256"]) == 64 and len(data["runs"]) == 1
    cfg_used, start, end, runs = calls[0]
    assert start == HOLDOUT_START == 1_704_067_200
    assert end == int(df["ts"].iloc[-1]) + H and runs == 7
    assert (cfg_used.wave.K4, cfg_used.strategy.Q_MIN, cfg_used.strategy.S_MIN) == PARAMS
    assert out["window"] == (start, end) and out["sha256"] == data["sha256"]
    assert out["lock_path"] == str(lock)
    assert allowed == [True]  # the only caller that may let compare() into the holdout

    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=7)  # identical: allowed
    assert len(json.loads(lock.read_text())["runs"]) == 2

    before = lock.read_text()
    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(BotConfig(), df, (2.5, 0.6, 0.25), lock_path=lock, confirm=True, random_runs=7)
    assert len(calls) == 2  # the refused call never ran anything
    assert lock.read_text() == before  # lock untouched


@pytest.mark.parametrize(
    "change",
    [
        {"costs.slippage": 0.001},
        {"costs.fee_rate": 0.0006},
        {"costs.funding_apr_long": 0.2},
        {"seed": 1},
        {"initial_equity": 20_000.0},
        {"wave.atr_len": 21},
        {"wave.sigma": 0.2},
        {"strategy.r_gate": 2.0},
        {"strategy.setups_enabled": ("W3",)},
        {"breakers.daily_loss": 0.05},
    ],
    ids=lambda ch: next(iter(ch)),
)
def test_lock_binds_the_full_config(tmp_path, df, monkeypatch, change):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    before = lock.read_text()
    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(with_overrides(BotConfig(), **change), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert lock.read_text() == before and len(calls) == 1


def test_lock_binds_the_random_control_size(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    with pytest.raises(HoldoutError, match="refusing"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=6)
    assert len(calls) == 1


def test_strategy_mode_is_pinned_and_not_part_of_the_binding(tmp_path, df, monkeypatch):
    """compare() runs A, C and HYBRID itself, so cfg.strategy.mode has no effect on the holdout."""
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    hybrid = with_overrides(BotConfig(), **{"strategy.mode": "HYBRID"})
    run_holdout(hybrid, df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert [c[0].strategy.mode for c in calls] == ["A", "A"] and calls[0][0] == calls[1][0]


def test_lock_digest_covers_config_grid_and_random_runs():
    base = holdout.lock_digest(BotConfig(), PARAMS)
    assert len(base) == 64
    assert base == holdout.lock_digest(BotConfig(), (3, 0.6, 0.25))  # int vs float K4
    assert base != holdout.lock_digest(BotConfig(), (3.0, 0.6, 0.5))
    assert base != holdout.lock_digest(with_overrides(BotConfig(), **{"costs.slippage": 0.0006}), PARAMS)
    assert base != holdout.lock_digest(BotConfig(), PARAMS, random_runs=50)
    assert base == holdout.lock_digest(with_overrides(BotConfig(), **{"strategy.mode": "C"}), PARAMS)


def test_lock_records_config_window_and_first_run_headline(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert data["config"] == json.loads(json.dumps(dataclasses.asdict(config_for(BotConfig(), PARAMS))))
    assert data["grid"] == {k: list(v) for k, v in GRID.items()} and data["keys"] == list(GRID)
    assert data["holdout_start"] == HOLDOUT_START and data["random_runs"] == 5
    assert data["params_sha256"] == params_digest(PARAMS)
    last_bar = int(df["ts"].iloc[-1])
    first = data["first_run"]
    assert (first["start_ts"], first["end_ts"], first["last_bar_ts"]) == (HOLDOUT_START, last_bar + H, last_bar)
    head = first["headline"]
    assert {"A", "HYBRID", "C", "BH", "RANDOM"} <= set(head)
    assert head["A"] == {"sharpe": 1.0, "calmar": 2.0, "max_dd": 0.1, "n_trades": 60}
    assert head["RANDOM"]["runs"] == 5 and head["RANDOM"]["sharpe"]["mean"] == 0.1
    assert first["criteria"]["automatic_criteria_met"] is True and first["criteria"]["n_trades"]["A"] == 60
    assert data["runs"][0]["completed"] is not None and data["runs"][0]["last_bar_ts"] == last_bar

    more = pd.concat([df, df.tail(1).assign(ts=last_bar + H)], ignore_index=True)  # one more bar later on
    run_holdout(BotConfig(), more, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data2 = json.loads(lock.read_text())
    assert data2["first_run"] == first  # the first run's record is never overwritten
    assert len(data2["runs"]) == 2 and data2["runs"][1]["last_bar_ts"] == last_bar + H


def test_crash_during_the_run_keeps_the_lock(tmp_path, df, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(holdout, "compare", boom)
    lock = tmp_path / "holdout.lock"
    with pytest.raises(RuntimeError, match="crashed"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert data["first_run"] is None and data["runs"][0]["completed"] is None
    with pytest.raises(HoldoutError, match="refusing"):  # a crash cannot be used to retune
        run_holdout(BotConfig(), df, (2.5, 0.6, 0.25), lock_path=lock, confirm=True, random_runs=5)
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert len(data["runs"]) == 2 and data["first_run"]["headline"]["A"]["n_trades"] == 60


def test_unreadable_lock_is_refused(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    lock.write_text("{not json", encoding="utf-8")
    with pytest.raises(HoldoutError, match="unreadable"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert calls == [] and lock.read_text() == "{not json"


@pytest.mark.parametrize(
    "cfg, params, runs, msg",
    [
        (BotConfig(), (3.0, 0.6, 0.3), 5, "grid"),
        (BotConfig(), (3.0, 0.6), 5, "grid"),
        (with_overrides(BotConfig(), **{"breakers.enabled": False}), PARAMS, 5, "breakers"),
        (with_overrides(BotConfig(), risk="aggressive"), PARAMS, 5, "conservative"),
        (with_overrides(BotConfig(), **{"risk.risk_pct": 0.02}), PARAMS, 5, "conservative"),
        (with_overrides(BotConfig(), risk={"risk_pct": 0.01, "max_leverage": 3.0}), PARAMS, 5, "conservative"),
        (BotConfig(), PARAMS, 0, "random"),
    ],
    ids=["off-grid", "short-params", "breakers-off", "aggressive", "conservative-1pct-changed", "custom", "no-random"],
)
def test_invalid_holdout_setups_are_refused_before_any_write(tmp_path, df, monkeypatch, cfg, params, runs, msg):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match=msg):
        run_holdout(cfg, df, params, lock_path=lock, confirm=True, random_runs=runs)
    assert not lock.exists() and calls == []


def test_no_data_in_holdout_does_not_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    old = pd.DataFrame({"ts": [HOLDOUT_START - 10 * H], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    lock = tmp_path / "holdout.lock"
    with pytest.raises(HoldoutError, match="no data"):
        run_holdout(BotConfig(), old, PARAMS, lock_path=lock, confirm=True)
    assert not lock.exists()


def test_default_lock_path_is_the_project_root_not_the_cwd():
    repo_root = Path(__file__).resolve().parents[2]  # tests/backtest/test_holdout.py -> repo root
    assert holdout.PROJECT_ROOT == repo_root and (repo_root / "pyproject.toml").is_file()
    assert (repo_root / "src" / "ewbot" / "backtest" / "holdout.py").is_file()
    assert _MODULE_DEFAULT_LOCK == repo_root / "holdout.lock"


def test_run_holdout_without_lock_path_uses_the_default_whatever_the_cwd(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    target = tmp_path / "root" / "holdout.lock"
    monkeypatch.setattr(holdout, "DEFAULT_LOCK_PATH", target)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    out = run_holdout(BotConfig(), df, PARAMS, confirm=True, random_runs=5)
    assert target.exists() and out["lock_path"] == str(target)
    assert not (cwd / "holdout.lock").exists()


def test_default_lock_path_needs_a_recognisable_project_root(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    monkeypatch.setattr(holdout, "PROJECT_ROOT", tmp_path / "site-packages")  # e.g. a non-editable install
    with pytest.raises(HoldoutError, match="lock_path"):
        run_holdout(BotConfig(), df, PARAMS, confirm=True, random_runs=5)
    assert calls == []


def test_params_digest_is_stable_and_sensitive():
    assert params_digest((3.0, 0.6, 0.25)) == params_digest([3, 0.6, 0.25])
    assert params_digest((3.0, 0.6, 0.25)) != params_digest((3.0, 0.6, 0.5))


def test_criteria(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    out = run_holdout(BotConfig(), df, PARAMS, lock_path=tmp_path / "l.lock", confirm=True)
    c = out["criteria"]
    assert c["winners"] == ["A"] and c["passing"] == ["A"] and c["automatic_criteria_met"] is True
    assert c["random_control_evaluated"] is True
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    cmp["comparison"]["A"]["max_dd"] = 0.2
    assert evaluate_criteria(cmp)["passing"] == [] and evaluate_criteria(cmp)["max_dd_ok"] is False
    cmp["comparison"]["A"].update(max_dd=0.1, n_trades=49)
    assert evaluate_criteria(cmp)["n_trades_ok"] is False
    cmp["comparison"]["A"].update(n_trades=60, sharpe=0.05)  # below random-control mean
    assert evaluate_criteria(cmp)["winners"] == []


@pytest.mark.parametrize(
    "random",
    [{"runs": 0, "n_signals": 0, "dist": {}, "beaten_by": {}}, {"runs": 0, "n_signals": 5, "dist": {}}, {}],
    ids=["no-runs", "no-dist", "missing"],
)
def test_random_control_is_mandatory(random):
    cmp = _fake_compare_factory([])(None, None, 0, 1)
    assert evaluate_criteria(cmp)["automatic_criteria_met"] is True  # A beats C, BH and the random mean
    cmp["random"] = random  # e.g. random_runs=0, or A made no trades so there was nothing to replay
    crit = evaluate_criteria(cmp)
    assert crit["random_control_evaluated"] is False
    assert crit["winners"] == [] and crit["passing"] == [] and crit["automatic_criteria_met"] is False


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


# ----------------------------------------------------------------------------- code / data fingerprints


def test_code_fingerprint_covers_every_package_source_file(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "sub" / "b.py").write_text("y = 2\n")
    base = holdout.code_fingerprint(pkg)
    assert len(base) == 64 and base == holdout.code_fingerprint(pkg)
    (pkg / "sub" / "__pycache__").mkdir()
    (pkg / "sub" / "__pycache__" / "b.cpython-312.pyc").write_bytes(b"\0junk")
    (pkg / "notes.txt").write_text("not code")
    assert holdout.code_fingerprint(pkg) == base  # only *.py sources count
    (pkg / "sub" / "b.py").write_text("y = 3\n")
    changed = holdout.code_fingerprint(pkg)
    assert changed != base
    (pkg / "c.py").write_text("")
    assert holdout.code_fingerprint(pkg) != changed  # a new module counts too
    assert holdout.PACKAGE_DIR == Path(holdout.__file__).resolve().parents[1]  # src/ewbot


def test_lock_digest_binds_the_code_fingerprint(monkeypatch):
    base = holdout.lock_digest(BotConfig(), PARAMS)
    assert base == holdout.lock_digest(BotConfig(), PARAMS, code_sha256=holdout.code_fingerprint())
    assert base != holdout.lock_digest(BotConfig(), PARAMS, code_sha256="0" * 64)


def test_rerun_after_a_code_change_is_refused(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    assert data["code_sha256"] == holdout.code_fingerprint() and data["version"] == holdout.LOCK_VERSION
    run0 = data["runs"][0]
    assert "git_head" in run0 and "git_dirty" in run0  # informational only
    before = lock.read_text()
    monkeypatch.setattr(holdout, "code_fingerprint", lambda pkg=None: "f" * 64)  # e.g. a retuned strategy file
    with pytest.raises(HoldoutError, match="code_sha256"):
        run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    assert lock.read_text() == before and len(calls) == 1


def test_data_fingerprint_is_the_holdout_window_prefix(df):
    last = int(df["ts"].iloc[-1])
    fp = holdout.data_fingerprint(df, HOLDOUT_START, last)
    assert len(fp) == 64
    warm = df.copy()
    warm.loc[warm["ts"] < HOLDOUT_START, "close"] = 2.0  # warm-up bars are not part of it
    assert holdout.data_fingerprint(warm, HOLDOUT_START, last) == fp
    longer = pd.concat([df, df.tail(1).assign(ts=last + H)], ignore_index=True)
    assert holdout.data_fingerprint(longer, HOLDOUT_START, last) == fp  # appended bars: same prefix
    for col in ("open", "high", "low", "close", "volume"):
        moved = df.copy()
        moved.loc[moved["ts"] == HOLDOUT_START + H, col] = 1.5
        assert holdout.data_fingerprint(moved, HOLDOUT_START, last) != fp, col
    assert holdout.data_fingerprint(df.iloc[:-1], HOLDOUT_START, last) != fp  # a missing bar


def test_rerun_with_changed_holdout_bars_is_refused(tmp_path, df, monkeypatch):
    calls = []
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory(calls))
    lock = tmp_path / "holdout.lock"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    data = json.loads(lock.read_text())
    last_bar = int(df["ts"].iloc[-1])
    assert data["data"] == {
        "start_ts": HOLDOUT_START,
        "last_bar_ts": last_bar,
        "n_bars": int((df["ts"] >= HOLDOUT_START).sum()),
        "sha256": holdout.data_fingerprint(df, HOLDOUT_START, last_bar),
    }
    before = lock.read_text()
    edited = df.copy()
    edited.loc[edited["ts"] == HOLDOUT_START + 2 * H, "close"] = 0.9  # a revised holdout candle
    with pytest.raises(HoldoutError, match="data"):
        run_holdout(BotConfig(), edited, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    with pytest.raises(HoldoutError, match="data"):
        run_holdout(BotConfig(), df.iloc[:-2], PARAMS, lock_path=lock, confirm=True, random_runs=5)  # shorter
    assert lock.read_text() == before and len(calls) == 1
    longer = pd.concat([df, df.tail(3).assign(ts=[last_bar + k * H for k in (1, 2, 3)])], ignore_index=True)
    run_holdout(BotConfig(), longer, PARAMS, lock_path=lock, confirm=True, random_runs=5)  # newer bars: fine
    assert len(calls) == 2 and json.loads(lock.read_text())["data"]["last_bar_ts"] == last_bar


def test_runs_record_params_provenance_and_their_own_headline(tmp_path, df, monkeypatch):
    monkeypatch.setattr(holdout, "compare", _fake_compare_factory([]))
    lock = tmp_path / "holdout.lock"
    src = "walkforward:reports/walkforward.json#chosen_final"
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5, params_source=src)
    run_holdout(BotConfig(), df, PARAMS, lock_path=lock, confirm=True, random_runs=5)
    runs = json.loads(lock.read_text())["runs"]
    assert [r["params_source"] for r in runs] == [src, "explicit"]
    for r in runs:
        assert r["completed"] is not None
        assert r["headline"]["A"] == {"sharpe": 1.0, "calmar": 2.0, "max_dd": 0.1, "n_trades": 60}
        assert r["criteria"]["automatic_criteria_met"] is True
