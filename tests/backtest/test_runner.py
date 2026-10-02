from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest import runner
from ewbot.backtest.benchmarks import RandomEntryStrategy
from ewbot.backtest.runner import breakdowns, compare, utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.config import BotConfig
from ewbot.engine.loop import EngineResult, EquityPoint

T0 = utc_ts("2020-01-01")
H = 3600
N = 1000


def _df(t0: int = T0) -> pd.DataFrame:
    px = [100.0 + 10.0 * ((i // 50) % 4) - 0.01 * i for i in range(N)]  # saw-tooth, positive
    return pd.DataFrame(
        {
            "ts": [t0 + i * H for i in range(N)],
            "open": px,
            "high": [p + 1 for p in px],
            "low": [p - 1 for p in px],
            "close": px,
            "volume": [1.0] * N,
            "synthetic": [False] * N,
        }
    )


class FakeEngine:
    """Replaces TradingEngine: returns canned results per strategy name, records RANDOM plans."""

    random_strategies: list = []
    trade_factory = None
    runs: list = []

    def __init__(self, cfg, strategy):
        self.strategy = strategy

    def run(self, df1h, start_ts=None, end_ts=None):
        name = self.strategy.name
        FakeEngine.runs.append((name, start_ts, end_ts))
        mk = FakeEngine.trade_factory
        if name == "A":
            trades = (
                mk(entry_ts=T0 + 100 * H, exit_ts=T0 + 110 * H, pnl=300.0, r=2.0, setup="W3"),
                mk(entry_ts=T0 + 200 * H, exit_ts=T0 + 210 * H, pnl=-100.0, r=-1.0, setup="W5", direction=-1),
                mk(entry_ts=T0 + 300 * H, exit_ts=T0 + 310 * H, pnl=200.0, r=1.5, setup="W3"),
            )
            vals = [10_000 + 2 * i for i in range(N)]
        elif name == "RANDOM":
            FakeEngine.random_strategies.append(self.strategy)
            trades = ()
            k = self.strategy.seed + 1
            vals = [10_000 + k * (-1) ** (i // 24) * (i % 24) for i in range(N)]
        else:
            trades = ()
            vals = [10_000 + i for i in range(N)]
        eq = tuple(EquityPoint(ts=T0 + i * H, equity=float(v)) for i, v in enumerate(vals))
        return EngineResult(trades=trades, equity=eq, signals=(), events=())


@pytest.fixture
def patched(monkeypatch, mk_trade):
    from types import SimpleNamespace

    FakeEngine.random_strategies = []
    FakeEngine.runs = []
    FakeEngine.trade_factory = staticmethod(mk_trade)
    monkeypatch.setattr(runner, "TradingEngine", FakeEngine)
    monkeypatch.setattr(runner, "make_strategy", lambda sp: SimpleNamespace(name=sp.mode))
    return FakeEngine


def test_utc_ts():
    assert utc_ts("2017-01-01") == 1_483_228_800
    assert utc_ts("2024-01-01") == 1_704_067_200


def test_run_backtest_uses_mode_strategy(patched):
    from ewbot.config import with_overrides

    cfg = with_overrides(BotConfig(), **{"strategy.mode": "C"})
    res = runner.run_backtest(cfg, _df(), T0, T0 + N * H)
    assert len(res.equity) == N and res.trades == ()


def test_compare_structure_and_random(patched):
    df = _df()
    cmp = compare(BotConfig(), df, T0, T0 + N * H, random_runs=3, workers=1)
    assert set(cmp["comparison"]) == {"A", "C", "HYBRID", "BH", "EMA"}
    assert set(cmp["equity"]) == set(cmp["comparison"])
    a = cmp["comparison"]["A"]
    assert a["n_trades"] == 3 and a["win_rate"] == pytest.approx(2 / 3)
    assert cmp["comparison"]["BH"]["exposure"] == 1.0
    assert cmp["comparison"]["BH"]["n_trades"] == 1
    assert cmp["ci"]["A"]["avg_r"][0] <= (2.0 - 1.0 + 1.5) / 3 <= cmp["ci"]["A"]["avg_r"][1]
    assert len(cmp["equity"]["BH"]) == N

    rnd = cmp["random"]
    assert rnd["runs"] == 3 and rnd["n_signals"] == 3
    d = rnd["dist"]["sharpe"]
    assert d["p05"] <= d["p50"] <= d["p95"]
    assert set(rnd["beaten_by"]) == {"A", "C", "HYBRID"}
    assert all(0.0 <= v <= 1.0 for v in rnd["beaten_by"]["A"].values())
    # RANDOM strategies: seeds cfg.seed + k, same count as A's trades, 3 planned entries each
    seeds = sorted(s.seed for s in patched.random_strategies)
    assert seeds == [0, 1, 2]
    assert all(isinstance(s, RandomEntryStrategy) and len(s.planned_times) == 3 for s in patched.random_strategies)

    bd = cmp["breakdown"]["A"]
    assert set(bd["per_setup"]) == {"W3", "W5"} and bd["per_setup"]["W3"]["n"] == 2
    assert bd["long_short"]["long"]["n"] == 2 and bd["long_short"]["short"]["n"] == 1
    assert list(bd["per_year"]) == ["2020"]


def test_compare_without_random_runs(patched):
    cmp = compare(BotConfig(), _df(), T0, T0 + N * H, random_runs=0)
    assert cmp["random"]["runs"] == 0 and cmp["random"]["dist"] == {}


def test_breakdowns_per_year_boundaries(mk_trade):
    t1 = mk_trade(entry_ts=utc_ts("2020-12-31"), exit_ts=utc_ts("2020-12-31") + H, pnl=1.0)
    t2 = mk_trade(entry_ts=utc_ts("2021-01-02"), exit_ts=utc_ts("2021-01-02") + H, pnl=-1.0)
    eq = tuple(
        EquityPoint(ts=utc_ts("2020-12-30") + i * 86400, equity=10_000.0 + i) for i in range(6)
    )
    res = EngineResult(trades=(t1, t2), equity=eq, signals=(), events=())
    bd = breakdowns(res, utc_ts("2020-12-30"), utc_ts("2021-01-05"))
    assert list(bd["per_year"]) == ["2020", "2021"]
    assert bd["per_year"]["2020"]["n_trades"] == 1 and bd["per_year"]["2021"]["n_trades"] == 1
    assert bd["per_year"]["2021"]["total_return"] > 0


def test_run_backtest_refuses_the_holdout_unless_allowed(patched):
    cfg = BotConfig()
    with pytest.raises(ValueError, match="holdout"):
        runner.run_backtest(cfg, _df(), T0, HOLDOUT_START + H)  # end after the walk-forward boundary
    spanning = _df(t0=HOLDOUT_START - 500 * H)  # 500 bars before 2024-01-01, 500 at/after it
    with pytest.raises(ValueError, match="holdout"):
        runner.run_backtest(cfg, spanning)  # no end_ts: the engine would run into the holdout bars
    assert patched.runs == []  # refused before the engine ran
    runner.run_backtest(cfg, spanning, None, HOLDOUT_START)  # ends exactly at the boundary: fine
    runner.run_backtest(cfg, spanning, None, None, allow_holdout=True)  # holdout.run_holdout only
    assert [r[2] for r in patched.runs] == [HOLDOUT_START, None]


def test_compare_refuses_the_holdout_unless_allowed(patched):
    end = utc_ts("2025-01-01")
    with pytest.raises(ValueError, match="holdout"):
        compare(BotConfig(), _df(), T0, end, random_runs=1)
    assert patched.runs == []  # nothing ran: not A/C/HYBRID, not the random control
    cmp = compare(BotConfig(), _df(), T0, end, random_runs=1, allow_holdout=True)
    assert cmp["period"] == {"start": T0, "end": end}
    assert {r[0] for r in patched.runs} == {"A", "C", "HYBRID", "RANDOM"}
    compare(BotConfig(), _df(), T0, HOLDOUT_START, random_runs=0)  # up to the boundary: fine
