from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import ewbot
from ewbot import cli
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.cli import build_parser, main
from tests.synth import random_bars

SRC = str(Path(ewbot.__file__).resolve().parents[1])  # works with or without an editable install
WEEK = 7 * 24 * 3600


def _write_cache(cache: Path, shift_weeks: int = 0, n_days: int = 120, drop: tuple[int, ...] = ()) -> pd.DataFrame:
    """tests/synth random bars from 2023-11-15 (120 days reach 2024-03-14), moved by whole weeks."""
    bars = random_bars(seed=0, n=n_days * 24, vol=30.0, start=20_000.0)
    df = pd.DataFrame(
        {
            "ts": [b.ts + shift_weeks * WEEK for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    cache.mkdir(parents=True, exist_ok=True)
    df.drop(index=list(drop)).to_parquet(cache / "BTC-USD_1h.parquet", index=False)
    return df


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-m", "ewbot.cli", *args], capture_output=True, text=True, timeout=120, env=env
    )


def test_help_lists_all_subcommands():
    r = _run("--help")
    assert r.returncode == 0
    for name in ("fetch", "backtest", "compare", "walkforward", "holdout"):
        assert name in r.stdout


def test_subcommand_help_flags():
    r = _run("compare", "--help")
    assert r.returncode == 0
    for flag in ("--config", "--profile", "--mode", "--start", "--end", "--workers", "--out", "--random-runs"):
        assert flag in r.stdout
    r = _run("holdout", "--help")
    assert r.returncode == 0 and "--confirm" in r.stdout and "--params" in r.stdout
    assert "--lock" not in r.stdout  # the lock location is not a CLI choice


def test_parser_defaults_and_types():
    ns = build_parser().parse_args(["compare", "--start", "2019-01-01", "--end", "2024-01-01", "--workers", "3"])
    assert (ns.cmd, ns.start, ns.end, ns.workers, ns.random_runs) == ("compare", "2019-01-01", "2024-01-01", 3, 100)
    ho = build_parser().parse_args(["holdout", "--params", "3.0,0.6,0.25"])
    assert ho.confirm is False and not hasattr(ho, "lock")  # always <project root>/holdout.lock
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["holdout", "--params", "3.0,0.6,0.25", "--lock", "/tmp/other.lock"])
    assert exc.value.code == 2


def test_holdout_without_confirm_is_refused_before_touching_data(capsys, tmp_path):
    rc = main(["holdout", "--params", "3.0,0.6,0.25", "--cache-dir", str(tmp_path)])
    assert rc == 2
    assert "--confirm" in capsys.readouterr().err
    assert not (tmp_path / "BTC-USD_1h.parquet").exists()


def test_missing_cache_gives_clear_error(tmp_path):
    r = _run("backtest", "--cache-dir", str(tmp_path))
    assert r.returncode != 0 and "ewbot fetch" in r.stderr


@pytest.fixture
def tmp_lock(tmp_path, monkeypatch):
    """Point the (only) holdout lock location at a temp file: no test may write <repo>/holdout.lock."""
    from ewbot.backtest import holdout

    lock = tmp_path / "root" / "holdout.lock"
    monkeypatch.setattr(holdout, "DEFAULT_LOCK_PATH", lock)
    return lock


def test_params_from_refuses_a_fallback_chosen_final(tmp_path, tmp_lock):
    wf = tmp_path / "walkforward.json"
    wf.write_text(
        json.dumps({"chosen_final": [3.0, 0.6, 0.25], "chosen_final_from_fold": 7, "chosen_final_fallback": True}),
        encoding="utf-8",
    )
    argv = ["holdout", "--params-from", str(wf), "--confirm", "--cache-dir", str(tmp_path)]
    with pytest.raises(SystemExit, match="fold 7"):
        main(argv)
    assert not tmp_lock.exists()  # refused before any data or lock is touched


def test_holdout_cli_records_provenance_and_asks_to_commit_the_lock(tmp_path, tmp_lock, capsys):
    """Synthetic cache only (tests never run the holdout on real data)."""
    cache = tmp_path / "cache"
    _write_cache(cache)  # 2023-11-15 .. 2024-03-14
    wf = tmp_path / "walkforward.json"
    wf.write_text(json.dumps({"chosen_final": [3.0, 0.6, 0.25], "chosen_final_fallback": False}), encoding="utf-8")
    common = ["--confirm", "--cache-dir", str(cache), "--random-runs", "1", "--workers", "1"]
    assert main(["holdout", "--params-from", str(wf), *common, "--out", str(tmp_path / "h1.html")]) == 0
    out = capsys.readouterr().out
    assert "git add" in out and "git commit" in out and str(tmp_lock) in out
    assert main(["holdout", "--params", "3.0,0.6,0.25", *common, "--out", str(tmp_path / "h2.html")]) == 0
    runs = json.loads(tmp_lock.read_text(encoding="utf-8"))["runs"]
    assert [r["params_source"] for r in runs] == [f"walkforward:{wf}#chosen_final", "explicit"]
    assert all(r["headline"]["A"]["n_trades"] >= 0 for r in runs)


def test_gitignore_keeps_the_holdout_lock_and_runtime_log_tracked():
    root = Path(__file__).resolve().parents[2]
    lines = {ln.strip() for ln in (root / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "holdout.lock" not in lines  # the lock must be committed after the one holdout run
    assert not {ln for ln in lines if ln.startswith("docs")}  # docs/backtest-runtime.md is tracked


def test_compare_end_to_end_on_synthetic_cache_writes_report(tmp_path, capsys):
    """Real stack through the CLI: parquet cache -> load_1h/fill_gaps -> compare -> HTML report."""
    cache = tmp_path / "cache"
    _write_cache(cache, shift_weeks=-26, drop=(1_500,))  # 2023-05-17 .. 2023-09-14, one missing hour
    out = tmp_path / "compare.html"
    argv = ["compare", "--cache-dir", str(cache), "--start", "2023-06-27", "--random-runs", "2", "--workers", "1"]
    assert main([*argv, "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "1 missing" in captured.err and f"report: {out}" in captured.out
    for name in ("A", "C", "HYBRID", "BH", "EMA"):
        assert any(line.startswith(name) for line in captured.out.splitlines()), name
    for line in captured.out.splitlines():  # benchmarks: win_rate and avg_r are not comparable -> "-"
        cols = line.split()
        if cols and cols[0] in ("BH", "EMA"):
            assert cols[6:8] == ["-", "-"] and cols[8].isdigit(), line
    html = out.read_text(encoding="utf-8")
    for heading in ("<h2>Equity", "<h2>Drawdown", "<h2>Comparison", "<h2>Trades - A"):
        assert heading in html


def _spy_compare(monkeypatch) -> list[tuple[int, int, int]]:
    seen: list[tuple[int, int, int]] = []
    real = cli.compare

    def spy(cfg, df1h, start_ts, end_ts, **kw):
        seen.append((int(df1h["ts"].max()), start_ts, end_ts))
        return real(cfg, df1h, start_ts, end_ts, **kw)

    monkeypatch.setattr(cli, "compare", spy)
    return seen


def test_default_compare_on_a_cache_spanning_2024_only_sees_bars_before_it(tmp_path, capsys, monkeypatch):
    cache = tmp_path / "cache"
    _write_cache(cache)  # 2023-11-15 .. 2024-03-14: about 2.5 months inside the holdout
    seen = _spy_compare(monkeypatch)
    out = tmp_path / "c.html"
    assert main(["compare", "--cache-dir", str(cache), "--random-runs", "2", "--workers", "1", "--out", str(out)]) == 0
    [(last_bar, start, end)] = seen
    assert last_bar == HOLDOUT_START - 3600 and end == HOLDOUT_START  # the frame itself was cut
    assert "excluded" in capsys.readouterr().err  # the [data] line says the holdout bars were dropped
    html = out.read_text(encoding="utf-8")
    assert not re.search(r"20(2[4-9]|[3-9]\d)-\d\d-\d\dT", html)  # no ISO timestamp from 2024 on
    c4 = json.loads(re.search(r"const C4 = (\{.*?\});\n", html, re.S).group(1))
    assert c4["ts"] and max(c4["ts"]) < HOLDOUT_START  # report candles stop before the holdout
    trades = json.loads(re.search(r"const TRADES = (\[.*?\]);\n", html, re.S).group(1))
    assert all(t["exit_ts"] < HOLDOUT_START for t in trades)


def test_default_backtest_window_ends_at_the_holdout(tmp_path, capsys):
    cache = tmp_path / "cache"
    _write_cache(cache)
    out = tmp_path / "b.html"
    assert main(["backtest", "--cache-dir", str(cache), "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert "2023-11-15 .. 2024-01-01 (UTC, end exclusive)" in html
    assert not re.search(r"20(2[4-9]|[3-9]\d)-\d\d-\d\dT", html)


@pytest.mark.parametrize(
    "dates",
    [("--end", "2025-01-01"), ("--end", "2024-01-02"), ("--start", "2024-01-01"), ("--start", "2024-02-01")],
    ids=["end-2025", "end-one-day-in", "start-at-boundary", "start-inside"],
)
@pytest.mark.parametrize("cmd", ["backtest", "compare"])
def test_explicit_dates_inside_the_holdout_are_refused(tmp_path, capsys, cmd, dates):
    cache = tmp_path / "cache"
    _write_cache(cache)
    with pytest.raises(SystemExit) as exc:
        main([cmd, "--cache-dir", str(cache), *dates, "--out", str(tmp_path / "x.html")])
    assert exc.value.code == 2
    assert "holdout" in capsys.readouterr().err
    assert not (tmp_path / "x.html").exists()


def test_end_exactly_at_the_holdout_start_is_allowed(tmp_path):
    cache = tmp_path / "cache"
    _write_cache(cache)
    out = tmp_path / "b.html"
    assert main(["backtest", "--cache-dir", str(cache), "--start", "2023-12-20", "--end", "2024-01-01", "--out", str(out)]) == 0
