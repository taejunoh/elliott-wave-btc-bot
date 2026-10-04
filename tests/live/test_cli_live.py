from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
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
    # the refusal wait) - never a fatal exit with a healthchecks.io /fail
    jr = Journal(settings.journal_path)
    jr.set_stage("SHADOW", "test", NOW)
    jr.close()
    monkeypatch.setattr("ewbot.live.runtime.STARTUP_READ_MAX_S", 0.0)

    def down() -> None:
        raise ExchangeError("session: 503")

    ex.session = down
    waits.clear()
    assert main(["live", "run"]) == 2 and "refusing: session 조회 실패" in capsys.readouterr().err and sum(waits) == 3.0
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
    monkeypatch.setattr(cli, "LIVE_REFUSAL_WAIT_S", 2.0)
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
