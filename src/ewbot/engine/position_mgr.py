"""Engine result types and per-trade bookkeeping / position-management helpers (spec 5.4)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ewbot.execution.paper import Fill, PaperPosition
from ewbot.strategy.base import Signal
from ewbot.waves.engine import WaveState
from ewbot.waves.zigzag import Pivot


@dataclass(frozen=True, slots=True)
class Trade:
    entry_ts: int
    exit_ts: int
    setup: str
    direction: int
    contracts: int
    entry_price: float
    exit_price_avg: float
    stop0: float
    tp1: float
    tp2: float | None
    pnl: float
    fees: float
    funding: float
    r_multiple: float
    exit_reason: str  # "STOP","TP2","TIME","LINEAGE","FLIP","BREAKER","END" (+ "TP1" for single-contract trades)
    snapshot: dict


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: int
    equity: float


@dataclass(frozen=True, slots=True)
class EngineResult:
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]
    signals: tuple[Signal, ...]
    events: tuple[tuple[int, str], ...]


def _tp1_split(contracts: int, tp1_frac: float) -> int:
    """Contracts closed at TP1. One contract: TP1 closes it (TP2 unused). Otherwise at least 1."""
    if contracts <= 1:
        return contracts
    return min(contracts, max(1, math.floor(contracts * tp1_frac + 1e-9)))


def _tighten(direction: int, current: float, candidate: float) -> float:
    """Stops only move in the trade's favour: max for longs, min for shorts."""
    return max(current, candidate) if direction == 1 else min(current, candidate)


def _trail_candidate(direction: int, pivot: Pivot | None, atr4h: float | None, buf_atr: float) -> float | None:
    """Structural trailing level from a newly confirmed 4h pivot on the protective side
    (pivot low for longs, pivot high for shorts): pivot -/+ buf_atr * ATR4h."""
    if pivot is None or atr4h is None or pivot.kind != -direction:
        return None
    return pivot.price - direction * buf_atr * atr4h


def _make_snapshot(ws: WaveState, sig: Signal, top_k: int) -> dict[str, Any]:
    counts: list[dict[str, Any]] = []
    for idx, c in enumerate(ws.counts[:top_k]):
        counts.append(
            {
                "pattern": c.pattern,
                "d": c.d,
                "phase": c.phase,
                "points": [(p.sub_ts, p.price) for p in c.points],
                "q": c.q,
                "p": ws.probs[idx] if idx < len(ws.probs) else None,
            }
        )
    return {
        "ts": sig.ts,
        "setup": sig.setup,
        "direction": sig.direction,
        "counts": counts,
        "q_top": ws.q_top,
        "s_rev": ws.s_rev,
        "E_live": ws.E_live,
        "reason": sig.reason,
    }


@dataclass(slots=True)
class _PendingEntry:
    sig: Signal
    snapshot: dict[str, Any]


@dataclass(slots=True)
class _OpenTrade:
    sig: Signal
    snapshot: dict[str, Any]
    contract_size: float
    entry_ts: int
    entry_price: float
    contracts: int
    pos: PaperPosition | None  # live broker object (accrues funding); None if closed inside the entry bar
    fees: float = 0.0
    gross: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    tp1_done: bool = False
    n4h: int = 0  # 4h closes seen since entry (incl. the entry bar's own bucket close)
    exit_reason: str | None = None  # reason for a queued market exit

    @property
    def direction(self) -> int:
        return self.sig.direction

    def add_exit_fill(self, f: Fill) -> bool:
        """Book a reducing fill; returns True when the position is fully closed."""
        self.fees += f.fee
        self.gross += self.direction * (f.price - self.entry_price) * f.contracts * self.contract_size
        self.exit_qty += f.contracts
        self.exit_value += f.price * f.contracts
        if f.kind == "TP1":
            self.tp1_done = True
        return self.exit_qty >= self.contracts

    def to_trade(self, exit_ts: int, reason: str) -> Trade:
        funding = self.pos.funding if self.pos is not None else 0.0
        pnl = self.gross - self.fees - funding
        risk_usd = abs(self.entry_price - self.sig.stop) * self.contracts * self.contract_size
        return Trade(
            entry_ts=self.entry_ts,
            exit_ts=exit_ts,
            setup=self.sig.setup,
            direction=self.direction,
            contracts=self.contracts,
            entry_price=self.entry_price,
            exit_price_avg=self.exit_value / self.exit_qty if self.exit_qty else self.entry_price,
            stop0=self.sig.stop,
            tp1=self.sig.tp1,
            tp2=self.sig.tp2,
            pnl=pnl,
            fees=self.fees,
            funding=funding,
            r_multiple=pnl / risk_usd if risk_usd > 0.0 else 0.0,
            exit_reason=reason,
            snapshot=self.snapshot,
        )
