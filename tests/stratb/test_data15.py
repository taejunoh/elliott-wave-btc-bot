from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest
import requests

from ewbot.data import coinbase_history as ch
from ewbot.stratb import data15 as d15
from ewbot.stratb.data15 import (
    LOCK_KIND,
    REPLAY_ANCHOR,
    START_TS,
    HoldoutGuardError,
    cache_path,
    cache_paths,
    fetch_15m,
    gap_report,
    load_15m,
    lock_exists,
    resample_1h,
    update_cache_15m,
    update_caches_15m,
)

HS = 1_704_067_200  # 2024-01-01 00:00 UTC
M15 = 900


def _row(ts: int) -> list[float]:
    base = 40_000.0 + (ts - HS) / M15
    return [ts, base - 5.0, base + 10.0, base, base + 1.0, 1.5]  # [time, low, high, open, close, volume]


class FakeSession:
    """Honours start/end (inclusive), at most 300 rows, newest first; records every request. Raises the
    queued exceptions first (one per request), and every request from fail_from on fails."""

    def __init__(self, first_ts: int, last_ts: int, errors=(), fail_from: int | None = None):
        self.first, self.last = first_ts, last_ts
        self.errors, self.fail_from = list(errors), fail_from
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, **params})
        if self.errors:
            raise self.errors.pop(0)
        if self.fail_from is not None and len(self.calls) > self.fail_from:
            raise requests.ConnectionError("coinbase down")
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
    assert {a: p.name for a, p in cache_paths("data/cache").items()} == {
        "BTC": "BTC-USD_15m.parquet", "ETH": "ETH-USD_15m.parquet"}
    assert d15.DEFAULT_LOCK_PATH.name == "holdout-b.lock" and d15.PRODUCTS == {"BTC": "BTC-USD", "ETH": "ETH-USD"}


def test_fetch_pages_300_bars_at_granularity_900():
    t0 = HS - 1000 * M15
    sess = FakeSession(t0, HS)
    df = fetch_15m("BTC-USD", t0, t0 + 700 * M15, session=sess, sleep_s=0)
    assert len(sess.calls) == 3 and sess.calls[0]["url"].endswith("/products/BTC-USD/candles")
    assert df["ts"].tolist() == [t0 + k * M15 for k in range(700)]
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"] and str(df["ts"].dtype) == "int64"
    assert df.loc[0, "open"] == _row(t0)[3] and df.loc[0, "low"] == _row(t0)[1]


def test_fetch_refuses_a_2024_window_without_the_lock(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 50 * M15)
    with pytest.raises(HoldoutGuardError):
        fetch_15m("ETH-USD", HS - 10 * M15, HS + M15, session=sess, sleep_s=0, lock_path=tmp_path / "none.lock")
    assert sess.calls == []
    df = fetch_15m("ETH-USD", HS - 10 * M15, HS + M15, session=sess, sleep_s=0, lock_path=_lock(tmp_path / "l.lock"))
    assert int(df["ts"].max()) == HS


def test_connection_errors_and_timeouts_are_retried_with_backoff(monkeypatch):
    waits = []
    monkeypatch.setattr(d15.time, "sleep", waits.append)
    t0 = HS - 400 * M15
    sess = FakeSession(t0, HS, errors=[requests.ConnectionError("reset"), requests.Timeout("slow")])
    assert len(fetch_15m("BTC-USD", t0, t0 + 10 * M15, session=sess, sleep_s=0)) == 10
    assert waits == [1.0, 2.0] and len(sess.calls) == 3
    sess = FakeSession(t0, HS, errors=[requests.ConnectionError("x")] * 3)
    with pytest.raises(requests.ConnectionError):
        fetch_15m("BTC-USD", t0, t0 + 10 * M15, session=sess, sleep_s=0)


def test_an_interrupted_download_keeps_its_pages_and_resumes(tmp_path):
    t0 = HS - 1000 * M15
    none = tmp_path / "none.lock"
    with pytest.raises(requests.ConnectionError):
        update_cache_15m(tmp_path, product="BTC-USD", lock_path=none, start_ts=t0, now_ts=HS,
                         session=FakeSession(t0, HS, fail_from=2), sleep_s=0)
    assert len(pd.read_parquet(cache_path(tmp_path, "BTC-USD"))) == 600
    sess = FakeSession(t0, HS)
    path = update_cache_15m(tmp_path, product="BTC-USD", lock_path=none, now_ts=HS, session=sess, sleep_s=0)
    df = pd.read_parquet(path)
    assert sess.calls[0]["start"] == datetime.fromtimestamp(t0 + 600 * M15, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert df["ts"].tolist() == [t0 + k * M15 for k in range(1000)]


def test_without_a_lock_the_caches_stop_before_2024(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    paths = update_caches_15m(tmp_path, lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15,
                              now_ts=HS + 400 * M15, session=sess, sleep_s=0)
    assert list(paths) == ["BTC", "ETH"] and [c["url"].split("/")[-2] for c in sess.calls] == ["BTC-USD", "ETH-USD"]
    for p in paths.values():
        df = pd.read_parquet(p)
        assert int(df["ts"].max()) == HS - M15 and len(df) == 50
    assert all(c["end"] < "2024-01-01" for c in sess.calls)


def test_with_a_lock_the_cache_extends_to_the_last_closed_bar(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    lock = _lock(tmp_path / "holdout-b.lock")
    update_cache_15m(tmp_path, product="BTC-USD", lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15,
                     now_ts=HS + 400 * M15, session=sess, sleep_s=0)
    path = update_cache_15m(tmp_path, product="BTC-USD", lock_path=lock, now_ts=HS + 10 * M15 + 899, session=sess,
                            sleep_s=0)
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
