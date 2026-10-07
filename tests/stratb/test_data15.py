from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from ewbot.data import coinbase_history as ch
from ewbot.stratb import data15 as d15
from ewbot.stratb.data15 import (
    LOCK_KIND,
    REPLAY_ANCHOR,
    START_TS,
    HoldoutGuardError,
    cache_path,
    fetch_15m,
    gap_report,
    load_15m,
    lock_exists,
    resample_1h,
    update_cache_15m,
)

HS = 1_704_067_200  # 2024-01-01 00:00 UTC
M15 = 900


def _row(ts: int) -> list[float]:
    base = 40_000.0 + (ts - HS) / M15
    return [ts, base - 5.0, base + 10.0, base, base + 1.0, 1.5]  # [time, low, high, open, close, volume]


class FakeSession:
    """Honours start/end (inclusive), at most 300 rows, newest first; records every request."""

    def __init__(self, first_ts: int, last_ts: int):
        self.first, self.last = first_ts, last_ts
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, **params})
        assert params["granularity"] == 900
        s = int(datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        e = int(datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        tss = [t for t in range(s, e + 1, M15) if self.first <= t <= self.last]
        assert len(tss) <= 300

        class R:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return [_row(t) for t in reversed(tss)]

        return R()


def _lock(path, kind=LOCK_KIND):
    path.write_text(json.dumps({"kind": kind, "sha256": "ab" * 32}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)


def test_constants():
    assert (START_TS, REPLAY_ANCHOR) == (1_483_228_800, 1_696_118_400)
    assert cache_path("data/cache").name == "BTC-USD_15m.parquet"
    assert d15.DEFAULT_LOCK_PATH.name == "holdout-b.lock"


def test_fetch_pages_300_bars_at_granularity_900():
    t0 = HS - 1000 * M15
    sess = FakeSession(t0, HS)
    df = fetch_15m("BTC-USD", t0, t0 + 700 * M15, session=sess, sleep_s=0)
    assert len(sess.calls) == 3 and sess.calls[0]["url"].endswith("/products/BTC-USD/candles")
    assert df["ts"].tolist() == [t0 + k * M15 for k in range(700)]
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"] and str(df["ts"].dtype) == "int64"
    assert df.loc[0, "open"] == _row(t0)[3] and df.loc[0, "low"] == _row(t0)[1]


def test_without_a_lock_the_cache_stops_before_2024(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    path = update_cache_15m(tmp_path, lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15,
                            now_ts=HS + 400 * M15, session=sess, sleep_s=0)
    df = pd.read_parquet(path)
    assert int(df["ts"].max()) == HS - M15 and len(df) == 50 and len(sess.calls) == 1
    assert all(c["end"] < "2024-01-01" for c in sess.calls)


def test_with_a_lock_the_cache_extends_to_the_last_closed_bar(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    lock = _lock(tmp_path / "holdout-b.lock")
    update_cache_15m(tmp_path, lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15, now_ts=HS + 400 * M15,
                     session=sess, sleep_s=0)
    path = update_cache_15m(tmp_path, lock_path=lock, now_ts=HS + 10 * M15 + 899, session=sess, sleep_s=0)
    df = pd.read_parquet(path)
    assert int(df["ts"].max()) == HS + 9 * M15 and df["ts"].is_unique and len(df) == 60
    assert sess.calls[-1]["start"] == "2024-01-01T00:00:00Z"


def test_load_never_returns_2024_bars_without_include_holdout(tmp_path):
    p = tmp_path / "c.parquet"
    pd.DataFrame([_row(t) for t in range(HS - 4 * M15, HS + 4 * M15, M15)],
                 columns=["ts", "low", "high", "open", "close", "volume"]).to_parquet(p, index=False)
    df = load_15m(p, lock_path=_lock(tmp_path / "l.lock"))
    assert int(df["ts"].max()) == HS - M15 and len(df) == 4
    with pytest.raises(HoldoutGuardError):
        load_15m(p, include_holdout=True, lock_path=tmp_path / "missing.lock")
    (tmp_path / "bogus.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(HoldoutGuardError):
        load_15m(p, include_holdout=True, lock_path=tmp_path / "bogus.lock")
    assert len(load_15m(p, include_holdout=True, lock_path=tmp_path / "l.lock")) == 8


def test_lock_exists_needs_a_strategy_b_lock(tmp_path):
    assert not lock_exists(tmp_path / "x")
    (tmp_path / "x").write_text("not json", encoding="utf-8")
    assert not lock_exists(tmp_path / "x")
    assert not lock_exists(_lock(tmp_path / "a", kind="other"))
    assert lock_exists(_lock(tmp_path / "b"))


def test_resample_keeps_complete_hours_only_and_flags_synthetic():
    rows = [[HS + k * M15, 10.0 + k, 20.0 + k, 5.0 + k, 11.0 + k, 1.0] for k in range(10)]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["synthetic"] = [False] * 5 + [True] + [False] * 4
    h = resample_1h(df)
    assert h["ts"].tolist() == [HS, HS + 3600]
    assert h.loc[0, ["open", "high", "low", "close", "volume"]].tolist() == [10.0, 23.0, 5.0, 14.0, 4.0]
    assert h["synthetic"].tolist() == [False, True]


def test_gap_report_lists_missing_15m_bars():
    df = pd.DataFrame({"ts": [HS, HS + M15, HS + 3 * M15], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                       "volume": 1.0})
    assert gap_report(df).missing_ts == (HS + 2 * M15,)
