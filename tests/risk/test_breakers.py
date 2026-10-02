from __future__ import annotations

from ewbot.config import BreakerParams
from ewbot.risk.breakers import CircuitBreakers

P = BreakerParams()  # daily 3%, total 15%, 5 losses -> 24h
H = 3600

NY_MIDNIGHT_MAR10 = 1_710_046_800   # 2024-03-10 00:00 EST (05:00 UTC); DST starts 02:00 that day
NY_MIDNIGHT_MAR11 = 1_710_129_600   # 2024-03-11 00:00 EDT (04:00 UTC)
NY_MIDNIGHT_NOV03 = 1_730_606_400   # 2024-11-03 00:00 EDT (04:00 UTC); DST ends 02:00 that day
NY_MIDNIGHT_NOV04 = 1_730_696_400   # 2024-11-04 00:00 EST (05:00 UTC)


def test_daily_halt_triggers_at_three_percent_and_keeps_others_open() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    assert cb.on_equity(NY_MIDNIGHT_MAR10, 10_000.0) == []
    assert cb.on_equity(NY_MIDNIGHT_MAR10 + 3 * H, 9_710.0) == []       # -2.9%
    assert cb.can_enter(NY_MIDNIGHT_MAR10 + 3 * H)
    assert cb.on_equity(NY_MIDNIGHT_MAR10 + 10 * H, 9_690.0) == ["DAILY_HALT"]  # -3.1%
    assert not cb.can_enter(NY_MIDNIGHT_MAR10 + 10 * H)
    assert cb.on_equity(NY_MIDNIGHT_MAR10 + 11 * H, 9_600.0) == []      # emitted once per day
    assert not cb.halted


def test_daily_halt_resets_at_ny_midnight_across_spring_dst() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    cb.on_equity(NY_MIDNIGHT_MAR10, 10_000.0)
    cb.on_equity(NY_MIDNIGHT_MAR10 + 10 * H, 9_690.0)
    # 23 wall-clock hours later it is 2024-03-10 23:00 EDT (03:00 UTC): still the halted day
    assert not cb.can_enter(NY_MIDNIGHT_MAR11 - H)
    # 2024-03-11 00:00 EDT = 04:00 UTC (a fixed UTC-5 offset would wrongly say 23:00)
    assert cb.can_enter(NY_MIDNIGHT_MAR11)
    # the first observation of the new day becomes the new reference: no halt at 9_690
    assert cb.on_equity(NY_MIDNIGHT_MAR11, 9_690.0) == []
    assert cb.can_enter(NY_MIDNIGHT_MAR11 + H)


def test_daily_halt_resets_at_ny_midnight_across_fall_dst() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    cb.on_equity(NY_MIDNIGHT_NOV03, 10_000.0)
    assert cb.on_equity(NY_MIDNIGHT_NOV03 + 20 * H, 9_650.0) == ["DAILY_HALT"]
    # 2024-11-03 is 25 hours long: 04:59:59 UTC on Nov 4 is still 23:59:59 EST on Nov 3
    assert not cb.can_enter(NY_MIDNIGHT_NOV04 - 1)
    assert cb.can_enter(NY_MIDNIGHT_NOV04)


def test_total_drawdown_latches() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    t = NY_MIDNIGHT_MAR10
    assert cb.on_equity(t, 12_000.0) == []                  # new peak 12_000
    assert cb.on_equity(t + 30 * H, 10_250.0) == []         # -14.6% from peak (new NY day)
    assert "TOTAL_HALT" in cb.on_equity(t + 31 * H, 10_150.0)  # -15.4% from peak
    assert cb.halted
    assert not cb.can_enter(t + 32 * H)
    assert cb.on_equity(t + 80 * H, 13_000.0) == []         # recovery does not un-latch
    assert cb.halted and not cb.can_enter(t + 80 * H)


def test_five_consecutive_losses_pause_24h() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    t = 1_704_067_200
    for k in range(4):
        assert cb.on_trade_closed(t + k * H, -10.0) == []
    assert cb.on_trade_closed(t + 4 * H, -10.0) == ["CONSEC_PAUSE"]
    assert not cb.can_enter(t + 4 * H)
    assert not cb.can_enter(t + 28 * H - 1)
    assert cb.can_enter(t + 28 * H)
    # streak restarted: one more loss does not pause again
    assert cb.on_trade_closed(t + 29 * H, -10.0) == []
    assert cb.can_enter(t + 29 * H)


def test_win_resets_loss_streak() -> None:
    cb = CircuitBreakers(P, 10_000.0)
    t = 1_704_067_200
    for k in range(4):
        cb.on_trade_closed(t + k * H, -10.0)
    assert cb.on_trade_closed(t + 4 * H, 5.0) == []
    assert cb.on_trade_closed(t + 5 * H, -10.0) == []
    assert cb.can_enter(t + 5 * H)


def test_disabled_breakers_are_a_no_op() -> None:
    cb = CircuitBreakers(BreakerParams(enabled=False), 10_000.0)
    t = NY_MIDNIGHT_MAR10
    assert cb.on_equity(t, 10_000.0) == []
    assert cb.on_equity(t + H, 5_000.0) == []  # -50%: neither DAILY_HALT nor TOTAL_HALT
    for k in range(10):
        assert cb.on_trade_closed(t + (2 + k) * H, -10.0) == []  # no CONSEC_PAUSE
    assert not cb.halted
    assert cb.can_enter(t + 12 * H)
