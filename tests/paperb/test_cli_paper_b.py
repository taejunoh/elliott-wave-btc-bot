from __future__ import annotations

from pathlib import Path

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.paperb.notify import HEAD_B
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH
from tests.paperb.helpers import LAST_BAR, make_env


@pytest.fixture
def senders(tmp_path, monkeypatch, lock_text):
    built = []

    def factory(args):
        env, _, sender = make_env(tmp_path / "w", lock_text, LAST_BAR + 900 + 3 * 86_400)
        env.ledger_dir = args.ledger_dir
        built.append((env, sender))
        return env

    monkeypatch.setattr(cli, "_b_paper_env", factory)
    return built


def test_production_wiring_uses_the_repo_lock_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._b_paper_env(build_parser().parse_args(["stratb", "paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == DEFAULT_LOCK_PATH and DEFAULT_LOCK_PATH.name == "holdout-b.lock" and env.sender is None
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_paper_b_run_then_status(senders, tmp_path, capsys):
    ledger_dir = tmp_path / "paper-b"
    assert main(["stratb", "paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("paper-B run ") and "epoch 1" in out and "/30" in out
    assert main(["stratb", "paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "실거래 전환 조건 (Plan 4 명세 7.1)" in out and "1. 홀드아웃 합격: 충족" in out
    assert "holdout data ok, holdout trades recomputed 124 (lock: 124)" in out and "discrepancies 0" in out


def test_a_failed_run_alerts_with_the_paper_b_prefix(senders, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/9")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._b_paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_b_paper_env", failing)
    assert main(["stratb", "paper", "run", "--ledger-dir", str(tmp_path / "pb")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    (_, sender), = senders
    assert sender.texts == [f"{HEAD_B} 실패 · 단계 data\ncandle update failed 3 times: ConnectionError: coinbase down\n"
                            "로그: https://github.com/o/r/actions/runs/9"]
    assert marker.read_text() == "data" and not (tmp_path / "pb" / "ledger.json").exists()


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["stratb", "paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err
