from __future__ import annotations

import copy
import json

import pytest

from ewbot.aeth.config import FROZEN_A_FINGERPRINT, AethError
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.stage1 import STAGE1_KIND, data_record, headline, run_stage1, stage1_verdict, write_stage1
from ewbot.backtest.holdout import code_fingerprint
from ewbot.data.candles import fill_gaps
from ewbot.types import H1
from tests.aeth.helpers import HS, a_lock_sha, init_repo, pre2024_frame, write_a_lock
from tests.paper.helpers import PARAMS

HEAD = {
    "A": {"total_return": 0.2, "max_dd": 0.08, "sharpe": 0.9, "calmar": 0.4},
    "C": {"total_return": 0.05, "max_dd": 0.1, "sharpe": 0.2, "calmar": 0.1},
    "RANDOM": {"runs": 100, "sharpe": {"mean": -0.1}, "calmar": {"mean": 0.0}},
}


def _with(path: tuple[str, ...], value):
    head = copy.deepcopy(HEAD)
    node = head
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return head


def test_the_verdict_passes_only_when_every_check_holds():
    assert stage1_verdict(HEAD) == {"checks": dict.fromkeys(
        ("total_return_gt_0", "max_dd_le_15pct", "sharpe_gt_c", "calmar_gt_c", "sharpe_gt_random_mean",
         "calmar_gt_random_mean"), True), "passed": True}
    assert stage1_verdict(_with(("A", "max_dd"), 0.15))["passed"] is True  # at most 15%


@pytest.mark.parametrize("path, value, check", [
    (("A", "total_return"), 0.0, "total_return_gt_0"),
    (("A", "max_dd"), 0.1501, "max_dd_le_15pct"),
    (("C", "sharpe"), 0.9, "sharpe_gt_c"),
    (("C", "calmar"), 0.5, "calmar_gt_c"),
    (("RANDOM", "sharpe", "mean"), 0.95, "sharpe_gt_random_mean"),
    (("RANDOM", "calmar", "mean"), 0.4, "calmar_gt_random_mean"),
    (("A", "sharpe"), None, "sharpe_gt_c"),  # a non-finite metric becomes null in JSON: never a pass
])
def test_each_failed_check_fails_stage_1(path, value, check):
    v = stage1_verdict(_with(path, value))
    assert v["checks"][check] is False and v["passed"] is False


def test_without_a_random_distribution_stage_1_fails():
    head = copy.deepcopy(HEAD)
    head["RANDOM"] = {"runs": 0, "n_signals": 0}
    v = stage1_verdict(head)
    assert not v["checks"]["sharpe_gt_random_mean"] and not v["checks"]["calmar_gt_random_mean"] and not v["passed"]


@pytest.fixture(scope="module")
def raw():
    return pre2024_frame()


@pytest.fixture(scope="module")
def rec(raw):
    start = int(raw["ts"].iloc[0])
    return run_stage1(fill_gaps(raw), PARAMS, random_runs=2, start_ts=start, sub_start_ts=start + 1500 * H1)


def test_run_stage1_compares_a_with_c_bh_ema_and_random_on_eth_costs(raw, rec):
    start = int(raw["ts"].iloc[0])
    assert rec["kind"] == STAGE1_KIND and rec["product"] == "ETH-USD" and rec["future"] == "ETP-20DEC30-CDE"
    assert rec["params"] == list(PARAMS) and rec["window"] == [start, HS] and rec["random_runs"] == 2
    assert rec["config"]["costs"]["contract_size"] == 0.1 and rec["config"]["breakers"]["enabled"] is True
    head = rec["headline"]
    assert sorted(head) == ["A", "BH", "C", "EMA", "HYBRID", "RANDOM"]
    assert head["A"]["n_trades"] == 7 and head["RANDOM"]["runs"] == 2 and head["RANDOM"]["n_signals"] == 7
    assert rec["criteria"] == stage1_verdict(head)
    assert rec["criteria"]["checks"]["total_return_gt_0"] and not rec["criteria"]["checks"]["sharpe_gt_c"]
    assert rec["per_year_A"]["2023"]["n_trades"] == 7
    json.dumps(rec, allow_nan=False)  # strict JSON as written


def test_the_information_runs_are_the_sub_period_and_double_costs(raw, rec):
    sub = rec["info"]["sub_period"]
    assert sub["window"] == [int(raw["ts"].iloc[0]) + 1500 * H1, HS]
    assert sub["checks_info_only"] == stage1_verdict(sub["headline"])
    cost = rec["info"]["cost_x2"]
    assert (cost["fee_rate"], cost["slippage"]) == (0.001, 0.001)
    assert cost["A"]["n_trades"] == rec["headline"]["A"]["n_trades"]
    assert cost["A"]["total_return"] < rec["headline"]["A"]["total_return"]


def test_headline_keeps_the_metrics_and_the_random_distribution():
    cmp = {"comparison": {"A": {k: 1.0 for k in ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate",
                                                     "avg_r")} | {"n_trades": 3, "sortino": 9.0}},
           "random": {"runs": 2, "n_signals": 3, "dist": {"sharpe": {"mean": 0.1}, "avg_r": {"mean": 0.2}}}}
    head = headline(cmp)
    assert "sortino" not in head["A"] and head["A"]["n_trades"] == 3
    assert head["RANDOM"] == {"runs": 2, "n_signals": 3, "sharpe": {"mean": 0.1}}


def test_stage_1_refuses_2024_bars(raw):
    with pytest.raises(AethError, match="before 2024-01-01"):
        run_stage1(fill_gaps(raw), PARAMS, end_ts=HS + H1)
    late = raw.copy()
    late.loc[len(late) - 1, "ts"] = HS
    with pytest.raises(AethError, match="before 2024-01-01"):
        run_stage1(late, PARAMS)


def test_data_record_hashes_the_cached_bars_before_2024(raw):
    rec = data_record(raw)
    assert rec["n_bars"] == 3000 and rec["first_ts"] == int(raw["ts"].iloc[0]) and rec["last_ts"] == HS - H1
    assert rec["missing"] == 0 and len(rec["sha256"]) == 64
    gap = raw.drop(index=[10, 11]).reset_index(drop=True)
    assert data_record(gap)["missing"] == 2 and data_record(gap)["sha256"] != rec["sha256"]
    edited = raw.copy()
    edited.loc[5, "close"] += 1.0
    assert data_record(edited)["sha256"] != rec["sha256"]


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, df, params, **kw):
        self.calls.append((df, params, kw))
        return {"kind": STAGE1_KIND, "params": list(params), "criteria": {"passed": False}}


@pytest.fixture
def world(tmp_path, raw):
    repo = init_repo(tmp_path / "repo")
    return {"repo": repo, "out": repo / "aeth-stage1.json", "a_lock": write_a_lock(tmp_path / "a"),
            "start": int(raw["ts"].iloc[0])}


def test_write_stage1_runs_once_on_gap_filled_bars_and_records_its_provenance(world, raw):
    run = FakeRun()
    rec = write_stage1(raw, out_path=world["out"], a_lock_path=world["a_lock"], start_ts=world["start"],
                       sub_start_ts=world["start"] + H1, random_runs=2, run=run)
    ((df, params, kw),) = run.calls
    assert params == PARAMS and df.equals(fill_gaps(raw)) and kw["random_runs"] == 2
    prov = rec["provenance"]
    assert prov["a_lock_sha256"] == a_lock_sha(world["a_lock"]) and prov["a_fingerprint"] == FROZEN_A_FINGERPRINT
    assert prov["aeth_sha256"] == aeth_fingerprint() and prov["code_sha256"] == code_fingerprint()
    assert prov["data"] == data_record(raw) and {"git_head", "git_dirty", "started", "completed"} <= set(prov)
    assert json.loads(world["out"].read_text(encoding="utf-8")) == rec
    with pytest.raises(AethError, match="runs once"):
        write_stage1(raw, out_path=world["out"], a_lock_path=world["a_lock"], start_ts=world["start"], run=run)
    assert len(run.calls) == 1


@pytest.mark.parametrize("case, why", [
    ("dirty", "uncommitted changes"), ("fingerprint", "not the frozen"), ("late_start", "to completion"),
    ("early_end", "to completion"), ("2024", "before 2024-01-01"), ("a_lock", "not strategy A's completed"),
])
def test_write_stage1_refuses_before_running(world, raw, case, why):
    kw = {"out_path": world["out"], "a_lock_path": world["a_lock"], "start_ts": world["start"]}
    bars = raw
    if case == "dirty":
        (world["repo"] / "README.md").write_text("edited\n", encoding="utf-8")
    if case == "fingerprint":
        kw["a_fingerprint"] = lambda: "0" * 64
    if case == "late_start":
        bars = raw.iloc[48:].reset_index(drop=True)
    if case == "early_end":
        bars = raw.iloc[:-48].reset_index(drop=True)
    if case == "2024":
        bars = raw.copy()
        bars.loc[len(bars) - 1, "ts"] = HS
    if case == "a_lock":
        kw["a_lock_path"] = world["repo"] / "missing.lock"
    run = FakeRun()
    with pytest.raises(AethError, match=why):
        write_stage1(bars, run=run, **kw)
    assert run.calls == [] and not world["out"].exists()
