"""Production wiring of `ewbot live` (Plan 3): settings -> Coinbase exchange (ShadowExchange in SHADOW),
journal, [LIVE] notifier, WebSocket price feed (Coinbase's public endpoint: no key), Telegram control,
heartbeat and paper audit -> LiveRuntime. The products are fixed (FUTURES_PRODUCT, SPOT_PRODUCT: no setting
names them); the settings' secrets are revealed only here, where they are used. The CLI calls these factories;
tests replace `real_exchange`, `price_feed` and `paper_status` (no network)."""

from __future__ import annotations

import queue
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ewbot.data.coinbase_history import update_cache
from ewbot.live.audit import AuditError, fetch_paper_ledger
from ewbot.live.coinbase import CoinbaseExchange
from ewbot.live.control import Command, TelegramControl
from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT, Exchange
from ewbot.live.feed import TickerFeed, public_ws_factory
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier
from ewbot.live.runtime import LiveDeps, LiveRuntime
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.shadow import ShadowExchange
from ewbot.paper.criteria import golive_status
from ewbot.paper.ledger import Ledger, LedgerError
from ewbot.paper.notify import TelegramSender


def real_exchange(s: LiveSettings) -> Exchange:
    if s.key_file is None:
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")
    return CoinbaseExchange.from_key_file(s.key_file, FUTURES_PRODUCT, SPOT_PRODUCT)


def stage_exchange(real: Exchange, journal: Journal, stage: str, notify: Callable[[str, str], Any]) -> Exchange:
    """SHADOW trades virtually on real prices (previews only); CANARY and FULL trade for real."""
    return ShadowExchange(real, journal, alert=notify) if stage == "SHADOW" else real


def notifier(s: LiveSettings) -> LiveNotifier:
    """The [LIVE] notifier with its sending thread started: a call only queues, the loop never waits on Telegram."""
    sender = (TelegramSender(s.telegram_token.reveal(), s.telegram_chat)
              if s.telegram_token and s.telegram_chat else None)
    notify = LiveNotifier(sender)
    notify.start()
    return notify


def redact(text: str, s: LiveSettings) -> str:
    """`text` with the settings' secrets (bot token, GitHub token, health-check URL) replaced by <secret>."""
    for secret in (s.telegram_token, s.github_token, s.healthcheck_url):
        if secret:
            text = text.replace(secret.reveal(), "<secret>")
    return text


def fatal_report(exc: BaseException, s: LiveSettings) -> str:
    """A fatal exception's traceback for stderr, `redact`ed."""
    return redact("".join(traceback.format_exception(exc)), s)


def paper_fetcher(s: LiveSettings) -> Callable[[], dict[str, Any]] | None:
    if not (s.paper_repo and s.github_token):
        return None
    repo, token = s.paper_repo, s.github_token.reveal()
    return lambda: fetch_paper_ledger(repo, token)


def paper_status(s: LiveSettings, now: float) -> dict[str, Any] | None:
    """The paper runtime's go-live status (criteria 1-4) from its ledger, or None when unreadable."""
    fetch = paper_fetcher(s)
    if fetch is None:
        return None
    try:
        return golive_status(Ledger.from_dict(fetch()), int(now))
    except (AuditError, LedgerError, KeyError, TypeError, ValueError) as exc:
        print(f"paper ledger unreadable: {exc}", file=sys.stderr)
        return None


def price_feed(s: LiveSettings) -> TickerFeed | None:
    """The WebSocket ticker feed for the watchdog (public channels: no key, verified TLS)."""
    return TickerFeed([FUTURES_PRODUCT, SPOT_PRODUCT], public_ws_factory())


@dataclass
class Wired:
    runtime: LiveRuntime
    feed: TickerFeed | None
    control: TelegramControl | None
    heartbeat: Heartbeat
    notify: LiveNotifier

    def close(self, flush_s: float = 10.0) -> None:
        """Shutdown: stop the Telegram poller (its long poll ends with the process: a command fetched but not yet
        handled is lost - at most once) and the price feed, then give the queued messages up to flush_s to go out."""
        if self.control is not None:
            self.control.stop()
        if self.feed is not None:
            self.feed.stop()
        self.notify.flush(flush_s)
        self.notify.stop()


def build(s: LiveSettings, lock: dict[str, Any], real: Exchange, journal: Journal,
          notify: LiveNotifier | None = None) -> Wired:
    notify = notifier(s) if notify is None else notify
    stage, _ = journal.stage()
    ex = stage_exchange(real, journal, stage, notify)
    feed = price_feed(s)
    commands: queue.Queue[Command] = queue.Queue()
    control = (TelegramControl(s.telegram_token.reveal(), s.telegram_chat, journal)
               if s.telegram_token and s.telegram_chat else None)
    heartbeat = Heartbeat(s.healthcheck_url.reveal() if s.healthcheck_url else None)
    deps = LiveDeps(
        journal=journal, exchange=ex, lock=lock,
        fetch=lambda cache_dir: update_cache(SPOT_PRODUCT, Path(cache_dir)), cache_dir=s.cache_dir, notify=notify,
        clock=time.time, sleep=time.sleep, feed=feed, heartbeat=heartbeat, commands=commands, control=control,
        paper_ledger=paper_fetcher(s), edit_stops=s.edit_stops, expiry=dict(s.expiry),
        redact=lambda text: redact(text, s),
    )
    rt = LiveRuntime(deps)
    if feed is not None:
        feed.start()
    if control is not None:
        control.start(commands)
    return Wired(rt, feed, control, heartbeat, notify)
