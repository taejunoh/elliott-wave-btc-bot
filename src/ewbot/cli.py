"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ewbot.backtest.holdout import HoldoutError, run_holdout
from ewbot.backtest.metrics import compute_metrics, reliability_by_q
from ewbot.backtest.report import to_jsonable, write_report
from ewbot.backtest.runner import breakdowns, compare, equity_pairs, run_backtest, utc_ts
from ewbot.backtest.walkforward import GRID, MIN_TRAIN_TRADES, walk_forward
from ewbot.config import AGGRESSIVE, CONSERVATIVE, BotConfig, load_config, with_overrides
from ewbot.data.candles import aggregate_4h, check_integrity, fill_gaps, load_1h
from ewbot.data.coinbase_history import update_cache
from ewbot.types import H1

DEFAULT_CONFIG = Path("config/default.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ewbot", description="Elliott Wave BTC bot - research tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="YAML config (default: config/default.yaml if present)")
    common.add_argument("--profile", choices=("conservative", "aggressive"), default=None, help="risk profile override")
    common.add_argument("--mode", choices=("A", "C", "HYBRID"), default=None, help="strategy for `backtest` (compare runs all three)")
    common.add_argument("--start", default=None, help="UTC date YYYY-MM-DD (default: first bar)")
    common.add_argument("--end", default=None, help="UTC date YYYY-MM-DD, exclusive (default: after the last bar)")
    common.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parallel processes")
    common.add_argument("--out", type=Path, default=None, help="output HTML path (default: reports/<command>.html)")
    common.add_argument("--product", default="BTC-USD")
    common.add_argument("--cache-dir", type=Path, default=Path("data/cache"))

    sub.add_parser("fetch", parents=[common], help="download/update the Coinbase 1h candle cache")
    sub.add_parser("backtest", parents=[common], help="one backtest of --mode over --start/--end")
    p_cmp = sub.add_parser("compare", parents=[common], help="A, C, HYBRID vs buy&hold, EMA 50/200 and random entries")
    p_cmp.add_argument("--random-runs", type=int, default=100)
    sub.add_parser("walkforward", parents=[common], help="60-combo walk-forward on 2017-01..2023-12")
    p_ho = sub.add_parser("holdout", parents=[common], help="ONE-SHOT evaluation on 2024-01-01..end")
    p_ho.add_argument("--random-runs", type=int, default=100)
    p_ho.add_argument("--params", default=None, help="K4,Q_MIN,S_MIN e.g. 3.0,0.6,0.25")
    p_ho.add_argument("--params-from", type=Path, default=None, help="reports/walkforward.json (uses chosen_final)")
    p_ho.add_argument("--lock", type=Path, default=None, help="lock file (default: <project root>/holdout.lock)")
    p_ho.add_argument("--confirm", action="store_true", help="required: acknowledge the holdout is single-use")
    return parser


# ----------------------------------------------------------------------------- helpers


def _load_cfg(args: argparse.Namespace) -> BotConfig:
    path = args.config if args.config is not None else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    cfg = load_config(path)
    if args.profile:
        cfg = dataclasses.replace(cfg, risk=AGGRESSIVE if args.profile == "aggressive" else CONSERVATIVE)
    if args.mode:
        cfg = with_overrides(cfg, **{"strategy.mode": args.mode})
    return cfg


def _load_df(args: argparse.Namespace) -> pd.DataFrame:
    path = Path(args.cache_dir) / f"{args.product}_1h.parquet"
    if not path.exists():
        raise SystemExit(f"candle cache not found: {path} - run `ewbot fetch` first")
    raw = load_1h(path)
    rep = check_integrity(raw)
    print(f"[data] {rep.n} bars, {len(rep.missing_ts)} missing, {len(rep.duplicate_ts)} duplicate", file=sys.stderr)
    return fill_gaps(raw)


def _range(args: argparse.Namespace, df: pd.DataFrame) -> tuple[int, int]:
    start = utc_ts(args.start) if args.start else int(df["ts"].iloc[0])
    end = utc_ts(args.end) if args.end else int(df["ts"].iloc[-1]) + H1
    if end <= start:
        raise SystemExit("--end must be after --start")
    return start, end


def _out(args: argparse.Namespace) -> Path:
    return args.out if args.out is not None else Path("reports") / f"{args.cmd}.html"


def _candles4h(df: pd.DataFrame) -> dict[str, list]:
    d4 = aggregate_4h(df)
    return {"ts": d4["ts"].astype("int64").tolist(), "close": d4["close"].astype("float64").tolist()}


def _print_comparison(comp: dict[str, dict[str, Any]]) -> None:
    cols = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate", "avg_r", "n_trades")
    print(f"{'':8}" + "".join(f"{c:>13}" for c in cols))
    for name, m in comp.items():
        print(f"{name:8}" + "".join(f"{m[c]:>13.3f}" if c != "n_trades" else f"{m[c]:>13d}" for c in cols))


def _compare_sections(cmp: dict[str, Any], df: pd.DataFrame, notes: list[str]) -> dict[str, Any]:
    bd = cmp["breakdown"]
    elliott_trades = {k: v for k, v in cmp["trades"].items() if k in bd}
    return {
        "notes": notes,
        "equity": cmp["equity"],
        "comparison": cmp["comparison"],
        "random": cmp["random"],
        "ci": cmp["ci"],
        "per_setup": {k: v["per_setup"] for k, v in bd.items()},
        "long_short": {k: v["long_short"] for k, v in bd.items()},
        "per_year": {k: v["per_year"] for k, v in bd.items()},
        "reliability": reliability_by_q(cmp["trades"].get("A", [])),
        "trades": elliott_trades,
        "candles4h": _candles4h(df),
    }


def _notes(cfg: BotConfig, start: int, end: int) -> list[str]:
    c = cfg.costs
    return [
        f"window {time.strftime('%Y-%m-%d', time.gmtime(start))} .. {time.strftime('%Y-%m-%d', time.gmtime(end))} (UTC, end exclusive)",
        f"risk profile {cfg.risk.name}; fee {c.fee_rate:.4%}, slippage {c.slippage:.4%}, funding {c.funding_apr_long:.0%} APR (long pays)",
        f"K4={cfg.wave.K4} Q_MIN={cfg.strategy.Q_MIN} S_MIN={cfg.strategy.S_MIN}",
    ]


# ----------------------------------------------------------------------------- commands


def cmd_fetch(args: argparse.Namespace) -> int:
    path = update_cache(args.product, Path(args.cache_dir))
    df = load_1h(path)
    rep = check_integrity(df)
    print(f"cache: {path}\nbars: {rep.n}  first_ts: {int(df['ts'].iloc[0])}  last_ts: {int(df['ts'].iloc[-1])}")
    print(f"missing: {len(rep.missing_ts)}  duplicate: {len(rep.duplicate_ts)}  nonpositive: {len(rep.nonpositive_ts)}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg, df = _load_cfg(args), _load_df(args)
    start, end = _range(args, df)
    t0 = time.perf_counter()
    res = run_backtest(cfg, df, start, end)
    dt = time.perf_counter() - t0
    name = cfg.strategy.mode
    m = compute_metrics(res.equity, res.trades, start, end)
    bd = breakdowns(res, start, end)
    _print_comparison({name: dataclasses.asdict(m)})
    print(f"backtest {name}: {len(res.trades)} trades in {dt:.1f}s")
    sections = {
        "notes": _notes(cfg, start, end) + [f"runtime {dt:.1f}s"],
        "equity": {name: equity_pairs(res.equity)},
        "comparison": {name: m},
        "per_setup": {name: bd["per_setup"]},
        "long_short": {name: bd["long_short"]},
        "per_year": {name: bd["per_year"]},
        "reliability": reliability_by_q(res.trades),
        "trades": {name: list(res.trades)},
        "candles4h": _candles4h(df),
    }
    print(f"report: {write_report(_out(args), f'Backtest {name}', sections)}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    cfg, df = _load_cfg(args), _load_df(args)
    start, end = _range(args, df)
    t0 = time.perf_counter()
    cmp = compare(cfg, df, start, end, random_runs=args.random_runs, workers=args.workers)
    dt = time.perf_counter() - t0
    _print_comparison(cmp["comparison"])
    print(f"random control: {cmp['random']['runs']} runs, {cmp['random']['n_signals']} entries each; compare took {dt:.1f}s")
    sections = _compare_sections(cmp, df, _notes(cfg, start, end) + [f"runtime {dt:.1f}s"])
    print(f"report: {write_report(_out(args), 'Strategy comparison', sections)}")
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    cfg, df = _load_cfg(args), _load_df(args)
    t0 = time.perf_counter()
    wf = walk_forward(cfg, df, args.workers)
    dt = time.perf_counter() - t0
    out = _out(args)
    rows = []
    for f in wf["folds"]:
        k4, q, s = f["chosen"] if f["chosen"] else (None, None, None)
        oos = f["oos"] or {}
        rows.append(
            {
                "test_start": time.strftime("%Y-%m-%d", time.gmtime(f["test"][0])),
                "K4": k4, "Q_MIN": q, "S_MIN": s,
                "train_calmar": None if f["train_calmar"] is None else round(f["train_calmar"], 2),
                "oos_return": oos.get("total_return"), "oos_max_dd": oos.get("max_dd"), "oos_trades": oos.get("n_trades"),
            }
        )
    jpath = out.with_suffix(".json")
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(json.dumps(to_jsonable({k: v for k, v in wf.items() if k != "oos_trades"}), indent=1), encoding="utf-8")
    print(f"walk-forward: {len(wf['folds'])} folds in {dt:.0f}s; chosen_final = {wf['chosen_final']}; json: {jpath}")
    warn = []
    if wf["chosen_final_fallback"]:  # the last fold had no selectable combo: do not pass this on silently
        warn = [
            f"WARNING: chosen_final {wf['chosen_final']} comes from fold {wf['chosen_final_from_fold']}, not the last "
            "fold (no selectable combo there); `holdout --params-from` refuses it"
        ]
        print(warn[0], file=sys.stderr)
    _print_comparison({"WF-OOS": wf["stitched_metrics"]})
    sections = {
        "notes": _notes(cfg, 0, 0)[1:]
        + [
            f"runtime {dt:.0f}s",
            f"grid {dict(GRID)}",
            f"circuit breakers disabled in grid runs; a combo needs >= {MIN_TRAIN_TRADES} trades per training window",
        ]
        + warn,
        "equity": {"WF-OOS (stitched)": wf["stitched_equity"]},
        "comparison": {"WF-OOS": wf["stitched_metrics"]},
        "trades": {"WF-OOS": wf["oos_trades"]},
        "candles4h": _candles4h(df),
        "folds": rows,
        "combo_table": wf["combo_table"],
    }
    print(f"report: {write_report(out, 'Walk-forward (OOS stitched)', sections)}")
    return 0


def _parse_params(args: argparse.Namespace) -> tuple[float, ...]:
    if args.params:
        vals = tuple(float(x) for x in args.params.split(","))
    elif args.params_from:
        wf = json.loads(Path(args.params_from).read_text(encoding="utf-8"))
        chosen = wf.get("chosen_final")
        if not chosen:
            raise SystemExit(f"{args.params_from} has no chosen_final")
        if wf.get("chosen_final_fallback"):
            raise SystemExit(
                f"{args.params_from}: chosen_final {chosen} comes from fold {wf.get('chosen_final_from_fold')}, not the "
                "last fold (no selectable combo there); pass --params explicitly if you accept it"
            )
        vals = tuple(float(x) for x in chosen)
    else:
        raise SystemExit("holdout needs --params K4,Q_MIN,S_MIN or --params-from reports/walkforward.json")
    if len(vals) != len(GRID):
        raise SystemExit(f"expected {len(GRID)} params for {list(GRID)}, got {len(vals)}")
    return vals


def cmd_holdout(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing: the holdout is single-use. Re-run with --confirm once you are done tuning.", file=sys.stderr)
        return 2
    params = _parse_params(args)
    cfg, df = _load_cfg(args), _load_df(args)
    try:
        out = run_holdout(cfg, df, params, lock_path=args.lock, confirm=True, random_runs=args.random_runs, workers=args.workers)
    except HoldoutError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    cmp = out["compare"]
    _print_comparison(cmp["comparison"])
    print(json.dumps(to_jsonable(out["criteria"]), indent=1))
    for line in out["criteria"]["trade_count"]:  # spec 9.6 criterion 3: out-of-sample trades N/50
        print(line)
    start, end = out["window"]
    notes = _notes(with_overrides(cfg, **dict(zip(GRID, params))), start, end) + [f"lock {out['lock_path']} sha256 {out['sha256'][:12]}"]
    sections = _compare_sections(cmp, df, out["criteria"]["trade_count"] + notes)
    sections["criteria"] = out["criteria"]
    print(f"report: {write_report(_out(args), 'Holdout 2024-01-01..end (one-shot)', sections)}")
    return 0


_COMMANDS = {
    "fetch": cmd_fetch,
    "backtest": cmd_backtest,
    "compare": cmd_compare,
    "walkforward": cmd_walkforward,
    "holdout": cmd_holdout,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
