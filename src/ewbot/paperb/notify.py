"""Paper-B Telegram messages (spec 7): strategy A's formats and delivery (ewbot.paper.notify), with the
prefix `[ewbot paper-B]`. Only the entry message is B's own (A's describes wave counts). Every trade and
position line names its asset through the trade's setup ("B-BTC", "B-ETH")."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from ewbot.engine.loop import Trade
from ewbot.paper import notify as a_notify
from ewbot.paper.notify import make_event, position_line
from ewbot.paperb.decisions import key_of

HEAD_B = "[ewbot paper-B]"


def b_head(text: str) -> str:
    """A's message with the paper-B prefix in place of `[ewbot paper]`."""
    if not text.startswith(a_notify.HEAD):
        raise ValueError(f"not a paper message: {text[:40]!r}")
    return HEAD_B + text[len(a_notify.HEAD):]


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def entry_text_b(t: Trade) -> str:
    snap = t.snapshot or {}
    return "\n".join([
        f"{HEAD_B} 신규 진입",
        f"{t.setup} {'롱' if t.direction > 0 else '숏'} · {_utc(t.entry_ts)}",
        f"진입 {t.entry_price:,.2f} · 손절 {t.stop0:,.2f} · {t.contracts}계약",
        f"TP1 {t.tp1:,.2f} (절반) · TP2 {float(t.tp2 or 0.0):,.2f}",
        f"근거: {snap.get('reason', '-')}",
    ])


def close_text_b(t: Trade, n_at: int, required: int, counted: bool) -> str:
    return b_head(a_notify.close_text(t, {"total": n_at, "required": required}, counted))


def holdout_trades_text_b(expected: int, got: int) -> str:
    return "\n".join([
        f"{HEAD_B} 경고: 홀드아웃 재현 불일치",
        f"재계산한 홀드아웃 구간 B 거래 {got}건 · 잠금 파일 {expected}건",
        "실행은 계속함 (모의 카운트는 잠금 이후 진입만 셈, 재현성 손상 기록)",
    ])


def daily_text_b(ny_date: str, lines: Sequence[str]) -> str:
    return "\n".join([f"{HEAD_B} 일일 요약 {ny_date} (뉴욕)", *lines])


def position_lines_b(open_position: Mapping[str, Mapping[str, Any]] | None) -> list[str]:
    """One A position line per open position (the ledger's open_position: {asset: position} or None)."""
    if not open_position:
        return [position_line(None)]
    return [position_line(p) for p in open_position.values()]


def decision_events_b(
    fresh: Sequence[dict[str, Any]],
    trades: Mapping[int, Trade],
    open_keys: set[int],
    epoch_start: int,
    count: Mapping[str, Any],
    cur_closed: Sequence[Trade],
) -> list[dict[str, Any]]:
    """ENTRY, BREAKEVEN and CLOSE events for the decisions first recorded in this run (A's rules). `trades`
    maps the B trade key (decisions.key_of) to every trade of the run; open_keys are the open positions'."""
    out: list[dict[str, Any]] = []
    for d in fresh:
        t = trades.get(d["trade_key"])
        if t is None:
            continue  # SIGNAL: its ENTRY fill carries the message
        if d["kind"] == "ENTRY":
            out.append(make_event("ENTRY", d["ts"], d["trade_key"], entry_text_b(t)))
        elif d["kind"] == "BREAKEVEN":
            out.append(make_event("BREAKEVEN", d["ts"], d["trade_key"], b_head(a_notify.stop_move_text(d, t))))
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT") and key_of(t) not in open_keys and t.exit_ts == d["ts"]:
            counted = t.entry_ts > int(epoch_start)
            n_at = sum(1 for x in cur_closed if x.exit_ts <= t.exit_ts)
            out.append(make_event("CLOSE", d["ts"], d["trade_key"], close_text_b(t, n_at, count["required"], counted)))
    return out
