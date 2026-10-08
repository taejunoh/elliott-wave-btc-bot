from __future__ import annotations

import json

import pandas as pd
import pytest

from ewbot.aeth.config import DATA_START, AethError
from ewbot.aeth.data import CHUNK_HOURS, HoldoutGuardError, cache_path, fetch_eth, load_eth
from ewbot.aeth.lockfile import (
    LOCK_BOUND,
    LOCK_KIND,
    committed_text,
    dirty_files,
    lock_digest,
    lock_exists,
    read_lock,
    refuse_dirty,
)
from ewbot.types import H1
from tests.aeth.helpers import HS, FakeFetch1h, commit_file, eth_frame, init_repo, pre2024_frame, save_cache

NOW = HS + 40 * 86_400 + 1_800  # 2024-02-10 00:30 UTC: the last closed hour opened at 2024-02-09 23:00


def _valid_lock(path):
    lock = {"kind": LOCK_KIND, "version": 1, "params": [2.0, 0.5, 0.0]}
    lock["sha256"] = lock_digest(lock)
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


@pytest.mark.parametrize("case", ["missing", "not_json", "other_kind", "digest"])
def test_only_a_lock_whose_sha256_is_its_digest_exists(tmp_path, case):
    p = tmp_path / "holdout-aeth.lock"
    lock = {"kind": LOCK_KIND, "version": 1}
    lock["sha256"] = lock_digest(lock)
    if case == "not_json":
        p.write_text("{", encoding="utf-8")
    if case == "other_kind":
        p.write_text(json.dumps({**lock, "kind": "ewbot-stratb-holdout"}), encoding="utf-8")
    if case == "digest":
        p.write_text(json.dumps({**lock, "version": 2}), encoding="utf-8")
    assert lock_exists(p) is False
    with pytest.raises(AethError):
        read_lock(p)
    assert lock_exists(_valid_lock(p)) is True and read_lock(p)["kind"] == LOCK_KIND


def test_the_digest_binds_exactly_the_bound_fields():
    lock = {k: f"v-{k}" for k in LOCK_BOUND}
    d = lock_digest(lock)
    assert lock_digest({**lock, "first_run": {"x": 1}, "attempts": [1], "locked_at": "now"}) == d
    assert all(lock_digest({**lock, k: "other"}) != d for k in LOCK_BOUND)


def test_committed_text_and_dirty_files(tmp_path):
    repo = init_repo(tmp_path / "repo")
    p = commit_file(repo, "holdout-aeth.lock", "one\n")
    assert committed_text(p) == "one\n" and committed_text(repo / "nope.json") is None
    assert dirty_files(p) == []
    p.write_text("two\n", encoding="utf-8")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    assert dirty_files(p) == ["README.md", "holdout-aeth.lock"]
    assert dirty_files(p, p.name) == ["README.md"]
    with pytest.raises(AethError, match="uncommitted changes"):
        refuse_dirty(p, p.name)
    with pytest.raises(AethError, match="not a git working tree"):
        dirty_files(tmp_path / "elsewhere" / "x.json")


def test_fetch_before_the_lock_stops_at_2024_in_saved_chunks(tmp_path):
    src = eth_frame()
    fake = FakeFetch1h(src)
    path = fetch_eth(tmp_path / "cache", lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=fake)
    assert path == cache_path(tmp_path / "cache") and path.name == "ETH-USD_1h.parquet"
    assert fake.calls[0][:2] == ("ETH-USD", DATA_START)
    assert all(end - start <= CHUNK_HOURS * H1 for _, start, end in fake.calls)
    assert max(end for _, _, end in fake.calls) == HS  # no window reaches 2024-01-01
    got = pd.read_parquet(path)
    assert int(got["ts"].max()) == HS - H1 and got.equals(src[src["ts"] < HS].reset_index(drop=True))
    fake.calls.clear()
    fetch_eth(tmp_path / "cache", lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=fake)
    assert fake.calls == []  # nothing new before 2024


def test_an_interrupted_fetch_resumes_from_the_last_saved_chunk(tmp_path):
    src = pre2024_frame()  # 2023-08-29 .. 2023-12-31: two chunks hold bars
    fake = FakeFetch1h(src)

    def flaky(product, start, end):
        if start > int(src["ts"].iloc[0]):  # the chunk after the first one with bars fails
            raise ConnectionError("coinbase down")
        return fake(product, start, end)

    with pytest.raises(ConnectionError):
        fetch_eth(tmp_path, lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=flaky)
    saved = int(pd.read_parquet(cache_path(tmp_path))["ts"].max())
    assert int(src["ts"].iloc[0]) <= saved < HS - H1
    fake.calls.clear()
    fetch_eth(tmp_path, lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=fake)
    assert fake.calls[0][1] == saved + H1 and int(pd.read_parquet(cache_path(tmp_path))["ts"].max()) == HS - H1


def test_with_the_lock_fetch_extends_to_the_last_closed_hour(tmp_path):
    src = eth_frame()
    lock = _valid_lock(tmp_path / "holdout-aeth.lock")
    save_cache(tmp_path / "cache", src[src["ts"] < HS])
    fake = FakeFetch1h(src)
    path = fetch_eth(tmp_path / "cache", lock_path=lock, now_ts=NOW, fetch=fake)
    assert fake.calls[0][1] == HS and max(end for _, _, end in fake.calls) == NOW // H1 * H1
    assert int(pd.read_parquet(path)["ts"].max()) == NOW // H1 * H1 - H1  # the hour in progress is not stored


def test_load_filters_2024_out_until_the_lock_unlocks_it(tmp_path):
    src = eth_frame()
    lock_path = tmp_path / "holdout-aeth.lock"
    path = save_cache(tmp_path / "cache", src[src["ts"] < HS])
    assert int(load_eth(path, lock_path=lock_path)["ts"].max()) == HS - H1
    with pytest.raises(HoldoutGuardError):
        load_eth(path, unlock_2024=True, lock_path=lock_path)
    _valid_lock(lock_path)
    save_cache(tmp_path / "cache", src)
    assert int(load_eth(path, lock_path=lock_path)["ts"].max()) == HS - H1  # still filtered without unlock_2024
    full = load_eth(path, unlock_2024=True, lock_path=lock_path)
    assert full.equals(src.astype({"ts": "int64"}).reset_index(drop=True))


def test_2024_bars_without_a_lock_did_not_come_from_aeth_and_are_refused(tmp_path):
    """E.g. strategy A's `ewbot fetch --product ETH-USD` writes the same file up to now."""
    path = save_cache(tmp_path / "cache", eth_frame())
    lock_path = tmp_path / "missing.lock"
    with pytest.raises(HoldoutGuardError, match="did not come from A-ETH"):
        load_eth(path, lock_path=lock_path)
    with pytest.raises(HoldoutGuardError, match="did not come from A-ETH"):
        fetch_eth(tmp_path / "cache", lock_path=lock_path, now_ts=NOW, fetch=FakeFetch1h(eth_frame()))
