"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper | live | stratb.

Holdout isolation: every research command (backtest, compare, walkforward) cuts the candle
frame to ts < HOLDOUT_START (2024-01-01) right after loading it, before any engine run, benchmark or
report candle sees it, and an explicit --start at/after HOLDOUT_START or --end after it is refused
(exit 2). runner.run_backtest and runner.compare refuse the holdout on their own as well (defence in
depth). `holdout` is the one-shot evaluation. `paper run` (Plan 2) works on the period AFTER the
holdout: it reads the params from the committed holdout.lock only and has no strategy or config
options (ewbot.paper.runner). `live run | preflight | status | stage | rebase | backup` (Plan 3) runs and
inspects the live-trading runtime (ewbot.live); like `paper` it has no strategy or config options.
`stratb fetch | backtest | walkforward | holdout lock | holdout run | paper run | paper status` (Plan 4) is
strategy B (ewbot.stratb, ewbot.paperb) on BTC and ETH with its own 15m caches, lock (holdout-b.lock) and
ledger (paper-b branch); its research commands never read a bar from 2024-01-01 on (data15 filters them out).
`aeth fetch | stage1 | holdout lock | holdout run | paper run | paper status` (Plan 5a) is A-ETH (ewbot.aeth):
strategy A, frozen, on ETH-USD with its own 1h cache, stage-1 record (aeth-stage1.json), lock (holdout-aeth.lock)
and ledger (paper-aeth branch); like `paper` it has no strategy or config options, and ewbot.aeth is imported only
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, NoReturn, Sequence

import pandas as pd

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH, HoldoutError, run_holdout
from ewbot.backtest.metrics import compute_metrics, reliability_by_q
from ewbot.backtest.report import BENCHMARKS, to_jsonable, write_report
from ewbot.backtest.runner import breakdowns, compare, equity_pairs, run_backtest, utc_ts
from ewbot.backtest.walkforward import GRID, HOLDOUT_START, MIN_TRAIN_TRADES, WF_END, walk_forward
from ewbot.config import AGGRESSIVE, CONSERVATIVE, BotConfig, load_config, with_overrides
from ewbot.data.candles import aggregate_4h, check_integrity, fill_gaps, load_1h
from ewbot.data.coinbase_history import update_cache
from ewbot.live import app as live_app
from ewbot.live import runtime as live_runtime
from ewbot.live.exchange import Exchange, ExchangeError
from ewbot.live.journal import Journal
from ewbot.live.preflight import LOCK_FILE_SHA256, LOCK_SHA256, Check, check_alerts, run_preflight
from ewbot.live.settings import LiveSettings, SettingsError, load_settings
from ewbot.live.stage import (
    STAGES,
    Gate,
    StageError,
    canary_entry_gates,
    canary_gates,
    full_entry_gates,
    gate_lines,
    restart_stint,
    shadow_gates,
    transition,
)
from ewbot.live.watchdog import AccountGuard
from ewbot.paper.criteria import golive_status, status_lines
from ewbot.paper.ledger import LedgerError, current_epoch, load_ledger
from ewbot.paper.notify import deliver, failure_text, make_event, position_line, sender_from_env
from ewbot.paper.runner import PaperEnv, PaperRunError, run_paper
from ewbot.paperb.ledger import paper_count_b
from ewbot.paperb.notify import b_head, position_lines_b
from ewbot.paperb.runner import PaperBEnv, PaperBRunError, run_paper_b, status_lines_b
from ewbot.stratb.config import combo_to_json, config_for, normalize_combo
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH as STRATB_LOCK
from ewbot.stratb.data15 import cache_paths as b_cache_paths
from ewbot.stratb.data15 import gap_report, load_caches_15m, lock_exists, update_caches_15m
from ewbot.stratb.holdout import HoldoutBError, create_lock, run_holdout_b
from ewbot.stratb.research import backtest_b, trades_by_asset, walk_forward_b
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
    common.add_argument(
        "--end", default=None, help=f"UTC date YYYY-MM-DD, exclusive, at most {WF_END} (default: after the last bar before it)"
    )
    common.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parallel processes")
    common.add_argument("--out", type=Path, default=None, help="output HTML path (default: reports/<command>.html)")
    common.add_argument("--product", default="BTC-USD")
    common.add_argument("--cache-dir", type=Path, default=Path("data/cache"))

    sub.add_parser("fetch", parents=[common], help="download/update the Coinbase 1h candle cache")
    sub.add_parser("backtest", parents=[common], help="one backtest of --mode over --start/--end")
    p_cmp = sub.add_parser("compare", parents=[common], help="A, C, HYBRID vs buy&hold, EMA 50/200 and random entries")
    p_cmp.add_argument("--random-runs", type=int, default=100)
    sub.add_parser("walkforward", parents=[common], help="60-combo walk-forward on 2017-01..2023-12")
    p_ho = sub.add_parser(
        "holdout", parents=[common], help="ONE-SHOT evaluation on 2024-01-01..end (lock: <project root>/holdout.lock)"
    )
    p_ho.add_argument("--random-runs", type=int, default=100)
    p_ho.add_argument("--params", default=None, help="K4,Q_MIN,S_MIN e.g. 3.0,0.6,0.25")
    p_ho.add_argument("--params-from", type=Path, default=None, help="reports/walkforward.json (uses chosen_final)")
    p_ho.add_argument("--confirm", action="store_true", help="required: acknowledge the holdout is single-use")

    p_paper = sub.add_parser("paper", help="paper-trading runtime (Plan 2): hourly recompute, ledger, Telegram")
    paper_sub = p_paper.add_subparsers(dest="paper_cmd", required=True)
    p_run = paper_sub.add_parser("run", help="one paper run; params come from holdout.lock only")
    p_run.add_argument("--ledger-dir", type=Path, required=True, help="paper branch worktree (ledger.json, report.html, runs.log)")
    p_run.add_argument("--cache-dir", type=Path, default=Path("data/cache"), help="candle cache directory")
    p_status = paper_sub.add_parser("status", help="print the ledger's go-live status (read-only, no network)")
    p_status.add_argument("--ledger-dir", type=Path, required=True)

    p_live = sub.add_parser("live", help="live-trading runtime (Plan 3): Coinbase CFM BIP, SHADOW -> CANARY -> FULL")
    live_sub = p_live.add_subparsers(dest="live_cmd", required=True)
    live_sub.add_parser("run", help="run the live runtime (settings and secrets from the environment)")
    p_pre = live_sub.add_parser("preflight", help="startup checks: fingerprint, holdout.lock, alerts, key permissions, "
                                "product")
    p_pre.add_argument("--offline", action="store_true", help="fingerprint, lock and alerts only (no Coinbase request)")
    p_ls = live_sub.add_parser("status", help="journal status and the current stage's gates (read-only)")
    p_ls.add_argument("--json", action="store_true", help="machine-readable (deploy/live/deploy.sh)")
    p_ls.add_argument("--max-age", type=float, default=None, help="exit 1 when the loop heartbeat is older (seconds)")
    p_ls.add_argument("--exchange", action="store_true", help="also read the Coinbase position and open orders")
    p_ls.add_argument("--require-flat", action="store_true",
                      help="exit 3 unless the journal holds no open trade, no order of unknown outcome and no standing "
                      "flatten and (with --exchange) the account has no position and no resting order (deploy.sh)")
    p_stage = live_sub.add_parser("stage", help="change the operating stage (promotions need every gate met)")
    p_stage.add_argument("to", choices=("shadow", "canary", "full"))
    p_stage.add_argument("--confirm", action="store_true", help="required: the user's explicit approval")
    p_stage.add_argument("--restart", action="store_true",
                         help="shadow or canary, the current stage: start a new stint of it (flat book)")
    p_rb = live_sub.add_parser("rebase", help="after a deliberate deposit or withdrawal: re-base the account loss "
                               "breakers (the running runtime applies it at its next minute)")
    p_rb.add_argument("--confirm", action="store_true", help="required: the user's explicit approval")
    p_bk = live_sub.add_parser("backup", help="consistent copy of the journal (SQLite online backup)")
    p_bk.add_argument("--out", type=Path, required=True)

    p_b = sub.add_parser("stratb", help="strategy B (Plan 4): 15m data, research, one-shot holdout, paper trading")
    b_sub = p_b.add_subparsers(dest="b_cmd", required=True)
    b_cache = argparse.ArgumentParser(add_help=False)
    b_cache.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    b_sub.add_parser("fetch", parents=[b_cache], help="update the BTC and ETH 15m caches (before 2024 until the lock)")
    p_bb = b_sub.add_parser("backtest", parents=[b_cache], help="one strategy-B backtest (BTC + ETH) before 2024")
    p_bb.add_argument("--params", required=True, help="zz_k,z_lo,z_hi,tp1_r,ext,remainder e.g. 2.0,0.382,0.618,1.0,1.272,breakeven")
    p_bb.add_argument("--start", default=None, help="UTC date YYYY-MM-DD (default: first bar)")
    p_bb.add_argument("--end", default=None, help=f"UTC date YYYY-MM-DD, exclusive, at most {WF_END}")
    p_bb.add_argument("--out", type=Path, default=Path("reports/stratb-backtest.json"))
    p_bw = b_sub.add_parser("walkforward", parents=[b_cache], help="32-combo walk-forward 2017-01..2023-12 (A's rules)")
    p_bw.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_bw.add_argument("--out", type=Path, default=Path("reports/stratb-walkforward.json"))
    p_bh = b_sub.add_parser("holdout", help="holdout-b.lock and the ONE-SHOT strategy-B holdout")
    bh_sub = p_bh.add_subparsers(dest="bh_cmd", required=True)
    p_bhl = bh_sub.add_parser("lock", parents=[b_cache], help="write holdout-b.lock from the walk-forward (commit it)")
    p_bhl.add_argument("--params-from", type=Path, required=True, help="reports/stratb-walkforward.json")
    p_bhr = bh_sub.add_parser("run", parents=[b_cache], help="run the holdout ONCE (the user's approval: --confirm)")
    p_bhr.add_argument("--confirm", action="store_true")
    p_bhr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_bp = b_sub.add_parser("paper", help="paper trading of strategy B (ledger on the paper-b branch)")
    bp_sub = p_bp.add_subparsers(dest="bp_cmd", required=True)
    p_bpr = bp_sub.add_parser("run", parents=[b_cache], help="one paper-B run; params come from holdout-b.lock only")
    p_bpr.add_argument("--ledger-dir", type=Path, required=True)
    p_bps = bp_sub.add_parser("status", help="print the paper-B ledger's status (read-only, no network)")
    p_bps.add_argument("--ledger-dir", type=Path, required=True)

    p_e = sub.add_parser("aeth", help="A-ETH (Plan 5a): strategy A, frozen, on ETH - stage 1, one-shot holdout, paper")
    e_sub = p_e.add_subparsers(dest="e_cmd", required=True)
    e_cache = argparse.ArgumentParser(add_help=False)
    e_cache.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    e_sub.add_parser("fetch", parents=[e_cache], help="update the ETH-USD 1h cache (before 2024 until holdout-aeth.lock)")
    p_e1 = e_sub.add_parser("stage1", parents=[e_cache], help="stage 1, ONCE: A on ETH 2017-2023 -> aeth-stage1.json")
    p_e1.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_eh = e_sub.add_parser("holdout", help="holdout-aeth.lock and the ONE-SHOT ETH holdout")
    eh_sub = p_eh.add_subparsers(dest="eh_cmd", required=True)
    eh_sub.add_parser("lock", parents=[e_cache], help="write holdout-aeth.lock from aeth-stage1.json (commit it)")
    p_ehr = eh_sub.add_parser("run", parents=[e_cache], help="run the ETH holdout ONCE (the user's approval: --confirm)")
    p_ehr.add_argument("--confirm", action="store_true")
    p_ehr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_ep = e_sub.add_parser("paper", help="paper trading of A on ETH (ledger on the paper-aeth branch)")
    ep_sub = p_ep.add_subparsers(dest="ep_cmd", required=True)
    p_epr = ep_sub.add_parser("run", parents=[e_cache], help="one paper-A-ETH run; everything comes from holdout-aeth.lock")
    p_epr.add_argument("--ledger-dir", type=Path, required=True)
    p_eps = ep_sub.add_parser("status", help="print the paper-A-ETH ledger's status (read-only, no network)")
    p_eps.add_argument("--ledger-dir", type=Path, required=True)
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


def _refuse(msg: str) -> NoReturn:
    print(f"refusing: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _load_df(args: argparse.Namespace, *, holdout: bool = False) -> pd.DataFrame:
    """The gap-filled 1h frame. Unless holdout=True (only `ewbot holdout`), every bar at/after
    HOLDOUT_START is dropped HERE, before anything else can see it."""
    path = Path(args.cache_dir) / f"{args.product}_1h.parquet"
    if not path.exists():
        raise SystemExit(f"candle cache not found: {path} - run `ewbot fetch` first")
    raw = load_1h(path)
    note = ""
    if not holdout:
        n_all = len(raw)
        raw = raw[raw["ts"] < HOLDOUT_START].reset_index(drop=True)
        if raw.empty:
            raise SystemExit(f"no bars before {WF_END} in {path}")
        note = f", {n_all - len(raw)} bars at/after {WF_END} excluded (holdout)"
    rep = check_integrity(raw)
    print(
        f"[data] {rep.n} bars, {len(rep.missing_ts)} missing, {len(rep.duplicate_ts)} duplicate{note}",
        file=sys.stderr,
    )
    return fill_gaps(raw)


def _requested_window(args: argparse.Namespace) -> tuple[int | None, int | None]:
    """Explicit --start/--end as epoch seconds (None when not given). Research commands never reach
    into the holdout: --start at/after HOLDOUT_START or --end after it is refused with exit code 2."""
    start = utc_ts(args.start) if args.start else None
    end = utc_ts(args.end) if args.end else None
    if (start is not None and start >= HOLDOUT_START) or (end is not None and end > HOLDOUT_START):
        _refuse(
            f"--start {args.start} / --end {args.end} reach into the holdout ({WF_END} onwards). Research "
            f"windows must end by {WF_END}; the holdout is evaluated once, with `ewbot holdout`."
        )
    return start, end


def _range(args: argparse.Namespace, df: pd.DataFrame) -> tuple[int, int]:
    """[start, end): explicit --start/--end (checked by _requested_window), else the first bar and
    one hour after the last bar of the (already cut) frame."""
    req_start, req_end = _requested_window(args)
    start = req_start if req_start is not None else int(df["ts"].iloc[0])
    end = req_end if req_end is not None else int(df["ts"].iloc[-1]) + H1
    if end <= start:
        raise SystemExit("--end must be after --start")
    return start, end


def _out(args: argparse.Namespace) -> Path:
    return args.out if args.out is not None else Path("reports") / f"{args.cmd}.html"


def _candles4h(df: pd.DataFrame) -> dict[str, list]:
    d4 = aggregate_4h(df)
    return {"ts": d4["ts"].astype("int64").tolist(), "close": d4["close"].astype("float64").tolist()}


def _print_comparison(comp: dict[str, dict[str, Any]]) -> None:
    """Console table; BH / EMA show "-" for win_rate and avg_r (benchmark R is not comparable)."""
    cols = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate", "avg_r", "n_trades")
    print(f"{'':8}" + "".join(f"{c:>13}" for c in cols))
    for name, m in comp.items():
        cells = []
        for c in cols:
            if c == "n_trades":
                cells.append(f"{m[c]:>13d}")
            elif name in BENCHMARKS and c in ("win_rate", "avg_r"):
                cells.append(f"{'-':>13}")
            else:
                cells.append(f"{m[c]:>13.3f}")
        print(f"{name:8}" + "".join(cells))


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
    _requested_window(args)  # refuse holdout dates before touching any data
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
    _requested_window(args)  # refuse holdout dates before touching any data
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


def _parse_params(args: argparse.Namespace) -> tuple[tuple[float, ...], str]:
    """(K4, Q_MIN, S_MIN) and their provenance: "explicit" or "walkforward:<path>#chosen_final"."""
    if args.params:
        vals = tuple(float(x) for x in args.params.split(","))
        source = "explicit"
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
        source = f"walkforward:{args.params_from}#chosen_final"
    else:
        raise SystemExit("holdout needs --params K4,Q_MIN,S_MIN or --params-from reports/walkforward.json")
    if len(vals) != len(GRID):
        raise SystemExit(f"expected {len(GRID)} params for {list(GRID)}, got {len(vals)}")
    return vals, source


def cmd_holdout(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing: the holdout is single-use. Re-run with --confirm once you are done tuning.", file=sys.stderr)
        return 2
    params, source = _parse_params(args)
    cfg, df = _load_cfg(args), _load_df(args, holdout=True)
    try:  # the lock is always DEFAULT_LOCK_PATH (<project root>/holdout.lock): not a CLI choice
        out = run_holdout(
            cfg, df, params, confirm=True, random_runs=args.random_runs, workers=args.workers, params_source=source
        )
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
    lock = out["lock_path"]
    print(
        f"\nholdout lock written: {lock} (sha256 {out['sha256'][:12]}).\n"
        "Commit it NOW - it is the permanent record that the holdout has been used (not done automatically):\n"
        f'  git add "{lock}"\n'
        f'  git commit -m "chore(holdout): record holdout run {out["sha256"][:12]}"'
    )
    return 0


RUN_URL_ENV = "EWBOT_RUN_URL"  # set by .github/workflows/paper.yml: link to the Actions run log
FAILURE_MARKER_ENV = "EWBOT_FAILURE_MARKER"  # file the workflow checks before its own fallback alert


def _paper_env(args: argparse.Namespace) -> PaperEnv:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    return PaperEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _alert_failure(env: PaperEnv, stage: str, message: str) -> None:
    """Failure alert with the stage and the run-log link; on success touch the marker file so the
    workflow's fallback step does not send a second one."""
    if env.sender is None:
        return
    event = make_event("FAILURE", env.clock(), stage, failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([event], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _paper_run(args: argparse.Namespace) -> int:
    env = _paper_env(args)
    try:
        out = run_paper(env)
    except PaperRunError as exc:
        print(f"paper run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    count = out.status["count"]
    print(
        f"paper run {out.run_id}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(out.last_bar_ts))} UTC, "
        f"epoch {out.epoch_id}, out-of-sample {count['total']}/{count['required']}, {out.new_decisions} new "
        f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending"
    )
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _paper_status(args: argparse.Namespace) -> int:
    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    st = golive_status(led, int(time.time()))
    for line in status_lines(st):
        print(line)
    print(position_line(led.open_position))
    cur = current_epoch(led)
    print(
        f"epoch {cur['id']} of {len(led.epochs)}: fingerprint {cur['fingerprint'][:12]} "
        f"(baseline {led.baseline['fingerprint'][:12]} @ {led.baseline['commit'][:7]})"
    )
    if led.runs:
        r = led.runs[-1]
        print(
            f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
            f"data {r['data_sha256'][:12]}, holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH'}, "
            f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['A']})"
        )
    unexplained = sum(1 for d in led.discrepancies if d["cause"] == "UNEXPLAINED")
    environment = sum(1 for d in led.discrepancies if d["cause"] == "ENVIRONMENT_CHANGE")
    print(
        f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)} (unexplained {unexplained}, "
        f"environment change {environment}), pending events {len(led.pending_events)}"
    )
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    return _paper_status(args) if args.paper_cmd == "status" else _paper_run(args)


LIVE_REFUSAL_WAIT_S = 600.0  # a refused start waits before exiting: Docker restarts it, alerts stay sparse
# (a start refused because Coinbase could not be read - an outage - waits live_app.OUTAGE_WAIT_S instead)
ALERTS_GATE = "알림 설정 (Telegram 알림·명령, 헬스체크)"  # a promotion to CANARY or FULL needs them (preflight `alerts`)
CYCLE_WINDOW = (59, 6)  # promotions and stint restarts are refused from hh:59 to hh:06 UTC (the hour's cycle, entries)


def _in_cycle_window(now: float) -> bool:
    """`now` falls from hh:59 to hh:06 UTC: the runtime's hourly cycle (hh:00:30) and its entries (until hh:05) may
    open a trade between a promotion's flat check and its stage write."""
    minute = int(now) // 60 % 60
    return minute >= CYCLE_WINDOW[0] or minute < CYCLE_WINDOW[1]


def _live_settings() -> LiveSettings:
    """Production settings from the environment and the secrets files (tests replace this function)."""
    return load_settings()


def _live_lock() -> tuple[Path, str, str]:
    """The committed holdout lock, its pinned sha256 field and the pinned sha256 of the file's bytes (tests replace
    this function)."""
    return DEFAULT_LOCK_PATH, LOCK_SHA256, LOCK_FILE_SHA256


def _print_checks(checks: list[Check]) -> bool:
    for c in checks:
        print(f"preflight {c.name}: {'ok' if c.ok else 'FAILED'} - {c.detail}")
    return all(c.ok for c in checks)


def _exchange(s: LiveSettings) -> Exchange:
    """The real exchange: anything that fails to set it up is a SettingsError (a refusal, never a traceback)."""
    try:
        return live_app.real_exchange(s)
    except SettingsError:
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. the SDK's bare Exception for a malformed key file
        raise SettingsError(live_app.redact(f"Coinbase: {type(exc).__name__}: {exc}", s)) from None


def _live_run(s: LiveSettings) -> int:
    """`ewbot live run`: the preflight for the process's stage (`live_app.process_stage`: a demoted journal with
    something real left runs its last real stage), then the runtime until a stage change (exit 0, Docker starts it
    again), a stop (SIGTERM / Ctrl-C: after the current tick, exit 0), a refusal (exit 2 after LIVE_REFUSAL_WAIT_S - or,
    when only Coinbase could not be read, an outage, after live_app.OUTAGE_WAIT_S, alerted once an hour) or a fatal
    error (exit 1, after a back-off when it repeats). The wiring is closed - the Telegram poller and the feed stopped,
    the queued messages flushed - before any wait, and on every exit."""
    stop = live_app.StopRequest()
    notify = live_app.notifier(s)
    journal: Journal | None = None
    wired: live_app.Wired | None = None
    closed = False

    def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if wired is not None:
            wired.close()
        else:
            notify.flush(10.0)
            notify.stop()

    def refuse(why: str, outage: bool = False) -> int:
        print(f"refusing: {why}", file=sys.stderr)
        if live_app.outage_alert(journal, time.time()) if outage else live_app.refusal_alert(journal, why, time.time()):
            again = (f" (Coinbase를 읽지 못함: {live_app.OUTAGE_WAIT_S:.0f}초마다 다시 시작, 알림은 1시간에 한 번)" if outage
                     else f" ({LIVE_REFUSAL_WAIT_S / 60:.0f}분마다 다시 시작, 같은 이유의 알림은 1시간에 한 번)")
            notify("PREFLIGHT", f"[시작 거부] {why}{again}")
        close()  # nothing keeps polling Telegram or the feed during the wait: a command sent now is answered later
        stop.wait(live_app.OUTAGE_WAIT_S if outage else LIVE_REFUSAL_WAIT_S)
        return 2

    def fatal(exc: Exception) -> int:
        kind = type(exc).__name__
        wait, alert = live_app.fatal_backoff(journal, kind, time.time())
        if alert:  # healthchecks.io is a third party: the type name only, never the text
            (wired.heartbeat if wired is not None else live_app.heartbeat(s)).fail(f"[LIVE] 중단: {kind}")
            again = (f" ({live_app.FATAL_REPEAT_S / 60:.0f}분 안에 반복: {wait / 60:.0f}분 뒤 종료, Docker가 다시 시작)"
                     if wait else "")
            notify("FATAL", f"[중단] {kind}: {live_app.redact(str(exc), s)[:300]}{again}")
        print(live_app.fatal_report(exc, s), file=sys.stderr, end="")  # re-raised, Python would print it unredacted
        close()
        stop.wait(wait)
        return 1

    with stop.installed():
        try:
            journal = Journal(s.journal_path)
            try:
                real = _exchange(s)
            except SettingsError as exc:
                return refuse(str(exc))
            try:
                stage, why = live_app.process_stage(journal, real)
            except live_app.RealExposure as exc:
                return refuse(str(exc), outage=isinstance(exc, live_app.AccountUnreadable))
            if why is not None:
                print(f"stage {stage}: {why} (journal stage {journal.stage()[0]})", file=sys.stderr)
            path, sha, file_sha = _live_lock()
            checks, lock = run_preflight(real, stage, path, expected_lock=sha, expected_lock_file=file_sha, settings=s)
            if not _print_checks(checks) or lock is None:
                failed = [c for c in checks if not c.ok]
                return refuse("시작 점검 실패: " + "; ".join(f"{c.name}: {c.detail}" for c in failed),
                              outage=bool(failed) and all(c.retry for c in failed))
            wired = live_app.build(s, lock, real, journal, stage, notify)
            stop.attach(wired.runtime)
            return 0 if stop.requested else wired.runtime.run_forever()
        except live_runtime.StartupRefused as exc:  # Coinbase unreadable for minutes at start: an outage, no /fail
            return refuse(str(exc), outage=True)
        except KeyboardInterrupt:  # a deliberate stop where no handler could be installed
            return 0
        except Exception as exc:  # noqa: BLE001 - every other failure, the wiring's included, is fatal
            return fatal(exc)
        finally:
            close()  # stops the Telegram poller and the feed, lets the queued messages go out
            if journal is not None:
                journal.close()


def _live_preflight(s: LiveSettings, args: argparse.Namespace) -> int:
    """The checks `live run` makes, for the stage it would run (`live_app.process_stage`: a demoted journal with
    something real left checks its last real stage; --offline reads the journal only)."""
    real = None
    if not args.offline:
        try:
            real = _exchange(s)
        except SettingsError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
    stage = "SHADOW"
    if s.journal_path.exists():
        journal = Journal(s.journal_path, read_only=True)  # a candidate image's probe never writes the live journal
        try:
            stage, why = live_app.process_stage(journal, real)
        except live_app.RealExposure as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        finally:
            journal.close()
        if why is not None:
            print(f"stage {stage}: {why}", file=sys.stderr)
    path, sha, file_sha = _live_lock()
    checks, _ = run_preflight(real, stage, path, expected_lock=sha, expected_lock_file=file_sha, settings=s)
    return 0 if _print_checks(checks) else 2


def _live_gates(journal: Journal, stage: str, now: float) -> list[Gate]:
    audit = live_runtime.audit_from_journal(journal)  # the runtime's last audit, a failed one included
    if stage == "SHADOW":
        return shadow_gates(journal, now, audit)
    if stage == "CANARY":
        return canary_gates(journal, now, audit)
    return []


def _live_status(s: LiveSettings, args: argparse.Namespace) -> int:
    """Journal status (no journal yet: --json still answers, stage null). Exit 1 when --max-age is exceeded
    (Docker HEALTHCHECK), 2 when --exchange cannot read Coinbase, 3 when --require-flat finds what the running
    runtime still manages (its flat book, `LiveRuntime.flat`): an open trade, an order of unknown outcome (a PENDING
    intent) or a standing flatten in the journal, or - with --exchange - a position or any resting order."""
    if not s.journal_path.exists() and not args.json:
        print(f"no journal at {s.journal_path}", file=sys.stderr)
        return 1
    # read-only: a candidate image's probe (deploy.sh) or the health check never writes or migrates the live journal
    journal = Journal(s.journal_path, read_only=True) if s.journal_path.exists() else None
    try:
        now = time.time()
        stage, since = journal.stage() if journal is not None else (None, None)
        hb = journal.get("heartbeat") if journal is not None else None
        age = None if hb is None else now - float(hb)
        trade = journal.get("trade") if journal is not None else None
        out: dict[str, Any] = {"stage": stage, "since": since, "heartbeat_age": age,
                               "paused": journal.get("paused") if journal is not None else None,
                               "trade": trade if trade and trade.get("status") == "OPEN" else None,
                               "pending_intents": len(journal.intents(status="PENDING")) if journal is not None else 0,
                               "flatten": journal.get(live_runtime.FLATTEN_KEY) if journal is not None else None}
        if args.exchange:
            try:
                real = _exchange(s)
                pos = real.position()
                out["exchange_position"] = None if pos is None else {"side": pos.side, "contracts": pos.contracts}
                out["exchange_open_orders"] = len(real.open_orders())
            except (SettingsError, ExchangeError) as exc:
                print(f"exchange unreadable: {exc}", file=sys.stderr)
                return 2
        if args.json:
            print(json.dumps(out, sort_keys=True))
        elif journal is not None:
            assert stage is not None
            for line in live_runtime.status_lines(journal, now) + gate_lines(_live_gates(journal, stage, now)):
                print(line)
            if args.exchange:
                print(f"exchange position: {out['exchange_position']}, open orders: {out['exchange_open_orders']}")
        held = _held(out["trade"] is not None, out["pending_intents"], out["flatten"] is not None,
                     out.get("exchange_position") is not None, out.get("exchange_open_orders", 0))
        if args.require_flat and held:
            print(f"not flat: {', '.join(held)} (flatten first: /flatten, or wait until the runtime is done)",
                  file=sys.stderr)
            return 3
        return 1 if args.max_age is not None and (age is None or age > args.max_age) else 0
    finally:
        if journal is not None:
            journal.close()


def _held(trade: bool, pending: int, flatten: bool, position: bool, orders: int) -> list[str]:
    """What keeps a book from flat - the running runtime's flat (LiveRuntime.flat): an open trade, an order of unknown
    outcome (a PENDING intent) or a standing flatten in the journal, a position or a resting order on the account."""
    return [what for what, there in (
        ("an open trade", trade), (f"{pending} order(s) of unknown outcome (PENDING)", pending > 0),
        ("a standing flatten", flatten), ("a position", position), (f"{orders} resting order(s)", orders > 0)) if there]


def _flat_book(s: LiveSettings, journal: Journal) -> tuple[bool, Exchange]:
    """(the book is flat as the running runtime sees it - `_held`: the journal's trade, orders of unknown outcome and
    standing flatten, Coinbase's position and resting orders -, the real exchange); what holds it is printed."""
    trade = journal.get("trade")
    real = _exchange(s)
    held = _held(bool(trade and trade.get("status") == "OPEN"), len(journal.intents(status="PENDING")),
                 journal.get(live_runtime.FLATTEN_KEY) is not None, real.position() is not None, len(real.open_orders()))
    if held:
        print(f"not flat: {', '.join(held)}", file=sys.stderr)
    return not held, real


def _live_stage(s: LiveSettings, args: argparse.Namespace) -> int:
    """A promotion (one stage up, every gate met, the alerts of the new stage configured) and a restart of the
    current SHADOW or CANARY stint (--restart) need --confirm and a flat book (journal and Coinbase) before anything
    is written. A demotion needs only --confirm: it is written first - a running runtime sizes new entries for it at
    once - and then the command reports whether the book is flat (the runtime keeps protecting and managing an open
    position - after a restart too - and restarts into the new stage once flat). Without a journal nothing is
    written (exit 1)."""
    if not s.journal_path.exists():
        print(f"no journal at {s.journal_path}: start the runtime first (ewbot live run)", file=sys.stderr)
        return 1
    journal = Journal(s.journal_path)
    try:
        now = time.time()
        to = args.to.upper()
        cur, _ = journal.stage()
        if not args.restart and STAGES.index(to) < STAGES.index(cur):
            try:
                transition(journal, to, now, [], confirm=args.confirm, flat=False)
            except StageError as exc:
                print(f"refusing: {exc}", file=sys.stderr)
                return 2
            print(f"stage {cur} -> {to} recorded: the running runtime sizes new entries for {to} at once")
            try:
                flat, _ = _flat_book(s, journal)
            except (SettingsError, ExchangeError) as exc:
                print(f"book: the account could not be read ({exc}) - check it with ewbot live status --exchange")
                return 0
            print("book flat: the runtime restarts into the new stage within a minute" if flat else
                  "book not flat: the runtime keeps protecting and managing the open position and restarts into the "
                  "new stage once flat")
            return 0
        if _in_cycle_window(now):  # the hour's cycle may open a trade between the flat check and the write
            print("refusing: promotions and stint restarts are refused from hh:59 to hh:06 UTC (the hourly cycle and "
                  "its entries) - try again after hh:06", file=sys.stderr)
            return 2
        try:  # the real account must be flat too
            flat, real = _flat_book(s, journal)
        except (SettingsError, ExchangeError) as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        if args.restart:
            try:
                restart_stint(journal, to, now, confirm=args.confirm, flat=flat)
            except StageError as exc:
                print(f"refusing: {exc}", file=sys.stderr)
                return 2
            print(f"{to} stint restarted: every {to} gate counts from now; the running runtime restarts into it "
                  "within a minute (a pause stays until /resume)")
            return 0
        try:  # the trade key is a CANARY / FULL gate
            perms = real.key_permissions()
        except ExchangeError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        gates: list[Gate] = []
        audit = live_runtime.audit_from_journal(journal)
        if (cur, to) == ("SHADOW", "CANARY"):
            gates = canary_entry_gates(journal, now, audit, live_app.paper_status(s, now), perms)
        elif (cur, to) == ("CANARY", "FULL"):
            gates = full_entry_gates(journal, now, audit, live_app.paper_status(s, now), perms)
        alerts = check_alerts(s, to)  # the new stage refuses to start without them: never written without them
        gates.append(Gate(ALERTS_GATE, alerts.ok, alerts.detail))
        for line in gate_lines(gates):
            print(line)
        try:
            transition(journal, to, now, gates, confirm=args.confirm, flat=flat)
        except StageError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        print(f"stage {cur} -> {to} recorded; the running runtime restarts into it once flat")
        return 0
    finally:
        journal.close()


def _usd(x: Any) -> str:
    return "-" if x is None else f"{float(x):,.2f}"


def _live_rebase(s: LiveSettings, args: argparse.Namespace) -> int:
    """`ewbot live rebase [--confirm]`: after a deliberate deposit or withdrawal (docs/live-trading.md) the account loss
    breakers' references - the day's and the peak - are based again. The CLI only asks (REBASE_KEY, a REBASE_REQUESTED
    event): the running runtime, the breakers' one writer, applies it at its next minute with its own balance reads
    (the median of three; alerted). Prints the references before and the equity now; needs --confirm and a flat book as
    the running runtime sees it (`_held`: an open trade's unrealized P&L would be folded into both references; an order
    of unknown outcome, a standing flatten, a position or a resting order - the latter two when Coinbase can be read)."""
    if not s.journal_path.exists():
        print(f"no journal at {s.journal_path}: start the runtime first (ewbot live run)", file=sys.stderr)
        return 1
    journal = Journal(s.journal_path)
    try:
        now = time.time()
        g = journal.get(AccountGuard.KEY) or {}
        print(f"before: day {g.get('day') or '-'} reference {_usd(g.get('day_ref'))} USD, peak {_usd(g.get('peak'))} USD")
        position, orders = False, 0
        try:
            real = _exchange(s)
            equity = f"equity now {_usd(real.balance().equity)} USD"
            position, orders = real.position() is not None, len(real.open_orders())
        except (SettingsError, ExchangeError) as exc:
            equity = f"equity now unreadable ({exc})"
        print(f"after: both references based again from the runtime's next balance read and the two after it (their "
              f"median) - {equity}")
        trade = journal.get("trade")
        held = _held(bool(trade and trade.get("status") == "OPEN"), len(journal.intents(status="PENDING")),
                     journal.get(live_runtime.FLATTEN_KEY) is not None, position, orders)
        if held:
            print(f"refusing: not flat: {', '.join(held)} - rebase with a flat book (/flatten, or wait until the runtime "
                  "is done)", file=sys.stderr)
            return 2
        if not args.confirm:
            print("refusing: a rebase needs --confirm (the user's explicit approval)", file=sys.stderr)
            return 2
        journal.put(live_runtime.REBASE_KEY, {"ts": now})
        journal.add_event(now, "REBASE_REQUESTED", {"day_ref": g.get("day_ref"), "peak": g.get("peak")})
        print("rebase requested: the running runtime applies it at its next minute ([손실 한도 기준 재설정] alert)")
        return 0
    finally:
        journal.close()


def _live_backup(s: LiveSettings, args: argparse.Namespace) -> int:
    if not s.journal_path.exists():  # never an empty journal created and copied as if it were the real one
        print(f"no journal at {s.journal_path}", file=sys.stderr)
        return 1
    journal = Journal(s.journal_path)
    try:
        print(f"journal backup: {journal.backup(args.out)}")
    finally:
        journal.close()
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    try:
        s = _live_settings()
    except SettingsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        if args.live_cmd == "run":  # no Telegram without settings: Docker's restarts sparse; a stop ends the wait
            stop = live_app.StopRequest()
            with stop.installed():
                stop.wait(LIVE_REFUSAL_WAIT_S)
        return 2
    if args.live_cmd == "run":
        return _live_run(s)
    handlers = {"preflight": _live_preflight, "status": _live_status, "stage": _live_stage, "rebase": _live_rebase,
                "backup": _live_backup}
    return handlers[args.live_cmd](s, args)


# ----------------------------------------------------------------------------- strategy B (Plan 4)


def _b_params(text: str) -> tuple:
    """"zz_k,z_lo,z_hi,tp1_r,ext,remainder" -> the grid combo; exit 2 when it is not one."""
    parts = [x.strip() for x in text.split(",")]
    try:
        if len(parts) != 6:
            raise ValueError(f"expected 6 values, got {len(parts)}")
        return normalize_combo([parts[0], [parts[1], parts[2]], parts[3], parts[4], parts[5]])
    except ValueError as exc:
        _refuse(f"--params {text}: {exc}")


def _b_dfs(args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    """Every asset's 15m bars before 2024-01-01 (data15 filters the rest out while reading)."""
    for path in b_cache_paths(args.cache_dir).values():
        if not path.exists():
            raise SystemExit(f"15m cache not found: {path} - run `ewbot stratb fetch` first")
    dfs = load_caches_15m(args.cache_dir)
    for a, df in dfs.items():
        rep = gap_report(df)
        print(f"[data] {a}: {rep.n} 15m bars before {WF_END}, {len(rep.missing_ts)} missing", file=sys.stderr)
    return dfs


def _b_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(obj), indent=1), encoding="utf-8")


def _b_fetch(args: argparse.Namespace) -> int:
    paths = update_caches_15m(args.cache_dir, lock_path=STRATB_LOCK)
    for a, df in load_caches_15m(args.cache_dir).items():
        rep = gap_report(df)
        print(f"{a} cache: {paths[a]}\n{a} bars before {WF_END}: {rep.n}  missing: {len(rep.missing_ts)}  duplicate: "
              f"{len(rep.duplicate_ts)}")
    unlocked = "2024+ included (holdout-b.lock exists)" if lock_exists(STRATB_LOCK) else f"stops at {WF_END} (no lock)"
    print(f"fetch window: {unlocked}")
    return 0


def _b_backtest(args: argparse.Namespace) -> int:
    combo = _b_params(args.params)
    start, end = _requested_window(args)
    dfs = _b_dfs(args)
    start = start if start is not None else min(int(df["ts"].iloc[0]) for df in dfs.values())
    end = end if end is not None else max(int(df["ts"].iloc[-1]) for df in dfs.values()) + 900
    res = backtest_b(dfs, config_for(combo), start, end)
    m = compute_metrics(res.equity, res.trades, start, end)
    _print_comparison({"B": dataclasses.asdict(m)})
    by_asset = trades_by_asset([t for t in res.trades if start <= t.entry_ts < end])
    print(f"trades by asset: {by_asset}")
    _b_write_json(args.out, {"params": combo_to_json(combo), "window": [start, end], "metrics": m,
                             "n_trades_by_asset": by_asset, "trades": list(res.trades), "events": list(res.events)})
    print(f"json: {args.out}")
    return 0


def _b_walkforward(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    wf = walk_forward_b(_b_dfs(args), args.workers)
    _b_write_json(args.out, {k: v for k, v in wf.items() if k != "oos_trades"})
    _print_comparison({"WF-OOS": wf["stitched_metrics"]})
    chosen = None if wf["chosen_final"] is None else combo_to_json(wf["chosen_final"])
    print(f"walk-forward B: {len(wf['folds'])} folds in {time.perf_counter() - t0:.0f}s; chosen_final = {chosen}; "
          f"fallback = {wf['chosen_final_fallback']}; proceed = {wf['proceed']}; json: {args.out}")
    if not wf["proceed"]:
        print("spec 6.3: no holdout - the stitched out-of-sample return is negative or there is no final combo "
              "from the last fold; strategy B ends here", file=sys.stderr)
    return 0


def _b_holdout_lock(args: argparse.Namespace) -> int:
    """The lock from the walk-forward report; stratb.holdout.create_lock refuses one that does not proceed or
    was made on other bars or code, and a dirty tracked tree."""
    try:
        lock = create_lock(Path(args.params_from), _b_dfs(args), now_ts=int(time.time()), lock_path=STRATB_LOCK)
    except HoldoutBError as exc:
        _refuse(str(exc))
    print(f"holdout-b.lock written: {STRATB_LOCK} (sha256 {lock['sha256'][:12]}, params {lock['params']}, "
          f"last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(lock['last_bar_ts']))} UTC).\n"
          "Commit it NOW, before the holdout runs:\n"
          f'  git add "{STRATB_LOCK}"\n  git commit -m "chore(stratb): lock the strategy-B holdout {lock["sha256"][:12]}"')
    return 0


def _b_holdout_run(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing: the strategy-B holdout runs once, after the user's approval: re-run with --confirm",
              file=sys.stderr)
        return 2
    if not lock_exists(STRATB_LOCK):
        _refuse(f"{STRATB_LOCK} is missing or not a valid strategy-B lock (its sha256 must be the digest of its "
                "fields): `ewbot stratb holdout lock` first")
    update_caches_15m(args.cache_dir, lock_path=STRATB_LOCK)
    try:
        out = run_holdout_b(confirm=True, cache_dir=args.cache_dir, lock_path=STRATB_LOCK, workers=args.workers)
    except HoldoutBError as exc:
        _refuse(str(exc))
    fr = out["first_run"]
    _print_comparison({k: fr["metrics"][k] for k in ("B", "BH", "B_cost_x1_5")})
    print(f"trades by asset: {fr['n_trades_by_asset']}")
    print(json.dumps(fr["criteria"], indent=1))
    print(f"holdout {'PASSED' if fr['criteria']['passed'] else 'FAILED - strategy B ends (spec 6.5)'}; result in "
          f"{STRATB_LOCK} first_run. Commit it:\n  git add \"{STRATB_LOCK}\"\n"
          '  git commit -m "chore(stratb): record the strategy-B holdout run"')
    return 0


def _b_paper_env(args: argparse.Namespace) -> PaperBEnv:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    return PaperBEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _b_alert_failure(env: PaperBEnv, stage: str, message: str) -> None:
    if env.sender is None:
        return
    text = b_head(failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([make_event("FAILURE", env.clock(), stage, text)], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _b_paper_run(args: argparse.Namespace) -> int:
    env = _b_paper_env(args)
    try:
        out = run_paper_b(env)
    except PaperBRunError as exc:
        print(f"paper-B run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _b_alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _b_alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    print(f"paper-B run {out.run_id}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(out.last_bar_ts))} UTC, "
          f"epoch {out.epoch_id}, paper {out.count['paper']}/{out.count['required']}, {out.new_decisions} new "
          f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending")
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _b_paper_status(args: argparse.Namespace) -> int:
    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    for line in status_lines_b(led, paper_count_b(led)) + position_lines_b(led.open_position):
        print(line)
    cur = current_epoch(led)
    print(f"epoch {cur['id']} of {len(led.epochs)}: B fingerprint {cur['fingerprint'][:12]} "
          f"(lock {led.baseline['fingerprint'][:12]})")
    if led.runs:
        r = led.runs[-1]
        print(f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
              f"holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH (warned, not refused)'}, "
              f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['B']})")
    print(f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)}, "
          f"pending events {len(led.pending_events)}")
    return 0


def cmd_stratb(args: argparse.Namespace) -> int:
    if args.b_cmd == "holdout":
        return _b_holdout_lock(args) if args.bh_cmd == "lock" else _b_holdout_run(args)
    if args.b_cmd == "paper":
        return _b_paper_status(args) if args.bp_cmd == "status" else _b_paper_run(args)
    return {"fetch": _b_fetch, "backtest": _b_backtest, "walkforward": _b_walkforward}[args.b_cmd](args)


# ----------------------------------------------------------------------------- A-ETH (Plan 5a)
# ewbot.aeth is imported inside these handlers only: `ewbot live …` and `ewbot paper …` never load it.


def _e_raw(args: argparse.Namespace) -> pd.DataFrame:
    """The cached ETH bars before 2024-01-01 (load_eth filters the rest out while reading)."""
    from ewbot.aeth import config as ec
    from ewbot.aeth.data import cache_path, load_eth

    path = cache_path(args.cache_dir)
    if not path.exists():
        raise SystemExit(f"ETH candle cache not found: {path} - run `ewbot aeth fetch` first")
    raw = load_eth(path, lock_path=ec.LOCK_PATH)
    rep = check_integrity(raw)
    print(f"[data] ETH-USD: {rep.n} 1h bars before {WF_END}, {len(rep.missing_ts)} missing", file=sys.stderr)
    return raw


def _e_fetch(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.data import fetch_eth
    from ewbot.aeth.lockfile import lock_exists

    try:
        path = fetch_eth(args.cache_dir, lock_path=ec.LOCK_PATH)
        raw = _e_raw(args)
    except ec.AethError as exc:
        _refuse(str(exc))
    first = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(raw["ts"].iloc[0]))) if len(raw) else "-"
    last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(raw["ts"].iloc[-1]))) if len(raw) else "-"
    print(f"ETH cache: {path}\nETH bars before {WF_END}: {len(raw)} ({first} .. {last} UTC)")
    window = "2024+ included (holdout-aeth.lock exists)" if lock_exists(ec.LOCK_PATH) else f"stops at {WF_END} (no lock)"
    print(f"fetch window: {window}")
    return 0


def _e_head_lines(head: dict[str, Any]) -> None:
    _print_comparison({k: v for k, v in head.items() if k != "RANDOM"})
    rnd = head.get("RANDOM") or {}
    sharpe, calmar = (rnd.get("sharpe") or {}).get("mean"), (rnd.get("calmar") or {}).get("mean")
    print(f"random control: {rnd.get('runs')} runs, {rnd.get('n_signals')} entries each; mean Sharpe {sharpe}, "
          f"mean Calmar {calmar}")


def _e_stage1(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.stage1 import write_stage1

    try:
        rec = write_stage1(_e_raw(args), out_path=ec.STAGE1_PATH, a_lock_path=ec.A_LOCK_PATH, workers=args.workers)
    except ec.AethError as exc:
        _refuse(str(exc))
    _e_head_lines(rec["headline"])
    print(json.dumps(rec["criteria"], indent=1))
    sub, dear = rec["info"]["sub_period"]["headline"]["A"], rec["info"]["cost_x2"]["A"]
    print(f"information only - A 2019..2023: return {sub['total_return']:.3f}, max dd {sub['max_dd']:.3f}, Sharpe "
          f"{sub['sharpe']:.3f}; A with fees and slippage x 2: return {dear['total_return']:.3f}, Sharpe "
          f"{dear['sharpe']:.3f}")
    verdict = ("PASSED - next: `ewbot aeth holdout lock`" if rec["criteria"]["passed"]
               else "FAILED - A-ETH ends (spec 6): no lock, no 2024 data, no other asset")
    print(f"stage 1 {verdict}. Record: {ec.STAGE1_PATH}. Commit it NOW:\n  git add \"{ec.STAGE1_PATH}\"\n"
          '  git commit -m "chore(aeth): record stage 1"')
    return 0


def _e_holdout_lock(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.holdout import create_lock

    try:
        lock = create_lock(_e_raw(args), now_ts=int(time.time()), lock_path=ec.LOCK_PATH, stage1_path=ec.STAGE1_PATH,
                           a_lock_path=ec.A_LOCK_PATH)
    except ec.AethError as exc:
        _refuse(str(exc))
    print(f"holdout-aeth.lock written: {ec.LOCK_PATH} (sha256 {lock['sha256'][:12]}, params {lock['params']}, last bar "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(lock['last_bar_ts']))} UTC).\n"
          "Commit it NOW, before the holdout runs:\n"
          f'  git add "{ec.LOCK_PATH}"\n  git commit -m "chore(aeth): lock the ETH holdout {lock["sha256"][:12]}"')
    return 0


def _e_holdout_run(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.data import fetch_eth
    from ewbot.aeth.holdout import run_holdout_aeth
    from ewbot.aeth.lockfile import lock_exists

    if not args.confirm:
        print("refusing: the ETH holdout runs once, after the user's approval: re-run with --confirm", file=sys.stderr)
        return 2
    if not lock_exists(ec.LOCK_PATH):
        _refuse(f"{ec.LOCK_PATH} is missing or not a valid A-ETH lock: `ewbot aeth holdout lock` first")
    fetch_eth(args.cache_dir, lock_path=ec.LOCK_PATH)
    try:
        out = run_holdout_aeth(confirm=True, cache_dir=args.cache_dir, lock_path=ec.LOCK_PATH,
                               a_lock_path=ec.A_LOCK_PATH, workers=args.workers)
    except ec.AethError as exc:
        _refuse(str(exc))
    fr = out["first_run"]
    _e_head_lines(fr["headline"])
    for line in fr["criteria"]["trade_count"]:
        print(line)
    print(json.dumps(fr["verdict"], indent=1))
    verdict = "PASSED" if fr["verdict"]["passed"] else "FAILED - A-ETH ends (spec 7.3): no paper trading"
    print(f"ETH holdout {verdict}; result in {ec.LOCK_PATH} first_run. Commit it NOW:\n  git add \"{ec.LOCK_PATH}\"\n"
          '  git commit -m "chore(aeth): record the ETH holdout run"')
    return 0


def _e_paper_env(args: argparse.Namespace) -> Any:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    from ewbot.aeth.paper import AethPaperEnv

    return AethPaperEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _e_alert_failure(env: Any, stage: str, message: str) -> None:
    from ewbot.aeth.paper import eth_head

    if env.sender is None:
        return
    text = eth_head(failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([make_event("FAILURE", env.clock(), stage, text)], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _e_paper_run(args: argparse.Namespace) -> int:
    from ewbot.aeth.paper import AethPaperError, run_paper_aeth

    env = _e_paper_env(args)
    try:
        out = run_paper_aeth(env)
    except AethPaperError as exc:
        print(f"paper-A-ETH run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _e_alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _e_alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    count = out.status["count"]
    last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(out.last_bar_ts))
    print(f"paper-A-ETH run {out.run_id}: last bar {last} UTC, "
          f"epoch {out.epoch_id}, ETH out-of-sample {count['total']}/{count['required']}, {out.new_decisions} new "
          f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending")
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _e_paper_status(args: argparse.Namespace) -> int:
    from ewbot.aeth.paper import status_aeth, status_lines_aeth

    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    for line in status_lines_aeth(status_aeth(led, int(time.time()))):
        print(line)
    print(position_line(led.open_position))
    cur = current_epoch(led)
    print(f"epoch {cur['id']} of {len(led.epochs)}: fingerprint {cur['fingerprint'][:12]} "
          f"(lock {led.baseline['fingerprint'][:12]})")
    if led.runs:
        r = led.runs[-1]
        print(f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
              f"holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH (warned, not refused)'}, "
              f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['A']})")
    unexplained = sum(1 for d in led.discrepancies if d["cause"] == "UNEXPLAINED")
    print(f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)} (unexplained {unexplained}), "
          f"pending events {len(led.pending_events)}")
    return 0


def cmd_aeth(args: argparse.Namespace) -> int:
    if args.e_cmd == "holdout":
        return _e_holdout_lock(args) if args.eh_cmd == "lock" else _e_holdout_run(args)
    if args.e_cmd == "paper":
        return _e_paper_status(args) if args.ep_cmd == "status" else _e_paper_run(args)
    return {"fetch": _e_fetch, "stage1": _e_stage1}[args.e_cmd](args)


_COMMANDS = {
    "fetch": cmd_fetch,
    "backtest": cmd_backtest,
    "compare": cmd_compare,
    "walkforward": cmd_walkforward,
    "holdout": cmd_holdout,
    "paper": cmd_paper,
    "live": cmd_live,
    "stratb": cmd_stratb,
    "aeth": cmd_aeth,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
