"""Telegram delivery and message formats (Plan 2 spec 6.3).

Secrets come from the environment (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; GitHub Secrets in the
workflow). Without them nothing is sent and the run still succeeds. Every message is an event with
id "kind:ts:trade_key"; an id already in the ledger's sent_event_ids is never sent again. A send is
tried 3 times; an event that still fails stays pending (with every event after it) for the next run."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import requests

from ewbot.engine.loop import Trade
from ewbot.paper.ledger import Ledger
from ewbot.strategy.base import blended_r
from ewbot.types import H1

NY = ZoneInfo("America/New_York")
DAILY_HOUR_NY = 9
CRITERIA_ATTEMPTS = 2  # criteria 1-2 recomputes per New York date: the daily-summary run and one retry
GAP_WARN_S = 3 * H1
SEND_ATTEMPTS = 3
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
HEAD = "[ewbot paper]"


class SendError(RuntimeError):
    """Telegram rejected or did not answer a sendMessage call."""


class Sender(Protocol):
    def __call__(self, text: str) -> None:
        """Send one plain-text message; raise on any failure."""


class TelegramSender:
    """POST sendMessage (plain text, no parse mode). Raises SendError on any failure: an HTTP status
    other than 200 or a reply that is not a JSON object with "ok": true (message: the status and the
    start of Telegram's reply), or any exception while posting or reading the reply (message: its
    type name only - the API URL holds the token)."""

    def __init__(self, token: str, chat_id: str, session: Any | None = None, timeout: float = 15.0) -> None:
        self._url = TELEGRAM_API.format(token=token)
        self._chat_id = chat_id
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    def __call__(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            resp = self._session.post(self._url, json=payload, timeout=self._timeout)
            status = int(resp.status_code)
            try:
                body = resp.json()
            except ValueError:  # not JSON (requests' JSONDecodeError is a ValueError)
                body = None
            ok = status == 200 and isinstance(body, dict) and bool(body.get("ok"))
            reply = "" if ok else str(resp.text)[:200]
        except Exception as exc:  # noqa: BLE001 - transport or reply errors: type name only, no token
            raise SendError(f"telegram request failed: {type(exc).__name__}") from None
        if not ok:
            raise SendError(f"telegram HTTP {status}: {reply}")


def sender_from_env(env: Mapping[str, str] | None = None) -> TelegramSender | None:
    env = os.environ if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("TELEGRAM_CHAT_ID", "").strip()
    return TelegramSender(token, chat) if token and chat else None


def make_event(kind: str, ts: int, trade_key: int | str, text: str) -> dict[str, Any]:
    return {"id": f"{kind}:{int(ts)}:{trade_key}", "kind": kind, "text": text, "attempts": 0}


def deliver(
    pending: Sequence[dict[str, Any]],
    sent_ids: Sequence[str],
    sender: Sender | None,
    attempts: int = SEND_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Send pending events in order. Returns (still pending, newly sent ids, error notes).
    No sender: the events are dropped unsent (not kept, so a later token does not flood the chat).
    Ids already sent are dropped silently. The first event that fails `attempts` times stops the
    batch: it and every later event stay pending, in order. The error note keeps a SendError's
    message (token-free by construction) and only the type name of any other exception."""
    if sender is None:
        return [], [], [f"telegram not configured: {len(pending)} event(s) not sent"] if pending else []
    done = set(sent_ids)
    newly: list[str] = []
    for i, ev in enumerate(pending):
        if ev["id"] in done:
            continue
        err: Exception | None = None
        for a in range(attempts):
            try:
                sender(ev["text"])
                err = None
                break
            except Exception as exc:  # noqa: BLE001 - any transport error is retried, then recorded
                err = exc
                if a < attempts - 1:
                    sleep(2.0**a)
        if err is not None:
            rest = [dict(e) for e in pending[i:] if e["id"] not in done]
            rest[0]["attempts"] = int(rest[0].get("attempts", 0)) + 1
            why = str(err) if isinstance(err, SendError) else type(err).__name__
            return rest, newly, [f"send failed for {ev['id']}: {why}"]
        newly.append(ev["id"])
        done.add(ev["id"])
    return [], newly, []


# ----------------------------------------------------------------------------- timing


def daily_summary_due(now_ts: int, last_date: str | None) -> str | None:
    """The New York date if this is the first run at/after 09:00 New York time that day, else None."""
    local = datetime.fromtimestamp(now_ts, NY)
    if local.hour < DAILY_HOUR_NY:
        return None
    day = local.date().isoformat()
    return None if day == last_date else day


def last_ok_run_ts(runs: Sequence[dict[str, Any]]) -> int | None:
    ok = [int(r["ts"]) for r in runs if r.get("status") == "ok"]
    return max(ok) if ok else None


def gap_seconds(now_ts: int, runs: Sequence[dict[str, Any]]) -> int | None:
    """Seconds since the last successful run, or None before the first one."""
    last = last_ok_run_ts(runs)
    return None if last is None else int(now_ts) - last


# ----------------------------------------------------------------------------- formats


def _px(x: float | None) -> str:
    return "-" if x is None else f"{x:,.2f}"


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _side(direction: int) -> str:
    return "롱" if direction > 0 else "숏"


def _num(x: Any) -> str:
    return "-" if x is None else f"{float(x):.3f}"


def position_line(pos: Mapping[str, Any] | None) -> str:
    """One line for ledger.open_position (daily summary and report)."""
    if pos is None:
        return "포지션: 없음"
    counted = "" if pos["counted"] else " (구간 시작 전 진입: 카운트 제외)"
    return (
        f"포지션: {pos['setup']} {_side(pos['direction'])} {pos['remaining']}/{pos['contracts']}계약 · "
        f"진입 {_px(pos['entry_price'])} ({_utc(pos['entry_ts'])}) · 손절 {_px(pos['stop'])} · "
        f"청산 시 손익 {pos['pnl_if_closed']:+,.2f}{counted}"
    )


def entry_text(t: Trade, tp1_frac: float) -> str:
    snap = t.snapshot or {}
    counts = snap.get("counts") or []
    top = counts[0] if counts else {}
    br = blended_r(t.direction, t.entry_price, t.stop0, t.tp1, t.tp2, tp1_frac)
    q = snap.get("q_top")
    s_rev = snap.get("s_rev")
    return "\n".join(
        [
            f"{HEAD} 신규 진입",
            f"{t.setup} {_side(t.direction)} · {_utc(t.entry_ts)}",
            f"진입 {_px(t.entry_price)} · 손절 {_px(t.stop0)} · {t.contracts}계약",
            f"TP1 {_px(t.tp1)} · TP2 {_px(t.tp2)} · blended R {br:.2f}",
            f"1등 카운트 {top.get('pattern', '?')} {top.get('phase', '?')} · q {_num(q)} · S_rev {_num(s_rev)}",
        ]
    )


def stop_move_text(dec: dict[str, Any], t: Trade) -> str:
    label = "본전" if dec["kind"] == "BREAKEVEN" else "추적"
    old = dec["levels"][0] if dec["levels"] else None
    return "\n".join(
        [
            f"{HEAD} 손절 이동 ({label})",
            f"{t.setup} {_side(t.direction)} · 진입 {_utc(t.entry_ts)}",
            f"손절 {_px(old)} → {_px(dec['price'])} · {_utc(dec['ts'])} 봉 마감 · 잔여 {dec['contracts']}계약",
        ]
    )


def close_text(t: Trade, count: Mapping[str, Any], counted: bool) -> str:
    tail = "" if counted else " (이 거래는 구간 시작 전 진입: 카운트 제외)"
    return "\n".join(
        [
            f"{HEAD} 청산 · {t.exit_reason}",
            f"{t.setup} {_side(t.direction)} · 진입 {_utc(t.entry_ts)} → 청산 {_utc(t.exit_ts)}",
            f"손익 {t.pnl:+,.2f} USD · R {t.r_multiple:+.2f}",
            f"표본 외 누적 {count['total']}/{count['required']}{tail}",
        ]
    )


def daily_text(
    ny_date: str, position: str, equity: str, count: str, status: Sequence[str], stale: str | None = None
) -> str:
    """stale: why criteria 1-2 could not be recomputed on this date (the status block then shows the
    previous result and the summary is marked 기준 미갱신(stale))."""
    head = f"{HEAD} 일일 요약 {ny_date} (뉴욕)"
    if stale is None:
        return "\n".join([head, position, equity, count, *status])
    note = f"실거래 전환 판정 1·2번 재계산 {CRITERIA_ATTEMPTS}회 실패: 이전 결과 표시 ({stale[:200]})"
    return "\n".join([f"{head} · 기준 미갱신(stale)", position, equity, count, note, *status])


def epoch_text(epoch: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 전략 지문 변경 · 구간 {epoch['id']} 시작 ({_utc(epoch['started_ts'])})",
            f"{epoch['reason']}",
            "표본 외 카운터를 0으로 리셋 (기준 지문과 다르면 실거래 전환 판정 대상 아님)",
        ]
    )


def discrepancy_text(items: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"{HEAD} 경고: 결정 불일치 {len(items)}건"]
    for d in items[:10]:
        lines.append(f"{d['type']} {d['decision_id']} · {d['cause']}")
    if len(items) > 10:
        lines.append(f"외 {len(items) - 10}건 더 (report.html 참고)")
    if any(d["cause"] == "ENVIRONMENT_CHANGE" for d in items):
        lines.append(
            "ENVIRONMENT_CHANGE: 실행 환경(Python·OS·numpy·pandas·pyarrow·uv.lock·실행기 이미지)이 바뀐 뒤 첫 실행이고 "
            "결정을 기록한 실행과도 환경이 다름 - 4번 기준 원인 불명 집계에서 제외"
        )
    return "\n".join(lines)


def data_hash_text(expected: str, got: str) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 홀드아웃 구간 데이터 해시 불일치",
            f"잠금 파일 {expected[:12]} · 이번 데이터 {got[:12]}",
            "실행은 계속함 (표본 외 카운트에는 영향 없음, 재현성 손상 기록)",
        ]
    )


def holdout_trades_text(expected: int, got: int) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 홀드아웃 재현 불일치",
            f"재계산한 홀드아웃 구간 A 거래 {got}건 · 잠금 파일 {expected}건",
            "실행은 계속함 (표본 외 카운트는 잠금 파일 값을 씀, 재현성 손상 기록)",
        ]
    )


def criteria_error_text(ny_date: str, error: str) -> str:
    return "\n".join(
        [
            f"{HEAD} 경고: 실거래 전환 판정 1·2번 재계산 실패 ({ny_date} 뉴욕)",
            error[:300],
            "이전 결과를 유지하고 다음 실행에서 1회 다시 계산함 (다시 실패하면 일일 요약을 '기준 미갱신(stale)'으로 보냄)",
        ]
    )


def gap_text(seconds: int, last_ts: int) -> str:
    return f"{HEAD} 경고: 실행 공백 {seconds / H1:.1f}시간 (직전 성공 실행 {_utc(last_ts)})"


def failure_text(stage: str, message: str, run_url: str | None) -> str:
    lines = [f"{HEAD} 실패 · 단계 {stage}", message[:500]]
    if run_url:
        lines.append(f"로그: {run_url}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- events from decisions


def decision_events(
    fresh: Sequence[dict[str, Any]],
    trades: Mapping[int, Trade],
    open_key: int | None,
    epoch_start: int,
    count: Mapping[str, Any],
    cur_closed: Sequence[Trade],
    tp1_frac: float,
) -> list[dict[str, Any]]:
    """Events for the decisions first recorded in this run (spec 4.7): ENTRY for an entry fill,
    BREAKEVEN / TRAIL for a stop move, CLOSE for the fill that closed a trade. `trades` maps entry_ts
    to every trade of the run (open one included), `open_key` is the open trade's entry_ts,
    `cur_closed` the current epoch's closed paper trades (for the running N/50 of each close)."""
    out: list[dict[str, Any]] = []
    for d in fresh:
        t = trades.get(d["trade_key"])
        if t is None:
            continue  # SIGNAL: its ENTRY fill carries the notification
        if d["kind"] == "ENTRY":
            out.append(make_event("ENTRY", d["ts"], d["trade_key"], entry_text(t, tp1_frac)))
        elif d["kind"] in ("BREAKEVEN", "TRAIL"):
            out.append(make_event(d["kind"], d["ts"], d["trade_key"], stop_move_text(d, t)))
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT") and t.entry_ts != open_key and t.exit_ts == d["ts"]:
            counted = t.entry_ts > int(epoch_start)
            n_at = int(count["holdout"]) + sum(1 for x in cur_closed if x.exit_ts <= t.exit_ts)
            out.append(
                make_event("CLOSE", d["ts"], d["trade_key"],
                           close_text(t, {"total": n_at, "required": count["required"]}, counted))
            )
    return out


def queue_events(ledger: Ledger, events: Sequence[dict[str, Any]]) -> list[str]:
    """Append events to ledger.pending_events unless their id was already sent or queued (no
    duplicate notifications, also within one batch). Returns the newly queued ids."""
    seen = set(ledger.sent_event_ids) | {e["id"] for e in ledger.pending_events}
    queued: list[str] = []
    for ev in events:
        if ev["id"] not in seen:
            ledger.pending_events.append(dict(ev))
            seen.add(ev["id"])
            queued.append(ev["id"])
    return queued
