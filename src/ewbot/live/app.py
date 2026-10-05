"""Production wiring of `ewbot live` (Plan 3): settings -> Coinbase exchange (ShadowExchange in SHADOW),
journal, [LIVE] notifier, WebSocket price feed (Coinbase's public endpoint: no key), Telegram control,
heartbeat and paper audit -> LiveRuntime. The products are fixed (FUTURES_PRODUCT, SPOT_PRODUCT: no setting
names them); the settings' secrets are revealed only here, where they are used. The CLI calls these factories;
tests replace `real_exchange`, `price_feed` and `paper_status` (no network).

Real exposure is never hidden behind the virtual book: `process_stage` keeps a process on the real account in the
last real stage while a demoted journal still holds something real (an open trade or an order of unknown outcome
of ours) or the real account shows orders of ours, and `stage_exchange` wraps the exchange in ShadowExchange only
for SHADOW; a position the bot has nothing of (no order of ours, nothing in the journal) - the user's own - is
never traded at start: the start is refused (RealExposure), as is a journal never CANARY or FULL beside an
account that cannot be read (AccountUnreadable). `StopRequest` turns SIGTERM (`docker stop`) and SIGINT into a
graceful stop; `fatal_backoff` keeps a crash loop's restarts and alerts sparse - and short while the journal
shows something real to manage; a start refused for a Coinbase outage is tried again soon (OUTAGE_WAIT_S), its
alert sent once an hour (`outage_alert`), and any other refusal's alert once an hour per reason (`refusal_alert`).
`candle_fetch` is the runtime's candle download: a cache it cannot read (a kill in the middle of its non-atomic
write) is set aside and the history downloaded again."""

from __future__ import annotations

import contextlib
import hashlib
import os
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

from ewbot.data.candles import load_1h
from ewbot.data.coinbase_history import update_cache
from ewbot.live.audit import fetch_paper_ledger
from ewbot.live.coinbase import CoinbaseExchange
from ewbot.live.control import Command, TelegramControl
from ewbot.live.exchange import FUTURES_PRODUCT, SPOT_PRODUCT, Exchange, ExchangeError
from ewbot.live.feed import TickerFeed, public_ws_factory
from ewbot.live.health import Heartbeat
from ewbot.live.journal import Journal
from ewbot.live.notify import LiveNotifier, TelegramSender
from ewbot.live.preflight import EXPIRY_MARGIN_DAYS, PRODUCT_EXPIRY
from ewbot.live.reconcile import is_ours
from ewbot.live.runtime import FLATTEN_KEY, LiveDeps, LiveRuntime
from ewbot.live.settings import LiveSettings, SettingsError
from ewbot.live.shadow import ShadowExchange
from ewbot.paper.criteria import golive_status
from ewbot.paper.ledger import Ledger

REAL_STAGES = ("CANARY", "FULL")
VIRTUAL_ORDER_ID = re.compile(r"v\d+-")  # live.shadow's virtual order ids: v<book>-<sim id>
FATAL_KEY = "fatal"  # journal: the last fatal exit (ts, type, streak) and the [중단] alerts sent per error type
FATAL_REPEAT_S = 900.0  # a fatal exit within this of the previous one waits before exiting ...
FATAL_WAIT_S = 60.0  # ... first this long, doubling with every repeat ...
FATAL_WAIT_MAX_S = 1800.0  # ... up to this
FATAL_ALERT_S = 3600.0  # one [중단] alert and healthchecks.io /fail per error type in this time, across restarts
FATAL_EXPOSED_WAIT_S = 120.0  # ... but never longer than this while the journal shows something real to manage
OUTAGE_KEY = "outage_refusal"  # journal: when a start refused for a Coinbase outage was last alerted
OUTAGE_WAIT_S = 60.0  # a start refused because Coinbase could not be read is tried again after this (not 10 minutes)
OUTAGE_ALERT_S = 3600.0  # ... and its [시작 거부] alert goes out at most once in this time, across restarts
REFUSAL_KEY = "start_refusals"  # journal: when each other refusal reason (by digest) was last alerted
REFUSAL_ALERT_S = 3600.0  # ... at most once in this time per reason, across restarts (one every LIVE_REFUSAL_WAIT_S)
CACHE_FILE = f"{SPOT_PRODUCT}_1h.parquet"  # data.coinbase_history.update_cache's file in the cache directory


class RealExposure(RuntimeError):
    """The start is refused rather than run SHADOW beside something real or trade what is not the bot's: the real account
    holds orders of ours while the journal was never CANARY or FULL (lost or replaced), or a position the bot has nothing
    of - no order of ours on the account, nothing real in the journal: the user's own (runbook: never hold a manual
    position in this account)."""


class AccountUnreadable(RealExposure):
    """A journal never CANARY or FULL and a real account that cannot be read at start: a position of ours may be
    there, so the start is refused - an outage, tried again soon (OUTAGE_WAIT_S)."""


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


def account_exposure(real: Exchange) -> tuple[str | None, bool]:
    """(what the real account holds - a position, orders of ours - or None for neither, whether an order of ours rests);
    raises ExchangeError when unreadable."""
    pos = real.position()
    ours = [o for o in real.open_orders() if is_ours(o)]
    if pos is None and not ours:
        return None, False
    held = [] if pos is None else [f"포지션 {'롱' if pos.side > 0 else '숏'} {pos.contracts}계약"]
    return "실계좌에 " + ", ".join(held + ([f"봇 주문 {len(ours)}건"] if ours else [])), bool(ours)


def process_stage(journal: Journal, real: Exchange | None) -> tuple[str, str | None]:
    """The stage this process runs, and why when it is not the journal's. CANARY and FULL run as recorded. Any other
    stage - SHADOW - runs on the virtual book only when nothing real is left: after a demotion, the journal's open
    trade or orders of unknown outcome that are real (`journal_exposure`), or orders of ours on the real account (read
    here; unreadable counts as there), keep the process on the real account in the last real stage of the stage
    history - entries stay blocked by the journal's SHADOW cap - until it is flat and restarts. The account counts as
    the bot's only through orders of ours or the journal: a position with neither - the user's own - refuses the start
    (RealExposure), never traded nor run beside. A journal never CANARY or FULL holds nothing real: orders of ours on
    the real account (a lost or replaced journal) refuse the start too, and so does an account that cannot be read
    (AccountUnreadable: nothing proves it flat). `real=None` (an offline preflight): the journal alone."""
    stage, _ = journal.stage()
    if stage in REAL_STAGES:
        return stage, None
    last = last_real_stage(journal)
    held = journal_exposure(journal) if last is not None else None
    if held is not None and last is not None:
        return last, held
    if real is None:
        return stage, None
    try:
        what, ours = account_exposure(real)
    except ExchangeError as exc:
        why = f"실계좌를 읽지 못함 ({type(exc).__name__}: {exc})"
        if last is None:
            raise AccountUnreadable(f"{why} - 저널에 CANARY/FULL 기록이 없어 실계좌에 봇의 포지션·주문이 없음을 확인해야 "
                                    f"{stage}로 시작함 (곧 다시 시도)") from None
        return last, why
    if what is None:
        return stage, None
    if not ours:
        raise RealExposure(f"{what} - 봇 주문도 저널 기록도 없는 포지션 (사용자의 수동 포지션): 거래하지도, 옆에서 "
                           f"{stage}로 시작하지도 않음 - Coinbase 앱에서 정리 (이 계좌에서는 수동 거래 금지)")
    if last is None:
        raise RealExposure(f"{what} - 저널에 CANARY/FULL 기록이 없어 {stage}로 시작하지 않음 (저널을 복구하거나 "
                           "Coinbase에서 직접 정리)")
    return last, what


def stage_exchange(real: Exchange, journal: Journal, stage: str, notify: Callable[[str, str], Any]) -> Exchange:
    """CANARY and FULL trade for real; any other stage - SHADOW - virtually on real prices (previews only). The
    stage is the process's (`process_stage`): SHADOW only when nothing real is left."""
    return real if stage in REAL_STAGES else ShadowExchange(real, journal, alert=notify)


def notifier(s: LiveSettings) -> LiveNotifier:
    """The [LIVE] notifier with its sending thread started: a call only queues, the loop never waits on Telegram.
    Its sender is live.notify's own (connect 5 s, read 15 s), which tells a send that never left from one Telegram
    may have posted."""
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
    one's exit - its time plus its wait: a long wait never resets the streak - waits FATAL_WAIT_S, doubling with every
    repeat up to FATAL_WAIT_MAX_S (Docker's restarts stay sparse), but at most FATAL_EXPOSED_WAIT_S while the journal
    shows something real to manage (a real open trade or order of unknown outcome, a standing flatten: the brackets
    protect meanwhile, nothing else does); the [중단] alert and the /fail go out once per error type per FATAL_ALERT_S,
    across restarts (the journal keeps the record). Without a usable journal: the longest wait, alerted."""
    try:
        if journal is None:
            raise RuntimeError("no journal")
        st = journal.get(FATAL_KEY) or {}
        last = st.get("ts")
        repeat = last is not None and 0 <= now - (float(last) + float(st.get("wait", 0.0))) < FATAL_REPEAT_S
        streak = int(st.get("streak", 0)) + 1 if repeat else 1
        wait = 0.0 if streak == 1 else min(FATAL_WAIT_S * 2 ** (streak - 2), FATAL_WAIT_MAX_S)
        if wait > FATAL_EXPOSED_WAIT_S and (journal_exposure(journal) is not None
                                            or journal.get(FLATTEN_KEY) is not None):
            wait = FATAL_EXPOSED_WAIT_S
        alerted = {k: float(v) for k, v in (st.get("alerted") or {}).items() if 0 <= now - float(v) < FATAL_ALERT_S}
        alert = kind not in alerted
        if alert:
            alerted[kind] = now
        journal.put(FATAL_KEY, {"ts": now, "type": kind, "streak": streak, "wait": wait, "alerted": alerted})
    except Exception:  # noqa: BLE001 - the journal itself may be what failed
        return FATAL_WAIT_MAX_S, True
    return wait, alert


def outage_alert(journal: Journal | None, now: float) -> bool:
    """Whether a start refused for a Coinbase outage (`retry` preflight checks, StartupRefused, AccountUnreadable) is
    alerted now: once per OUTAGE_ALERT_S across restarts (the journal keeps the time) - such a start is tried again every
    OUTAGE_WAIT_S. Without a usable journal: alerted."""
    try:
        if journal is None:
            return True
        last = journal.get(OUTAGE_KEY)
        if last is not None and 0 <= now - float(last) < OUTAGE_ALERT_S:
            return False
        journal.put(OUTAGE_KEY, now)
    except Exception:  # noqa: BLE001 - never at the cost of the refusal itself
        return True
    return True


def refusal_alert(journal: Journal | None, why: str, now: float) -> bool:
    """Whether a start refused for `why` (a configuration refusal, not an outage) is alerted now: once per
    REFUSAL_ALERT_S per reason across restarts (the journal keeps a digest of each reason and its time) - such a start
    is tried again every LIVE_REFUSAL_WAIT_S, about 144 times a day; a new reason at once. Without a usable journal:
    alerted."""
    try:
        if journal is None:
            return True
        key = hashlib.sha256(why.encode("utf-8")).hexdigest()[:16]
        seen = {k: float(v) for k, v in (journal.get(REFUSAL_KEY) or {}).items() if 0 <= now - float(v) < REFUSAL_ALERT_S}
        if key in seen:
            return False
        seen[key] = now
        journal.put(REFUSAL_KEY, seen)
    except Exception:  # noqa: BLE001 - never at the cost of the refusal itself
        return True
    return True


def candle_fetch(alert: Callable[[str, str], Any] | None = None, journal: Journal | None = None,
                 clock: Callable[[], float] = time.time) -> Callable[[Path], Path]:
    """The runtime's candle download (LiveDeps.fetch, on its worker thread): update_cache for SPOT_PRODUCT, after
    reading the cache it extends. The hourly write is not atomic (and data/coinbase_history.py is not changed here): a
    kill in the middle of it leaves a cache no later download can read, and every hour would be held. Such a cache is
    moved aside to <file>.bad-<epoch s> (CACHE_SET_ASIDE event, a [캔들 캐시 손상] alert) and the history downloaded again
    from scratch (minutes: the cycle meanwhile is late, then held)."""

    def fetch(cache_dir: Path) -> Path:
        path = Path(cache_dir) / CACHE_FILE
        if path.exists():
            try:
                load_1h(path)
            except Exception as exc:  # noqa: BLE001 - any unreadable cache (a truncated parquet, bad dtypes, ...)
                bad = path.with_name(f"{path.name}.bad-{int(clock())}")
                os.replace(path, bad)
                if journal is not None:
                    journal.add_event(clock(), "CACHE_SET_ASIDE", {"path": str(bad), "error": type(exc).__name__})
                if alert is not None:
                    alert("CACHE", f"[캔들 캐시 손상] {path.name}을 읽지 못해 ({type(exc).__name__}) {bad.name}(으)로 옮기고 "
                                   "처음부터 다시 받음 - 몇 분 걸리는 동안 판단 보류")
        return update_cache(SPOT_PRODUCT, Path(cache_dir))

    return fetch


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
    FULL, ShadowExchange otherwise - with the preflight-checked lock, the candle download (`candle_fetch`), the expiry
    reminders (the settings' and the contract's), the refusal codes the settings add, the Telegram control, the
    heartbeat and the redaction; the feed and the Telegram threads started."""
    notify = notifier(s) if notify is None else notify
    ex = stage_exchange(real, journal, stage, notify)
    feed = price_feed(s)
    commands: queue.Queue[Command] = queue.Queue()
    control = (TelegramControl(s.telegram_token.reveal(), s.telegram_chat, journal)
               if s.telegram_token and s.telegram_chat else None)
    beat = heartbeat(s)
    deps = LiveDeps(
        journal=journal, exchange=ex, lock=lock, fetch=candle_fetch(notify, journal), cache_dir=s.cache_dir,
        notify=notify, clock=time.time, sleep=time.sleep, feed=feed, heartbeat=beat, commands=commands, control=control,
        paper_ledger=paper_fetcher(s), edit_stops=s.edit_stops, expiry={**dict(s.expiry), **contract_reminders()},
        redact=lambda text: redact(text, s), stage=stage, close_conflicts=s.close_conflicts,
        overlap_refusals=s.overlap_refusals,
    )
    rt = LiveRuntime(deps)
    if feed is not None:
        feed.start()
    if control is not None:
        control.start(commands)
    return Wired(rt, feed, control, beat, notify)
