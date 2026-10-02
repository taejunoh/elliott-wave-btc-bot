from __future__ import annotations

import dataclasses

import pytest

from ewbot.config import WaveParams
from ewbot.types import H1, H4, Bar
from ewbot.waves.engine import WaveEngine, WaveState
from ewbot.waves.patterns import Invalidation
from ewbot.waves.scoring import is_done, softmax

T0 = 1_700_006_400        # multiple of H4
P = WaveParams(warmup_4h=50)
# lead-in, S=1000, P1=1100, P2=1045, P3=1206.8, P4=1158.26, P5=1258.26, A=1168.26, B=1223.88, C=1133.88, tail
WAY = [1250.0, 1150.0, 1200.0, 1080.0, 1130.0, 1040.0, 1090.0, 1000.0, 1100.0, 1045.0, 1206.8, 1158.26,
       1258.26, 1168.26, 1223.88, 1133.88, 1187.88]


def _linear(way: list[float]) -> tuple[list[Bar], list[int]]:
    """1h bars moving ~1 price unit per hour along straight lines; every waypoint is hit exactly."""
    closes, hours = [way[0]], [0]
    for a, b in zip(way, way[1:]):
        n = round(abs(b - a))
        closes += [a + (b - a) * k / n for k in range(1, n)] + [b]
        hours.append(len(closes) - 1)
    bars, o = [], closes[0]
    for k, c in enumerate(closes):
        bars.append(Bar(T0 + k * H1, o, max(o, c), min(o, c), c, 1.0))
        o = c
    return bars, hours


def _feed(bars: list[Bar]):
    buf: list[Bar] = []
    for b in bars:
        if not buf or b.ts // H4 != buf[0].ts // H4:
            buf = []
        buf.append(b)
        if len(buf) == 4:
            yield b, Bar(buf[0].ts, buf[0].open, max(x.high for x in buf), min(x.low for x in buf),
                         buf[-1].close, 4.0), tuple(buf)
        else:
            yield b, None, None


def _run(bars: list[Bar], params: WaveParams = P) -> list[WaveState]:
    eng = WaveEngine(params)
    return [eng.on_1h_close(*x) for x in _feed(bars)]


BARS, HOURS = _linear(WAY)
STATES = _run(BARS)
H = dict(zip(["", "", "", "", "", "", "", "S", "1", "2", "3", "4", "5", "A", "B", "C", "end"], HOURS))


def test_bookkeeping_and_ready() -> None:
    first = STATES[0]
    assert (first.ready, first.counts, first.top, first.q_top, first.s_rev) == (False, (), None, 0.0, 0.0)
    assert (first.E_live, first.E_live_ts, first.leg_dir, first.i4h, first.closed_4h) == (0.0, -1, 0, -1, False)
    assert STATES[3].closed_4h and STATES[3].i4h == 0 and not STATES[4].closed_4h
    assert all(s.i4h == (k + 1) // 4 - 1 for k, s in enumerate(STATES))
    # ATR(14) on 4h needs 14 closed 4h bars; the first pivot (1250 at hour 0) confirms soon after
    first_init = next(k for k, s in enumerate(STATES) if s.leg_dir != 0)
    assert STATES[first_init].new_pivot_4h is not None and STATES[first_init].new_pivot_4h.price == 1250.0
    first_ready = next(k for k, s in enumerate(STATES) if s.ready)
    assert first_ready == 4 * P.warmup_4h - 1          # warmup is the binding constraint here
    assert not any(s.new_pivot_4h for s in STATES if not s.closed_4h)


def test_pivots_are_the_waypoints() -> None:
    piv = STATES[-1].pivots4h
    assert [(p.sub_ts, p.price) for p in piv] == [(T0 + h * H1, w) for h, w in zip(HOURS[:-1], WAY[:-1])]
    assert all(p.i == (p.sub_ts - T0) // H4 for p in piv)
    assert all(p.confirmed_at >= p.i for p in piv)


def test_e_live_tracks_closed_1h_bars_between_4h_closes() -> None:
    h = H["S"] + 40                                   # mid W1, leg up
    while not STATES[h].closed_4h:
        h += 1
    for k in range(h + 1, h + 4):                     # the three 1h bars after a 4h close
        s = STATES[k]
        assert not s.closed_4h and s.leg_dir == 1
        assert s.E_live == BARS[k].high and s.E_live_ts == BARS[k].ts
    assert STATES[h + 4].closed_4h and STATES[h + 4].E_live == BARS[h + 4].high


def test_top_through_the_impulse_and_correction() -> None:
    def top(label: str) -> tuple:
        t = STATES[H[label]].top
        return (t.pattern, t.d, t.phase, t.prices)

    assert top("2") == ("IMP", 1, "W2", (1000.0, 1100.0))
    assert top("3") == ("IMP", 1, "W3", (1000.0, 1100.0, 1045.0))
    assert top("4") == ("IMP", 1, "W4", (1000.0, 1100.0, 1045.0, 1206.8))
    assert top("5") == ("IMP", 1, "W5", (1000.0, 1100.0, 1045.0, 1206.8, 1158.26))
    assert top("C") == ("ABC", -1, "C", (1258.26, 1168.26, 1223.88))
    s5 = STATES[H["5"]]
    assert s5.q_top == pytest.approx(6.0 / 6.5, abs=1e-6)
    assert s5.s_rev > 0.9                              # W5 = W1 and beyond P3: done
    k4 = next(k for k, s in enumerate(STATES) if s.new_pivot_4h and s.new_pivot_4h.price == 1158.26)
    assert STATES[k4].top.phase == "W5" and STATES[k4].s_rev < 0     # W5 just started: not done


def _check_probs_and_s_rev(states: list[WaveState], params: WaveParams) -> int:
    """Recompute q' (hysteresis at the last 4h close), order, probs and S_rev for every state."""
    bonus_used, prev_at_close = 0, None
    for k, s in enumerate(states):
        if s.closed_4h:
            prev_at_close = states[k - 1].top if k else None
        if not s.counts:
            assert s.probs == () and s.top is None and s.s_rev == 0.0
            continue
        pv = prev_at_close
        qp = [c.q + params.h if pv is not None and c.lineage == pv.lineage
              and c.points[:len(pv.points)] == pv.points else c.q for c in s.counts]
        bonus_used += sum(1 for c, x in zip(s.counts, qp) if x != c.q)
        assert all(a >= b for a, b in zip(qp, qp[1:]))
        assert list(s.probs) == pytest.approx(softmax(qp, params.T), abs=1e-12)
        assert sum(s.probs) == pytest.approx(1.0)
        exp = sum(p if is_done(c.pattern, c.d, c.phase, c.prices, s.E_live) else -p
                  for c, p in zip(s.counts, s.probs))
        assert s.s_rev == pytest.approx(exp, abs=1e-12)
        assert s.top is s.counts[0] and s.q_top == s.counts[0].q
    return bonus_used


def test_probs_hysteresis_and_s_rev_are_consistent() -> None:
    assert _check_probs_and_s_rev(STATES, P) > 0


def _first_non_4h_hour(start: int) -> int:
    h = start
    while (h + 1) % 4 == 0:
        h += 1
    return h


def test_wick_through_p2_kills_w3_and_every_extension() -> None:
    hw = _first_non_4h_hour(H["2"] + 29)                  # mid W3, not a 4h close
    bars = list(BARS)
    bars[hw] = dataclasses.replace(bars[hw], low=1043.0)  # wick 2 below P2 = 1045, close unchanged
    states = _run(bars)
    w3 = (1000.0, 1100.0, 1045.0)
    has = lambda s, pts: any(c.pattern == "IMP" and c.d == 1 and c.prices[:3] == pts for c in s.counts)  # noqa: E731
    assert has(states[hw - 1], w3) and states[hw - 1].top.prices == w3
    assert not has(states[hw], w3)                         # killed on the wick bar
    assert sum(states[hw].probs) == pytest.approx(1.0)
    assert states[hw].probs[0] == pytest.approx(states[hw - 1].probs[1] / sum(states[hw - 1].probs[1:]))
    # never resurrected: not at later 4h closes of W3, and no W4/W5 built on (S, P1, P2)
    assert not any(has(s, w3) for s in states[hw:H["5"] + 1])
    assert [p.price for p in states[-1].pivots4h] == [p.price for p in STATES[-1].pivots4h]
    assert _check_probs_and_s_rev(states, P) > 0


def test_upper_wick_through_w5_cap_kills_count() -> None:
    # short W3 impulse: S=1000 P1=1100 P2=1050 P3=1140 (W3=90 < W1=100) P4=1110 -> W5 cap = 1110 + 90 = 1200
    way = WAY[:7] + [1000.0, 1100.0, 1050.0, 1140.0, 1110.0, 1190.0, 1120.0]
    bars, hours = _linear(way)
    h5 = hours[11] + 40                                  # in W5 (leg up from P4 = 1110)
    h5 = _first_non_4h_hour(h5)
    pts = (1000.0, 1100.0, 1050.0, 1140.0, 1110.0)
    ok = _run(bars)
    w5 = next(c for c in ok[h5].counts if c.prices == pts)
    assert (w5.phase, w5.inval) == ("W5", Invalidation(1110.0, 1200.0))
    # without the spike the count lives until P5 (every high stays below the cap)
    assert bars[h5].high < 1200.0 and all(any(c.prices == pts for c in s.counts) for s in ok[h5 - 1:hours[12] + 1])
    bars[h5] = dataclasses.replace(bars[h5], high=1200.5)      # one 1h spike above the H3 cap
    hit = _run(bars)
    assert any(c.prices == pts for c in hit[h5 - 1].counts)
    assert not any(c.prices == pts for s in hit[h5:] for c in s.counts)
    assert hit[h5].E_live == 1200.5 and hit[h5].E_live_ts == bars[h5].ts


def test_wick_before_a_count_is_enumerable_still_kills_it() -> None:
    # S=1000 P1=1100 P2=1045, a small 1080/1050 swing, then up to 1200 and back to 1150. (S, P1, P2) as W3
    # skips 1080/1050, so it is only enumerable once E >= 1080. A 1h wick to 1040 (< P2) before that must
    # kill it when it first shows up, and every W4/W5 built on it.
    way = WAY[:7] + [1000.0, 1100.0, 1045.0, 1080.0, 1050.0, 1200.0, 1150.0]
    bars, hours = _linear(way)
    w3 = (1000.0, 1100.0, 1045.0)
    has = lambda s: any(c.pattern == "IMP" and c.d == 1 and c.prices[:3] == w3 for c in s.counts)  # noqa: E731
    ok = _run(bars)
    up = next(k for k in range(hours[11], len(ok)) if ok[k].pivots4h[-1].price == 1050.0)
    hw = _first_non_4h_hour(up + 1)                       # leg up from 1050, E still below 1080
    assert ok[hw].E_live < 1080.0 and not has(ok[hw])
    appear = next(k for k in range(hw, len(ok)) if has(ok[k]))
    assert ok[appear].closed_4h and ok[appear].top.prices == w3
    assert ok[-1].top.prices == w3 + (1200.0,) and ok[-1].top.phase == "W4"
    bars[hw] = dataclasses.replace(bars[hw], low=1040.0)  # wick 5 below P2, close unchanged
    states = _run(bars)
    assert [p.price for p in states[-1].pivots4h] == [p.price for p in ok[-1].pivots4h]
    assert not any(has(s) for s in states[hw:])
    assert _check_probs_and_s_rev(states, P) > 0


REF = 2000.0                                              # mirror p' = 2*REF - p


def _mirror(bars: list[Bar]) -> list[Bar]:
    return [Bar(b.ts, 2 * REF - b.open, 2 * REF - b.low, 2 * REF - b.high, 2 * REF - b.close, b.volume)
            for b in bars]


def test_mirrored_leg_down_tracks_lows_and_dies_on_an_upper_wick() -> None:
    m = lambda *xs: tuple(2 * REF - x for x in xs)  # noqa: E731
    bars = _mirror(BARS)
    states = _run(bars)
    assert [p.price for p in states[-1].pivots4h] == list(m(*WAY[:-1]))
    h = H["S"] + 40                                       # mid W1, now a leg down
    while not states[h].closed_4h:
        h += 1
    for k in range(h + 1, h + 4):
        s = states[k]
        assert not s.closed_4h and s.leg_dir == -1
        assert s.E_live == bars[k].low and s.E_live_ts == bars[k].ts
    t3 = states[H["3"]].top
    assert (t3.pattern, t3.d, t3.phase, t3.prices) == ("IMP", -1, "W3", m(1000.0, 1100.0, 1045.0))
    hw = _first_non_4h_hour(H["2"] + 29)
    w3 = m(1000.0, 1100.0, 1045.0)
    assert t3.inval == Invalidation(None, w3[2])
    bars[hw] = dataclasses.replace(bars[hw], high=w3[2] + 2.0)       # wick 2 above P2
    hit = _run(bars)
    has = lambda s: any(c.pattern == "IMP" and c.d == -1 and c.prices[:3] == w3 for c in s.counts)  # noqa: E731
    assert has(hit[hw - 1]) and not any(has(s) for s in hit[hw:H["5"] + 1])
    assert _check_probs_and_s_rev(hit, P) > 0


def test_4h_close_inputs_are_validated() -> None:
    b = BARS[:4]
    agg = Bar(b[0].ts, b[0].open, max(x.high for x in b), min(x.low for x in b), b[3].close, 4.0)
    eng = WaveEngine(P)
    with pytest.raises(ValueError):                       # sub_bars must end with the closing 1h bar
        eng.on_1h_close(b[3], agg, b[:3])
    with pytest.raises(ValueError):                       # bar1h does not close a 4h bucket
        eng.on_1h_close(b[2], agg, b[:3])
    with pytest.raises(ValueError):                       # bar4h is not the bucket bar1h closes
        eng.on_1h_close(b[3], dataclasses.replace(agg, ts=b[0].ts + H4), b)
    assert eng.on_1h_close(b[3], agg, b).i4h == 0          # rejected calls left no trace
