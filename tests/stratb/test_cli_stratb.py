from __future__ import annotations

import json

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.stratb.data15 import REPLAY_ANCHOR, cache_paths
from ewbot.stratb.holdout import read_lock
from tests.paper.helpers import git
from tests.stratb.helpers import save_world, world_dfs

HS = 1_704_067_200
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]


@pytest.fixture
def cache(tmp_path):
    c = tmp_path / "cache"
    c.mkdir()
    save_world(c, world_dfs(REPLAY_ANCHOR))
    return c


def test_the_parser_has_the_spec_subcommands():
    p = build_parser()
    assert p.parse_args(["stratb", "fetch"]).b_cmd == "fetch"
    ns = p.parse_args(["stratb", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven"])
    assert (ns.b_cmd, ns.start, ns.end) == ("backtest", None, None)
    assert p.parse_args(["stratb", "walkforward", "--workers", "2"]).workers == 2
    assert p.parse_args(["stratb", "holdout", "lock", "--params-from", "wf.json"]).bh_cmd == "lock"
    assert p.parse_args(["stratb", "holdout", "run", "--confirm"]).confirm is True
    assert p.parse_args(["stratb", "paper", "run", "--ledger-dir", "x"]).bp_cmd == "run"
    assert p.parse_args(["stratb", "paper", "status", "--ledger-dir", "x"]).bp_cmd == "status"
    for argv in (["stratb", "paper", "run", "--ledger-dir", "x", "--params", "1"], ["stratb", "holdout", "run", "--params", "1"]):
        with pytest.raises(SystemExit):
            p.parse_args(argv)


def test_params_must_be_a_grid_combo():
    assert cli._b_params("3,0.5,0.786,1.5,1.618,original") == (3.0, (0.5, 0.786), 1.5, 1.618, "original")
    with pytest.raises(SystemExit):
        cli._b_params("2.5,0.382,0.618,1,1.272,breakeven")


def test_fetch_reports_both_guarded_caches(tmp_path, cache, monkeypatch, capsys):
    monkeypatch.setattr(cli, "STRATB_LOCK", tmp_path / "none.lock")
    monkeypatch.setattr(cli, "update_caches_15m", lambda c, lock_path: cache_paths(c))
    assert main(["stratb", "fetch", "--cache-dir", str(cache)]) == 0
    out = capsys.readouterr().out
    assert "BTC bars before 2024-01-01: 8832" in out and "ETH bars before 2024-01-01: 8832" in out
    assert "stops at 2024-01-01 (no lock)" in out


def test_backtest_writes_json_and_never_reaches_into_2024(tmp_path, cache, capsys):
    out = tmp_path / "bt.json"
    assert main(["stratb", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache),
                 "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["params"] == COMBO and res["window"][1] == HS and res["metrics"]["n_trades"] == len(res["trades"]) > 0
    assert all(t["entry_ts"] < HS for t in res["trades"]) and res["n_trades_by_asset"]["ETH"] == len(res["trades"]) // 2
    with pytest.raises(SystemExit) as exc:
        main(["stratb", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache),
              "--end", "2024-02-01"])
    assert exc.value.code == 2


def test_walkforward_writes_json_and_says_whether_to_proceed(tmp_path, cache, monkeypatch, capsys):
    def fake(dfs, workers):
        assert sorted(dfs) == ["BTC", "ETH"] and all(int(df["ts"].max()) < HS for df in dfs.values())
        return {"folds": [], "stitched_metrics": {k: 0.0 for k in ("total_return", "cagr", "max_dd", "sharpe", "sortino",
                "calmar", "win_rate", "avg_r")} | {"n_trades": 0, "exposure": 0.0}, "oos_trades": [],
                "chosen_final": None, "chosen_final_fallback": False, "proceed": False}

    monkeypatch.setattr(cli, "walk_forward_b", fake)
    out = tmp_path / "wf.json"
    assert main(["stratb", "walkforward", "--cache-dir", str(cache), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["proceed"] is False and "oos_trades" not in json.loads(out.read_text())
    assert "strategy B ends here" in capsys.readouterr().err


def test_holdout_lock_needs_a_walk_forward_that_proceeds(tmp_path, cache, monkeypatch, capsys):
    (tmp_path / "repo").mkdir()
    git(tmp_path / "repo", "init", "-q")
    lock_path = tmp_path / "repo" / "holdout-b.lock"
    monkeypatch.setattr(cli, "STRATB_LOCK", lock_path)
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": False}))
    with pytest.raises(SystemExit) as exc:
        main(["stratb", "holdout", "lock", "--params-from", str(wf), "--cache-dir", str(cache)])
    assert exc.value.code == 2 and not lock_path.exists()
    wf.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": True}))
    assert main(["stratb", "holdout", "lock", "--params-from", str(wf), "--cache-dir", str(cache)]) == 0
    lock = read_lock(lock_path)
    assert lock["params"] == COMBO and lock["random_runs"] == 1000 and lock["params_source"].startswith("walkforward:")
    assert "Commit it NOW" in capsys.readouterr().out


def test_holdout_run_needs_confirm_and_a_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "STRATB_LOCK", tmp_path / "holdout-b.lock")
    assert main(["stratb", "holdout", "run"]) == 2
    assert "--confirm" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["stratb", "holdout", "run", "--confirm"])
    assert exc.value.code == 2
