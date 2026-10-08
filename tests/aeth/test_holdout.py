from __future__ import annotations

import hashlib
import json

import pytest

from ewbot.aeth.config import FROZEN_A_FINGERPRINT, AethError, config_json
from ewbot.aeth.fingerprint import aeth_decision_fingerprint, aeth_fingerprint
from ewbot.aeth.holdout import create_lock, lock_mismatch, run_holdout_aeth, stage2_verdict
from ewbot.aeth.lockfile import LOCK_KIND, lock_digest, lock_exists
from ewbot.aeth.stage1 import data_record, headline
from ewbot.backtest.holdout import code_fingerprint, data_fingerprint, evaluate_criteria
from ewbot.backtest.report import to_jsonable
from ewbot.data.candles import fill_gaps
from ewbot.types import H1
from tests.aeth.helpers import (
    HOLDOUT_TRADES,
    HS,
    LOCK_NOW,
    a_lock_sha,
    commit_file,
    eth_frame,
    init_repo,
    save_cache,
    write_a_lock,
)
from tests.aeth.records import stage1_record
from tests.paper.helpers import BOUNDARY, PARAMS, git


def test_stage2_passes_when_a_wins_a_s_criteria_and_keeps_its_drawdown():
    comp = {"A": {"max_dd": 0.05, "n_trades": 30}}
    assert stage2_verdict({"winners": ["A", "HYBRID"]}, comp) == {
        "c1_a_beats_c_bh_random_mean": True, "c2_max_dd_le_15pct": True, "n_trades": 30, "passed": True}
    assert stage2_verdict({"winners": ["HYBRID"]}, comp)["passed"] is False  # HYBRID alone is not A on ETH
    assert stage2_verdict({"winners": ["A"]}, {"A": {"max_dd": 0.16, "n_trades": 30}})["passed"] is False


def _world(tmp_path, *, stage1=None, commit_stage1=True):
    df = eth_frame()
    a_lock = write_a_lock(tmp_path / "a")
    repo = init_repo(tmp_path / "repo")
    cache = tmp_path / "cache"
    save_cache(cache, df)
    raw = df[df["ts"] < HS].reset_index(drop=True)
    rec = stage1_record(raw, a_lock) if stage1 is None else stage1
    text = json.dumps(rec, indent=2) + "\n"
    s1 = commit_file(repo, "aeth-stage1.json", text) if commit_stage1 else repo / "aeth-stage1.json"
    if not commit_stage1:
        s1.write_text(text, encoding="utf-8")
    return {"df": df, "raw": raw, "a_lock": a_lock, "repo": repo, "cache": cache, "s1": s1, "text": text,
            "lock": repo / "holdout-aeth.lock"}


def _lock(w, **kw):
    return create_lock(w["raw"], now_ts=kw.pop("now_ts", LOCK_NOW), lock_path=w["lock"], stage1_path=w["s1"],
                       a_lock_path=w["a_lock"], random_runs=2, **kw)


def test_the_lock_binds_the_passed_stage_1_record_and_what_it_ran_on(tmp_path):
    w = _world(tmp_path)
    lock = _lock(w)
    assert lock_exists(w["lock"]) and lock == json.loads(w["lock"].read_text(encoding="utf-8"))
    assert (lock["kind"], lock["product"], lock["future"]) == (LOCK_KIND, "ETH-USD", "ETP-20DEC30-CDE")
    assert lock["params"] == list(PARAMS) and lock["a_lock_sha256"] == a_lock_sha(w["a_lock"])
    assert lock["config"] == config_json(PARAMS) and lock["holdout_start"] == HS
    assert lock["last_bar_ts"] == BOUNDARY and lock["random_runs"] == 2
    assert lock["a_fingerprint"] == FROZEN_A_FINGERPRINT and lock["aeth_sha256"] == aeth_fingerprint()
    assert lock["aeth_decision_sha256"] == aeth_decision_fingerprint()  # the paper epoch's baseline
    assert lock["code_sha256"] == code_fingerprint() and lock["pre2024"] == data_record(w["raw"])
    assert lock["stage1_sha256"] == hashlib.sha256(w["text"].encode("utf-8")).hexdigest()
    assert lock["sha256"] == lock_digest(lock) and lock["first_run"] is None and lock["attempts"] == []
    assert lock_mismatch(lock, w["a_lock"]) == []
    with pytest.raises(AethError, match="written once"):
        _lock(w)


@pytest.mark.parametrize("case, why", [
    ("uncommitted", "committed unchanged"), ("edited", "committed unchanged"), ("failed", "did not pass"),
    ("hand_passed", "did not pass"), ("a_lock", r"other \['params'\]"), ("aeth", r"other \['aeth_sha256'\]"),
    ("data", r"other \['data'\]"), ("a_fingerprint", r"other \['a_fingerprint'\]"), ("dirty", "uncommitted changes"),
    ("before_2024", "after 2024-01-01"), ("bars_2024", "before 2024-01-01 only"),
])
def test_the_lock_is_refused(tmp_path, case, why):
    w = _world(tmp_path, commit_stage1=case != "uncommitted")
    raw = w["df"][w["df"]["ts"] < HS].reset_index(drop=True)
    kw = {}
    if case == "edited":
        w["s1"].write_text(w["text"].replace('"version": 1', '"version": 2'), encoding="utf-8")
    if case in ("failed", "hand_passed", "a_lock", "aeth", "data", "a_fingerprint"):
        head = {**json.loads(w["text"])["headline"]}
        if case in ("failed", "hand_passed"):
            head["C"] = {**head["C"], "sharpe": 5.0}
        prov = {"a_lock": {"a_lock_sha256": "0" * 64}, "aeth": {"aeth_sha256": "0" * 64},
                "data": {"data": data_record(raw.iloc[:-1])}, "a_fingerprint": {"a_fingerprint": "0" * 64}}
        rec = stage1_record(raw, w["a_lock"], headline=head, **prov.get(case, {}))
        if case == "hand_passed":
            rec["criteria"]["passed"] = True
        w = _world(tmp_path / "again", stage1=rec)
    if case == "dirty":
        (w["repo"] / "README.md").write_text("edited\n", encoding="utf-8")
    if case == "before_2024":
        kw["now_ts"] = HS + 600
    if case == "bars_2024":
        w["raw"] = w["df"]
    with pytest.raises(AethError, match=why):
        _lock(w, **kw)
    assert not w["lock"].exists()


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    """One real (synthetic) ETH holdout run: the lock committed, then run_holdout_aeth once."""
    w = _world(tmp_path_factory.mktemp("holdout"))
    _lock(w)
    git(w["repo"], "add", w["lock"].name)
    git(w["repo"], "commit", "-q", "-m", "lock")
    out = run_holdout_aeth(confirm=True, cache_dir=w["cache"], lock_path=w["lock"], a_lock_path=w["a_lock"])
    return w, out


def test_the_holdout_runs_a_s_comparison_and_criteria_on_the_window(ran):
    w, out = ran
    fr = out["first_run"]
    assert (fr["start_ts"], fr["end_ts"], fr["last_bar_ts"]) == (HS, BOUNDARY + H1, BOUNDARY)
    assert fr["data"]["last_bar_ts"] == BOUNDARY and fr["data"]["n_bars"] == (BOUNDARY - HS) // H1 + 1
    assert fr["headline"] == headline(out["compare"])
    assert fr["criteria"] == to_jsonable(evaluate_criteria(out["compare"]))  # strategy A's, unchanged
    assert fr["verdict"] == stage2_verdict(fr["criteria"], out["compare"]["comparison"])
    assert fr["verdict"]["n_trades"] == HOLDOUT_TRADES == fr["headline"]["A"]["n_trades"]
    assert out["compare"]["random"]["runs"] == 2
    lock = json.loads(w["lock"].read_text(encoding="utf-8"))
    assert lock["first_run"] == fr and lock_exists(w["lock"])
    assert len(lock["attempts"]) == 1 and lock["attempts"][0]["data_sha256"] == fr["data"]["sha256"]


def test_a_missing_last_bar_is_filled_before_the_cut_like_the_paper_runtime_does(tmp_path):
    w = _world(tmp_path)
    gap = w["df"][w["df"]["ts"] != BOUNDARY].reset_index(drop=True)
    save_cache(w["cache"], gap)
    _lock(w)
    git(w["repo"], "add", w["lock"].name)
    git(w["repo"], "commit", "-q", "-m", "lock")
    fr = run_holdout_aeth(confirm=True, cache_dir=w["cache"], lock_path=w["lock"], a_lock_path=w["a_lock"])["first_run"]
    assert fr["data"]["sha256"] == data_fingerprint(fill_gaps(gap), HS, BOUNDARY)
    assert fr["data"]["n_bars"] == (BOUNDARY - HS) // H1 + 1


def test_the_holdout_runs_once(ran):
    w, _ = ran
    with pytest.raises(AethError, match="already run once"):
        run_holdout_aeth(confirm=True, cache_dir=w["cache"], lock_path=w["lock"], a_lock_path=w["a_lock"])


@pytest.mark.parametrize("case, why", [
    ("confirm", "single-use"), ("uncommitted", "differs from the committed lock"),
    ("tampered", "not a valid A-ETH lock"),
    ("a_fp", "strategy A fingerprint"), ("aeth_fp", "A-ETH fingerprint"), ("code_fp", "code fingerprint"),
    ("a_params", "a_lock_sha256"), ("dirty", "uncommitted changes"), ("pre2024", "differ from the ones stage 1"),
    ("short", "ends before the lock's last bar"), ("attempt", "unfinished attempt"),
])
def test_the_holdout_is_refused(tmp_path, case, why):
    w = _world(tmp_path)
    _lock(w)
    if case != "uncommitted":
        git(w["repo"], "add", w["lock"].name)
        git(w["repo"], "commit", "-q", "-m", "lock")
    kw = {"confirm": case != "confirm", "cache_dir": w["cache"], "lock_path": w["lock"], "a_lock_path": w["a_lock"]}
    if case == "tampered":
        lock = json.loads(w["lock"].read_text(encoding="utf-8"))
        lock["random_runs"] = 1
        w["lock"].write_text(json.dumps(lock), encoding="utf-8")
    if case in ("a_fp", "aeth_fp", "code_fp"):
        kw[{"a_fp": "a_fingerprint", "aeth_fp": "aeth_fp", "code_fp": "code_fp"}[case]] = lambda: "0" * 64
    if case == "a_params":
        kw["a_lock_path"] = write_a_lock(tmp_path / "other", random_runs=3)  # same params, another A lock sha256
    if case == "dirty":
        (w["repo"] / "README.md").write_text("edited\n", encoding="utf-8")
    if case == "pre2024":
        df = w["df"].copy()
        df.loc[5, "close"] += 1.0
        save_cache(w["cache"], df)
    if case == "short":
        save_cache(w["cache"], w["df"][w["df"]["ts"] < BOUNDARY])
    if case == "attempt":
        lock = json.loads(w["lock"].read_text(encoding="utf-8"))
        lock["attempts"] = [{"started": "earlier", "data_sha256": "0" * 64}]
        w["lock"].write_text(json.dumps(lock), encoding="utf-8")
    before = w["lock"].read_text(encoding="utf-8")
    with pytest.raises(AethError, match=why):
        run_holdout_aeth(**kw)
    assert w["lock"].read_text(encoding="utf-8") == before  # nothing recorded
