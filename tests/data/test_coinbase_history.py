from datetime import UTC, datetime

import pandas as pd
import pytest

from ewbot.data import coinbase_history as ch
from ewbot.types import H1

T0 = 1_700_000_000 // H1 * H1  # 2023-11-14 22:00:00 UTC, hour-aligned


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.text = f"fake body {status_code}"

    def json(self):
        return self._payload


def _row(ts: int) -> list[float]:
    """Coinbase row layout: [time, low, high, open, close, volume]."""
    k = (ts - T0) // H1
    base = 30_000.0 + k
    return [ts, base - 5.0, base + 10.0, base, base + 1.0, 2.5]


class FakeSession:
    """Behaves like the exchange: honours start/end (inclusive), max 300, newest first."""

    def __init__(self, first_ts: int, last_ts: int):
        self.first, self.last = first_ts, last_ts
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params)})
        assert params["granularity"] == 3600
        s = int(datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        e = int(datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        tss = [t for t in range(s, e + 1, H1) if self.first <= t <= self.last]
        assert len(tss) <= 300, "request window larger than the API maximum"
        return FakeResponse(200, [_row(t) for t in reversed(tss)])


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)


def test_fetch_single_window_schema_and_order():
    sess = FakeSession(T0, T0 + 1000 * H1)
    df = ch.fetch_1h("BTC-USD", T0, T0 + 10 * H1, session=sess)
    assert len(sess.calls) == 1
    assert sess.calls[0]["url"] == "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert str(df["ts"].dtype) == "int64"
    assert all(str(df[c].dtype) == "float64" for c in ["open", "high", "low", "close", "volume"])
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(10)]  # ascending, end exclusive
    assert isinstance(df.index, pd.RangeIndex)
    first = df.iloc[0]
    # raw row is [ts, low, high, open, close, volume] = [T0, 29995, 30010, 30000, 30001, 2.5]
    assert (first["low"], first["high"], first["open"], first["close"], first["volume"]) == (
        29_995.0, 30_010.0, 30_000.0, 30_001.0, 2.5,
    )


def test_fetch_paginates_across_two_windows():
    n = 400  # > 300 -> needs two windows
    sess = FakeSession(T0, T0 + 10_000 * H1)
    df = ch.fetch_1h("BTC-USD", T0, T0 + n * H1, session=sess)
    assert len(sess.calls) == 2
    starts = [c["params"]["start"] for c in sess.calls]
    assert starts[0] == datetime.fromtimestamp(T0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert starts[1] == datetime.fromtimestamp(T0 + 300 * H1, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(df) == n
    assert df["ts"].is_unique and df["ts"].is_monotonic_increasing
    assert df["ts"].iloc[0] == T0 and df["ts"].iloc[-1] == T0 + (n - 1) * H1
    assert (df["ts"].diff().dropna() == H1).all()


def test_fetch_exact_300_is_one_window_and_301_is_two():
    sess = FakeSession(T0, T0 + 10_000 * H1)
    assert len(ch.fetch_1h("BTC-USD", T0, T0 + 300 * H1, session=sess)) == 300
    assert len(sess.calls) == 1
    sess2 = FakeSession(T0, T0 + 10_000 * H1)
    assert len(ch.fetch_1h("BTC-USD", T0, T0 + 301 * H1, session=sess2)) == 301
    assert len(sess2.calls) == 2


def test_fetch_dedupes_and_sorts_unordered_duplicates():
    class Dup:
        def get(self, url, params=None, timeout=None):
            rows = [_row(T0 + 2 * H1), _row(T0), _row(T0 + H1), _row(T0 + H1)]
            return FakeResponse(200, rows)

    df = ch.fetch_1h("BTC-USD", T0, T0 + 3 * H1, session=Dup())
    assert df["ts"].tolist() == [T0, T0 + H1, T0 + 2 * H1]


def test_fetch_filters_rows_outside_requested_range():
    class Wide:
        def get(self, url, params=None, timeout=None):
            return FakeResponse(200, [_row(T0 + i * H1) for i in range(-2, 8)])

    df = ch.fetch_1h("BTC-USD", T0, T0 + 5 * H1, session=Wide())
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(5)]


def test_fetch_empty_range_and_empty_response():
    sess = FakeSession(T0, T0 + 10 * H1)
    assert ch.fetch_1h("BTC-USD", T0 + 5 * H1, T0 + 5 * H1, session=sess).empty
    assert sess.calls == []  # nothing requested for an empty range
    none = FakeSession(T0 + 1000 * H1, T0 + 2000 * H1)  # no data in the requested window
    df = ch.fetch_1h("BTC-USD", T0, T0 + 10 * H1, session=none)
    assert df.empty and list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_retry_on_429_then_success(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(ch.time, "sleep", lambda s: sleeps.append(s))

    class Flaky:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            if self.n == 1:
                return FakeResponse(429)
            if self.n == 2:
                return FakeResponse(503)
            return FakeResponse(200, [_row(T0 + i * H1) for i in range(3)])

    s = Flaky()
    df = ch.fetch_1h("BTC-USD", T0, T0 + 3 * H1, session=s, sleep_s=0.0)
    assert s.n == 3  # two failures + one success = 3 tries
    assert len(df) == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff between the tries


def test_retry_gives_up_after_three_tries():
    class AlwaysLimited:
        n = 0

        def get(self, url, params=None, timeout=None):
            type(self).n += 1
            return FakeResponse(429)

    with pytest.raises(RuntimeError, match="after 3 tries"):
        ch.fetch_1h("BTC-USD", T0, T0 + 3 * H1, session=AlwaysLimited())
    assert AlwaysLimited.n == 3


def test_client_error_is_not_retried():
    class NotFound:
        n = 0

        def get(self, url, params=None, timeout=None):
            type(self).n += 1
            return FakeResponse(404)

    with pytest.raises(RuntimeError, match="HTTP 404"):
        ch.fetch_1h("NOPE-USD", T0, T0 + 3 * H1, session=NotFound())
    assert NotFound.n == 1


def test_update_cache_creates_then_extends_incrementally(tmp_path, monkeypatch):
    calls: list[tuple[str, int, int]] = []

    def fake_fetch(product, start_ts, end_ts, session=None, sleep_s=0.15):
        calls.append((product, start_ts, end_ts))
        return pd.DataFrame(
            {
                "ts": pd.Series(range(start_ts, end_ts, H1), dtype="int64"),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0,
            }
        )

    monkeypatch.setattr(ch, "fetch_1h", fake_fetch)
    monkeypatch.setattr(ch, "_now_ts", lambda: T0 + 5 * H1 + 1234)  # mid-hour: bar T0+5h still open

    path = ch.update_cache("BTC-USD", tmp_path, start_ts=T0)
    assert path == tmp_path / "BTC-USD_1h.parquet"
    assert calls == [("BTC-USD", T0, T0 + 5 * H1)]  # only closed hours: T0 .. T0+4h
    df = pd.read_parquet(path)
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(5)]

    monkeypatch.setattr(ch, "_now_ts", lambda: T0 + 8 * H1)
    ch.update_cache("BTC-USD", tmp_path, start_ts=T0)
    assert calls[-1] == ("BTC-USD", T0 + 5 * H1, T0 + 8 * H1)  # resumes after the last cached bar
    df = pd.read_parquet(path)
    assert df["ts"].tolist() == [T0 + i * H1 for i in range(8)]
    assert df["ts"].is_unique

    n_calls = len(calls)
    ch.update_cache("BTC-USD", tmp_path, start_ts=T0)  # already current -> no fetch
    assert len(calls) == n_calls
