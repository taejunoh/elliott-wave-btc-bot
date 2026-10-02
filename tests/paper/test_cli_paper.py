from __future__ import annotations

from pathlib import Path

import pytest

from ewbot import cli
from ewbot.backtest.holdout import DEFAULT_LOCK_PATH
from ewbot.cli import build_parser, main
from ewbot.paper.runner import PaperEnv
from tests.paper.helpers import Clock, FakeFetch, FakeSender, commit_lock, make_lock, synth_frame, ts


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    df = synth_frame()
    lock_path = commit_lock(tmp_path / "repo", make_lock(df))
    clock = Clock(ts("2024-02-10 12:07"))
    sender = FakeSender()
    built: list[PaperEnv] = []

    def factory(args):
        env = PaperEnv(
            ledger_dir=args.ledger_dir, lock_path=lock_path, cache_dir=tmp_path / "cache", fetch=FakeFetch(df, clock),
            sender=sender, clock=clock, fingerprint=lambda: "fp", baseline=lambda: ("fp", "c" * 40),
            random_runs=2, workers=1, sleep=lambda s: None,
        )
        built.append(env)
        return env

    monkeypatch.setattr(cli, "_paper_env", factory)
    return built, sender


def test_paper_parser_has_no_strategy_options():
    ns = build_parser().parse_args(["paper", "run", "--ledger-dir", "/tmp/x"])
    assert (ns.cmd, ns.paper_cmd, ns.ledger_dir, ns.cache_dir) == ("paper", "run", Path("/tmp/x"), Path("data/cache"))
    for flag in ("--params", "--profile", "--mode", "--config", "--random-runs", "--lock"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["paper", "run", "--ledger-dir", "/tmp/x", flag, "1"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["paper", "run"])  # --ledger-dir is required


def test_production_env_uses_the_repo_lock_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._paper_env(build_parser().parse_args(["paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == DEFAULT_LOCK_PATH and env.sender is None and env.random_runs == 100
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_paper_run_then_status(fake_env, tmp_path, capsys):
    ledger_dir = tmp_path / "paper"
    assert main(["paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "paper run 20240210T120700Z" in out and "out-of-sample 4/50" in out and "epoch 1" in out
    assert main(["paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "실거래 전환 판정" in out and "3. 표본 외 거래 4/50" in out and "포지션: 없음" in out
    assert "holdout trades recomputed 2 (lock: 2)" in out and "holdout data ok" in out
    assert "discrepancies 0 (unexplained 0, environment change 0)" in out


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err


def test_failed_run_alerts_with_stage_and_log_link(fake_env, tmp_path, monkeypatch, capsys):
    built, sender = fake_env
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/7")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_paper_env", failing)
    assert main(["paper", "run", "--ledger-dir", str(tmp_path / "paper")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    assert len(sender.texts) == 1 and "실패 · 단계 data" in sender.texts[0]
    assert "https://github.com/o/r/actions/runs/7" in sender.texts[0]
    assert marker.read_text() == "data" and not (tmp_path / "paper" / "ledger.json").exists()


def test_unexpected_error_is_alerted_too(fake_env, tmp_path, monkeypatch):
    _, sender = fake_env

    def boom(env):
        raise ValueError("bad state")

    monkeypatch.setattr(cli, "run_paper", boom)
    monkeypatch.delenv("EWBOT_FAILURE_MARKER", raising=False)
    assert main(["paper", "run", "--ledger-dir", str(tmp_path / "paper")]) == 1
    assert "단계 unexpected" in sender.texts[0] and "ValueError: bad state" in sender.texts[0]
