import logging

import numpy as np
import pandas as pd
import pytest

from ewbot.data.candles import (
    IntegrityReport,
    aggregate_4h,
    check_integrity,
    clip_wicks,
    entry_blackout,
    fill_gaps,
    futures_closed,
    load_1h,
    slice_ts,
    to_bars,
)
from ewbot.types import H1, H4, Bar

T = 1_704_067_200  # Mon 2024-01-01 00:00:00 UTC (also a 4h boundary)

# Friday 17:00 America/New_York as exact UTC epoch seconds (bar OPEN time of the halted hour):
JAN_FRI_17ET = 1_705_096_800  # Fri 2024-01-12 22:00 UTC  (EST = UTC-5)
JUL_FRI_17ET = 1_720_818_000  # Fri 2024-07-12 21:00 UTC  (EDT = UTC-4)
MAR_FRI_17ET_EST = 1_709_935_200  # Fri 2024-03-08 22:00 UTC (last Friday before DST starts Mar 10)
MAR_FRI_17ET_EDT = 1_710_536_400  # Fri 2024-03-15 21:00 UTC (first Friday after DST starts)


def make_df(hours, base: float = 100.0) -> pd.DataFrame:
    """1h bars at T + h*3600 with o=base+h, h=o+10, l=o-10, c=o+1, v=1 (h = hour offset)."""
    hours = list(hours)
    o = np.array([base + h for h in hours], dtype=float)
    return pd.DataFrame(
        {
            "ts": np.array([T + h * H1 for h in hours], dtype="int64"),
            "open": o,
            "high": o + 10,
            "low": o - 10,
            "close": o + 1,
            "volume": np.ones(len(hours)),
        }
    )


# ---------- integrity / load ----------

def test_check_integrity_gap_and_duplicate():
    df = make_df([0, 1, 2, 4, 5, 5, 6])  # hour 3 missing, hour 5 duplicated
    rep = check_integrity(df)
    assert rep == IntegrityReport(
        n=7, missing_ts=(T + 3 * H1,), duplicate_ts=(T + 5 * H1,), nonpositive_ts=()
    )


def test_check_integrity_clean_empty_and_nonpositive():
    assert check_integrity(make_df(range(5))) == IntegrityReport(5, (), (), ())
    assert check_integrity(make_df([])) == IntegrityReport(0, (), (), ())
    df = make_df(range(4))
    df.loc[2, "low"] = 0.0
    df.loc[3, "close"] = -1.0
    rep = check_integrity(df)
    assert rep.nonpositive_ts == (T + 2 * H1, T + 3 * H1)
    assert rep.missing_ts == () and rep.duplicate_ts == ()


def test_check_integrity_multi_hour_gap_and_other_tf():
    df = make_df([0, 1, 5])
    assert check_integrity(df).missing_ts == (T + 2 * H1, T + 3 * H1, T + 4 * H1)
    four = pd.DataFrame({"ts": [T, T + 3 * H4], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0})
    assert check_integrity(four, tf=H4).missing_ts == (T + H4, T + 2 * H4)


def test_load_1h_sorts_and_enforces_dtypes(tmp_path):
    df = make_df([2, 0, 1])
    df["ts"] = df["ts"].astype("int32")
    p = tmp_path / "x.parquet"
    df.to_parquet(p, index=False)
    out = load_1h(p)
    assert out["ts"].tolist() == [T, T + H1, T + 2 * H1]
    assert str(out["ts"].dtype) == "int64"
    assert str(out["close"].dtype) == "float64"
    assert isinstance(out.index, pd.RangeIndex)


# ---------- fill_gaps ----------

def test_fill_gaps_inserts_flat_synthetic_bar():
    df = make_df([0, 1, 3])  # closes 101, 102, 104 -> hour 2 missing
    out = fill_gaps(df)
    assert out["ts"].tolist() == [T + i * H1 for i in range(4)]
    assert out["synthetic"].tolist() == [False, False, True, False]
    row = out.iloc[2]
    assert (row["open"], row["high"], row["low"], row["close"]) == (102.0, 102.0, 102.0, 102.0)
    assert row["volume"] == 0.0
    assert out.iloc[3]["open"] == 103.0  # real bars untouched
    assert str(out["ts"].dtype) == "int64" and str(out["synthetic"].dtype) == "bool"
    assert check_integrity(out).missing_ts == ()


def test_fill_gaps_multi_hour_gap_dedupe_and_idempotent():
    df = make_df([0, 1, 1, 5])  # duplicate hour 1, hours 2-4 missing
    out = fill_gaps(df)
    assert len(out) == 6
    assert out["synthetic"].tolist() == [False, False, True, True, True, False]
    assert out.loc[2:4, "close"].tolist() == [102.0, 102.0, 102.0]  # all flat at hour-1 close
    assert out.loc[2:4, "volume"].tolist() == [0.0, 0.0, 0.0]
    again = fill_gaps(out)
    pd.testing.assert_frame_equal(again, out)  # flags preserved, nothing new inserted


def test_fill_gaps_no_gap_and_empty():
    out = fill_gaps(make_df(range(3)))
    assert out["synthetic"].tolist() == [False, False, False]
    empty = fill_gaps(make_df([]))
    assert len(empty) == 0 and "synthetic" in empty.columns


# ---------- aggregate_4h ----------

def test_aggregate_4h_values_and_trailing_incomplete_bucket_dropped():
    out = aggregate_4h(make_df(range(9)))  # hours 0-3, 4-7 complete; hour 8 starts a bucket
    assert out["ts"].tolist() == [T, T + H4]  # trailing partial bucket (hour 8) dropped
    b0, b1 = out.iloc[0], out.iloc[1]
    # hour h: open=100+h, high=110+h, low=90+h, close=101+h, volume=1
    assert (b0["open"], b0["high"], b0["low"], b0["close"], b0["volume"]) == (100.0, 113.0, 90.0, 104.0, 4.0)
    assert (b1["open"], b1["high"], b1["low"], b1["close"], b1["volume"]) == (104.0, 117.0, 94.0, 108.0, 4.0)
    assert out["synthetic"].tolist() == [False, False]
    assert (out["ts"] % H4 == 0).all()


def test_aggregate_4h_alignment_drops_leading_partial_bucket():
    out = aggregate_4h(make_df(range(1, 9)))  # starts at 01:00 -> bucket 0 has 3 bars
    assert out["ts"].tolist() == [T + H4]


def test_aggregate_4h_bucket_with_missing_hour_dropped_until_gaps_filled():
    raw = make_df([0, 1, 3, 4, 5, 6, 7])  # hour 2 missing
    assert aggregate_4h(raw)["ts"].tolist() == [T + H4]  # bucket 0 incomplete -> dropped
    out = aggregate_4h(fill_gaps(raw))
    assert out["ts"].tolist() == [T, T + H4]
    assert out["synthetic"].tolist() == [True, False]
    b0 = out.iloc[0]
    # hours 0,1 real, hour 2 synthetic (flat 102, vol 0), hour 3 real
    assert (b0["open"], b0["high"], b0["low"], b0["close"], b0["volume"]) == (100.0, 113.0, 90.0, 104.0, 3.0)


def test_aggregate_4h_to_bars_roundtrip_types():
    out = aggregate_4h(make_df(range(8)))
    bars = to_bars(out)
    assert bars[0] == Bar(T, 100.0, 113.0, 90.0, 104.0, 4.0, False)


# ---------- clip_wicks ----------

def test_clip_wicks_uses_previous_bar_atr():
    df = pd.DataFrame(
        {
            "ts": [T + i * H1 for i in range(4)],
            "open": [100.0, 101.0, 102.0, 100.0],
            "close": [101.0, 102.0, 100.0, 101.0],
            "high": [150.0, 130.0, 103.0, 102.0],
            "low": [99.0, 100.0, 60.0, 99.0],
            "volume": 1.0,
        }
    )
    atr = np.array([2.0, 2.0, np.nan, 2.0])
    out, n = clip_wicks(df, atr, k=3.0)
    assert n == 2
    # bar 0: no previous ATR -> untouched, even with a 150 wick (no wrap-around to atr[-1])
    assert out.loc[0, "high"] == 150.0
    # bar 1: cap = max(101,102) + 3*atr[0] = 108 -> high 130 clipped; floor = 101 - 6 = 95 <= low 100 -> unchanged
    assert out.loc[1, "high"] == 108.0 and out.loc[1, "low"] == 100.0
    # bar 2: floor = min(102,100) - 3*atr[1] = 94 -> low 60 clipped
    assert out.loc[2, "low"] == 94.0 and out.loc[2, "high"] == 103.0
    # bar 3: previous ATR is NaN -> untouched
    assert out.loc[3, "high"] == 102.0 and out.loc[3, "low"] == 99.0
    # original frame is not modified; open/close/volume are untouched
    assert df.loc[1, "high"] == 130.0
    pd.testing.assert_series_equal(out["open"], df["open"])
    pd.testing.assert_series_equal(out["close"], df["close"])


def test_clip_wicks_no_change_logs_nothing_and_validates_length(caplog):
    df = make_df(range(5))
    with caplog.at_level(logging.INFO, logger="ewbot.data.candles"):
        out, n = clip_wicks(df, np.full(5, 100.0), k=6.0)
    assert n == 0 and caplog.records == []
    pd.testing.assert_frame_equal(out, df)
    with pytest.raises(ValueError):
        clip_wicks(df, np.ones(4), k=6.0)


def test_clip_wicks_logs_when_clipped(caplog):
    df = make_df(range(3))
    with caplog.at_level(logging.INFO, logger="ewbot.data.candles"):
        _, n = clip_wicks(df, np.full(3, 1.0), k=1.0)  # wicks are 10 wide, cap is body +/- 1
    assert n == 2
    assert any("clipped 2" in r.getMessage() for r in caplog.records)


# ---------- futures_closed / entry_blackout across DST ----------

def test_constants_are_the_stated_new_york_times():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    for ts in (JAN_FRI_17ET, JUL_FRI_17ET, MAR_FRI_17ET_EST, MAR_FRI_17ET_EDT):
        d = datetime.fromtimestamp(ts, ny)
        assert (d.weekday(), d.hour, d.minute) == (4, 17, 0)


def test_futures_closed_friday_january_est():
    assert futures_closed(JAN_FRI_17ET) is True  # [22:00, 23:00) UTC = 17:00-18:00 EST
    assert futures_closed(JAN_FRI_17ET - H1) is False  # 16:00 EST bar ends exactly at the halt
    assert futures_closed(JAN_FRI_17ET + H1) is False  # 18:00 EST bar starts exactly at reopen
    assert futures_closed(JAN_FRI_17ET - 24 * H1) is False  # same time Thursday
    assert futures_closed(JAN_FRI_17ET + 24 * H1) is False  # same time Saturday


def test_futures_closed_friday_july_edt():
    assert futures_closed(JUL_FRI_17ET) is True  # [21:00, 22:00) UTC = 17:00-18:00 EDT
    assert futures_closed(JUL_FRI_17ET - H1) is False
    assert futures_closed(JUL_FRI_17ET + H1) is False  # 22:00 UTC is 18:00 EDT -> already open
    # a fixed-EST implementation would wrongly flag 22:00 UTC and miss 21:00 UTC in July
    assert futures_closed(JUL_FRI_17ET - 24 * H1) is False


def test_futures_closed_around_dst_change():
    assert futures_closed(MAR_FRI_17ET_EST) is True  # 22:00 UTC, still EST on 2024-03-08
    assert futures_closed(MAR_FRI_17ET_EST - H1) is False
    assert futures_closed(MAR_FRI_17ET_EDT) is True  # 21:00 UTC, EDT on 2024-03-15
    assert futures_closed(MAR_FRI_17ET_EDT + H1) is False


def test_futures_closed_handles_non_aligned_bar_start():
    assert futures_closed(JAN_FRI_17ET - 1800) is True  # [21:30, 22:30) UTC overlaps the halt
    assert futures_closed(JAN_FRI_17ET - 3600) is False


def test_entry_blackout_january_est():
    assert entry_blackout(JAN_FRI_17ET - 2 * H1) is False  # 15:00 EST
    assert entry_blackout(JAN_FRI_17ET - H1) is True  # 16:00 EST
    assert entry_blackout(JAN_FRI_17ET) is True  # 17:00 EST
    assert entry_blackout(JAN_FRI_17ET + H1) is False  # 18:00 EST
    assert entry_blackout(JAN_FRI_17ET - H1 - 24 * H1) is False  # Thursday 16:00 EST


def test_entry_blackout_july_edt():
    assert entry_blackout(JUL_FRI_17ET - 2 * H1) is False  # 15:00 EDT
    assert entry_blackout(JUL_FRI_17ET - H1) is True  # 16:00 EDT
    assert entry_blackout(JUL_FRI_17ET) is True  # 17:00 EDT
    assert entry_blackout(JUL_FRI_17ET + H1) is False  # 18:00 EDT


def test_entry_blackout_around_dst_change():
    assert entry_blackout(MAR_FRI_17ET_EST - H1) is True and entry_blackout(MAR_FRI_17ET_EST + H1) is False
    assert entry_blackout(MAR_FRI_17ET_EDT - H1) is True and entry_blackout(MAR_FRI_17ET_EDT + H1) is False


def test_exactly_two_blackout_hours_and_one_closed_hour_per_friday():
    week_start = T  # Monday 2024-01-01 00:00 UTC
    hours = [week_start + i * H1 for i in range(7 * 24)]
    assert sum(futures_closed(t) for t in hours) == 1
    assert sum(entry_blackout(t) for t in hours) == 2


# ---------- slice_ts / to_bars ----------

def test_slice_ts_half_open_interval():
    df = make_df(range(6))
    assert slice_ts(df, T + 2 * H1, T + 4 * H1)["ts"].tolist() == [T + 2 * H1, T + 3 * H1]
    assert slice_ts(df, None, T + 2 * H1)["ts"].tolist() == [T, T + H1]
    assert slice_ts(df, T + 4 * H1, None)["ts"].tolist() == [T + 4 * H1, T + 5 * H1]
    assert len(slice_ts(df, None, None)) == 6
    assert slice_ts(df, T + 10 * H1, None).empty
    out = slice_ts(df, T + 2 * H1, None)
    assert isinstance(out.index, pd.RangeIndex) and out.index[0] == 0


def test_to_bars_with_and_without_synthetic_column():
    df = make_df([0, 2])
    bars = to_bars(df)
    assert bars == [
        Bar(T, 100.0, 110.0, 90.0, 101.0, 1.0, False),
        Bar(T + 2 * H1, 102.0, 112.0, 92.0, 103.0, 1.0, False),
    ]
    assert type(bars[0].ts) is int and type(bars[0].open) is float

    filled = to_bars(fill_gaps(df))
    assert [b.synthetic for b in filled] == [False, True, False]
    assert filled[1] == Bar(T + H1, 101.0, 101.0, 101.0, 101.0, 0.0, True)
    assert to_bars(make_df([])) == []
