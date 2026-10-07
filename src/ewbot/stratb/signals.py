"""Strategy-B signals over a gap-filled 15m frame (Plan 4 spec 4.1-4.4). Decisions at a bar's CLOSE
from bars up to it only; the output for bars <= t never depends on later bars (tested by truncation).

1. Structure: complete 1h bars (4 x 15m) feed A's ATR ZigZag (waves/zigzag.py, k = zz_k, ATR 14). A
   pivot confirmed on the hour whose last 15m bar is i makes the leg (previous pivot -> new pivot) a
   reference from bar i on: a confirmed high ends an up leg L -> H, the reference of long setups; a
   confirmed low ends a down leg H -> L, the reference of short setups. Each side keeps its latest leg
   ("the most recently confirmed up leg") until the next leg of the same direction replaces it.
2. Candidate (spec 4.2): retracement zone [H - z_hi (H - L), H - z_lo (H - L)] (mirrored for shorts) and a
   1h pivot before the leg's start, at most confluence_lookback_hours earlier, inside the zone +/-
   confluence_atr x ATR1h. No overlap -> the leg is never traded.
3. Life: from the confirming bar for confirm_hours (72 h), until the next leg of the same direction, or
   until a 15m close beyond the start (below L for a long) invalidates it.
4. Confirmation (spec 4.3): two consecutive 15m pivots of the trade's kind (lows for a long, w = 3) after
   the leg's extreme 15m bar make a lower low with a higher RSI(14) low, the second inside the zone; the
   latest such pair stays the setup until a newer pair replaces it or a later bar undercuts its second
   pivot. Confirmed at the first close beyond the swing between the two pivots (highest high for a
   long) with no gap-filled bar from p1 - w to the confirming bar.
5. Levels (spec 4.4): entry_ref = the confirming close; stop = second pivot -/+ 0.2 x ATR15; TP1 = entry +
   tp1_r x R; TP2 = start + e (end - start). Skipped (the reference stays alive) when R > 2.5 x ATR1h or TP2
   is not beyond TP1. A signal consumes its reference: one signal per leg."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ewbot.data.candles import fill_gaps
from ewbot.indicators import atr_series
from ewbot.stratb.config import M15, StratBParams
from ewbot.stratb.rules import (
    breakout_level,
    extension_target,
    first_target,
    has_confluence,
    is_divergence,
    pivot_highs,
    pivot_lows,
    retracement_zone,
    rsi_series,
    skip_reason,
    stop_level,
)
from ewbot.types import H1, Bar
from ewbot.waves.zigzag import ZigZag


@dataclass(frozen=True)
class Frame15:
    """Gap-filled 15m bars with their indicators, and the complete 1h bars built from them."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    synthetic: np.ndarray
    rsi: np.ndarray
    atr15: np.ndarray
    piv_lo: np.ndarray
    piv_hi: np.ndarray
    h_ts: np.ndarray
    h_open: np.ndarray
    h_high: np.ndarray
    h_low: np.ndarray
    h_close: np.ndarray
    h_atr: np.ndarray
    h_end: np.ndarray  # 15m index of each 1h bar's last 15m bar (its close is the hour's close)
    last_hour: np.ndarray  # per 15m bar: index of the latest 1h bar closed at its close, -1 if none

    def __len__(self) -> int:
        return int(self.ts.size)


@dataclass(frozen=True, slots=True)
class RefLeg:
    known_i: int  # 15m bar at whose close the leg is known
    direction: int  # +1: up leg, long setups; -1: down leg, short setups
    start: float  # L of an up leg, H of a down leg
    end: float  # H of an up leg, L of a down leg
    start_ts: int  # 1h bar of the start pivot
    end_ts: int  # 1h bar of the end pivot
    end_i: int  # 15m bar of the end pivot's extreme (first one reaching it)
    atr1h: float  # ATR1h at confirmation
    prior: tuple[float, ...]  # 1h pivot prices before the start pivot, within the lookback


@dataclass(frozen=True, slots=True)
class SignalB:
    i: int  # confirming 15m bar (index into the frame)
    ts: int
    direction: int
    entry_ref: float
    stop: float
    tp1: float
    tp2: float
    leg_start: float = 0.0
    leg_end: float = 0.0
    p1_ts: int = 0
    p2_ts: int = 0
    level: float = 0.0
    reason: str = ""


def build_frame(df15: pd.DataFrame, p: StratBParams = StratBParams()) -> Frame15:
    """Gap-fill (A's fill_gaps on the 15m grid: flat synthetic bars) and compute every causal series."""
    df = fill_gaps(df15, tf=M15)
    ts = df["ts"].to_numpy(dtype=np.int64)
    o, h, lo, c = (df[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close"))
    syn = df["synthetic"].to_numpy(dtype=bool)
    ends = np.flatnonzero(ts % H1 == H1 - M15)
    ends = ends[(ends >= 3) & (ts[np.maximum(ends - 3, 0)] == ts[ends] - (H1 - M15))]
    starts = ends - 3
    hh = np.maximum.reduce([h[ends - k] for k in range(4)]) if ends.size else np.zeros(0)
    hl = np.minimum.reduce([lo[ends - k] for k in range(4)]) if ends.size else np.zeros(0)
    hc = c[ends]
    return Frame15(
        ts=ts, open=o, high=h, low=lo, close=c, synthetic=syn,
        rsi=rsi_series(c, p.rsi_len), atr15=atr_series(h, lo, c, p.atr_len),
        piv_lo=pivot_lows(lo, p.pivot_w), piv_hi=pivot_highs(h, p.pivot_w),
        h_ts=ts[starts], h_open=o[starts], h_high=hh, h_low=hl, h_close=hc,
        h_atr=atr_series(hh, hl, hc, p.atr_len), h_end=ends,
        last_hour=np.searchsorted(ends, np.arange(ts.size), side="right") - 1,
    )


def reference_legs(f: Frame15, zz_k: float, p: StratBParams = StratBParams()) -> list[RefLeg]:
    """Every leg the 1h ZigZag confirms, in order (spec 4.1)."""
    zz = ZigZag(zz_k, p.atr_len)
    legs: list[RefLeg] = []
    lookback = p.confluence_lookback_hours * H1
    for j in range(f.h_ts.size):
        bar = Bar(int(f.h_ts[j]), float(f.h_open[j]), float(f.h_high[j]), float(f.h_low[j]), float(f.h_close[j]), 0.0)
        piv = zz.update(j, bar)
        if piv is None or len(zz.pivots) < 2:
            continue
        prev = zz.pivots[-2]
        d = piv.kind
        first = int(f.h_end[piv.i]) - 3
        seg = f.high[first : first + 4] if d > 0 else f.low[first : first + 4]
        k = int(np.argmax(seg)) if d > 0 else int(np.argmin(seg))
        prior = tuple(q.price for q in zz.pivots[:-2] if q.sub_ts >= prev.sub_ts - lookback)
        legs.append(RefLeg(int(f.h_end[j]), d, prev.price, piv.price, prev.sub_ts, piv.sub_ts, first + k,
                           float(zz.atr), prior))
    return legs


def _scan(f: Frame15, p: StratBParams, leg: RefLeg, last_i: int) -> SignalB | None:
    d, w = leg.direction, p.pivot_w
    zone = retracement_zone(leg.start, leg.end, *p.zone)
    if not has_confluence(zone, leg.prior, p.confluence_atr * leg.atr1h):
        return None
    piv = f.piv_lo if d > 0 else f.piv_hi
    ext = f.low if d > 0 else f.high
    tp2 = extension_target(leg.start, leg.end, p.ext)
    prev: int | None = None  # latest pivot of the trade's kind after the leg's extreme
    pair: tuple[int, int] | None = None  # latest consecutive divergence pair, second pivot in the zone

    def add(j: int) -> None:
        nonlocal prev, pair
        in_zone = zone[0] <= ext[j] <= zone[1]
        if prev is not None and in_zone and is_divergence(d, ext[prev], ext[j], f.rsi[prev], f.rsi[j]):
            pair = (prev, j)
        prev = j

    for j in range(leg.end_i + 1, leg.known_i - w):
        if piv[j]:
            add(j)
    for i in range(leg.known_i, last_i + 1):
        if d * (f.close[i] - leg.start) < 0.0:
            return None  # invalidated: a close beyond the leg's start
        j = i - w
        if j > leg.end_i and piv[j]:
            add(j)
        if pair is None:
            continue
        p1, p2 = pair
        x2 = float(ext[p2])
        after = ext[p2 + 1 : i + 1]
        if after.size and d * (float(after.min() if d > 0 else after.max()) - x2) < 0.0:
            pair = None  # the second pivot was undercut: wait for a new pair
            continue
        level = breakout_level(d, f.high, f.low, p1, p2)
        if d * (f.close[i] - level) <= 0.0 or f.synthetic[max(p1 - w, 0) : i + 1].any():
            continue
        hour = int(f.last_hour[i])
        atr15, atr1h = float(f.atr15[i]), float(f.h_atr[hour]) if hour >= 0 else float("nan")
        if not (np.isfinite(atr15) and np.isfinite(atr1h)):
            continue
        entry = float(f.close[i])
        stop = stop_level(d, x2, atr15, p.stop_buf_atr)
        tp1 = first_target(entry, stop, p.tp1_r)
        if skip_reason(d, entry, stop, tp1, tp2, atr1h, p.max_stop_atr1h) is not None:
            continue
        p1_ts, p2_ts = int(f.ts[p1]), int(f.ts[p2])
        return SignalB(i=i, ts=int(f.ts[i]), direction=d, entry_ref=entry, stop=stop, tp1=tp1, tp2=tp2,
                       leg_start=leg.start, leg_end=leg.end, p1_ts=p1_ts, p2_ts=p2_ts, level=level,
                       reason=f"{'long' if d > 0 else 'short'} div {p1_ts}/{p2_ts} break {level:.2f}")
    return None


def generate_signals(f: Frame15, p: StratBParams, legs: list[RefLeg] | None = None) -> list[SignalB]:
    """Every strategy-B signal over the frame, in bar order. Longs use the latest up leg and shorts the
    latest down leg, each until the next leg of the same direction; at most one signal per leg, and on
    the (rare) bar where a long and a short both confirm, the newer leg's signal."""
    legs = reference_legs(f, p.zz_k, p) if legs is None else legs
    n = len(f)
    life = p.confirm_hours * H1 // M15
    found: list[tuple[int, int, SignalB]] = []
    for d in (1, -1):
        mine = [leg for leg in legs if leg.direction == d]
        for k, leg in enumerate(mine):
            nxt = mine[k + 1].known_i if k + 1 < len(mine) else n
            sig = _scan(f, p, leg, min(nxt - 1, leg.known_i + life, n - 1))
            if sig is not None:
                found.append((sig.i, -leg.known_i, sig))
    out: list[SignalB] = []
    for i, _, sig in sorted(found, key=lambda x: (x[0], x[1])):
        if not out or out[-1].i != i:
            out.append(sig)
    return out
