from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import ewbot
from ewbot.cli import build_parser, main
from tests.synth import random_bars

SRC = str(Path(ewbot.__file__).resolve().parents[1])  # works with or without an editable install


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
    assert r.returncode == 0 and "--confirm" in r.stdout and "--params" in r.stdout and "--lock" in r.stdout


def test_parser_defaults_and_types():
    ns = build_parser().parse_args(["compare", "--start", "2019-01-01", "--end", "2024-01-01", "--workers", "3"])
    assert (ns.cmd, ns.start, ns.end, ns.workers, ns.random_runs) == ("compare", "2019-01-01", "2024-01-01", 3, 100)
    ho = build_parser().parse_args(["holdout", "--params", "3.0,0.6,0.25"])
    assert ho.confirm is False and ho.lock is None  # run_holdout then uses <project root>/holdout.lock


def test_holdout_without_confirm_is_refused_before_touching_data(capsys, tmp_path):
    rc = main(["holdout", "--params", "3.0,0.6,0.25", "--cache-dir", str(tmp_path)])
    assert rc == 2
    assert "--confirm" in capsys.readouterr().err
    assert not (tmp_path / "BTC-USD_1h.parquet").exists()


def test_missing_cache_gives_clear_error(tmp_path):
    r = _run("backtest", "--cache-dir", str(tmp_path))
    assert r.returncode != 0 and "ewbot fetch" in r.stderr


def test_params_from_refuses_a_fallback_chosen_final(tmp_path):
    wf = tmp_path / "walkforward.json"
    wf.write_text(
        json.dumps({"chosen_final": [3.0, 0.6, 0.25], "chosen_final_from_fold": 7, "chosen_final_fallback": True}),
        encoding="utf-8",
    )
    lock = tmp_path / "holdout.lock"
    argv = ["holdout", "--params-from", str(wf), "--confirm", "--cache-dir", str(tmp_path), "--lock", str(lock)]
    with pytest.raises(SystemExit, match="fold 7"):
        main(argv)
    assert not lock.exists()  # refused before any data or lock is touched


def test_compare_end_to_end_on_synthetic_cache_writes_report(tmp_path, capsys):
    """Real stack through the CLI: parquet cache -> load_1h/fill_gaps -> compare -> HTML report."""
    bars = random_bars(seed=0, n=120 * 24, vol=30.0, start=20_000.0)  # 2023-11-15 .. 2024-03-14
    df = pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    df.drop(index=[1_500]).to_parquet(cache / "BTC-USD_1h.parquet", index=False)  # one missing hour
    out = tmp_path / "compare.html"
    argv = ["compare", "--cache-dir", str(cache), "--start", "2023-12-26", "--random-runs", "2", "--workers", "1"]
    assert main([*argv, "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "1 missing" in captured.err and f"report: {out}" in captured.out
    for name in ("A", "C", "HYBRID", "BH", "EMA"):
        assert any(line.startswith(name) for line in captured.out.splitlines()), name
    html = out.read_text(encoding="utf-8")
    for heading in ("<h2>Equity", "<h2>Drawdown", "<h2>Comparison", "<h2>Trades - A"):
        assert heading in html
