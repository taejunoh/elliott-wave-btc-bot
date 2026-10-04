from __future__ import annotations

import dataclasses
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.live import app as live_app
from ewbot.live.audit import AuditResult
from ewbot.live.exchange import ExchangeError, KeyPermissions
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier
from ewbot.live.runtime import LiveRuntime
from ewbot.live.settings import LiveSettings, Secret, SettingsError
from ewbot.live.sim import SimExchange
from tests.live.helpers import Clock
from tests.paper.helpers import FakeFetch, FakeSender, make_lock, synth_frame, ts

NOW = ts("2024-03-13 23:00") + 30


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Settings in a temp dir, a temp holdout lock (pinned by its own sha256 field and file sha256), a SimExchange
    instead of Coinbase, synthetic candles."""
    df = synth_frame()
    lock = make_lock(df)
    lock_path = tmp_path / "holdout.lock"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    settings = LiveSettings(data_dir=tmp_path / "data")
    clock = Clock(NOW)
    ex = SimExchange(clock=clock)
    ex.set_prices(26_744.5, 26_744.5)
    monkeypatch.setattr(cli, "_live_settings", lambda: settings)
    lock_file_sha = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    monkeypatch.setattr(cli, "_live_lock", lambda: (lock_path, lock["sha256"], lock_file_sha))
    monkeypatch.setattr(live_app, "real_exchange", lambda s: ex)
    monkeypatch.setattr(live_app, "price_feed", lambda s: None)  # no WebSocket in tests
    monkeypatch.setattr(live_app, "update_cache", lambda product, cache_dir: FakeFetch(df, clock)(cache_dir))
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 0.0)
    return settings, ex, lock


def test_live_parser_has_no_strategy_options():
    ns = build_parser().parse_args(["live", "stage", "canary", "--confirm"])
    assert (ns.cmd, ns.live_cmd, ns.to, ns.confirm) == ("live", "stage", "canary", True)
    for argv in (["live", "run", "--params", "1"], ["live", "run", "--profile", "aggressive"],
                 ["live", "stage", "paper"], ["live", "backup"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


def test_the_price_feed_uses_the_public_endpoint_without_a_key():
    feed = live_app.price_feed(LiveSettings())  # no key file: the public ticker and heartbeats need none
    sock = feed.ws_factory(on_message=feed.on_message)  # built, never opened (no network)
    assert feed.products == ["BIP-20DEC30-CDE", "BTC-USD"] and sock.url == "wss://advanced-trade-ws.coinbase.com"


def test_preflight_offline_and_online(live_env, capsys, monkeypatch):
    assert main(["live", "preflight", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "preflight fingerprint: ok" in out and "preflight lock: ok - file sha256" in out and "key" not in out
    assert "preflight alerts: ok - WARNING: TELEGRAM_BOT_TOKEN" in out  # SHADOW without Telegram: a warning
    assert main(["live", "preflight"]) == 0
    assert "preflight product: ok" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_live_lock", lambda: (live_env[0].data_dir / "missing.lock", "0" * 64, "0" * 64))
    assert main(["live", "preflight", "--offline"]) == 2


def test_run_refuses_without_a_key_file_or_a_passing_preflight(live_env, monkeypatch, capsys):
    def no_key(s):
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")

    monkeypatch.setattr(live_app, "real_exchange", no_key)
    assert main(["live", "run"]) == 2 and "refusing" in capsys.readouterr().err
    settings, ex, lock = live_env
    transfer = SimExchange(permissions=KeyPermissions(True, True, True))
    monkeypatch.setattr(live_app, "real_exchange", lambda s: transfer)
    assert main(["live", "run"]) == 2 and "preflight key: FAILED" in capsys.readouterr().out
    # CANARY and FULL trade real money: no start without the Telegram alerts and commands and the heartbeat
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "test", NOW)
    jr.close()
    monkeypatch.setattr(live_app, "real_exchange", lambda s: ex)
    assert main(["live", "run"]) == 2
    out = capsys.readouterr().out
    assert "preflight alerts: FAILED - CANARY needs TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EWBOT_HEALTHCHECK_URL" in out
    # Coinbase unreadable at start after a passing preflight: the reads are retried, then the start is refused (exit 2,
    # the refusal wait) - never a fatal exit with a healthchecks.io /fail
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "test", NOW)
    jr.close()
    monkeypatch.setattr("ewbot.live.runtime.STARTUP_READ_MAX_S", 0.0)

    def down() -> None:
        raise ExchangeError("session: 503")

    ex.session = down
    assert main(["live", "run"]) == 2 and "refusing: session 조회 실패" in capsys.readouterr().err
    del ex.session
    # settings refused (e.g. a secret in the environment): `live run` waits too, the other commands do not
    waits: list[float] = []
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 7.0)
    monkeypatch.setattr("ewbot.cli.time.sleep", waits.append)

    def refused() -> LiveSettings:
        raise SettingsError("TELEGRAM_BOT_TOKEN is set in the environment")

    monkeypatch.setattr(cli, "_live_settings", refused)
    assert main(["live", "run"]) == 2 and main(["live", "status"]) == 2 and waits == [7.0]


def test_run_status_and_backup(live_env, monkeypatch, capsys, tmp_path):
    settings, ex, lock = live_env
    original = LiveRuntime.run_forever
    monkeypatch.setattr(LiveRuntime, "run_forever", lambda self, max_ticks=None: original(self, 2))
    monkeypatch.setattr("ewbot.live.runtime.time.time", lambda: NOW)
    assert main(["live", "run"]) == 0
    jr = Journal(settings.journal_path)
    assert jr.stage()[0] == "SHADOW" and [e["kind"] for e in jr.events(kind="STARTUP")] == ["STARTUP"]
    assert jr.cycles()[-1]["status"] == "OK"
    jr.close()
    capsys.readouterr()
    monkeypatch.setattr("ewbot.cli.time.time", lambda: NOW + 30)
    assert main(["live", "status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("단계 SHADOW") and "[미충족] 기간 14일 이상" in out
    assert main(["live", "status", "--json", "--exchange", "--max-age", "180"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stage"] == "SHADOW" and data["exchange_position"] is None and data["heartbeat_age"] == 30.0
    monkeypatch.setattr("ewbot.cli.time.time", lambda: NOW + 400)
    assert main(["live", "status", "--max-age", "180"]) == 1  # the Docker health check fails
    assert main(["live", "backup", "--out", str(tmp_path / "bk" / "j.sqlite")]) == 0
    assert (tmp_path / "bk" / "j.sqlite").exists()


def test_a_fatal_exit_tells_healthchecks_only_the_exception_type_and_stops_the_threads(live_env, monkeypatch, capsys):
    settings, _, _ = live_env
    token = "123456:SECRET-TOKEN"
    s = dataclasses.replace(settings, telegram_token=Secret(token), telegram_chat="42",
                            healthcheck_url=Secret("https://hc-ping.com/u1"))
    monkeypatch.setattr(cli, "_live_settings", lambda: s)
    posts: list[tuple[str, Any]] = []
    pings = SimpleNamespace(get=lambda url, timeout=None: SimpleNamespace(status_code=200),
                            post=lambda url, data=None, timeout=None: posts.append((url, data)) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(live_app, "Heartbeat", lambda url: Heartbeat(url, session=pings))
    sender = FakeSender()

    def started(_s: LiveSettings) -> LiveNotifier:  # the real notifier on a fake Telegram
        n = LiveNotifier(sender)
        n.start()
        return n

    monkeypatch.setattr(live_app, "notifier", started)

    class Control:  # no Telegram long poll in tests
        made: list[Any] = []

        def __init__(self, token: str, chat: str, journal: Journal) -> None:
            self.running = False
            Control.made.append(self)

        def start(self, commands: Any) -> None:
            self.running = True

        def stop(self) -> None:
            self.running = False

    monkeypatch.setattr(live_app, "TelegramControl", Control)
    watched: list[Any] = []

    def crash(self: LiveRuntime, max_ticks: int | None = None) -> int:
        watched.append(self.d.control)
        assert self.d.redact(f"bot{token} at https://hc-ping.com/u1") == "bot<secret> at <secret>"  # loop errors too
        raise RuntimeError(f"connect https://api.telegram.org/bot{token}/getUpdates failed")

    monkeypatch.setattr(LiveRuntime, "run_forever", crash)
    capsys.readouterr()
    assert main(["live", "run"]) == 1  # Docker starts it again
    err = capsys.readouterr().err
    assert "Traceback" in err and "bot<secret>/getUpdates" in err and token not in err  # the traceback, redacted
    assert posts == [("https://hc-ping.com/u1/fail", "[LIVE] 중단: RuntimeError".encode())]  # never the exception's text
    assert watched == Control.made and not Control.made[0].running  # the runtime watches it; stopped on the way out
    assert sender.texts == ["[LIVE] [중단] RuntimeError: connect https://api.telegram.org/bot<secret>/getUpdates failed"]


def test_status_without_a_journal(live_env, capsys):
    assert main(["live", "status"]) == 1 and "no journal" in capsys.readouterr().err
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 0  # first deployment
    assert json.loads(capsys.readouterr().out)["stage"] is None


def test_require_flat_sees_the_journal_trade_and_the_account(live_env, capsys):
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.put("trade", {"status": "OPEN", "trade_id": 1})
    jr.close()
    assert main(["live", "status", "--json", "--require-flat"]) == 3
    jr = Journal(settings.journal_path)
    jr.put("trade", {"status": "CLOSED", "trade_id": 1})
    jr.close()
    assert main(["live", "status", "--json", "--require-flat"]) == 0
    ex.place_market("manual", 1, 1)
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 3
    assert "not flat" in capsys.readouterr().err


def test_stage_changes_need_confirm_gates_and_a_flat_book(live_env, capsys):
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "initial", NOW - 86_400)
    jr.close()
    assert main(["live", "stage", "canary"]) == 2
    res = capsys.readouterr()
    assert "[미충족] ① 판단이 모의매매와 100% 일치" in res.out and "--confirm" in res.err
    assert main(["live", "stage", "canary", "--confirm"]) == 2 and "gates not met" in capsys.readouterr().err
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "test", NOW)
    jr.put("audit", AuditResult.failed("paper ledger fetch failed: HTTP 502", NOW).to_json())
    jr.close()
    assert main(["live", "status"]) == 0  # the CANARY gates read the runtime's last audit: a failed one is unmet
    assert "[미충족] 모의매매 대조 (최근 감사 통과): 감사 실패: paper ledger fetch failed: HTTP 502" in capsys.readouterr().out
    assert main(["live", "stage", "full", "--confirm"]) == 2 and "gates not met" in capsys.readouterr().err
    # a new CANARY stint after a CANARY failure: --confirm, a flat book, from CANARY only
    assert main(["live", "stage", "canary", "--restart"]) == 2 and "--confirm" in capsys.readouterr().err
    assert main(["live", "stage", "full", "--restart", "--confirm"]) == 2 and "SHADOW or CANARY" in capsys.readouterr().err
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 2 and "current stage" in capsys.readouterr().err
    def unreadable() -> None:
        raise ExchangeError("key_permissions: 503")

    ex.key_permissions = unreadable  # a restart needs no key gate: it is not read
    assert main(["live", "stage", "canary", "--restart", "--confirm"]) == 0 and "restarted" in capsys.readouterr().out
    del ex.key_permissions
    ex.place_market("manual", 1, 1)
    assert main(["live", "stage", "canary", "--restart", "--confirm"]) == 2 and "flat" in capsys.readouterr().err
    # a demotion is written first, then the book is reported: the runtime sizes for it at once
    assert main(["live", "stage", "shadow", "--confirm"]) == 0 and "book not flat" in capsys.readouterr().out
    # SHADOW again after a fix (spec 6.2): its own restart, with a flat book
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 2 and "flat" in capsys.readouterr().err
    ex.close_position("manual-x")
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 0 and "SHADOW stint restarted" in capsys.readouterr().out
    jr = Journal(settings.journal_path)
    assert jr.stage()[0] == "SHADOW"
    assert [e["data"]["reason"] for e in jr.events(kind="STAGE")] == ["restart", "demotion", "restart"]
    jr.close()
