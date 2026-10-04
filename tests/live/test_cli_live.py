from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import sqlite3
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.live import app as live_app
from ewbot.live import runtime as live_runtime_mod
from ewbot.live.audit import AuditResult
from ewbot.live.exchange import ExchangeError, KeyPermissions, Unavailable
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier
from ewbot.live.reconcile import is_ours
from ewbot.live.runtime import FLATTEN_KEY, LiveRuntime
from ewbot.live.settings import LiveSettings, Secret, SettingsError
from ewbot.live.shadow import ShadowExchange
from ewbot.live.sim import SimExchange
from ewbot.live.stage import SHADOW_PASS, Gate
from tests.live.harness import Harness
from tests.live.helpers import Clock
from tests.paper.helpers import FakeFetch, FakeSender, make_lock, synth_frame, ts

NOW = ts("2024-03-13 23:00") + 30
IN_CYCLE_WINDOW = cli._in_cycle_window  # the live_env fixture opens the window; its own test puts it back
ENTRY_BAR = ts("2024-03-13 23:00")  # the synthetic C_END long's entry bar (signal 22:00)
ALERTS = {"telegram_token": Secret("123456:SECRET-TOKEN"), "telegram_chat": "42",
          "healthcheck_url": Secret("https://hc-ping.com/u1")}


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Settings in a temp dir, a temp holdout lock (pinned by its own sha256 field and file sha256), a SimExchange
    instead of Coinbase, synthetic candles, the preflight's clock pinned to the test's."""
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
    monkeypatch.setattr("ewbot.live.preflight.time", SimpleNamespace(time=lambda: NOW))  # the contract's 90 days
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 0.0)
    monkeypatch.setattr(cli, "_in_cycle_window", lambda now: False)  # promotions at any minute of the wall clock
    return settings, ex, lock


class FakeControl:
    """TelegramControl without the long poll."""
    made: list[Any] = []

    def __init__(self, token: str, chat: str, journal: Journal) -> None:
        self.running = False
        FakeControl.made.append(self)

    def start(self, commands: Any) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


@pytest.fixture
def alerting(live_env, monkeypatch):
    """live_env with the Telegram alerts and the heartbeat configured: a fake Telegram (the real notifier), a fake
    healthchecks.io session and a Telegram control without the long poll. Returns (settings, sender, posts)."""
    settings, _, _ = live_env
    s = dataclasses.replace(settings, **ALERTS)
    monkeypatch.setattr(cli, "_live_settings", lambda: s)
    posts: list[tuple[str, Any]] = []
    ok = SimpleNamespace(status_code=200)
    pings = SimpleNamespace(get=lambda url, timeout=None: ok,
                            post=lambda url, data=None, timeout=None: posts.append((url, data)) or ok)
    monkeypatch.setattr(live_app, "Heartbeat", lambda url: Heartbeat(url, session=pings))
    sender = FakeSender()

    def started(_s: LiveSettings) -> LiveNotifier:  # the real notifier on a fake Telegram
        n = LiveNotifier(sender)
        n.start()
        return n

    monkeypatch.setattr(live_app, "notifier", started)
    FakeControl.made = []
    monkeypatch.setattr(live_app, "TelegramControl", FakeControl)
    return s, sender, posts


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
    monkeypatch.setattr("ewbot.cli.time.time", lambda: ts("2030-09-22 00:00"))  # 89 days before the contract expires
    assert main(["live", "preflight"]) == 0  # the tests' preflight runs on their own clock
    monkeypatch.setattr(cli, "_live_lock", lambda: (live_env[0].data_dir / "missing.lock", "0" * 64, "0" * 64))
    assert main(["live", "preflight", "--offline"]) == 2


def test_a_key_file_the_sdk_cannot_read_is_a_settings_error(tmp_path):
    bad = tmp_path / "cdp.json"
    bad.write_text("{}", encoding="utf-8")
    bad.chmod(0o600)
    with pytest.raises(SettingsError, match="Coinbase key file .* unusable"):
        live_app.real_exchange(LiveSettings(data_dir=tmp_path, key_file=bad))


def test_run_refuses_without_a_key_file_or_a_passing_preflight(live_env, monkeypatch, capsys):
    settings, ex, lock = live_env
    waits: list[float] = []
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 3.0)
    monkeypatch.setattr("ewbot.cli.time.sleep", waits.append)

    def no_key(s):
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")

    monkeypatch.setattr(live_app, "real_exchange", no_key)
    assert main(["live", "run"]) == 2 and "refusing" in capsys.readouterr().err and sum(waits) == 3.0

    def malformed(s):  # the SDK raises a bare Exception for a key file it cannot parse
        raise Exception("Error parsing key file: 'privateKey'")

    monkeypatch.setattr(live_app, "real_exchange", malformed)
    assert main(["live", "run"]) == 2 and "Error parsing key file" in capsys.readouterr().err and sum(waits) == 6.0
    transfer = SimExchange(permissions=KeyPermissions(True, True, True))
    monkeypatch.setattr(live_app, "real_exchange", lambda s: transfer)
    assert main(["live", "run"]) == 2 and "preflight key: FAILED" in capsys.readouterr().out and sum(waits) == 9.0
    # CANARY and FULL trade real money: no start without the Telegram alerts and commands and the heartbeat
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "test", NOW)
    jr.close()
    monkeypatch.setattr(live_app, "real_exchange", lambda s: ex)
    assert main(["live", "run"]) == 2
    out = capsys.readouterr().out
    assert "preflight alerts: FAILED - CANARY needs TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, EWBOT_HEALTHCHECK_URL" in out
    # Coinbase unreadable at start after a passing preflight: the reads are retried, then the start is refused (exit 2,
    # the short outage wait) - never a fatal exit with a healthchecks.io /fail
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "test", NOW)
    jr.close()
    monkeypatch.setattr("ewbot.live.runtime.STARTUP_READ_MAX_S", 0.0)
    monkeypatch.setattr(live_app, "OUTAGE_WAIT_S", 2.0)

    def down() -> None:
        raise ExchangeError("session: 503")

    ex.session = down
    waits.clear()
    assert main(["live", "run"]) == 2 and "refusing: session 조회 실패" in capsys.readouterr().err and sum(waits) == 2.0
    del ex.session
    # settings refused (e.g. a secret in the environment): `live run` waits too, the other commands do not
    waits.clear()
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 7.0)

    def refused() -> LiveSettings:
        raise SettingsError("TELEGRAM_BOT_TOKEN is set in the environment")

    monkeypatch.setattr(cli, "_live_settings", refused)
    assert main(["live", "run"]) == 2 and main(["live", "status"]) == 2 and sum(waits) == 7.0


def test_a_refused_start_stops_the_feed_and_flushes_before_its_wait(live_env, monkeypatch, capsys):
    settings, ex, _ = live_env

    class Feed:
        running = False

        def start(self) -> None:
            Feed.running = True

        def stop(self) -> None:
            Feed.running = False

        def take_resync(self) -> bool:
            return False

        def latest(self, product_id: str) -> None:
            return None

        def age(self) -> None:
            return None

    seen: list[bool] = []
    monkeypatch.setattr(live_app, "price_feed", lambda s: Feed())
    monkeypatch.setattr(live_app, "OUTAGE_WAIT_S", 2.0)  # the start's reads failed: an outage
    monkeypatch.setattr("ewbot.cli.time.sleep", lambda s: seen.append(Feed.running))
    monkeypatch.setattr("ewbot.live.runtime.STARTUP_READ_MAX_S", 0.0)

    def down() -> None:
        raise ExchangeError("session: 503")

    ex.session = down
    assert main(["live", "run"]) == 2 and seen == [False, False]  # stopped before the wait, not after it


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

    def down() -> None:
        raise ExchangeError("positions: 503")

    ex.position = down  # Coinbase unreadable: exit 2 (1 means a stale heartbeat), the reason on stderr
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 2
    assert "exchange unreadable: positions: 503" in capsys.readouterr().err
    del ex.position

    def malformed(s):
        raise Exception("Error parsing key file: 'privateKey'")

    monkeypatch.setattr(live_app, "real_exchange", malformed)
    assert main(["live", "status", "--exchange"]) == 2 and "Error parsing key file" in capsys.readouterr().err
    assert main(["live", "stage", "canary", "--confirm"]) == 2 and "refusing" in capsys.readouterr().err


def test_backup_and_stage_need_a_journal(live_env, capsys, tmp_path):
    settings, _, _ = live_env
    assert main(["live", "backup", "--out", str(tmp_path / "bk.sqlite")]) == 1
    assert "no journal" in capsys.readouterr().err
    assert main(["live", "stage", "shadow", "--confirm"]) == 1 and "no journal" in capsys.readouterr().err
    assert not settings.journal_path.exists() and not (tmp_path / "bk.sqlite").exists()


def test_a_fatal_exit_tells_healthchecks_only_the_exception_type_and_stops_the_threads(alerting, monkeypatch, capsys):
    s, sender, posts = alerting
    token = ALERTS["telegram_token"].reveal()
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
    assert watched == FakeControl.made and not FakeControl.made[0].running  # watched by the runtime, stopped at exit
    assert sender.texts == ["[LIVE] [중단] RuntimeError: connect https://api.telegram.org/bot<secret>/getUpdates failed"]


def test_a_failing_build_is_fatal_and_repeated_fatals_back_off(alerting, monkeypatch, capsys):
    s, sender, posts = alerting
    jr = Journal(s.journal_path)
    jr.put("trade", {"status": "OPEN", "trade_id": 1})  # a journal trade the runtime cannot load
    jr.close()
    waits: list[float] = []
    monkeypatch.setattr("ewbot.cli.time.sleep", waits.append)
    clock = [NOW]
    monkeypatch.setattr("ewbot.cli.time.time", lambda: clock[0])
    assert main(["live", "run"]) == 1 and waits == []  # the first fatal exits at once
    assert "TypeError" in capsys.readouterr().err
    assert posts == [("https://hc-ping.com/u1/fail", "[LIVE] 중단: TypeError".encode())]
    assert [t.split(":")[0] for t in sender.texts] == ["[LIVE] [중단] TypeError"]
    clock[0] += 120  # Docker started it again: the same error 2 minutes later waits before exiting, alerted once
    assert main(["live", "run"]) == 1 and sum(waits) == live_app.FATAL_WAIT_S
    assert len(posts) == 1 and len(sender.texts) == 1
    clock[0] += 120
    waits.clear()
    assert main(["live", "run"]) == 1 and sum(waits) == 2 * live_app.FATAL_WAIT_S  # the wait doubles
    clock[0] += 3700  # more than 15 minutes later: a new episode, and an hour later alerted again
    waits.clear()
    assert main(["live", "run"]) == 1 and waits == [] and len(posts) == 2 and len(sender.texts) == 2


def test_a_stop_signal_ends_the_run_after_its_tick_without_a_failure_alert(alerting, monkeypatch, capsys):
    s, sender, posts = alerting
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr("ewbot.live.runtime.time.time", lambda: NOW)
    monkeypatch.setattr("ewbot.cli.time.sleep", lambda seconds: None)
    ticks: list[int] = []
    original = LiveRuntime.tick

    def tick(self: LiveRuntime) -> None:
        original(self)
        ticks.append(1)
        if len(ticks) == 1:
            assert signal.getsignal(signal.SIGTERM) is not before, "no SIGTERM handler while running"
            os.kill(os.getpid(), signal.SIGTERM)  # docker stop

    monkeypatch.setattr(LiveRuntime, "tick", tick)
    assert main(["live", "run"]) == 0 and len(ticks) == 1  # the tick finished, no further one
    assert posts == [] and not any("[중단]" in t for t in sender.texts) and not FakeControl.made[0].running
    assert sender.texts[-1].startswith("[LIVE] [종료]") and signal.getsignal(signal.SIGTERM) is before
    jr = Journal(s.journal_path)
    assert [e["kind"] for e in jr.events(kind="STOPPED")] == ["STOPPED"]
    jr.close()

    def interrupted(self: LiveRuntime, max_ticks: int | None = None) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(LiveRuntime, "run_forever", interrupted)
    assert main(["live", "run"]) == 0 and posts == [] and not any("[중단]" in t for t in sender.texts)


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


def test_require_flat_is_the_runtimes_flat_book(live_env, capsys):
    """deploy.sh swaps only what the running runtime would call flat (LiveRuntime.flat): an order of unknown outcome
    or a standing flatten keeps the book open even with no trade and nothing on the exchange."""
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.record_intent("ewb-e-1700000000-L", "ENTRY", 1_700_000_000, {"side": 1, "contracts": 1}, NOW)
    jr.close()
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out)["pending_intents"] == 1 and "1 order(s) of unknown outcome" in captured.err
    jr = Journal(settings.journal_path)
    jr.update_intent("ewb-e-1700000000-L", "ABANDONED", NOW + 600)
    jr.put(FLATTEN_KEY, {"reason": "USER_FLATTEN", "origin": "USER", "ts": NOW, "deferred": True})
    jr.close()
    assert main(["live", "status", "--json", "--require-flat"]) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out)["flatten"]["reason"] == "USER_FLATTEN" and "a standing flatten" in captured.err
    jr = Journal(settings.journal_path)
    jr.put(FLATTEN_KEY, None)  # cleared once the account was flat (FLATTENED)
    jr.close()
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending_intents"] == 0 and out["flatten"] is None and out["exchange_open_orders"] == 0


def test_stage_changes_need_confirm_gates_and_a_flat_book(live_env, monkeypatch, capsys):
    settings, ex, _ = live_env
    monkeypatch.setattr(cli, "_live_settings", lambda: dataclasses.replace(settings, **ALERTS))
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


def test_a_promotion_reads_the_paper_criteria_and_needs_the_alerts(live_env, monkeypatch, capsys):
    settings, ex, _ = live_env
    paper = dataclasses.replace(settings, github_token=Secret("ghp_SECRET"), paper_repo="me/repo")
    monkeypatch.setattr(cli, "_live_settings", lambda: paper)
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "initial", NOW - 20 * 86_400)
    jr.close()
    monkeypatch.setattr("ewbot.live.stage.shadow_gates",
                        lambda journal, now, audit: [Gate(n, True, "ok") for n in SHADOW_PASS])
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(live_app, "fetch_paper_ledger",
                        lambda repo, token: fetched.append((repo, token)) or {"ledger": 1})
    monkeypatch.setattr(live_app.Ledger, "from_dict", staticmethod(lambda d: ("ledger", d)))
    status = {"c1": True, "c2": True, "c3": False, "c4": {"met": True, "days": 30.0, "coverage": 1.0, "unexplained": 0},
              "count": {"total": 12, "required": 50}}

    def broken(ledger: Any, now: int) -> dict[str, Any]:  # an epoch-less ledger: golive_status raises IndexError
        raise IndexError("list index out of range")

    monkeypatch.setattr(live_app, "golive_status", broken)
    assert main(["live", "stage", "canary", "--confirm"]) == 2  # the paper status unreadable: unmet, never a crash
    res = capsys.readouterr()
    assert "[미충족] 모의매매 판정: paper ledger를 읽지 못함" in res.out and "IndexError" in res.err
    seen: list[Any] = []
    monkeypatch.setattr(live_app, "golive_status", lambda ledger, now: seen.append(ledger) or status)
    assert main(["live", "stage", "canary", "--confirm"]) == 2  # every gate met but no Telegram alerts and heartbeat
    res = capsys.readouterr()
    assert "[미충족] 알림 설정" in res.out and "CANARY needs TELEGRAM_BOT_TOKEN" in res.err
    jr = Journal(settings.journal_path)
    assert jr.stage()[0] == "SHADOW"  # refused before anything was written
    jr.close()
    monkeypatch.setattr(cli, "_live_settings", lambda: dataclasses.replace(paper, **ALERTS))
    assert main(["live", "stage", "canary", "--confirm"]) == 0
    out = capsys.readouterr().out
    assert "[충족] 모의매매 1번 (위험 대비 수익): True" in out and "[충족] 알림 설정" in out  # paper_status -> gates
    assert "stage SHADOW -> CANARY recorded" in out and fetched[-1] == ("me/repo", "ghp_SECRET")
    assert seen[-1] == ("ledger", {"ledger": 1})
    jr = Journal(settings.journal_path)
    assert jr.stage()[0] == "CANARY"
    jr.close()


def test_a_demotion_with_a_real_position_keeps_it_managed_across_a_restart(tmp_path, monkeypatch, capsys):
    hs = Harness(tmp_path, ENTRY_BAR + 30, stage="CANARY")
    hs.price(float(hs.bar(ENTRY_BAR)["open"]))
    hs.rt.startup()
    hs.at(ENTRY_BAR + 30)
    real = hs.ex
    assert real.position() is not None and [o for o in real.open_orders() if is_ours(o)]
    s = LiveSettings(data_dir=tmp_path)
    lock_path = tmp_path / "holdout.lock"
    lock_path.write_text(json.dumps(hs.lock), encoding="utf-8")
    monkeypatch.setattr(cli, "_live_settings", lambda: s)
    monkeypatch.setattr(cli, "_live_lock", lambda: (lock_path, hs.lock["sha256"],
                                                    hashlib.sha256(lock_path.read_bytes()).hexdigest()))
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 0.0)
    monkeypatch.setattr(live_app, "real_exchange", lambda _s: real)
    monkeypatch.setattr(live_app, "price_feed", lambda _s: None)
    monkeypatch.setattr(live_app, "update_cache", lambda product, cache_dir: hs.fetch(cache_dir))
    monkeypatch.setattr("ewbot.live.preflight.time", SimpleNamespace(time=hs.clock))
    monkeypatch.setattr("ewbot.cli.time.time", hs.clock)  # the CLI and the wiring on the harness clock
    original = LiveRuntime.run_forever
    monkeypatch.setattr(LiveRuntime, "run_forever", lambda self, max_ticks=None: original(self, 1))
    assert main(["live", "stage", "shadow", "--confirm"]) == 0 and "book not flat" in capsys.readouterr().out
    # the container restarts before the book is flat: the start runs CANARY's preflight (here refused: no alerts)
    assert main(["live", "run"]) == 2
    res = capsys.readouterr()
    assert "preflight alerts: FAILED - CANARY needs" in res.out and "stage CANARY:" in res.err
    stage, why = live_app.process_stage(hs.journal, real)
    assert stage == "CANARY" and "1710367200" in str(why)  # the journal's trade (signal 22:00) is real
    wired = live_app.build(s, hs.lock, real, hs.journal, stage)
    rt = wired.runtime
    assert rt.ex is real and rt.stage == "CANARY" and not isinstance(rt.ex, ShadowExchange)
    rt.mono = hs.clock  # the loop's cadence on the harness clock too
    rt.startup()
    hs.clock.now += 11
    rt.tick()  # the real position is managed: its real bracket rests, no entry is sized for SHADOW
    assert real.position() is not None and [o for o in real.open_orders() if is_ours(o)] and rt.exit_code is None
    now = hs.clock()
    rt.handle("/flatten", now, now)
    assert rt.handle("/flatten_confirm", now, now + 1).startswith("전량 청산 완료")
    assert real.position() is None  # the REAL position, not a virtual one
    hs.clock.now += 61
    rt.tick()
    assert not [o for o in real.open_orders() if is_ours(o)] and rt.exit_code == 0  # flat: restarts into SHADOW
    wired.close(0.0)
    assert live_app.process_stage(hs.journal, real) == ("SHADOW", None)
    wired = live_app.build(s, hs.lock, real, hs.journal, "SHADOW")
    assert isinstance(wired.runtime.ex, ShadowExchange)
    wired.close(0.0)
    hs.journal.close()


def test_the_process_stage_never_hides_real_exposure(live_env, monkeypatch, capsys):
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    assert live_app.process_stage(jr, ex) == ("SHADOW", None)  # a fresh journal, a flat account
    ex.place_market("manual", 1, 1)  # a position on the real account of a journal never CANARY or FULL
    with pytest.raises(live_app.RealExposure):
        live_app.process_stage(jr, ex)
    jr.close()
    assert main(["live", "run"]) == 2 and "실계좌" in capsys.readouterr().err  # refused, never hidden behind SHADOW
    ex.close_position("manual-x")
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "promotion", NOW - 7200)
    jr.set_stage("SHADOW", "demotion", NOW - 3600)
    assert live_app.process_stage(jr, ex) == ("SHADOW", None)  # demoted and flat: SHADOW
    jr.record_intent("ewb-x-5-1", "CLOSE", 5, {"reason": "X"}, NOW)  # an outcome unknown on the real account
    assert live_app.process_stage(jr, ex)[0] == "CANARY"
    jr.update_intent("ewb-x-5-1", "ABANDONED", NOW)
    jr.put("trade", {"status": "OPEN", "trade_id": 7, "legs": [{"order_id": "v3-sim-2"}]})
    jr.record_intent("ewb-e-7-L", "ENTRY", 7, {}, NOW)
    jr.update_intent("ewb-e-7-L", "ACCEPTED", NOW, order_id="v3-sim-1")
    jr.record_intent("ewb-x-7-1", "CLOSE", 7, {"reason": "X"}, NOW)  # pending, of a virtual trade
    assert live_app.process_stage(jr, ex) == ("SHADOW", None)  # every order id of the trade is a virtual book's
    jr.update_intent("ewb-x-7-1", "ACCEPTED", NOW, order_id="9f1c-real")
    assert live_app.process_stage(jr, ex)[0] == "CANARY"  # one real id: real
    jr.put("trade", None)
    jr.update_intent("ewb-x-7-1", "ABANDONED", NOW)

    def down() -> None:
        raise ExchangeError("positions: 503")

    ex.position = down
    assert live_app.process_stage(jr, ex)[0] == "CANARY"  # unreadable: real and managed, never hidden
    jr.close()


def test_build_wraps_only_shadow_and_reminds_of_the_contract(live_env):
    settings, ex, lock = live_env
    jr = Journal(settings.journal_path)
    notify = live_app.notifier(settings)
    for stage, virtual in (("SHADOW", True), ("CANARY", False), ("FULL", False)):
        wired = live_app.build(settings, lock, ex, jr, stage, notify)
        assert isinstance(wired.runtime.ex, ShadowExchange) is virtual and wired.runtime.stage == stage
        assert wired.runtime.d.expiry == {"BIP-20DEC30-CDE 만기": "2030-12-20",
                                          "BIP-20DEC30-CDE 시작 점검 거부 (만기 90일 전)": "2030-09-21"}
    for odd in ("canary", "PAPER", ""):  # anything but CANARY or FULL trades virtually
        assert isinstance(live_app.stage_exchange(ex, jr, odd, notify), ShadowExchange)
    notify.stop()
    jr.close()


def test_redact_takes_every_form_of_the_secrets_out():
    hc = "https://hc-ping.com/0b5c2a3e-1111-4222-8333-944455556666?create=1"
    s = LiveSettings(telegram_token=Secret("123456:SECRET-TOKEN"), github_token=Secret("github_pat_ABC/DEF+1"),
                     healthcheck_url=Secret(hc))
    text = ("HTTPSConnectionPool(host='hc-ping.com', port=443): Max retries exceeded with url: "
            "/0b5c2a3e-1111-4222-8333-944455556666/fail?create=1 · "
            "https://api.telegram.org/bot123456%3ASECRET-TOKEN/getMe · token=github_pat_ABC%2FDEF%2B1 · "
            "check 0b5c2a3e-1111-4222-8333-944455556666 · " + hc)
    out = live_app.redact(text, s)
    for leak in ("0b5c2a3e", "SECRET-TOKEN", "github_pat", "create=1"):
        assert leak not in out, out
    assert "hc-ping.com" in out and "/getMe" in out


def test_paper_status_fails_closed_on_any_error(monkeypatch, capsys):
    s = LiveSettings(github_token=Secret("ghp_SECRET"), paper_repo="me/repo")

    def boom(repo: str, token: str) -> dict[str, Any]:
        raise RuntimeError(f"token {token} refused")

    monkeypatch.setattr(live_app, "fetch_paper_ledger", boom)
    assert live_app.paper_status(s, NOW) is None
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "ghp_SECRET" not in err


# ---------------------------------------------------------------------------------------------------------------------
# final fix


def test_a_start_refused_for_a_coinbase_outage_starts_again_soon_and_alerts_once_an_hour(alerting, monkeypatch, capsys):
    """Final fix (Task 12): Coinbase unreadable at start (the preflight's key or product read: 429, 5xx, timeout - or the
    start's own reads) is an outage, not a configuration error: tried again after OUTAGE_WAIT_S, not 10 minutes, its
    [시작 거부] alert sent once an hour across restarts. A configuration refusal keeps the long wait and its alert."""
    s, sender, posts = alerting
    ex = live_app.real_exchange(s)
    waits: list[float] = []
    monkeypatch.setattr("ewbot.cli.time.sleep", waits.append)
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 9.0)
    monkeypatch.setattr(live_app, "OUTAGE_WAIT_S", 3.0)
    clock = [NOW]
    monkeypatch.setattr("ewbot.cli.time.time", lambda: clock[0])
    ex.fail_next("key_permissions", Unavailable("503"), times=2)  # the preflight's key read, at both starts
    assert main(["live", "run"]) == 2 and sum(waits) == 3.0 and "preflight key: FAILED" in capsys.readouterr().out
    clock[0] += 70
    assert main(["live", "run"]) == 2 and sum(waits) == 6.0
    refusals = [t for t in sender.texts if "[시작 거부]" in t]
    assert len(refusals) == 1 and "3초마다 다시 시작" in refusals[0] and posts == []  # once; never a /fail
    transfer = SimExchange(permissions=KeyPermissions(True, True, True))  # a configuration problem
    monkeypatch.setattr(live_app, "real_exchange", lambda _s: transfer)
    waits.clear()
    assert main(["live", "run"]) == 2 and sum(waits) == 9.0
    assert len([t for t in sender.texts if "[시작 거부]" in t]) == 2


def test_the_account_is_the_bots_only_through_its_orders_or_the_journal(live_env, monkeypatch, capsys):
    """Final fix (Task 13 N-1, N-2): a position with no order of ours and nothing real in the journal is the user's own
    - the start is refused, never traded (a demoted journal ran it as CANARY and closed or adopted it) nor run beside;
    with a bracket of ours resting it is the bot's (managed in the last real stage). A journal never CANARY or FULL
    beside an account that cannot be read is refused (nothing proves it flat) - as an outage."""
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "promotion", NOW - 7200)
    jr.set_stage("SHADOW", "demotion", NOW - 3600)
    ex.place_market("manual", -1, 2)  # the user's own short, after the demotion
    with pytest.raises(live_app.RealExposure, match="수동 포지션") as err:
        live_app.process_stage(jr, ex)
    assert not isinstance(err.value, live_app.AccountUnreadable)
    ex.place_bracket("ewb-b-7-tp1-3", 1, 2, 26_000.0, 27_500.0)  # a bracket of ours on it: the bot's
    assert live_app.process_stage(jr, ex) == ("CANARY", "실계좌에 포지션 숏 2계약, 봇 주문 1건")
    jr.close()
    fresh = Journal(settings.journal_path.parent / "fresh.sqlite")

    def down() -> None:
        raise ExchangeError("positions: 503")

    ex.position = down
    with pytest.raises(live_app.AccountUnreadable, match="실계좌를 읽지 못함"):
        live_app.process_stage(fresh, ex)  # never SHADOW beside what may be real
    assert live_app.process_stage(fresh, None) == ("SHADOW", None)  # offline: the journal alone
    fresh.close()
    waits: list[float] = []
    monkeypatch.setattr("ewbot.cli.time.sleep", waits.append)
    monkeypatch.setattr(live_app, "OUTAGE_WAIT_S", 4.0)
    settings.journal_path.unlink()  # a fresh journal on an account that cannot be read: refused as an outage
    assert main(["live", "run"]) == 2 and sum(waits) == 4.0 and "실계좌를 읽지 못함" in capsys.readouterr().err


def test_a_crash_loop_reaches_the_longest_wait_and_waits_short_while_something_real_is_open(tmp_path):
    """Final fix (Task 13 N-3, N-4): the repeat is judged from the previous fatal's exit (its time plus its wait), so a
    deterministic crash loop climbs to FATAL_WAIT_MAX_S instead of resetting once a wait passed 15 minutes; while the
    journal shows something real to manage the wait stays at FATAL_EXPOSED_WAIT_S (the brackets protect meanwhile)."""
    def loop(jr: Journal) -> list[float]:
        now, out = NOW, []
        for _ in range(9):
            wait, _ = live_app.fatal_backoff(jr, "TypeError", now)
            out.append(wait)
            now += wait + 20.0  # Docker starts it again; it crashes 20 s later
        return out

    flat = Journal(tmp_path / "flat.sqlite")
    assert loop(flat) == [0.0, 60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0, 1800.0]
    flat.close()
    real = Journal(tmp_path / "real.sqlite")
    real.put("trade", {"status": "OPEN", "trade_id": 7, "legs": [{"order_id": "9f1c-real"}]})
    assert loop(real) == [0.0, 60.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0]
    real.close()


def test_live_preflight_checks_the_stage_the_run_would_run(live_env, monkeypatch, capsys):
    """Final fix (Task 13 N-6): a demoted journal with something real left runs its last real stage (CANARY: the alerts
    are required) - `live preflight` checks that stage, online and offline (the journal alone), and refuses like the run
    when the account holds a position that is not the bot's."""
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.set_stage("CANARY", "promotion", NOW - 7200)
    jr.set_stage("SHADOW", "demotion", NOW - 3600)
    jr.put("trade", {"status": "OPEN", "trade_id": 7, "legs": [{"order_id": "9f1c-real"}]})
    jr.close()
    for argv in (["live", "preflight"], ["live", "preflight", "--offline"]):
        assert main(argv) == 2
        res = capsys.readouterr()
        assert "stage CANARY: 저널의 거래 7가 실계좌에서 열려 있음" in res.err
        assert "preflight alerts: FAILED - CANARY needs TELEGRAM_BOT_TOKEN" in res.out
    jr = Journal(settings.journal_path)
    jr.put("trade", None)
    jr.close()
    ex.place_market("manual", 1, 1)
    assert main(["live", "preflight"]) == 2 and "수동 포지션" in capsys.readouterr().err
    assert main(["live", "preflight", "--offline"]) == 0  # offline: the journal alone (SHADOW)


def test_a_settings_refusal_waits_under_the_stop_handler(live_env, monkeypatch, capsys):
    """Final fix (Task 13 N-7): the 10-minute wait of a settings refusal runs with the SIGTERM handler installed, so
    `docker stop` ends it at once instead of after the stop grace period's SIGKILL."""
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 30.0)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not before, "no SIGTERM handler during the refusal wait"
        if len(slept) == 2:
            handler(signal.SIGTERM, None)  # what `docker stop` delivers

    monkeypatch.setattr("ewbot.cli.time.sleep", sleep)

    def refused() -> LiveSettings:
        raise SettingsError("TELEGRAM_BOT_TOKEN is set in the environment")

    monkeypatch.setattr(cli, "_live_settings", refused)
    assert main(["live", "run"]) == 2 and slept == [1.0, 1.0] and signal.getsignal(signal.SIGTERM) is before


def test_a_promotion_or_restart_needs_the_runtimes_flat_book_and_a_quiet_minute(live_env, monkeypatch, capsys):
    """Final review B minor 4: a promotion's flat book is the running runtime's (LiveRuntime.flat): an order of unknown
    outcome or a standing flatten keeps it open, like an open trade, a position or a resting order; and promotions and
    stint restarts are refused from hh:59 to hh:06 UTC, when the hour's cycle may open a (virtual) trade between the
    check and the write. Demotions are never refused."""
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "initial", NOW - 86_400)
    jr.record_intent("ewb-e-1700000000-L", "ENTRY", 1_700_000_000, {"side": 1, "contracts": 1}, NOW)
    jr.close()
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 2
    err = capsys.readouterr().err
    assert "not flat" in err and "unknown outcome" in err
    jr = Journal(settings.journal_path)
    jr.update_intent("ewb-e-1700000000-L", "ABANDONED", NOW + 600)
    jr.put(FLATTEN_KEY, {"reason": "USER_FLATTEN", "origin": "USER", "ts": NOW, "deferred": False})
    jr.close()
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 2 and "a standing flatten" in capsys.readouterr().err
    jr = Journal(settings.journal_path)
    jr.put(FLATTEN_KEY, None)
    jr.close()
    monkeypatch.setattr(cli, "_in_cycle_window", IN_CYCLE_WINDOW)
    for minute, quiet in ((58, False), (59, True), (0, True), (5, True), (6, False)):
        assert IN_CYCLE_WINDOW(ts("2024-03-14 10:00") + 60 * minute + 30) is quiet
    monkeypatch.setattr("ewbot.cli.time.time", lambda: ts("2024-03-14 10:02"))
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 2
    assert "hh:59" in capsys.readouterr().err
    assert main(["live", "stage", "canary", "--confirm"]) == 2 and "hh:59" in capsys.readouterr().err
    monkeypatch.setattr("ewbot.cli.time.time", lambda: ts("2024-03-14 10:20"))
    assert main(["live", "stage", "shadow", "--restart", "--confirm"]) == 0


def test_a_rebase_is_requested_with_confirm_and_refused_while_a_flatten_stands(live_env, monkeypatch, capsys):
    """Final review B-2: after a deliberate deposit or withdrawal, `ewbot live rebase --confirm` asks the runtime (the
    breakers' one writer) to re-base both references at its next minute; it prints before and after, needs --confirm,
    and is refused while a flatten stands or a tripped breaker still waits for its flatten."""
    settings, ex, _ = live_env
    monkeypatch.setattr("ewbot.cli.time.time", lambda: NOW)  # New York 2024-03-13
    assert main(["live", "rebase", "--confirm"]) == 1  # no journal
    jr = Journal(settings.journal_path)
    jr.put("account_guard", {"day": "2024-03-13", "day_ref": 10_000.0, "peak": 10_500.0, "daily_tripped": False,
                             "total_tripped": False})
    jr.close()
    assert main(["live", "rebase"]) == 2
    res = capsys.readouterr()
    assert "10,000.00" in res.out and "10,500.00" in res.out and "equity now 10,000.00" in res.out
    assert "--confirm" in res.err
    jr = Journal(settings.journal_path)
    assert jr.get(live_runtime_mod.REBASE_KEY) is None
    jr.put(FLATTEN_KEY, {"reason": "BREAKER_DAILY_LOSS", "origin": "BREAKER", "ts": NOW, "deferred": False})
    jr.close()
    assert main(["live", "rebase", "--confirm"]) == 2 and "flatten" in capsys.readouterr().err
    jr = Journal(settings.journal_path)
    jr.put(FLATTEN_KEY, None)
    jr.put("account_guard", {"day": "2024-03-13", "day_ref": 10_000.0, "peak": 10_500.0, "daily_tripped": True,
                             "total_tripped": False})
    jr.put("trade", {"status": "OPEN", "trade_id": 1})
    jr.close()
    assert main(["live", "rebase", "--confirm"]) == 2 and "breaker" in capsys.readouterr().err
    jr = Journal(settings.journal_path)
    jr.put("trade", None)
    jr.close()
    assert main(["live", "rebase", "--confirm"]) == 0 and "requested" in capsys.readouterr().out
    jr = Journal(settings.journal_path)
    assert jr.get(live_runtime_mod.REBASE_KEY)["ts"] > 0 and jr.events(kind="REBASE_REQUESTED")
    jr.close()


def test_status_and_preflight_open_the_journal_read_only(live_env, capsys):
    """Final review B minor 12: a candidate image's probes (`live status`, `live preflight`) never write, migrate or
    create the live journal (a newer schema would make the running image refuse it on a rollback)."""
    settings, ex, _ = live_env
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "initial", NOW - 3600)
    jr.close()
    raw = sqlite3.connect(settings.journal_path)
    raw.execute("PRAGMA user_version = 0")
    raw.close()
    before = settings.journal_path.read_bytes()
    assert main(["live", "status", "--json", "--exchange", "--require-flat"]) == 0
    assert main(["live", "status"]) == 0 and main(["live", "preflight", "--offline"]) == 0
    assert main(["live", "preflight"]) == 0
    capsys.readouterr()
    assert settings.journal_path.read_bytes() == before
    raw = sqlite3.connect(settings.journal_path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()


def test_an_unreadable_candle_cache_is_set_aside_and_downloaded_again(tmp_path, monkeypatch):
    """Final review B minor 6: the hourly cache write is not atomic (and data/coinbase_history.py is protected): a kill
    in the middle of it left a cache no later download could read - every hour held. The live download moves an
    unreadable cache aside (<name>.bad-<ts>), journals and alerts it, and downloads the history again."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "BTC-USD_1h.parquet").write_bytes(b"PAR1 half a file")
    df = synth_frame()
    clock = Clock(NOW)
    calls: list[Any] = []

    def update(product, cache_dir):
        calls.append((product, cache_dir))
        return FakeFetch(df, clock)(cache_dir)

    monkeypatch.setattr(live_app, "update_cache", update)
    alerts: list[tuple[str, str]] = []
    jr = Journal(tmp_path / "journal.sqlite")
    fetch = live_app.candle_fetch(lambda kind, text: alerts.append((kind, text)), jr, clock=clock)
    path = fetch(cache)
    assert (cache / f"BTC-USD_1h.parquet.bad-{int(NOW)}").read_bytes() == b"PAR1 half a file"
    assert path == cache / "BTC-USD_1h.parquet" and calls == [("BTC-USD", cache)] and len(pd.read_parquet(path)) > 0
    assert alerts[0][0] == "CACHE" and "BTC-USD_1h.parquet" in alerts[0][1]
    assert jr.events(kind="CACHE_SET_ASIDE")[0]["data"]["error"]
    fetch(cache)  # a readable cache is extended in place
    assert len(alerts) == 1 and len(list(cache.glob("*.bad-*"))) == 1
    jr.close()


def test_a_refusal_is_alerted_once_an_hour_per_reason(alerting, monkeypatch, capsys):
    """Final review B minor 8: a start refused for a configuration reason waits 10 minutes and Docker starts it again -
    its [시작 거부] went out every time (about 144 a day). Once an hour per reason now (the journal keeps the times),
    a new reason at once."""
    s, sender, posts = alerting
    monkeypatch.setattr("ewbot.cli.time.sleep", lambda seconds: None)
    clock = [NOW]
    monkeypatch.setattr("ewbot.cli.time.time", lambda: clock[0])
    transfer = SimExchange(permissions=KeyPermissions(True, True, True))
    monkeypatch.setattr(live_app, "real_exchange", lambda _s: transfer)
    for _ in range(3):
        assert main(["live", "run"]) == 2
        clock[0] += 610
    assert len([t for t in sender.texts if "[시작 거부]" in t]) == 1
    clock[0] += 3_600
    assert main(["live", "run"]) == 2 and len([t for t in sender.texts if "[시작 거부]" in t]) == 2
    viewer = SimExchange(permissions=KeyPermissions(True, False, False))  # another reason: alerted at once
    jr = Journal(s.journal_path)
    jr.set_stage("CANARY", "test", clock[0])
    jr.close()
    monkeypatch.setattr(live_app, "real_exchange", lambda _s: viewer)
    assert main(["live", "run"]) == 2 and len([t for t in sender.texts if "[시작 거부]" in t]) == 3
    capsys.readouterr()


def test_the_extra_refusal_codes_reach_the_runtime(live_env):
    settings, ex, lock = live_env
    s = dataclasses.replace(settings, close_conflicts=("BRACKETS_REST",), overlap_refusals=("TOO_MANY",))
    jr = Journal(s.journal_path)
    notify = live_app.notifier(s)
    wired = live_app.build(s, lock, ex, jr, "CANARY", notify)
    assert "BRACKETS_REST" in wired.runtime.executor.close_conflicts and "TOO_MANY" in wired.runtime.executor.overlap_refusals
    notify.stop()
    jr.close()
