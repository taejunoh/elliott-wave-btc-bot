from __future__ import annotations

import math

import pytest

from ewbot.config import WaveParams
from ewbot.waves.scoring import f_ip, f_pt, f_zone, fit_q, is_done, progress_x, softmax
from ewbot.waves.zigzag import Pivot

SIG = 0.12
P = WaveParams()


def test_f_pt() -> None:
    assert f_pt(1.618, (1.618, 2.618), SIG) == 1.0
    assert f_pt(2.0, (1.618, 2.618), SIG) == pytest.approx(0.2101543176, rel=1e-9)   # nearer 1.618
    assert f_pt(2.1, (1.618, 2.618), SIG) == pytest.approx(0.1849269023, rel=1e-9)   # nearer 2.618
    assert f_pt(0.0, (1.0,), SIG) == 0.0


def test_f_zone() -> None:
    assert f_zone(0.55, 0.5, 0.618, SIG) == 1.0
    assert f_zone(0.5, 0.5, 0.618, SIG) == 1.0
    assert f_zone(0.7, 0.5, 0.618, SIG) == pytest.approx(0.5833325892, rel=1e-9)   # vs 0.618
    assert f_zone(0.45, 0.5, 0.618, SIG) == pytest.approx(0.6801473773, rel=1e-9)  # vs 0.5


def test_f_ip_penalises_only_overshoot() -> None:
    assert f_ip(0.1, 2.618, SIG) == 1.0
    assert f_ip(2.618, 2.618, SIG) == 1.0
    assert f_ip(3.0, 2.618, SIG) == pytest.approx(0.5251203347, rel=1e-9)


def test_softmax() -> None:
    p = softmax([0.80, 0.75], 0.05)
    assert p[0] == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
    assert sum(p) == pytest.approx(1.0)
    assert softmax([], 0.05) == []
    assert softmax([10.0, 10.0, 10.0], 0.05) == pytest.approx([1 / 3] * 3)


# Fib-perfect bull impulse after a falling lead-in:
# S=1000 P1=1100 (W1=100) P2=1045 (r2=.55) P3=1206.8 (W3=1.618 W1) P4=1158.26 (r4=.3) P5=1258.26 (W5=W1)
LEAD = [(0, 1400.0, 1), (4, 1250.0, -1), (8, 1350.0, 1), (12, 1150.0, -1), (16, 1250.0, 1),
        (20, 1080.0, -1), (24, 1095.0, 1)]
IMP = [(28, 1000.0, -1), (32, 1100.0, 1), (36, 1045.0, -1), (44, 1206.8, 1), (48, 1158.26, -1)]


def _ctx(rows: list[tuple[int, float, int]]) -> list[Pivot]:
    return [Pivot(i, 1_700_006_400 + i * 14_400, p, k, i + 2) for i, p, k in rows]


CTX = _ctx(LEAD + IMP)
PTS = tuple(p for _, p, _ in IMP)


@pytest.mark.parametrize(
    ("phase", "E", "q"),
    [
        ("W2", 1045.0, 2.0 / 2.5),              # g0=1, ip r2=.55<=.786 (w .5)
        ("W3", 1206.8, 3.25 / 3.75),            # g0, r2 zone, ip W3/W1=1.618<=2.618 (w .75)
        ("W4", 1158.26, 4.5 / 5.0),             # + W3 at 1.618 (w 1.5), ip r4=.3<=.5
        ("W5", 1258.26, 6.0 / 6.5),             # + r4 zone, alt=min(1,.25/.2)=1, ip W5/W1=1<=1.618
    ],
)
def test_fit_q_fib_perfect_impulse(phase: str, E: float, q: float) -> None:
    n = {"W2": 2, "W3": 3, "W4": 4, "W5": 5}[phase]
    assert fit_q("IMP", 1, phase, PTS[:n], E, CTX, P) == pytest.approx(q, abs=1e-9)


def test_fit_q_in_progress_overshoot_is_penalised_at_half_weight() -> None:
    # W3/W1 = 3.0 > 2.618: s = 0.5251203347, weight 0.75
    q = fit_q("IMP", 1, "W3", PTS[:3], 1345.0, CTX, P)
    assert q == pytest.approx((2.5 + 0.75 * 0.5251203347) / 3.75, rel=1e-9)


def test_g_start_half_when_s_is_not_the_lowest_recent_low() -> None:
    ctx = _ctx(LEAD[:5] + [(20, 990.0, -1), (24, 1095.0, 1)] + IMP)   # a lower low (990) before S
    assert fit_q("IMP", 1, "W2", PTS[:2], 1045.0, ctx, P) == pytest.approx((0.5 + 0.5 + 0.5) / 2.5)


def test_g_start_zero_without_history() -> None:
    ctx = _ctx(IMP)
    assert fit_q("IMP", 1, "W2", PTS[:2], 1045.0, ctx, P) == pytest.approx((0.0 + 0.5 + 0.5) / 2.5)


def test_fit_q_abc_after_the_impulse() -> None:
    # X = P5 = 1258.26 (above the previous 4 pivots), A -> 1158.26 (|A|=100), B -> 1220.06 (.618), C/A = 1
    ctx = _ctx(LEAD + IMP + [(60, 1258.26, 1), (66, 1158.26, -1), (70, 1220.06, 1)])
    assert fit_q("ABC", -1, "B", (1258.26, 1158.26), 1220.06, ctx, P) == pytest.approx(2.0 / 2.5)
    assert fit_q("ABC", -1, "C", (1258.26, 1158.26, 1220.06), 1120.06, ctx, P) == pytest.approx(3.25 / 3.75)


def test_progress_x_and_done_impulse() -> None:
    assert progress_x("IMP", 1, "W2", (1000.0, 1100.0), 1065.0) == pytest.approx(0.35)
    assert not is_done("IMP", 1, "W2", (1000.0, 1100.0), 1065.0)
    assert is_done("IMP", 1, "W2", (1000.0, 1100.0), 1060.0)                  # r2 = .40 >= .382
    assert not is_done("IMP", 1, "W3", PTS[:3], 1140.0)                       # W3/W1 = .95
    assert is_done("IMP", 1, "W3", PTS[:3], 1150.0)                           # 1.05 >= 1
    assert progress_x("IMP", 1, "W4", PTS[:4], 1174.44) == pytest.approx(0.2)
    assert not is_done("IMP", 1, "W4", PTS[:4], 1174.44)
    assert is_done("IMP", 1, "W4", PTS[:4], 1166.35)                          # r4 = .25 >= .236


def test_w5_done_needs_ratio_and_e_beyond_p3() -> None:
    pts = (100.0, 120.0, 105.0, 150.0, 125.0)     # W1 = 20, deep W4 (r4 = .556)
    assert progress_x("IMP", 1, "W5", pts, 140.0) == pytest.approx(0.75)
    assert not is_done("IMP", 1, "W5", pts, 140.0)    # ratio ok, but 140 < P3 = 150
    assert is_done("IMP", 1, "W5", pts, 151.0)


def test_abc_done() -> None:
    assert is_done("ABC", -1, "B", (200.0, 150.0), 170.0)          # B/A = .4
    assert not is_done("ABC", -1, "B", (200.0, 150.0), 165.0)      # .3
    assert is_done("ABC", -1, "C", (200.0, 150.0, 180.0), 140.0)   # C/A = .8, below A_end
    assert not is_done("ABC", -1, "C", (200.0, 150.0, 180.0), 145.0)   # .7
    assert not is_done("ABC", -1, "C", (200.0, 150.0, 195.0), 152.0)   # .86 but above A_end 150


def test_scores_are_mirror_symmetric() -> None:
    mctx = [Pivot(p.i, p.sub_ts, 3000.0 - p.price, -p.kind, p.confirmed_at) for p in CTX]
    q_up = fit_q("IMP", 1, "W5", PTS, 1250.0, CTX, P)
    q_dn = fit_q("IMP", -1, "W5", tuple(3000.0 - x for x in PTS), 1750.0, mctx, P)
    assert q_up == pytest.approx(q_dn, abs=1e-12)
