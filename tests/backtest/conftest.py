from __future__ import annotations

import pytest

from ewbot.engine.loop import EquityPoint, Trade


def make_trade(
    entry_ts: int = 0,
    exit_ts: int = 3600,
    pnl: float = 10.0,
    r: float = 1.0,
    setup: str = "W3",
    direction: int = 1,
    snapshot: dict | None = None,
    entry: float = 100.0,
    stop0: float = 95.0,
    tp1: float = 105.0,
    tp2: float | None = 110.0,
    reason: str = "STOP",
) -> Trade:
    return Trade(
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        setup=setup,
        direction=direction,
        contracts=1,
        entry_price=entry,
        exit_price_avg=entry + pnl,
        stop0=stop0,
        tp1=tp1,
        tp2=tp2,
        pnl=pnl,
        fees=0.0,
        funding=0.0,
        r_multiple=r,
        exit_reason=reason,
        snapshot=snapshot if snapshot is not None else {},
    )


def make_equity(values, t0: int = 0, step: int = 3600) -> list[EquityPoint]:
    return [EquityPoint(ts=t0 + i * step, equity=float(v)) for i, v in enumerate(values)]


@pytest.fixture
def mk_trade():
    return make_trade


@pytest.fixture
def mk_equity():
    return make_equity
