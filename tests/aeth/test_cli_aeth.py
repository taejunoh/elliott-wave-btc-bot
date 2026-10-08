from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ewbot.aeth.config as ec
import ewbot.aeth.data as edata
from ewbot import cli
from ewbot.aeth.paper import HEAD_AETH, AethPaperEnv
from ewbot.cli import build_parser, main
from tests.aeth.helpers import (
    FakeCacheFetch,
    commit_file,
    eth_frame,
    init_repo,
    pre2024_frame,
    save_cache,
    write_a_lock,
)
from tests.aeth.records import aeth_lock
from tests.paper.helpers import Clock, FakeSender, ts


def _code(argv: list[str]) -> int:
    """main's exit code, also when a refusal exits through SystemExit (cli._refuse)."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code)


def test_the_parser_has_the_spec_subcommands_and_no_tuning_options():
    p = build_parser()
    assert p.parse_args(["aeth", "fetch"]).e_cmd == "fetch"
    assert p.parse_args(["aeth", "stage1", "--workers", "2"]).workers == 2
    assert p.parse_args(["aeth", "holdout", "lock"]).eh_cmd == "lock"
    assert p.parse_args(["aeth", "holdout", "run", "--confirm"]).confirm is True
    assert p.parse_args(["aeth", "paper", "run", "--ledger-dir", "x"]).ep_cmd == "run"
    assert p.parse_args(["aeth", "paper", "status", "--ledger-dir", "x"]).ep_cmd == "status"
    for argv in (["aeth", "stage1", "--params", "2,0.7,0.5"], ["aeth", "stage1", "--product", "SOL-USD"],
                 ["aeth", "holdout", "run", "--random-runs", "5"], ["aeth", "paper", "run", "--ledger-dir", "x",
                                                                     "--lock", "y"]):
        with pytest.raises(SystemExit):
            p.parse_args(argv)


def test_the_cli_loads_aeth_only_for_aeth_commands():
    """`ewbot live …` and `ewbot paper …` run with the same modules as before A-ETH."""
    code = "import sys, ewbot.cli; print(sorted(m for m in sys.modules if m.startswith('ewbot.aeth')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_production_wiring_uses_the_repo_locks_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._e_paper_env(build_parser().parse_args(["aeth", "paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == ec.LOCK_PATH and env.a_lock_path == ec.A_LOCK_PATH and env.sender is None
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_fetch_reports_the_guarded_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ec, "LOCK_PATH", tmp_path / "none.lock")
    seen = []

    def fake_fetch(cache_dir, lock_path):
        seen.append(lock_path)
        return save_cache(Path(cache_dir), pre2024_frame())

    monkeypatch.setattr(edata, "fetch_eth", fake_fetch)
    assert main(["aeth", "fetch", "--cache-dir", str(tmp_path / "cache")]) == 0
    out = capsys.readouterr().out
    assert "ETH bars before 2024-01-01: 3000 (2023-08-29 00:00 .. 2023-12-31 23:00 UTC)" in out
    assert "stops at 2024-01-01 (no lock)" in out and seen == [tmp_path / "none.lock"]


def _stage1_world(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr(ec, "STAGE1_PATH", repo / "aeth-stage1.json")
    monkeypatch.setattr(ec, "LOCK_PATH", repo / "holdout-aeth.lock")
    monkeypatch.setattr(ec, "A_LOCK_PATH", write_a_lock(tmp_path / "a"))
    save_cache(tmp_path / "cache", pre2024_frame())
    return repo


def test_stage1_runs_on_the_whole_2017_2023_window_only(tmp_path, monkeypatch, capsys):
    _stage1_world(tmp_path, monkeypatch)  # synthetic bars from 2023-08-29: the CLI has no window option
    assert _code(["aeth", "stage1", "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "run `ewbot aeth fetch` to completion first" in capsys.readouterr().err
    assert not ec.STAGE1_PATH.exists()


def test_stage1_runs_once(tmp_path, monkeypatch, capsys):
    repo = _stage1_world(tmp_path, monkeypatch)
    commit_file(repo, "aeth-stage1.json", "{}\n")
    assert _code(["aeth", "stage1", "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "stage 1 runs once" in capsys.readouterr().err


def test_the_lock_needs_the_committed_stage1_record(tmp_path, monkeypatch, capsys):
    _stage1_world(tmp_path, monkeypatch)
    assert _code(["aeth", "holdout", "lock", "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "must be committed unchanged" in capsys.readouterr().err and not ec.LOCK_PATH.exists()


@pytest.mark.parametrize("argv", [["fetch"], ["stage1"], ["holdout", "lock"]])
def test_a_cache_with_2024_bars_before_the_lock_is_refused_not_a_traceback(tmp_path, monkeypatch, capsys, argv):
    _stage1_world(tmp_path, monkeypatch)
    save_cache(tmp_path / "cache", eth_frame())  # reaches 2024 without a lock: not A-ETH's (A's `ewbot fetch`)
    assert _code(["aeth", *argv, "--cache-dir", str(tmp_path / "cache")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("refusing: ") and "did not come from A-ETH" in err
    assert not ec.STAGE1_PATH.exists() and not ec.LOCK_PATH.exists()


@pytest.mark.parametrize("argv, why", [
    (["--confirm"], "not a valid A-ETH lock"),
    ([], "runs once, after the user's approval"),
])
def test_the_holdout_run_needs_confirm_and_a_lock_before_any_download(tmp_path, monkeypatch, capsys, argv, why):
    _stage1_world(tmp_path, monkeypatch)

    def no_network(*a, **k):
        raise AssertionError("no download before the checks")

    monkeypatch.setattr(edata, "fetch_eth", no_network)
    assert _code(["aeth", "holdout", "run", "--cache-dir", str(tmp_path / "cache"), *argv]) == 2
    assert why in capsys.readouterr().err


@pytest.fixture
def senders(tmp_path, monkeypatch):
    built = []
    df = eth_frame()
    a_lock = write_a_lock(tmp_path / "a")
    lock_path = commit_file(tmp_path / "repo", "holdout-aeth.lock", json.dumps(aeth_lock(df, a_lock), indent=2))

    def factory(args):
        clock = Clock(ts("2024-02-10 12:07"))
        env = AethPaperEnv(ledger_dir=args.ledger_dir, lock_path=lock_path, a_lock_path=a_lock,
                           cache_dir=tmp_path / "cache", fetch=FakeCacheFetch(df, clock), sender=FakeSender(),
                           clock=clock, sleep=lambda s: None, environment=lambda: {"python": "3.12.13"})
        built.append(env)
        return env

    monkeypatch.setattr(cli, "_e_paper_env", factory)
    return built


def test_paper_run_then_status(senders, tmp_path, capsys):
    ledger_dir = tmp_path / "paper-aeth"
    assert main(["aeth", "paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("paper-A-ETH run 20240210T120700Z") and "ETH out-of-sample 4/50" in out
    assert main(["aeth", "paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "A-ETH 실거래 검토 조건 (Plan 5a 명세 8.1)" in out and "1. 단계 2 홀드아웃 합격 (A의 9.6절 1·2번): 충족" in out
    assert "holdout data ok, holdout trades recomputed 2 (lock: 2)" in out and "discrepancies 0 (unexplained 0)" in out


def test_a_failed_paper_run_alerts_with_the_a_eth_prefix(senders, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/9")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._e_paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_e_paper_env", failing)
    assert main(["aeth", "paper", "run", "--ledger-dir", str(tmp_path / "pa")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    (env,) = senders
    assert env.sender.texts == [f"{HEAD_AETH} 실패 · 단계 data\ncandle update failed 3 times: ConnectionError: coinbase "
                                "down\n로그: https://github.com/o/r/actions/runs/9"]
    assert marker.read_text() == "data" and not (tmp_path / "pa" / "ledger.json").exists()


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["aeth", "paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err
