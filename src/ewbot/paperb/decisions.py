"""Decisions of one strategy-B recompute in strategy A's decision format (ewbot.paper.decisions), so that
A's replay check - verify_decisions, decisions_to_record, classify (DATA_REVISION, FINGERPRINT_CHANGE,
ENVIRONMENT_CHANGE, UNEXPLAINED), same_decision - runs unchanged on them.

  SIGNAL     ts = confirming 15m bar, price = entry_ref, contracts = queued size, levels = [stop, tp1, tp2]
  ENTRY      ts = fill bar (the next 15m open), reason = setup ("B-BTC" or "B-ETH")
  TP1 / TP2 / STOP / EXIT   fills (EXIT reason: TIME or BREAKER)
  BREAKEVEN  the rest's stop moved to the entry price after TP1: price = new stop, levels = [old stop]
setup and direction name the asset and the side. A's decision id is KIND:ts:trade_key with an integer
trade key, the position's entry ts; BTC and ETH may enter on the same 15m bar, so a B trade key is the
entry ts (the signal ts for SIGNAL) plus the asset's index in config.ASSETS - BTC + 0, ETH + 1 second
(every bar ts is a multiple of 900). The END close of a position still open at the last bar is not a
decision. A run whose last bar is T knows every fill and stop move with ts <= T and every signal with ts
<= T - 15m (no entry is evaluated on the last bar); A's known_at() dates a SIGNAL at ts + 1h, which only
ever delays its APPEARED check."""

from __future__ import annotations

from typing import Any, Sequence

from ewbot.engine.loop import Trade
from ewbot.paper.decisions import decision_id
from ewbot.stratb.config import ASSETS
from ewbot.stratb.engine import BResult, asset_of, setup_of

_ORDER = {k: i for i, k in enumerate(("EXIT", "ENTRY", "STOP", "TP1", "TP2", "BREAKEVEN", "TRAIL", "SIGNAL"))}


def trade_key_b(asset: str, ts: int) -> int:
    """The integer key of A's decision format: ts + the asset's index in config.ASSETS."""
    return int(ts) + ASSETS.index(asset)


def key_of(t: Trade) -> int:
    return trade_key_b(asset_of(t), t.entry_ts)


def split_result_b(res: BResult) -> tuple[list[Trade], list[Trade]]:
    """(closed trades, the positions the engine force-closed at the last bar), each in run order."""
    ends = {(f.asset, f.trade_key) for f in res.end_fills}
    closed = [t for t in res.trades if (asset_of(t), t.entry_ts) not in ends]
    still = [t for t in res.trades if (asset_of(t), t.entry_ts) in ends]
    if len(still) != len(ends) or any(t.exit_reason != "END" for t in still):
        raise ValueError("the END fills do not match the END-closed trades of the run")
    return closed, still


def _decision(kind: str, ts: int, price: float, contracts: int, reason: str, key: int,
              levels: Sequence[float | None], setup: str, direction: int) -> dict[str, Any]:
    return {"id": decision_id(kind, ts, key), "kind": kind, "ts": int(ts), "price": float(price),
            "contracts": int(contracts), "reason": reason, "trade_key": int(key),
            "levels": [None if v is None else float(v) for v in levels], "setup": setup, "direction": int(direction)}


def build_decisions_b(res: BResult) -> list[dict[str, Any]]:
    """Every decision of one run, ordered by (ts, kind) as A's build_decisions, then by trade key."""
    by_entry = {(asset_of(t), t.entry_ts): t for t in res.trades}
    out = [
        _decision("SIGNAL", s.ts, s.entry_ref, o.contracts, s.reason, trade_key_b(o.asset, s.ts),
                  [s.stop, s.tp1, s.tp2], setup_of(o.asset), s.direction)
        for s, o in zip(res.signals, res.orders, strict=True)
    ]
    tp1_done: list[tuple[int, str, int, int]] = []
    for f in res.fills:
        t = by_entry[(f.asset, f.trade_key)]
        reason = t.setup if f.kind == "ENTRY" else (t.exit_reason if f.kind == "EXIT" else f.kind)
        out.append(_decision(f.kind, f.ts, f.price, f.contracts, reason, trade_key_b(f.asset, f.trade_key), [],
                             t.setup, t.direction))
        if f.kind == "TP1":
            tp1_done.append((f.ts, f.asset, f.trade_key, f.contracts))
    for s in res.stops:
        t = by_entry[(s.asset, s.trade_key)]
        left = t.contracts - sum(n for ts, a, key, n in tp1_done if (a, key) == (s.asset, s.trade_key) and ts <= s.ts)
        out.append(_decision(s.kind, s.ts, s.new, left, s.kind, trade_key_b(s.asset, s.trade_key), [s.old], t.setup,
                             t.direction))
    out.sort(key=lambda d: (d["ts"], _ORDER[d["kind"]], d["trade_key"]))
    return out


def open_positions_b(still: Sequence[Trade], res: BResult, epoch_start: int) -> dict[str, dict[str, Any]] | None:
    """{asset: the position held after the last bar, in the shape A's notify.position_line reads}, None if
    flat (stored as the ledger's open_position)."""
    out: dict[str, dict[str, Any]] = {}
    for t in still:
        a, key = asset_of(t), t.entry_ts
        tp1 = sum(f.contracts for f in res.fills if (f.asset, f.trade_key) == (a, key) and f.kind == "TP1")
        moves = [s for s in res.stops if (s.asset, s.trade_key) == (a, key)]
        end = next(f for f in res.end_fills if (f.asset, f.trade_key) == (a, key))
        out[a] = {
            "asset": a, "entry_ts": key, "setup": t.setup, "direction": t.direction,
            "contracts": t.contracts, "remaining": t.contracts - tp1, "entry_price": t.entry_price,
            "stop0": t.stop0, "stop": moves[-1].new if moves else t.stop0, "tp1": t.tp1, "tp2": t.tp2,
            "tp1_done": tp1 > 0, "mark_ts": end.ts, "pnl_if_closed": t.pnl, "r_if_closed": t.r_multiple,
            "counted": key > int(epoch_start),
        }
    return out or None
