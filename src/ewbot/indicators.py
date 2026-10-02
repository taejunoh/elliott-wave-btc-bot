"""Streaming-friendly indicators: Wilder ATR and EMA."""
from __future__ import annotations

import numpy as np


class ATR:
    """Wilder ATR (RMA of true range). First value = SMA of the first ``n`` true ranges.

    The first bar's true range is ``high - low`` (no previous close). Afterwards
    ``TR = max(high - low, |high - prev_close|, |low - prev_close|)`` and
    ``ATR_t = (ATR_{t-1} * (n - 1) + TR_t) / n``.
    """

    def __init__(self, n: int = 14) -> None:
        if n < 1:
            raise ValueError("n must be >= 1")
        self._n = n
        self._prev_close: float | None = None
        self._count = 0
        self._sum = 0.0
        self._value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self._value is None:
            self._count += 1
            self._sum += tr
            if self._count == self._n:
                self._value = self._sum / self._n
        else:
            self._value = (self._value * (self._n - 1) + tr) / self._n
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


def atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """ATR for every bar; NaN until ``n`` bars have been seen. Identical to streaming ``ATR``."""
    h = np.asarray(high, dtype=np.float64)
    lo = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    if not (h.shape == lo.shape == c.shape):
        raise ValueError("high/low/close must have the same shape")
    out = np.full(h.shape[0], np.nan, dtype=np.float64)
    atr = ATR(n)
    for i in range(h.shape[0]):
        v = atr.update(float(h[i]), float(lo[i]), float(c[i]))
        if v is not None:
            out[i] = v
    return out


def ema_series(x: np.ndarray, n: int) -> np.ndarray:
    """EMA with alpha = 2/(n+1), seeded with ``x[0]`` (same as pandas ``ewm(span=n, adjust=False)``)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    arr = np.asarray(x, dtype=np.float64)
    out = np.empty_like(arr)
    if arr.size == 0:
        return out
    alpha = 2.0 / (n + 1)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out
