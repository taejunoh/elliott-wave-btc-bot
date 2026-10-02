# tests/strategy/test_choch.py
from __future__ import annotations

import pytest

from ewbot.strategy.choch import ChochDetector, _choch_level
from ewbot.types import H1, Bar

T0 = 1_700_000_000 - (1_700_000_000 % H1)

# ---------------------------------------------------------------- pure level logic

TS = [T0 + k * H1 for k in range(20)]
HIGHS = [100.0 + k for k in range(20)]
LOWS = [90.0 + k for k in range(20)]


def test_level_is_max_high_of_n_bars_strictly_before_e_bar() -> None:
    assert _choch_level(TS, HIGHS, LOWS, 1, TS[15], 3) == 114.0  # max(HIGHS[12:15])


def test_level_ignores_the_e_bar_and_later_bars() -> None:
    highs = list(HIGHS)
    highs[15] = highs[16] = 10_000.0
    assert _choch_level(TS, highs, LOWS, 1, TS[15], 3) == 114.0


def test_level_short_side_is_min_low() -> None:
    assert _choch_level(TS, HIGHS, LOWS, -1, TS[15], 3) == 102.0  # min(LOWS[12:15])


def test_level_uses_fewer_bars_when_history_is_short() -> None:
    assert _choch_level(TS, HIGHS, LOWS, 1, TS[2], 3) == 101.0


def test_level_none_when_no_bar_precedes_e() -> None:
    assert _choch_level(TS, HIGHS, LOWS, 1, TS[0], 3) is None


def test_level_rejects_bad_direction() -> None:
    with pytest.raises(ValueError):
        _choch_level(TS, HIGHS, LOWS, 0, TS[15], 3)


# ---------------------------------------------------------------- detector on hand-made series


def build_series(segments: list[tuple[str, int]], start: float = 1000.0) -> list[Bar]:
    """flat: O=C=c, H=c+3, L=c-3 | up: O=L=c, C=c+5, H=c+6 | down: O=H=c, C=c-5, L=c-6"""
    bars: list[Bar] = []
    c = start
    for kind, n in segments:
        for _ in range(n):
            ts = T0 + len(bars) * H1
            if kind == "flat":
                bars.append(Bar(ts, c, c + 3, c - 3, c, 1.0))
            elif kind == "up":
                bars.append(Bar(ts, c, c + 6, c, c + 5, 1.0))
                c += 5
            elif kind == "down":
                bars.append(Bar(ts, c, c, c - 6, c - 5, 1.0))
                c -= 5
            else:
                raise ValueError(kind)
    return bars


def mirror(bars: list[Bar], m: float = 1000.0) -> list[Bar]:
    return [Bar(b.ts, 2 * m - b.open, 2 * m - b.low, 2 * m - b.high, 2 * m - b.close, b.volume) for b in bars]


# idx 0-19 flat | 20-39 up | 40-49 down | 50-53 up (lower high) | 54-75 down (L 959 @75 = E) | 76-105 up
LOWER_HIGH = [("flat", 20), ("up", 20), ("down", 10), ("up", 4), ("down", 22), ("up", 30)]
# idx 0-19 flat | 20-39 up | 40-69 straight down (L 949 @69 = E) | 70-99 up
V_SHAPE = [("flat", 20), ("up", 20), ("down", 30), ("up", 30)]


def run(bars: list[Bar], direction: int, leg_start_idx: int, e_idx: int) -> tuple[list[float | None], list[bool]]:
    det = ChochDetector(n_bars=3)
    levels: list[float | None] = []
    fired: list[bool] = []
    for k, b in enumerate(bars):
        det.update(b)
        if k >= e_idx:
            levels.append(det.level(direction, bars[leg_start_idx].ts, bars[e_idx].ts))
            fired.append(det.triggered(direction, bars[leg_start_idx].ts, bars[e_idx].ts))
        else:
            levels.append(None)
            fired.append(False)
    return levels, fired


def test_long_level_is_three_bar_high_before_e_and_triggers_once_on_cross() -> None:
    bars = build_series(LOWER_HIGH)
    assert bars[75].low == 959.0 and bars[72].high == 980.0
    levels, fired = run(bars, 1, leg_start_idx=39, e_idx=75)
    assert levels[75] == 980.0 and levels[105] == 980.0  # the lower high (1071) is not used
    assert bars[79].close == 980.0 and bars[80].close == 985.0
    assert [k for k, f in enumerate(fired) if f] == [80]  # 980 is not > 980; prev 980 <= 980 counts


def test_v_reversal_same_rule() -> None:
    bars = build_series(V_SHAPE)
    assert bars[69].low == 949.0
    levels, fired = run(bars, 1, leg_start_idx=39, e_idx=69)
    assert levels[69] == 970.0  # max(high[66..68]) = bar 66 open
    assert [k for k, f in enumerate(fired) if f] == [74]


def test_short_side_is_mirror_image() -> None:
    levels, fired = run(mirror(build_series(LOWER_HIGH)), -1, leg_start_idx=39, e_idx=75)
    assert levels[75] == 1020.0  # 2000 - 980
    assert [k for k, f in enumerate(fired) if f] == [80]
    vlevels, vfired = run(mirror(build_series(V_SHAPE)), -1, leg_start_idx=39, e_idx=69)
    assert vlevels[69] == 1030.0  # 2000 - 970
    assert [k for k, f in enumerate(vfired) if f] == [74]


def test_triggered_false_before_two_bars_and_rejects_out_of_order_bars() -> None:
    det = ChochDetector()
    b0 = Bar(T0, 100.0, 101.0, 99.0, 100.0, 1.0)
    det.update(b0)
    assert det.triggered(1, T0 - H1, T0) is False
    with pytest.raises(ValueError):
        det.update(b0)


def test_n_bars_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ChochDetector(n_bars=0)
