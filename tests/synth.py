"""Synthetic Elliott paths for engine tests (spec 10).

pivot grammar (labelled waypoints with chosen Fib ratios)
  -> 1h closes: straight lines between waypoints + Brownian-bridge noise pinned at the waypoints
  -> 1h OHLC (open = previous close, small wicks, each waypoint is the strict first extreme of its legs)
  -> 4h bars by streaming aggregation (``feed``) or by ewbot.data.candles.aggregate_4h (``batch_feed``).
Prices live on a 1/64 grid so mirrored paths (p' = 2*p_ref - p) are bit-exact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

import numpy as np
import pandas as pd

from ewbot.config import WaveParams
from ewbot.data.candles import aggregate_4h, to_bars
from ewbot.indicators import ATR
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveEngine, WaveState

T0 = 1_700_006_400        # 2023-11-15 00:00 UTC, a multiple of H4
TICK = 1.0 / 64.0

Feed = tuple[Bar, Bar | None, tuple[Bar, ...] | None]


@dataclass(frozen=True, slots=True)
class Waypoint:
    price: float
    label: str            # "" = unlabelled lead-in pivot; else S,1..5 (impulse) or X,A,B,C


def q64(x: float) -> float:
    return round(x / TICK) * TICK


def impulse_abc(s: float = 1000.0, w1: float = 100.0, r2: float = 0.55, w3: float = 1.618,
                r4: float = 0.3, w5: float = 1.0, a: float = 0.9, rb: float = 0.618,
                ca: float = 1.0) -> list[Waypoint]:
    """Falling lead-in (S is the lowest low, P1 breaks the high before S), bull impulse S..P5 with
    the given ratios, a down ABC (|A| = a*W1, |B| = rb*|A|, |C| = ca*|A|), then a bounce so C confirms."""
    k = w1 / 100.0
    lead = [s + 250 * k, s + 150 * k, s + 200 * k, s + 80 * k, s + 130 * k, s + 40 * k, s + 90 * k]
    p1 = s + w1
    p2 = p1 - r2 * w1
    p3 = p2 + w3 * w1
    p4 = p3 - r4 * (p3 - p2)
    p5 = p4 + w5 * w1
    a_end = p5 - a * w1
    b_end = a_end + rb * a * w1
    c_end = b_end - ca * a * w1
    tail = c_end + 0.6 * a * w1
    pts = [Waypoint(q64(x), "") for x in lead]
    pts += [Waypoint(q64(x), lab) for x, lab in
            ((s, "S"), (p1, "1"), (p2, "2"), (p3, "3"), (p4, "4"), (p5, "5"), (a_end, "A"),
             (b_end, "B"), (c_end, "C"), (tail, ""))]
    return pts


def _hours(way: Sequence[Waypoint], hours_per_point: float) -> list[int]:
    hours = [0]
    for a, b in zip(way, way[1:]):
        hours.append(hours[-1] + max(4, round(abs(b.price - a.price) * hours_per_point)))
    return hours


def _ohlc(closes: np.ndarray, hours: list[int], way: Sequence[Waypoint], wick: np.ndarray) -> list[Bar]:
    bars: list[Bar] = []
    seg = 0
    o = closes[0]
    for k, c in enumerate(closes):
        while seg + 1 < len(hours) - 1 and k > hours[seg + 1]:
            seg += 1
        a, b = way[seg].price, way[seg + 1].price
        if k == 0:
            a = b = way[0].price
        top, bot = max(a, b), min(a, b)
        hi_body, lo_body = max(o, c), min(o, c)
        cap_hi = top if hi_body >= top else top - TICK          # only waypoint bars may touch the band edge
        cap_lo = bot if lo_body <= bot else bot + TICK
        h = q64(min(hi_body + wick[k, 0], cap_hi))
        lo = q64(max(lo_body - wick[k, 1], cap_lo))
        bars.append(Bar(T0 + k * H1, float(o), float(max(h, hi_body)), float(min(lo, lo_body)), float(c), 1.0))
        o = c
    return bars


def nominal_thr(way: Sequence[Waypoint], params: WaveParams, hours_per_point: float = 1.0) -> float:
    """K4 x median 4h ATR of the noiseless path (the noise scale unit)."""
    bars = make_path(way, 0.0, 0, params, hours_per_point, thr=1.0)[0]
    atr = ATR(params.atr_len)
    vals = []
    for _, b4, _ in feed(bars):
        if b4 is not None:
            v = atr.update(b4.high, b4.low, b4.close)
            if v is not None:
                vals.append(v)
    return params.K4 * float(np.median(vals))


KNOT_H = 8   # bridge knots every 8 hours: low-frequency noise that barely moves the 4h ATR


def _bridge(n: int, amp: float, rng: np.random.Generator) -> np.ndarray:
    """Brownian bridge over n hourly steps (0 at both ends), knots every KNOT_H hours, linearly
    interpolated, scaled so that max |deviation| == amp."""
    knots = np.unique(np.concatenate([np.arange(0, n, KNOT_H), [n]]))
    walk = np.concatenate([[0.0], np.cumsum(rng.standard_normal(len(knots) - 1))])
    bridge = walk - (knots / n) * walk[-1]
    out = np.interp(np.arange(n + 1), knots, bridge)
    m = float(np.max(np.abs(out)))
    return out * (amp / m) if m > 0 else out


def make_path(way: Sequence[Waypoint], noise: float, seed: int, params: WaveParams,
              hours_per_point: float = 1.0, thr: float | None = None) -> tuple[list[Bar], list[int]]:
    """1h bars through ``way``; bridge amplitude = noise * thr (thr defaults to nominal_thr).
    Returns (bars, hour index of every waypoint)."""
    hours = _hours(way, hours_per_point)
    if thr is None:
        thr = nominal_thr(way, params, hours_per_point)
    amp = noise * thr
    rng = np.random.default_rng(seed)
    closes = np.empty(hours[-1] + 1)
    for (a, b), (h0, h1) in zip(zip(way, way[1:]), zip(hours, hours[1:])):
        n = h1 - h0
        base = a.price + (b.price - a.price) * np.arange(n + 1) / n
        if amp > 0 and n > 1:
            base = base + _bridge(n, amp, rng)
        lo, hi = min(a.price, b.price), max(a.price, b.price)
        seg = np.clip(base, lo + TICK, hi - TICK)
        seg[0], seg[-1] = a.price, b.price
        closes[h0:h1 + 1] = seg
    closes = np.round(closes / TICK) * TICK
    wick = rng.uniform(0.0, 0.3 * amp, size=(len(closes), 2)) if amp > 0 else np.zeros((len(closes), 2))
    return _ohlc(closes, hours, way, wick), hours


def feed(bars1h: Sequence[Bar]) -> Iterator[Feed]:
    """Streaming 1h -> (bar1h, bar4h | None, sub_bars | None); 4h buckets aligned to UTC multiples of 4h,
    emitted only when complete (same semantics as aggregate_4h)."""
    buf: list[Bar] = []
    for b in bars1h:
        if not buf or b.ts // H4 != buf[0].ts // H4:
            buf = []
        buf.append(b)
        if len(buf) == 4 and buf[0].ts % H4 == 0:
            agg = Bar(buf[0].ts, buf[0].open, max(x.high for x in buf), min(x.low for x in buf),
                      buf[-1].close, sum(x.volume for x in buf), any(x.synthetic for x in buf))
            yield b, agg, tuple(buf)
        else:
            yield b, None, None


def batch_feed(bars1h: Sequence[Bar]) -> Iterator[Feed]:
    """Same stream, but the 4h bars come from the batch aggregate_4h on a DataFrame."""
    df = pd.DataFrame({"ts": [b.ts for b in bars1h], "open": [b.open for b in bars1h],
                       "high": [b.high for b in bars1h], "low": [b.low for b in bars1h],
                       "close": [b.close for b in bars1h], "volume": [b.volume for b in bars1h],
                       "synthetic": [b.synthetic for b in bars1h]})
    by_close = {b4.ts + H4 - H1: b4 for b4 in to_bars(aggregate_4h(df))}
    pos = {b.ts: k for k, b in enumerate(bars1h)}
    for b in bars1h:
        b4 = by_close.get(b.ts)
        if b4 is None:
            yield b, None, None
        else:
            k = pos[b.ts]
            yield b, b4, tuple(bars1h[k - 3:k + 1])


def run_engine(bars1h: Sequence[Bar], params: WaveParams, use_cache: bool = True,
               batch: bool = False) -> list[WaveState]:
    eng = WaveEngine(params)
    eng._use_cache = use_cache
    src = batch_feed(bars1h) if batch else feed(bars1h)
    return [eng.on_1h_close(b, b4, sub) for b, b4, sub in src]


def mirror_bars(bars: Sequence[Bar], p_ref: float) -> list[Bar]:
    two = 2.0 * p_ref
    return [Bar(b.ts, two - b.open, two - b.low, two - b.high, two - b.close, b.volume, b.synthetic)
            for b in bars]


def recovered(states: Sequence[WaveState], way: Sequence[Waypoint], hours: Sequence[int]) -> bool:
    """True iff the final 4h pivots are exactly the designed waypoints (all but the unconfirmed tail)."""
    piv = states[-1].pivots4h
    want = [(T0 + h * H1, w.price) for w, h in zip(way[:-1], hours[:-1])]
    return [(p.sub_ts, p.price) for p in piv] == want


def recovery_curve(levels: Sequence[float], seeds: Sequence[int], params: WaveParams,
                   way: Sequence[Waypoint] | None = None) -> dict[float, float]:
    """Exact-pivot-recovery rate per noise level (noise = bridge amplitude / nominal thr)."""
    way = impulse_abc() if way is None else way
    thr = nominal_thr(way, params)
    out: dict[float, float] = {}
    for lv in levels:
        ok = 0
        for sd in seeds:
            bars, hours = make_path(way, lv, sd, params, thr=thr)
            ok += recovered(run_engine(bars, params), way, hours)
        out[lv] = ok / len(seeds)
    return out


def state_at(states: Sequence[WaveState], hour: int) -> WaveState:
    s = states[hour]
    assert s.ts == T0 + hour * H1
    return s


def label_hours(way: Sequence[Waypoint], hours: Sequence[int]) -> dict[str, int]:
    return {w.label: h for w, h in zip(way, hours) if w.label}



def random_bars(seed: int, n: int, vol: float = 6.0, start: float = 5000.0) -> list[Bar]:
    """Random-walk 1h bars on the 1/64 grid with drifting regimes (property tests, mirror tests)."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0.0, vol / 3.0, size=n // 48 + 1), 48)[:n]
    closes = np.round((start + np.cumsum(rng.standard_t(4, size=n) * vol + drift)) / TICK) * TICK
    wick = np.round(np.abs(rng.normal(0.0, vol / 2.0, size=(n, 2))) / TICK) * TICK
    bars, o = [], float(closes[0])
    for k in range(n):
        c = float(closes[k])
        bars.append(Bar(T0 + k * H1, o, max(o, c) + float(wick[k, 0]), min(o, c) - float(wick[k, 1]), c, 1.0))
        o = c
    return bars
