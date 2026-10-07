"""Strategy-B trading rules as pure functions (Plan 4 spec 4.2-4.4). Direction +1 = long setup on an
up leg (start L, end H), -1 = short setup on a down leg (start H, end L); every formula below is written
once for both. signals.py applies them bar by bar."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def _rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder RSI(n): NaN until n price changes exist (index n), then RMA of gains and losses seeded with
    their simple mean. 100 when there are only gains, 50 when nothing moved. out[i] uses close[:i+1] only."""
    c = np.asarray(close, dtype=np.float64)
    out = np.full(c.size, np.nan)
    if c.size <= n:
        return out
    diff = np.diff(c)
    gains = np.maximum(diff, 0.0).tolist()
    losses = np.maximum(-diff, 0.0).tolist()
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = _rsi(ag, al)
    for i in range(n + 1, c.size):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi(ag, al)
    return out


def pivot_lows(low: np.ndarray, w: int = 3) -> np.ndarray:
    """Bar j is a pivot low when low[j] is strictly below each of the w lows before it and at or below
    each of the w lows after it (spec 4.3: left/right 3 bars). It becomes known at the CLOSE of bar j + w;
    the first and last w bars are never pivots."""
    x = np.asarray(low, dtype=np.float64)
    n = x.size
    out = np.zeros(n, dtype=bool)
    if n < 2 * w + 1:
        return out
    core = x[w : n - w]
    ok = np.ones(core.size, dtype=bool)
    for k in range(1, w + 1):
        ok &= core < x[w - k : n - w - k]
        ok &= core <= x[w + k : n - w + k]
    out[w : n - w] = ok
    return out


def pivot_highs(high: np.ndarray, w: int = 3) -> np.ndarray:
    """Mirror of pivot_lows: strictly above the w highs before, at or above the w highs after."""
    return pivot_lows(-np.asarray(high, dtype=np.float64), w)


def retracement_zone(start: float, end: float, z_lo: float, z_hi: float) -> tuple[float, float]:
    """(lo, hi) of the retracement of the leg start -> end between z_lo and z_hi of its length (spec 4.2).
    Up leg L -> H: [H - z_hi (H - L), H - z_lo (H - L)]; down leg H -> L: [L + z_lo (H - L), L + z_hi (H - L)]."""
    a = end - z_lo * (end - start)
    b = end - z_hi * (end - start)
    return (min(a, b), max(a, b))


def extension_target(start: float, end: float, e: float) -> float:
    """Second target (spec 4.4): L + e (H - L) for an up leg, H - e (H - L) for a down leg."""
    return start + e * (end - start)


def has_confluence(zone: tuple[float, float], levels: Sequence[float], tol: float) -> bool:
    """Supply/demand overlap (spec 4.2): some earlier swing level inside the zone widened by +/- tol."""
    lo, hi = zone
    return any(lo - tol <= x <= hi + tol for x in levels)


def is_divergence(direction: int, price1: float, price2: float, rsi1: float, rsi2: float) -> bool:
    """Long: lower low in price and higher low in RSI; short: higher high in price, lower high in RSI."""
    if not (math.isfinite(rsi1) and math.isfinite(rsi2)):
        return False
    return direction * (price1 - price2) > 0.0 and direction * (rsi2 - rsi1) > 0.0


def breakout_level(direction: int, high: np.ndarray, low: np.ndarray, p1: int, p2: int) -> float:
    """The 15m swing between the two pivots (bars p1+1 .. p2-1): highest high for a long, lowest low for a short."""
    if p2 - p1 < 2:
        raise ValueError(f"pivots {p1} and {p2} have no bar between them")
    if direction > 0:
        return float(np.max(high[p1 + 1 : p2]))
    return float(np.min(low[p1 + 1 : p2]))


def stop_level(direction: int, pivot_price: float, atr15: float, buf_atr: float) -> float:
    """Invalidation (spec 4.4): second pivot -/+ buf_atr x ATR15m."""
    return pivot_price - direction * buf_atr * atr15


def first_target(entry_ref: float, stop: float, tp1_r: float) -> float:
    """TP1 = entry + tp1_r x R with R = entry - stop (signed, so the same formula serves both sides)."""
    return entry_ref + tp1_r * (entry_ref - stop)


def skip_reason(
    direction: int, entry_ref: float, stop: float, tp1: float, tp2: float, atr1h: float, max_stop_atr1h: float
) -> str | None:
    """None when the setup may be traded, else why it is skipped (spec 4.4 filters)."""
    r = direction * (entry_ref - stop)
    if r <= 0.0:
        return "stop_wrong_side"
    if r > max_stop_atr1h * atr1h:
        return "stop_too_far"
    if direction * (tp2 - tp1) <= 0.0:
        return "tp2_not_beyond_tp1"
    return None
