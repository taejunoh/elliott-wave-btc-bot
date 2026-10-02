"""Out-of-sample extraction, decision records and the replay check (Plan 2 spec 4.6-4.7, 5.3).

A decision is (kind, ts, price, contracts, reason) plus its trade key (the position's entry_ts; the
signal's own ts for SIGNAL) and, for SIGNAL / stop moves, the levels it set:
  SIGNAL     ts = signal bar, price = entry_ref, contracts = queued size, reason = Signal.reason,
             levels = [stop, tp1, tp2]
  ENTRY      ts = fill bar, price = fill price, reason = setup
  TP1 / TP2 / STOP / EXIT   fills (EXIT reason = the trade's exit reason: TIME, LINEAGE, FLIP, BREAKER)
  BREAKEVEN / TRAIL         stop moves: price = new stop, contracts = contracts still open, levels = [old stop]
The END close of a position still open at the last bar is NOT a decision (it is an artefact of the
data ending). A run whose last closed bar is T knows every fill / stop move with ts <= T, but only
the signals with ts <= T - 1h (the engine never evaluates entries on the last bar): known_at()."""

from __future__ import annotations

from typing import Any, Callable, Sequence

from ewbot.engine.loop import EngineResult, Trade
from ewbot.paper.trace import EngineTrace
from ewbot.types import H1
from ewbot.waves.engine import WaveState

REL_TOL = 1e-9
DECISION_KINDS = ("SIGNAL", "ENTRY", "TP1", "TP2", "STOP", "EXIT", "BREAKEVEN", "TRAIL")
CAUSES = ("DATA_REVISION", "FINGERPRINT_CHANGE", "ENVIRONMENT_CHANGE", "UNEXPLAINED")  # in priority order
_ORDER = {k: i for i, k in enumerate(("EXIT", "ENTRY", "STOP", "TP1", "TP2", "BREAKEVEN", "TRAIL", "SIGNAL"))}
_COMPARED = ("kind", "ts", "trade_key", "contracts", "reason", "setup", "direction")


def split_result(res: EngineResult, trace: EngineTrace) -> tuple[list[Trade], Trade | None]:
    """(closed trades, the position still open at the last bar). The engine force-closes an open
    position at the last bar (exit_reason "END"); the trace's end_fill identifies it."""
    trades = list(res.trades)
    if trace.end_fill is None:
        return trades, None
    last = trades[-1]
    if last.exit_reason != "END" or last.entry_ts != trace.end_fill.trade_key:
        raise ValueError("the END-closed trade is not the last trade of the run")
    return trades[:-1], last


def paper_trades(closed: Sequence[Trade], boundary_ts: int) -> list[Trade]:
    """Closed trades that ENTERED after the boundary (holdout last bar or epoch start): a position
    already open at the boundary belongs to the earlier period and is never counted twice."""
    return [t for t in closed if t.entry_ts > boundary_ts]


def decision_id(kind: str, ts: int, trade_key: int) -> str:
    return f"{kind}:{int(ts)}:{int(trade_key)}"


def known_at(d: dict[str, Any]) -> int:
    """The last-bar ts from which a run can know decision d."""
    return int(d["ts"]) + (H1 if d["kind"] == "SIGNAL" else 0)


def _decision(
    kind: str, ts: int, price: float, contracts: int, reason: str, trade_key: int,
    levels: Sequence[float | None], setup: str, direction: int,
) -> dict[str, Any]:
    return {
        "id": decision_id(kind, ts, trade_key),
        "kind": kind,
        "ts": int(ts),
        "price": float(price),
        "contracts": int(contracts),
        "reason": reason,
        "trade_key": int(trade_key),
        "levels": [None if v is None else float(v) for v in levels],
        "setup": setup,
        "direction": int(direction),
    }


def build_decisions(res: EngineResult, trace: EngineTrace, trades: Sequence[Trade]) -> list[dict[str, Any]]:
    """Every decision of one traced run, ordered by (ts, kind). `trades` = closed + open trades."""
    if len(res.signals) != len(trace.orders):
        raise ValueError(f"{len(res.signals)} signals but {len(trace.orders)} queued entries in the trace")
    by_entry = {t.entry_ts: t for t in trades}
    out: list[dict[str, Any]] = []
    for sig, od in zip(res.signals, trace.orders, strict=True):
        if od.ts != sig.ts:
            raise ValueError(f"queued entry at {od.ts} does not match signal at {sig.ts}")
        out.append(
            _decision("SIGNAL", sig.ts, sig.entry_ref, od.contracts, sig.reason, sig.ts,
                      [sig.stop, sig.tp1, sig.tp2], sig.setup, sig.direction)
        )
    tp1_done: list[tuple[int, int, int]] = []  # (ts, trade_key, contracts)
    for f in trace.fills:
        t = by_entry[f.trade_key]
        reason = t.setup if f.kind == "ENTRY" else (t.exit_reason if f.kind == "EXIT" else f.kind)
        out.append(_decision(f.kind, f.ts, f.price, f.contracts, reason, f.trade_key, [], t.setup, t.direction))
        if f.kind == "TP1":
            tp1_done.append((f.ts, f.trade_key, f.contracts))
    for s in trace.stops:
        t = by_entry[s.trade_key]
        left = t.contracts - sum(n for ts, key, n in tp1_done if key == s.trade_key and ts <= s.ts)
        out.append(_decision(s.kind, s.ts, s.new, left, s.kind, s.trade_key, [s.old], t.setup, t.direction))
    out.sort(key=lambda d: (d["ts"], _ORDER[d["kind"]]))
    return out


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a == b or abs(a - b) <= REL_TOL * max(abs(a), abs(b))


def same_decision(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equal kind / ts / key / size / reason / setup / direction; prices and levels within 1e-9 relative."""
    if any(a[k] != b[k] for k in _COMPARED):
        return False
    if not _close(a["price"], b["price"]) or len(a["levels"]) != len(b["levels"]):
        return False
    return all(_close(x, y) for x, y in zip(a["levels"], b["levels"], strict=True))


def classify(
    recorded: dict[str, Any] | None, data_revised: bool, current_epoch_id: int, env_changed: bool = False
) -> str:
    """DATA_REVISION if a bar up to the decision changed; else FINGERPRINT_CHANGE if the decision
    was recorded under another epoch; else ENVIRONMENT_CHANGE if the run that recorded it (or should
    have) ran in another runtime environment (paper.environment); else UNEXPLAINED."""
    if data_revised:
        return "DATA_REVISION"
    if recorded is not None and int(recorded["epoch_id"]) != int(current_epoch_id):
        return "FINGERPRINT_CHANGE"
    if env_changed:
        return "ENVIRONMENT_CHANGE"
    return "UNEXPLAINED"


def _env_changed(recording: str | None, current: str | None) -> bool:
    """Only a known recording environment that differs from the current one counts."""
    return recording is not None and current is not None and recording != current


def verify_decisions(
    recorded: Sequence[dict[str, Any]],
    recomputed: Sequence[dict[str, Any]],
    epoch: dict[str, Any],
    revised_at: Callable[[dict[str, Any]], bool],
    appeared_revised: Callable[[dict[str, Any]], bool],
    *,
    appeared_after: int | None = None,
    env_sha256: str | None = None,
    appeared_env: Callable[[dict[str, Any]], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Replay check. Every recorded decision must reappear unchanged (else CHANGED / VANISHED); a
    recomputed decision of the current epoch that a previous run should already have known but did
    not record is APPEARED. revised_at(recorded) / appeared_revised(recomputed) say whether the bars
    up to that decision changed since it was (or should have been) recorded. The APPEARED window is
    known_at in (appeared_after, epoch through_ts]; appeared_after defaults to the epoch's started_ts
    (the runner passes where the epoch began recording, see runner._recording_floor).
    env_sha256 is the current run's environment digest; it is compared with a recorded decision's
    env_sha256 and, for APPEARED, with appeared_env(recomputed) - the environment digest of the run
    that should have recorded it (None: unknown, never an environment change)."""
    by_id = {d["id"]: d for d in recomputed}
    rec_ids = {d["id"] for d in recorded}
    found: list[dict[str, Any]] = []
    for d in recorded:
        r = by_id.get(d["id"])
        if r is not None and same_decision(d, r):
            continue
        kind = "VANISHED" if r is None else "CHANGED"
        cause = classify(d, revised_at(d), epoch["id"], _env_changed(d.get("env_sha256"), env_sha256))
        found.append({"id": f"{kind}:{d['id']}", "type": kind, "decision_id": d["id"], "cause": cause,
                      "recorded": d, "recomputed": r})
    lo = int(epoch["started_ts"]) if appeared_after is None else int(appeared_after)
    hi = int(epoch["through_ts"])
    for r in recomputed:
        if r["id"] in rec_ids or not lo < known_at(r) <= hi:
            continue
        recording_env = None if appeared_env is None else appeared_env(r)
        cause = classify(None, appeared_revised(r), epoch["id"], _env_changed(recording_env, env_sha256))
        found.append({"id": f"APPEARED:{r['id']}", "type": "APPEARED", "decision_id": r["id"], "cause": cause,
                      "recorded": None, "recomputed": r})
    return found


def decisions_to_record(
    recomputed: Sequence[dict[str, Any]], epoch: dict[str, Any], recorded_ids: set[str]
) -> list[dict[str, Any]]:
    """Decisions first known in this run: known_at after the epoch's through_ts, not yet recorded."""
    through = int(epoch["through_ts"])
    return [d for d in recomputed if known_at(d) > through and d["id"] not in recorded_ids]


def open_position_record(open_trade: Trade, trace: EngineTrace, epoch_start: int) -> dict[str, Any]:
    """The position the engine holds after the last bar (marked at the last close, exit costs included)."""
    key = open_trade.entry_ts
    tp1 = sum(f.contracts for f in trace.fills if f.trade_key == key and f.kind == "TP1")
    stops = [s for s in trace.stops if s.trade_key == key]
    snap = open_trade.snapshot or {}
    end = trace.end_fill
    return {
        "entry_ts": key,
        "setup": open_trade.setup,
        "direction": open_trade.direction,
        "contracts": open_trade.contracts,
        "remaining": open_trade.contracts - tp1,
        "entry_price": open_trade.entry_price,
        "stop0": open_trade.stop0,
        "stop": stops[-1].new if stops else open_trade.stop0,
        "tp1": open_trade.tp1,
        "tp2": open_trade.tp2,
        "tp1_done": tp1 > 0,
        "mark_ts": None if end is None else end.ts,
        "pnl_if_closed": open_trade.pnl,
        "r_if_closed": open_trade.r_multiple,
        "counted": key > epoch_start,
        "q_top": snap.get("q_top"),
        "s_rev": snap.get("s_rev"),
    }


def top_count_summary(ws: WaveState | None) -> dict[str, Any] | None:
    """The current top wave count after the last bar (report: "현재 1등 카운트")."""
    if ws is None or ws.top is None:
        return None
    c = ws.top
    return {
        "ts": ws.ts,
        "pattern": c.pattern,
        "d": c.d,
        "phase": c.phase,
        "q": c.q,
        "p": ws.probs[0] if ws.probs else None,
        "q_top": ws.q_top,
        "s_rev": ws.s_rev,
        "E_live": ws.E_live,
        "ready": ws.ready,
        "points": [[p.sub_ts, p.price] for p in c.points],
    }
