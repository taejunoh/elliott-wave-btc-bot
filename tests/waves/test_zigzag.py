from __future__ import annotations

import pytest

from ewbot.types import H1, H4, Bar
from ewbot.waves.zigzag import Pivot, ZigZag

T0 = 1_700_006_400  # 2023-11-15 00:00 UTC, multiple of H4


def _walk_bars(closes: list[float], first_open: float, tf: int = H1) -> list[Bar]:
    """open = previous close, wick 0.5 beyond the body on both sides -> TR == 2 for 1-point moves."""
    out, o = [], first_open
    for k, c in enumerate(closes):
        out.append(Bar(T0 + k * tf, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1.0))
        o = c
    return out


CLOSES = [100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 100, 101, 102, 103, 104]


def test_close_confirmed_pivots_and_constant_atr() -> None:
    zz = ZigZag(k=2.0, atr_len=3)          # ATR == 2.0 -> thr == 4.0
    bars = _walk_bars([float(c) for c in CLOSES], first_open=99.0)
    got = {}
    for i, b in enumerate(bars):
        p = zz.update(i, b)
        if p is not None:
            got[i] = p
    assert zz.atr == pytest.approx(2.0)
    # bar 3: close 103 - lowest low 98.5 = 4.5 >= 4 -> first pivot = low of bar 0
    # bar 9: E 105.5 (bar 5; bar 6 only TIES the high so E stays on bar 5) - close 101 = 4.5 >= 4
    # bar 14: close 104 - E 99.5 (bar 10) = 4.5 >= 4
    assert got == {
        3: Pivot(0, T0, 98.5, -1, 3),
        9: Pivot(5, T0 + 5 * H1, 105.5, 1, 9),
        14: Pivot(10, T0 + 10 * H1, 99.5, -1, 14),
    }
    assert zz.pivots == [got[3], got[9], got[14]]
    # new up leg after bar 14: E = max(close of the extreme bar 10 = 100, highs of bars 11..14) = 104.5
    assert (zz.leg_dir, zz.E, zz.E_i, zz.E_sub_ts) == (1, 104.5, 14, T0 + 14 * H1)


def test_no_pivot_before_atr_is_ready() -> None:
    zz = ZigZag(k=0.1, atr_len=5)
    bars = _walk_bars([100.0, 150.0, 90.0, 160.0], first_open=100.0)
    assert [zz.update(i, b) for i, b in enumerate(bars)] == [None, None, None, None]
    assert zz.leg_dir == 0 and zz.atr is None and zz.pivots == []


def _bar(ts: int, o: float, h: float, lo: float, c: float) -> Bar:
    return Bar(ts, o, h, lo, c, 1.0)


def _prefix_4h(zz: ZigZag) -> None:
    # four 4h bars with TR == 4 -> ATR(3) == 4 by bar 3 whether or not bar 0's high-low counts as a TR;
    # k = 1 -> thr = 4. Bar 2 closes 3.5 from both extremes, so the first pivot appears on bar 3.
    zz.update(0, _bar(T0, 100, 102, 98, 101))
    zz.update(1, _bar(T0 + H4, 101, 104, 100, 103))
    assert zz.update(2, _bar(T0 + 2 * H4, 103, 105, 101, 101.5)) is None
    p = zz.update(3, _bar(T0 + 3 * H4, 101.5, 105.5, 101.5, 105))
    # close 105 - lowest low 98 = 7 >= 4 -> pivot low at bar 0; up leg E = max(101 close, 104, 105, 105.5)
    assert p == Pivot(0, T0, 98.0, -1, 3)
    assert (zz.leg_dir, zz.E, zz.E_i, zz.atr) == (1, 105.5, 3, 4.0)


def test_same_4h_bar_extreme_and_reversal_uses_close_of_extreme_1h_bar() -> None:
    zz = ZigZag(k=1.0, atr_len=3)
    _prefix_4h(zz)
    t4 = T0 + 4 * H4
    subs = [
        _bar(t4, 105, 120, 100, 118),            # extreme 1h bar: high 120, its low 100 has unknown order
        _bar(t4 + H1, 118, 118.5, 112, 113),
        _bar(t4 + 2 * H1, 113, 113.5, 109, 110),
        _bar(t4 + 3 * H1, 110, 110.5, 108, 109),
    ]
    p = zz.update(4, _bar(t4, 105, 120, 100, 109), subs)
    # TR = max(20, |120-105|, |100-105|) = 20 -> ATR = (2*4 + 20)/3 = 9.333; 120 - 109 = 11 >= 9.333
    assert zz.atr == pytest.approx(28.0 / 3.0)
    assert p == Pivot(4, t4, 120.0, 1, 4)
    # new down leg: min(close 118 of the extreme 1h bar, lows 112, 109, 108) = 108 -- NOT the 100 low
    assert (zz.leg_dir, zz.E, zz.E_i, zz.E_sub_ts) == (-1, 108.0, 4, t4 + 3 * H1)


def test_same_bar_without_sub_bars_seeds_next_leg_with_bar_close() -> None:
    zz = ZigZag(k=1.0, atr_len=3)
    _prefix_4h(zz)
    t4 = T0 + 4 * H4
    p = zz.update(4, _bar(t4, 105, 120, 100, 109))
    assert p == Pivot(4, t4, 120.0, 1, 4)
    assert (zz.leg_dir, zz.E, zz.E_i, zz.E_sub_ts) == (-1, 109.0, 4, t4)


def test_extreme_sub_ts_is_the_1h_bar_holding_the_high() -> None:
    zz = ZigZag(k=1.0, atr_len=3)
    _prefix_4h(zz)
    t4 = T0 + 4 * H4
    subs = [_bar(t4, 105, 107, 104, 106), _bar(t4 + H1, 106, 111, 105, 110),
            _bar(t4 + 2 * H1, 110, 111, 108, 109), _bar(t4 + 3 * H1, 109, 110, 107, 108)]
    assert zz.update(4, _bar(t4, 105, 111, 104, 108), subs) is None
    # 111 appears twice: the FIRST 1h bar reaching it wins
    assert (zz.E, zz.E_i, zz.E_sub_ts) == (111.0, 4, t4 + H1)


def test_mirror_prices_give_mirrored_pivots() -> None:
    up, dn = ZigZag(k=2.0, atr_len=3), ZigZag(k=2.0, atr_len=3)
    for i, b in enumerate(_walk_bars([float(c) for c in CLOSES], first_open=99.0)):
        up.update(i, b)
        dn.update(i, Bar(b.ts, 200 - b.open, 200 - b.low, 200 - b.high, 200 - b.close, 1.0))
    assert [(p.i, p.sub_ts, 200 - p.price, -p.kind, p.confirmed_at) for p in up.pivots] == [
        (p.i, p.sub_ts, p.price, p.kind, p.confirmed_at) for p in dn.pivots
    ]


def test_index_must_increase() -> None:
    zz = ZigZag(k=2.0)
    zz.update(5, _bar(T0, 1, 2, 0.5, 1.5))
    with pytest.raises(ValueError):
        zz.update(5, _bar(T0 + H1, 1, 2, 0.5, 1.5))
