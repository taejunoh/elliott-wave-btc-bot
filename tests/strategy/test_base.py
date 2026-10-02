from __future__ import annotations

import dataclasses
import math

import pytest

from ewbot.config import StrategyParams
from ewbot.strategy.base import Signal, Strategy, _finalize, blended_r, time_stop_bars


def test_blended_r_long_and_short_are_symmetric() -> None:
    assert blended_r(1, 100.0, 90.0, 110.0, 130.0, 0.5) == pytest.approx(2.0)
    assert blended_r(-1, 100.0, 110.0, 90.0, 70.0, 0.5) == pytest.approx(2.0)


def test_blended_r_weights_and_missing_tp2() -> None:
    assert blended_r(1, 100.0, 90.0, 110.0, 130.0, 0.25) == pytest.approx(2.5)
    assert blended_r(1, 100.0, 90.0, 110.0, None, 0.5) == pytest.approx(1.0)


def test_blended_r_wrong_side_stop_is_minus_inf() -> None:
    assert blended_r(1, 100.0, 100.0, 110.0, 130.0, 0.5) == -math.inf
    assert blended_r(-1, 100.0, 95.0, 90.0, 70.0, 0.5) == -math.inf
    with pytest.raises(ValueError):
        blended_r(0, 100.0, 90.0, 110.0, 130.0, 0.5)


@pytest.mark.parametrize(
    ("leg", "expected"),
    [(10, 15), (9, 14), (7, 12), (3, 12), (0, 12), (-4, 12), (40, 60), (50, 60), (39, 59)],
)
def test_time_stop_bars_default_clip(leg: int, expected: int) -> None:
    assert time_stop_bars(leg, StrategyParams()) == expected


def test_time_stop_bars_custom_params_round_half_up() -> None:
    p = StrategyParams(time_stop_mult=1.5, time_stop_min_4h=1, time_stop_max_4h=100)
    assert time_stop_bars(7, p) == 11  # 10.5 -> 11 (half up, not banker's 10)
    assert time_stop_bars(1, p) == 2  # 1.5 -> 2


def _fin(**kw: object) -> Signal | None:
    base: dict[str, object] = dict(
        ts=0, setup="W3", direction=1, entry_ref=100.0, stop=90.0, tp1=110.0, tp2=130.0,
        risk_mult=1.0, time_stop_4h=15, instance=("IMP", 1, 0, "W2"), lineage=("IMP", 1, 0),
        reason="x", sp=StrategyParams(),
    )
    base.update(kw)
    return _finalize(**base)  # type: ignore[arg-type]


def test_finalize_builds_signal_and_appends_r() -> None:
    sig = _fin()
    assert sig is not None
    assert (sig.stop, sig.tp1, sig.tp2, sig.entry_ref) == (90.0, 110.0, 130.0, 100.0)
    assert sig.reason == "x R=2.00"
    with pytest.raises(dataclasses.FrozenInstanceError):
        sig.stop = 1.0  # type: ignore[misc]


def test_finalize_orders_targets_near_to_far() -> None:
    sig = _fin(direction=-1, entry_ref=100.0, stop=110.0, tp1=60.0, tp2=80.0)
    assert sig is not None and (sig.tp1, sig.tp2) == (80.0, 60.0)


def test_finalize_rejects_bad_sides_and_applies_r_gate_inclusively() -> None:
    assert _fin(stop=101.0) is None  # stop above a long entry
    assert _fin(tp1=99.0, tp2=99.5) is None  # TP1 already behind entry
    # R exactly 1.5 passes (>=): R1 = 1.0, R2 = 2.0
    assert _fin(tp1=110.0, tp2=120.0) is not None
    assert _fin(tp1=110.0, tp2=119.0) is None  # R = 1.45


def test_strategy_protocol_is_runtime_checkable() -> None:
    class Dummy:
        name = "D"

        def on_bar(self, bar, ws, choch):  # type: ignore[no-untyped-def]
            return None

    assert isinstance(Dummy(), Strategy)
    assert not isinstance(object(), Strategy)
