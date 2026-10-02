"""Fib-fit scoring of counts (spec 4.4). All ratios are arithmetic price ratios computed in u-space.

Terms (w = weight). Completed waves use the target function; the in-progress wave (ending at E)
uses f_ip(x; hi) with half weight. q = (sum w*s + w0*0.5) / (sum w + w0).

  g_start 1.0  IMP: 0.5*[S lowest of the (up to) 4 same-kind pivots before S]
                    + 0.5*[P1 beyond the pivot immediately before S]
               ABC: [X beyond all of the (up to) 4 pivots before X]   (no prior pivot -> indicator 0)
  g_r2    1.0  |W2|/|W1| zone [0.5, 0.618]                in progress: f_ip(., 0.786) w 0.5
  g_w3    1.5  |W3|/|W1| points {1.618, 2.618}            in progress: f_ip(., 2.618) w 0.75
  g_r4    1.0  |W4|/|W3| zone [0.236, 0.382]              in progress: f_ip(., 0.5)   w 0.5
  g_alt   0.5  min(1, |r2 - r4| / 0.2)                    only once W4 is complete (phase W5)
  g_w5    --   (W5 is never complete in a phase)         in progress: f_ip(W5/W1, 1.618) w 0.5
  g_rB    1.0  |B|/|A| zone [0.382,0.618] U [0.9,1.236]   in progress: f_ip(., 1.382) w 0.5
  g_C     --   (C is never complete in a phase)          in progress: f_ip(C/A, 1.618) w 0.75
"""
from __future__ import annotations

import math
from typing import Sequence

from ewbot.config import WaveParams
from ewbot.waves.patterns import N_POINTS, _pattern_of
from ewbot.waves.zigzag import Pivot

ZONE_LO = {"W2": 0.382, "W3": 1.0, "W4": 0.236, "W5": 0.618, "B": 0.382, "C": 0.786}
_IP = {  # phase -> (hi, weight) of the in-progress term
    "W2": (0.786, 0.5), "W3": (2.618, 0.75), "W4": (0.5, 0.5), "W5": (1.618, 0.5),
    "B": (1.382, 0.5), "C": (1.618, 0.75),
}


def _sim(x: float, t: float, sigma: float) -> float:
    z = math.log(x / t)
    return math.exp(-(z * z) / (2.0 * sigma * sigma))


def f_pt(x: float, targets: Sequence[float], sigma: float) -> float:
    if x <= 0.0:
        return 0.0
    return max(_sim(x, t, sigma) for t in targets)


def f_zone(x: float, a: float, b: float, sigma: float) -> float:
    if x <= 0.0:
        return 0.0
    if a <= x <= b:
        return 1.0
    return _sim(x, a if x < a else b, sigma)


def f_ip(x: float, hi: float, sigma: float) -> float:
    if x <= hi:
        return 1.0
    return _sim(x, hi, sigma)


def softmax(qs: Sequence[float], T: float) -> list[float]:
    if not qs:
        return []
    m = max(qs)
    ex = [math.exp((q - m) / T) for q in qs]
    s = sum(ex)
    return [e / s for e in ex]


def _u(d: int, pts: Sequence[float]) -> list[float]:
    return [d * p for p in pts]


def progress_x(pattern: str, d: int, phase: str, pts: Sequence[float], E_live: float) -> float:
    """Ratio of the in-progress wave used for done-ness: r2, W3/W1, r4, W5/W1, B/A, C/A."""
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    u = _u(d, pts)
    ue = d * E_live
    if phase == "W2":
        return (u[1] - ue) / (u[1] - u[0])
    if phase == "W3":
        return (ue - u[2]) / (u[1] - u[0])
    if phase == "W4":
        return (u[3] - ue) / (u[3] - u[2])
    if phase == "W5":
        return (ue - u[4]) / (u[1] - u[0])
    if phase == "B":
        return (u[1] - ue) / (u[1] - u[0])
    return (ue - u[2]) / (u[1] - u[0])           # C


def is_done(pattern: str, d: int, phase: str, pts: Sequence[float], E_live: float) -> bool:
    """done_i = x_i >= zone_lo(phase); W5 also needs E beyond P3, C also needs E beyond A_end."""
    if progress_x(pattern, d, phase, pts, E_live) < ZONE_LO[phase]:
        return False
    ue = d * E_live
    if phase == "W5":
        return ue > d * pts[3]
    if phase == "C":
        return ue > d * pts[1]
    return True


def _static_terms(pattern: str, phase: str, u: Sequence[float], sigma: float) -> list[tuple[float, float]]:
    """(weight, score) of the COMPLETED-wave terms, in a fixed order (g_start excluded)."""
    n = N_POINTS[phase]
    out: list[tuple[float, float]] = []
    if pattern == "IMP":
        w1 = u[1] - u[0]
        if n >= 3:
            r2 = (u[1] - u[2]) / w1
            out.append((1.0, f_zone(r2, 0.5, 0.618, sigma)))
        if n >= 4:
            w3 = u[3] - u[2]
            out.append((1.5, f_pt(w3 / w1, (1.618, 2.618), sigma)))
        if n >= 5:
            r4 = (u[3] - u[4]) / (u[3] - u[2])
            out.append((1.0, f_zone(r4, 0.236, 0.382, sigma)))
            out.append((0.5, min(1.0, abs(r2 - r4) / 0.2)))
    else:
        if n >= 3:
            rb = (u[1] - u[2]) / (u[1] - u[0])
            out.append((1.0, max(f_zone(rb, 0.382, 0.618, sigma), f_zone(rb, 0.9, 1.236, sigma))))
    return out


def _ip_term(pattern: str, d: int, phase: str, pts: Sequence[float], E: float, sigma: float) -> tuple[float, float]:
    hi, w = _IP[phase]
    return w, f_ip(progress_x(pattern, d, phase, pts, E), hi, sigma)


def _g_start_at(pattern: str, d: int, pts: Sequence[float], s_pos: int, ctx: Sequence[Pivot]) -> float:
    """g_start with the start pivot located at ctx[s_pos]."""
    s = ctx[s_pos]
    us = d * s.price
    if pattern == "IMP":
        prior_same: list[Pivot] = []
        k = s_pos - 1
        while k >= 0 and len(prior_same) < 4:
            if ctx[k].kind == s.kind:
                prior_same.append(ctx[k])
            k -= 1
        a = 1.0 if prior_same and all(us <= d * p.price for p in prior_same) else 0.0
        b = 1.0 if s_pos >= 1 and d * pts[1] > d * ctx[s_pos - 1].price else 0.0
        return 0.5 * a + 0.5 * b
    prior = ctx[max(0, s_pos - 4):s_pos]
    return 1.0 if prior and all(us <= d * p.price for p in prior) else 0.0


def _locate_start(pattern: str, d: int, pts: Sequence[float], ctx: Sequence[Pivot]) -> int:
    kind = -d   # IMP S and ABC X are u-space lows -> price kind -d
    for k in range(len(ctx) - 1, -1, -1):
        if ctx[k].kind == kind and ctx[k].price == pts[0]:
            return k
    raise ValueError("start point not found in ctx")


def _combine(static_ws: float, static_w: float, ip_w: float, ip_s: float, w0: float) -> float:
    return (static_ws + ip_w * ip_s + w0 * 0.5) / (static_w + ip_w + w0)


def _static_sums(pattern: str, d: int, phase: str, pts: Sequence[float], g_start: float,
                 sigma: float) -> tuple[float, float]:
    ws, w = 1.0 * g_start, 1.0
    for wj, sj in _static_terms(pattern, phase, _u(d, pts), sigma):
        ws += wj * sj
        w += wj
    return ws, w


def fit_q(pattern: str, d: int, phase: str, pts: Sequence[float], E: float, ctx: Sequence[Pivot],
          params: WaveParams) -> float:
    """Fit q in [0, 1]. ctx = confirmed pivots up to now; S/X is located as the most recent pivot of
    the start kind whose price equals pts[0] (the enumerator passes the exact position instead)."""
    if _pattern_of(phase) != pattern:
        raise ValueError(f"phase {phase} does not belong to {pattern}")
    g0 = _g_start_at(pattern, d, pts, _locate_start(pattern, d, pts, ctx), ctx)
    ws, w = _static_sums(pattern, d, phase, pts, g0, params.sigma)
    ip_w, ip_s = _ip_term(pattern, d, phase, pts, E, params.sigma)
    return _combine(ws, w, ip_w, ip_s, params.w0)
