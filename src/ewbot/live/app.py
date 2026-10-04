"""Production wiring of `ewbot live` (Plan 3): settings -> Coinbase exchange (ShadowExchange in SHADOW),
journal, [LIVE] notifier, WebSocket price feed (Coinbase's public endpoint: no key), Telegram control,
heartbeat and paper audit -> LiveRuntime. The products are fixed (FUTURES_PRODUCT, SPOT_PRODUCT: no setting
names them); the settings' secrets are revealed only here, where they are used. The CLI calls these factories;
tests replace `real_exchange`, `price_feed` and `paper_status` (no network).

Real exposure is never hidden behind the virtual book: `process_stage` keeps a process on the real account in the
last real stage while a demoted journal still holds something real (an open trade or an order of unknown outcome
of ours) or the real account shows a position or our orders, and `stage_exchange` wraps the exchange in
ShadowExchange only for SHADOW. `StopRequest` turns SIGTERM (`docker stop`) and SIGINT into a graceful stop;
`fatal_backoff` keeps a crash loop's restarts and alerts sparse."""

from __future__ import annotations

import contextlib
import queue
import re
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import quote, quote_plus, urlsplit

from ewbot.data.coinbase_history import update_cache
from ewbot.live.audit import fetch_paper_ledger
from ewbot.live.coinbase import CoinbaseExchange
from ewbot.live.control import Command, TelegramControl
from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT, Exchange, ExchangeError
from ewbot.live.feed import TickerFeed, public_ws_factory
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier
from ewbot.live.preflight import EXPIRY_MARGIN_DAYS, PRODUCT_EXPIRY
from ewbot.live.reconcile import is_ours
from ewbot.live.runtime import LiveDeps, LiveRuntime
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.shadow import ShadowExchange
from ewbot.paper.criteria import golive_status
from ewbot.paper.ledger import Ledger
from ewbot.paper.notify import TelegramSender

REAL_STAGES = ("CANARY", "FULL")
VIRTUAL_ORDER_ID = re.compile(r"v\d+-")  # live.shadow's virtual order ids: v<book>-<sim id>
FATAL_KEY = "fatal"  # journal: the last fatal exit (ts, type, streak) and the [중단] alerts sent per error type
FATAL_REPEAT_S = 900.0  # a fatal exit within this of the previous one waits before exiting ...
FATAL_WAIT_S = 60.0  # ... first this long, doubling with every repeat ...
FATAL_WAIT_MAX_S = 1800.0  # ... up to this
FATAL_ALERT_S = 3600.0  # one [중단] alert and healthchecks.io /fail per error type in this time, across restarts


class RealExposure(RuntimeError):
    """The real account holds a position or orders of ours, but the journal was never CANARY or FULL (lost or
    replaced): the start is refused rather than run SHADOW beside them."""


def real_exchange(s: LiveSettings) -> Exchange:
    """The Coinbase exchange. A missing key file, or one the SDK cannot use (it raises a bare Exception for a
    malformed file), is a SettingsError: a refused start, never a crash."""
    if s.key_file is None:
        raise SettingsError("EWBOT_COINBASE_KEY_FILE is not set")
    try:
        return CoinbaseExchange.from_key_file(s.key_file, FUTURES_PRODUCT, SPOT_PRODUCT)
    except Exception as exc:  # noqa: BLE001 - the SDK's errors name a field or a JSON position, never the key
        raise SettingsError(f"the Coinbase key file {s.key_file} is unusable: {type(exc).__name__}: "
                            f"{redact(str(exc), s)[:200]}") from None


def last_real_stage(journal: Journal) -> str | None:
    """The latest CANARY or FULL of the stage history (None: the journal was never real)."""
    return next((str(r["stage"]) for r in reversed(journal.stage_history()) if r["stage"] in REAL_STAGES), None)


def journal_exposure(journal: Journal) -> str | None:
    """What in the journal may be real: its OPEN trade or a PENDING intent (an outcome still unknown), unless order
    ids prove it virtual - there is one for its trade (intents, legs) and every one is a virtual book's (`v<n>-`).
    None: nothing real."""
    known: dict[Any, list[str]] = {}
    for row in journal.intents():
        if row["order_id"]:
            known.setdefault(row["trade_id"], []).append(str(row["order_id"]))

    def virtual(trade_id: Any, more: Sequence[Any] = ()) -> bool:
        ids = known.get(trade_id, []) + [str(x) for x in more if x]
        return bool(ids) and all(VIRTUAL_ORDER_ID.match(x) for x in ids)

    t = journal.get("trade")
    if t and t.get("status") == "OPEN" and not virtual(t.get("trade_id"),
                                                       [leg.get("order_id") for leg in t.get("legs") or []]):
        return f"저널의 거래 {t.get('trade_id')}가 실계좌에서 열려 있음"
    pending = [r for r in journal.intents(status="PENDING") if not virtual(r["trade_id"])]
    if pending:
        return f"실계좌 주문 {len(pending)}건의 결과 미확인 ({pending[0]['client_order_id']})"
    return None


def account_exposure(real: Exchange) -> str | None:
    """A position or orders of ours on the real account (None: neither); raises ExchangeError when unreadable."""
    pos = real.position()
    ours = [o for o in real.open_orders() if is_ours(o)]
    if pos is None and not ours:
        return None
    held = [] if pos is None else [f"포지션 {'롱' if pos.side > 0 else '숏'} {pos.contracts}계약"]
    return "실계좌에 " + ", ".join(held + ([f"봇 주문 {len(ours)}건"] if ours else []))


def process_stage(journal: Journal, real: Exchange) -> tuple[str, str | None]:
    """The stage this process runs, and why when it is not the journal's. CANARY and FULL run as recorded. Any other
    stage - SHADOW - runs on the virtual book only when nothing real is left: after a demotion, the journal's open
    trade or orders of unknown outcome that are real (`journal_exposure`) or a position or orders of ours on the real
    account (read here; unreadable counts as there) keep the process on the real account in the last real stage of the
    stage history - entries stay blocked by the journal's SHADOW cap - until it is flat and restarts. A journal never
    CANARY or FULL holds nothing real; a position or our orders on the real account then refuse the start
    (RealExposure), an unreadable account does not."""
    stage, _ = journal.stage()
    if stage in REAL_STAGES:
        return stage, None
    last = last_real_stage(journal)
    why = journal_exposure(journal) if last is not None else None
    if why is None:
        try:
            why = account_exposure(real)
        except ExchangeError as exc:
            if last is None:
                return stage, None
            why = f"실계좌를 읽지 못함 ({type(exc).__name__}: {exc})"
    if why is None:
        return stage, None
    if last is None:
        raise RealExposure(f"{why} - 저널에 CANARY/FULL 기록이 없어 {stage}로 시작하지 않음 (저널을 복구하거나 "
                           "Coinbase에서 직접 정리)")
    return last, why


def stage_exchange(real: Exchange, journal: Journal, stage: str, notify: Callable[[str, str], Any]) -> Exchange:
    """CANARY and FULL trade for real; any other stage - SHADOW - virtually on real prices (previews only). The
    stage is the process's (`process_stage`): SHADOW only when nothing real is left."""
    return real if stage in REAL_STAGES else ShadowExchange(real, journal, alert=notify)


def notifier(s: LiveSettings) -> LiveNotifier:
    """The [LIVE] notifier with its sending thread started: a call only queues, the loop never waits on Telegram."""
    sender = (TelegramSender(s.telegram_token.reveal(), s.telegram_chat)
              if s.telegram_token and s.telegram_chat else None)
    notify = LiveNotifier(sender)
    notify.start()
    return notify


def heartbeat(s: LiveSettings) -> Heartbeat:
    return Heartbeat(s.healthcheck_url.reveal() if s.healthcheck_url else None)


def _secret_forms(s: LiveSettings) -> list[str]:
    """Every text form of the settings' secrets: the tokens and the health-check URL as they are and URL-encoded,
    and the URL's path (its UUID or ping key, with and without the slashes, each long segment) and query."""
    raw = [x.reveal() for x in (s.telegram_token, s.github_token) if x]
    if s.healthcheck_url:
        url = s.healthcheck_url.reveal()
        parts = urlsplit(url)
        raw += [url, parts.path.rstrip("/"), parts.path.strip("/"), parts.query]
        raw += [seg for seg in parts.path.split("/") if len(seg) >= 16]
    forms = {f for x in raw if x and x != "/" for f in (x, quote(x, safe=""), quote_plus(x))}
    return sorted(forms, key=len, reverse=True)


def redact(text: str, s: LiveSettings) -> str:
    """`text` with every form of the settings' secrets (bot token, GitHub token, health-check URL - its path and query
    wherever they appear, e.g. `url: /<uuid>/fail?…` in a requests error - raw and URL-encoded) replaced by <secret>."""
    for secret in _secret_forms(s):
        text = text.replace(secret, "<secret>")
    return text


def fatal_report(exc: BaseException, s: LiveSettings) -> str:
    """A fatal exception's traceback for stderr, `redact`ed."""
    return redact("".join(traceback.format_exception(exc)), s)


def fatal_backoff(journal: Journal | None, kind: str, now: float) -> tuple[float, bool]:
    """(how long a fatal exit waits before exiting, whether to alert it). A fatal within FATAL_REPEAT_S of the previous
    one waits FATAL_WAIT_S, doubling with every repeat up to FATAL_WAIT_MAX_S (Docker's restarts stay sparse); the
    [중단] alert and the /fail go out once per error type per FATAL_ALERT_S, across restarts (the journal keeps the
    record). Without a usable journal: the longest wait, alerted."""
    try:
        if journal is None:
            raise RuntimeError("no journal")
        st = journal.get(FATAL_KEY) or {}
        last = st.get("ts")
        repeat = last is not None and 0 <= now - float(last) < FATAL_REPEAT_S
        streak = int(st.get("streak", 0)) + 1 if repeat else 1
        alerted = {k: float(v) for k, v in (st.get("alerted") or {}).items() if 0 <= now - float(v) < FATAL_ALERT_S}
        alert = kind not in alerted
        if alert:
            alerted[kind] = now
        journal.put(FATAL_KEY, {"ts": now, "type": kind, "streak": streak, "alerted": alerted})
    except Exception:  # noqa: BLE001 - the journal itself may be what failed
        return FATAL_WAIT_MAX_S, True
    return (0.0 if streak == 1 else min(FATAL_WAIT_S * 2 ** (streak - 2), FATAL_WAIT_MAX_S)), alert


def paper_fetcher(s: LiveSettings) -> Callable[[], dict[str, Any]] | None:
    if not (s.paper_repo and s.github_token):
        return None
    repo, token = s.paper_repo, s.github_token.reveal()
    return lambda: fetch_paper_ledger(repo, token)


def paper_status(s: LiveSettings, now: float) -> dict[str, Any] | None:
    """The paper runtime's go-live status (criteria 1-4) from its ledger, or None when it cannot be had for any reason
    (the gates then fail closed); the reason, redacted, on stderr."""
    fetch = paper_fetcher(s)
    if fetch is None:
        return None
    try:
        return golive_status(Ledger.from_dict(fetch()), int(now))
    except Exception as exc:  # noqa: BLE001 - an unreadable or unexpected ledger is no status, never a crash
        print(f"paper ledger unreadable: {redact(f'{type(exc).__name__}: {exc}', s)[:300]}", file=sys.stderr)
        return None


def price_feed(s: LiveSettings) -> TickerFeed | None:
    """The WebSocket ticker feed for the watchdog (public channels: no key, verified TLS)."""
    return TickerFeed([FUTURES_PRODUCT, SPOT_PRODUCT], public_ws_factory())


def contract_reminders() -> dict[str, str]:
    """The futures contract's dates for the daily expiry reminders: its expiry and the day from which the preflight
    refuses to start on it (EXPIRY_MARGIN_DAYS before)."""
    refuses = date.fromisoformat(PRODUCT_EXPIRY) - timedelta(days=EXPIRY_MARGIN_DAYS)
    return {f"{FUTURES_PRODUCT} 만기": PRODUCT_EXPIRY,
            f"{FUTURES_PRODUCT} 시작 점검 거부 (만기 {EXPIRY_MARGIN_DAYS}일 전)": refuses.isoformat()}


class StopRequest:
    """SIGTERM (`docker stop`) and SIGINT (Ctrl-C) while `ewbot live run` runs: a graceful stop. The handler only sets
    flags - the runtime ends its loop after the current tick (`LiveRuntime.stop_requested`), a wait ends early - and
    the process exits without a /fail or [중단]."""

    def __init__(self) -> None:
        self.requested = False
        self.runtime: LiveRuntime | None = None

    def __call__(self, signum: int, frame: Any) -> None:
        self.requested = True
        if self.runtime is not None:
            self.runtime.stop_requested = True

    def attach(self, runtime: LiveRuntime) -> None:
        self.runtime = runtime
        runtime.stop_requested = runtime.stop_requested or self.requested

    @contextlib.contextmanager
    def installed(self) -> Iterator[StopRequest]:
        """The handler for SIGTERM and SIGINT inside the block, the previous ones after it (none off the main
        thread, where Python allows no handler)."""
        previous: dict[signal.Signals, Any] = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, self)
            except ValueError:
                pass
        try:
            yield self
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def wait(self, seconds: float) -> None:
        """Sleep `seconds` in steps of at most 1 s, ending early once a stop was requested."""
        left = float(seconds)
        while left > 0 and not self.requested:
            step = min(left, 1.0)
            time.sleep(step)
            left -= step


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


def build(s: LiveSettings, lock: dict[str, Any], real: Exchange, journal: Journal, stage: str,
          notify: LiveNotifier | None = None) -> Wired:
    """The runtime for `stage` - the preflight's, from `process_stage` (read once): the real exchange in CANARY and
    FULL, ShadowExchange otherwise - with the preflight-checked lock, the expiry reminders (the settings' and the
    contract's), the Telegram control, the heartbeat and the redaction; the feed and the Telegram threads started."""
    notify = notifier(s) if notify is None else notify
    ex = stage_exchange(real, journal, stage, notify)
    feed = price_feed(s)
    commands: queue.Queue[Command] = queue.Queue()
    control = (TelegramControl(s.telegram_token.reveal(), s.telegram_chat, journal)
               if s.telegram_token and s.telegram_chat else None)
    beat = heartbeat(s)
    deps = LiveDeps(
        journal=journal, exchange=ex, lock=lock,
        fetch=lambda cache_dir: update_cache(SPOT_PRODUCT, Path(cache_dir)), cache_dir=s.cache_dir, notify=notify,
        clock=time.time, sleep=time.sleep, feed=feed, heartbeat=beat, commands=commands, control=control,
        paper_ledger=paper_fetcher(s), edit_stops=s.edit_stops, expiry={**dict(s.expiry), **contract_reminders()},
        redact=lambda text: redact(text, s), stage=stage,
    )
    rt = LiveRuntime(deps)
    if feed is not None:
        feed.start()
    if control is not None:
        control.start(commands)
    return Wired(rt, feed, control, beat, notify)
